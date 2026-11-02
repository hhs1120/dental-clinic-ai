"""실행 기록 저장소 (앱 설계 §3b 상태·저장소 주입)."""

import sqlite3
from datetime import date

import pytest

from backend import db
from backend.harness.store import RunLocked, RunStore, SqliteLlmLog


def test_second_running_run_on_same_target_is_refused(conn, clock):
    store = RunStore(conn, clock)
    store.create_run("a", "c-1", "v1", owner="x")
    conn.commit()
    with pytest.raises(RunLocked):
        store.create_run("a", "c-1", "v1", owner="y")


def test_expired_lock_can_be_taken_but_fresh_cannot(conn, clock):
    store = RunStore(conn, clock)
    run = store.create_run("a", "c-1", "v1", owner="x")
    conn.commit()
    with pytest.raises(RunLocked):
        store.take_over(run, owner="y")
    clock.advance(store.lock_ttl + 1)
    store.take_over(run, owner="y")
    assert store.run(run)["locked_by"] == "y"


def test_steps_round_trip_json(conn, clock):
    store = RunStore(conn, clock)
    run = store.create_run("a", "c-1", "v1", owner="x")
    store.add_step(run, "tool", tool="q", args={"가": 1}, result={"나": [2]}, truncated=True)
    [step] = store.steps(run)
    assert step["seq"] == 1 and step["args"] == {"가": 1} and step["result"] == {"나": [2]}
    assert step["truncated"] == 1


def test_block_count_accumulates(conn, clock):
    store = RunStore(conn, clock)
    store.add_block("tool.not_allowed")
    store.add_block("tool.not_allowed")
    assert store.block_counts() == {"tool.not_allowed": 2}


def test_injected_store_leaves_app_db_untouched(conn, paths, tmp_path, clock):
    """개선 에이전트처럼 다른 파일에 쓰면 앱 DB에는 실행 기록이 없다"""
    other = db.connect(tmp_path / "runs.sqlite", paths[1])
    db.init_schema(other)
    RunStore(other, clock).create_run("improve", "classify", "v1", owner="x")
    other.commit()
    assert conn.execute("SELECT COUNT(*) FROM ax_agent_runs").fetchone()[0] == 0
    assert other.execute("SELECT COUNT(*) FROM ax_agent_runs").fetchone()[0] == 1
    other.close()


@pytest.mark.parametrize("flag", [False, True])
def test_llm_log_marks_eval_calls(conn, flag):
    log = SqliteLlmLog(conn, flag)
    log.write(
        {
            "at": "2026-11-02T01:00:00",
            "task": "t",
            "alias": "a",
            "model": "m",
            "prompt_version": None,
            "fallback_used": False,
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_usd": 0.0,
            "latency_ms": 0,
            "ok": True,
            "error": None,
            "ref_type": None,
            "ref_id": None,
        }
    )
    assert conn.execute("SELECT is_eval FROM ax_llm_calls").fetchone()[0] == int(flag)


def test_llm_log_sums_cost_per_day(conn):
    log = SqliteLlmLog(conn)
    for at, cost in (("2026-11-02T01:00:00", 0.5), ("2026-11-01T23:00:00", 9.0)):
        log.write(
            {
                "at": at,
                "scope": "runtime",
                "task": "t",
                "alias": "chat",
                "model": "m",
                "prompt_version": "v1",
                "fallback_used": False,
                "input_tokens": 1,
                "output_tokens": 2,
                "cost_usd": cost,
                "latency_ms": 3,
                "ok": True,
                "error": None,
                "ref_type": "agent_run",
                "ref_id": 7,
            }
        )
    assert log.spent(date(2026, 11, 2), "runtime") == 0.5
    row = conn.execute("SELECT * FROM ax_llm_calls ORDER BY id LIMIT 1").fetchone()
    assert row["created_at"] == "2026-11-02 01:00:00" and row["ref_id"] == "7"


def test_resumable_lists_only_expired_running_runs(conn, clock):
    store = RunStore(conn, clock)
    expired = store.create_run("a", "c-1", "v1", owner="x")
    done = store.create_run("a", "c-2", "v1", owner="x")
    store.finish(done, "done", "x")
    clock.advance(store.lock_ttl + 1)
    fresh = store.create_run("a", "c-3", "v1", owner="x")
    conn.commit()
    assert store.resumable() == [expired]
    assert fresh not in store.resumable()


def test_refused_run_keeps_callers_uncommitted_write(conn, clock):
    store = RunStore(conn, clock)
    store.create_run("a", "c-1", "v1", owner="x")
    conn.commit()
    db.set_setting(conn, "k", 1)
    with pytest.raises(RunLocked):
        store.create_run("a", "c-1", "v1", owner="y")
    assert db.get_setting(conn, "k") == 1


def test_missing_parent_is_not_reported_as_locked(conn, clock):
    with pytest.raises(sqlite3.IntegrityError):
        RunStore(conn, clock).create_run("a", "c-1", "v1", owner="x", parent_run_id=999)
