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


# ── 실제 단계(Task 4) ──
def test_step_names_order():
    assert [s.name for s in nightly.STEPS] == [
        "source",
        "classify",
        "judge",
        "disagreements",
        "autoclose",
        "followup",
        "index",
        "prep",
        "claim_check",
        "claim_omission",
        "claim_evidence",
        "recon",
        "expense",
        "report",
    ]


def _real(names):
    return [s for s in nightly.STEPS if s.name in names]


def test_second_run_new_zero(cconn, clock):
    """같은 날 두 번 — 두 번째는 새로 만드는 것도, LLM 호출도 없다(캐시). 가짜 응답의 처리안 실패도
    실패 행으로 남아 다시 부르지 않는다(followup.targets)"""
    from backend.harness.store import SqliteLlmLog
    from shared.llm import FakeGateway

    ctx = Ctx(cconn, FakeGateway(SqliteLlmLog(cconn), scope="runtime"), TODAY, use_batch=False)
    steps = _real({"classify", "autoclose", "followup"})
    nightly.run(ctx, steps, clock)
    llm_calls = cconn.execute("SELECT COUNT(*) FROM ax_llm_calls").fetchone()[0]
    clock.advance(60)
    second = nightly.run(ctx, steps, clock)
    assert [d["new"] for d in second["detail"]] == [0, 0, 0]
    assert cconn.execute("SELECT COUNT(*) FROM ax_llm_calls").fetchone()[0] == llm_calls


def test_index_step_skips_without_index(conn):
    (index,) = _real({"index"})
    assert index.run(_ctx(conn)) == {"new": 0, "failed": 0, "note": "건너뜀: 색인 없음"}
    assert index.plan(_ctx(conn)) == 0


# ── 리뷰 반영 ──
def _fake_ctx(conn):
    from backend.harness.store import SqliteLlmLog
    from shared.llm import FakeGateway

    return Ctx(conn, FakeGateway(SqliteLlmLog(conn), scope="runtime"), TODAY, use_batch=False)


def _paste(conn, uid="p-0001"):
    conn.execute(
        "INSERT INTO ax_pasted_calls (call_uid, direction, started_at, transcript, added_by,"
        " added_at) VALUES (?, 'inbound', '2026-12-01 10:00:00', '환자: 예약요', 'desk',"
        " '2026-11-16 10:00:00')",
        (uid,),
    )
    conn.commit()


def test_keyboard_interrupt_records_stop(conn, clock):
    """Ctrl-C — 행을 failed·중단으로 남겨 다음 실행이 8시간 막히지 않게 하고 다시 던진다"""
    calls = []
    with pytest.raises(KeyboardInterrupt):
        nightly.run(_ctx(conn), _steps(calls, {"b": KeyboardInterrupt()}), clock)
    job = nightly.last_run(conn)
    assert job["status"] == "failed" and job["finished_at"] is not None
    assert job["detail"][-1] == {"step": "b", "new": 0, "failed": 0, "note": "중단"}
    assert nightly.run(_ctx(conn), _steps([]), clock)["status"] == "done"


def test_force_ignores_alive_lock(conn, clock):
    _running(conn, clock() - timedelta(hours=1))
    calls = []
    assert nightly.run(_ctx(conn), _steps(calls), clock, force=True)["status"] == "done"
    assert calls == ["a", "b", "c"]


def test_cost_since_reads_space_format(conn):
    """ax_llm_calls.created_at은 공백 형식(harness/store.py) — 같은 날 시작 뒤 호출을 센다"""
    from datetime import datetime

    conn.executemany(
        "INSERT INTO ax_llm_calls (task, model_alias, model_actual, ok, created_at, cost_usd)"
        " VALUES ('t', 'chat', 'm', 1, ?, ?)",
        [("2026-11-16 21:59:59", 5.0), ("2026-11-16 22:05:00", 1.5)],
    )
    assert nightly.cost_since(conn, datetime(2026, 11, 16, 22, 0)) == 1.5


def test_over_budget_with_real_gateway(conn, clock):
    from backend.harness.store import SqliteLlmLog
    from shared.llm import Gateway, load_config

    gw = Gateway(load_config(), SqliteLlmLog(conn), "runtime", clock=clock)
    assert not nightly.over_budget(gw)
    limit = load_config()["daily_usd_limit"]["runtime"]
    conn.execute(
        "INSERT INTO ax_llm_calls (task, model_alias, model_actual, ok, created_at, cost_usd)"
        " VALUES ('t', 'chat', 'm', 1, ?, ?)",
        (clock().strftime("%Y-%m-%d %H:%M:%S"), limit),
    )
    conn.commit()
    gw = Gateway(
        load_config(), SqliteLlmLog(conn), "runtime", clock=clock
    )  # 사용액은 처음 읽을 때 모은다
    assert nightly.over_budget(gw)


def test_followup_stops_at_budget(cconn, monkeypatch):
    """처리안 도중 상한 — 남은 통화를 부르지 않는다(부르면 handed_off가 실패 처리안으로 굳는다)"""
    from backend import followup

    monkeypatch.setattr(nightly, "over_budget", lambda gw: True)
    monkeypatch.setattr(followup, "targets", lambda conn, today: ["c-0001", "c-0002"])

    def boom(*a, **k):
        raise AssertionError("상한 뒤 호출")

    monkeypatch.setattr(followup, "run_for_call", boom)
    (step,) = _real({"followup"})
    assert step.run(_fake_ctx(cconn)) == {"new": 0, "failed": 0, "note": "상한으로 중단"}


def test_classify_counts_ok_only_and_budget(cconn, monkeypatch):
    """새로 = 활성 분류 ok가 늘어난 수. 상한 예외는 단계 실패가 아니라 그때까지의 수 + 상한 메모"""
    from backend import calls, classify_batch
    from shared.llm import BudgetExceeded
    from tests.backend.call_db import classify_row

    _paste(cconn, "p-0001")
    _paste(cconn, "p-0002")
    monkeypatch.setattr(classify_batch, "run", lambda *a, **k: {"done": 0, "failed": 0})

    def one(conn, gw, uid):
        if uid == "p-0002":
            raise BudgetExceeded("runtime 일일 상한")
        classify_row(conn, uid, status="failed")

    monkeypatch.setattr(calls, "classify_one", one)
    (step,) = _real({"classify"})
    assert step.run(_fake_ctx(cconn)) == {"new": 0, "failed": 1, "note": "상한으로 중단"}


def test_judge_fills_pasted_calls(cconn):
    """붙여넣은 통화 중 활성 분류는 ok인데 판정이 없는 것도 판정한다(추가 때 판정 예외)"""
    from tests.backend.call_db import classify_row

    _paste(cconn)
    classify_row(cconn, "p-0001")
    cconn.commit()
    (step,) = _real({"judge"})
    step.run(_fake_ctx(cconn))
    assert (
        cconn.execute(
            "SELECT COUNT(*) FROM ax_model_judgments WHERE call_uid = 'p-0001'"
        ).fetchone()[0]
        == 1
    )


def test_plan_lines_estimate_agents():
    lines = nightly.plan_lines([("judge", 3), ("index", None), ("recon", 10), ("expense", 0)])
    assert lines[0] == "judge: 3건 (앞 단계 뒤 늘 수 있음)"
    assert lines[1] == "index: —"
    assert lines[2].startswith("recon: 10건 · 예상 약 $")
    assert lines[3] == "expense: 0건"
    assert lines[-1].startswith("Batch는 하루 상한 밖")


def test_independent_step_plan_equals_run(tmp_path):
    """앞 단계와 무관한 단계(준비표)는 PLAN 수 == 실제 실행 new"""
    from backend import db
    from tests.backend.test_prep import NONE, FakeGw, make_source, res

    make_source(tmp_path / "source.sqlite").close()
    c = db.connect(tmp_path / "app.sqlite", tmp_path / "source.sqlite")
    db.init_schema(c)
    ctx = Ctx(c, FakeGw(*[res(NONE)] * 10), TODAY, use_batch=False)
    (step,) = _real({"prep"})
    n = step.plan(ctx)
    assert n > 0 and step.run(ctx)["new"] == n
