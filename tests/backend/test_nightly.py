"""야간 실행 (backend/nightly.py, 설계 docs/superpowers/specs/2026-11-16-nightly-briefing-design.md §3)."""

from datetime import date, timedelta

import pytest

from backend import nightly
from backend.nightly import Ctx, Step

TODAY = date(2026, 12, 1)


def _steps(calls, fail=None):
    """이름마다 호출을 적는 가짜 단계 — fail = {이름: 던질 예외}"""

    def make(name):
        def run(ctx):
            calls.append(name)
            if fail and name in fail:
                raise fail[name]
            return {"new": 1, "failed": 0, "note": None}

        return Step(name, run, lambda ctx: 2 if name == "a" else None)

    return [make(n) for n in ("a", "b", "c")]


def _ctx(conn):
    return Ctx(conn, None, TODAY, use_batch=True)


def _running(conn, started):
    conn.execute(
        "INSERT INTO ax_jobs (kind, status, started_at) VALUES ('nightly', 'running', ?)",
        (started.isoformat(timespec="seconds"),),
    )
    conn.commit()


def test_runs_steps_in_order_and_records(conn, clock):
    calls = []
    job = nightly.run(_ctx(conn), _steps(calls), clock)
    assert calls == ["a", "b", "c"]
    assert job["status"] == "done" and job["done"] == job["total"] == 3
    assert [d["step"] for d in job["detail"]] == ["a", "b", "c"]
    assert job["finished_at"] is not None
    assert nightly.last_run(conn) == job


def test_lock_alive_running_skips(conn, clock):
    _running(conn, clock() - timedelta(hours=1))
    calls = []
    assert nightly.run(_ctx(conn), _steps(calls), clock) is None
    assert calls == []


def test_lock_stale_running_ignored(conn, clock):
    _running(conn, clock() - timedelta(hours=9))
    calls = []
    assert nightly.run(_ctx(conn), _steps(calls), clock)["status"] == "done"
    assert calls == ["a", "b", "c"]


@pytest.mark.parametrize("exc", [RuntimeError("터짐"), SystemExit("판정기 모델 다름")])
def test_step_exception_continues(conn, clock, exc):
    calls = []
    job = nightly.run(_ctx(conn), _steps(calls, {"b": exc}), clock)
    assert calls == ["a", "b", "c"] and job["status"] == "done"
    b = job["detail"][1]
    assert b["step"] == "b" and b["failed"] == 1 and b["new"] == 0
    assert b["note"].startswith(type(exc).__name__)


def test_budget_stops_remaining(conn, clock, monkeypatch):
    monkeypatch.setattr(nightly, "over_budget", lambda gw: True)
    calls = []
    job = nightly.run(_ctx(conn), _steps(calls), clock)
    assert calls == ["a"] and job["status"] == "failed"
    assert job["detail"][-1] == {"step": "b", "new": 0, "failed": 0, "note": "상한으로 중단"}
    assert len(job["detail"]) == 2


def test_plan_writes_no_job(conn):
    calls = []
    assert nightly.plan(_ctx(conn), _steps(calls)[:2]) == [("a", 2), ("b", None)]
    assert calls == []
    assert conn.execute("SELECT COUNT(*) FROM ax_jobs").fetchone()[0] == 0


def test_last_run_none(conn):
    assert nightly.last_run(conn) is None
