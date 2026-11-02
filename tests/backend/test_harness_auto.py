"""자동 처리 (기능 정의 §3 원칙 1, 앱 설계 §3b·§7.3)."""

import pytest

from backend import db
from backend.harness.auto import (
    AutoKind,
    apply_auto_action,
    auto_action_tool,
    revert_auto_action,
)
from backend.harness.loop import AgentSpec, Runner
from backend.harness.policy import check_source
from backend.harness.store import RunStore
from backend.harness.tools import ToolRegistry
from tests.backend.script import Script, answer, call

STATE = []
PERIOD = [""]  # 코드가 계산하는 기간


def kinds():
    STATE.clear()
    PERIOD[0] = ""
    return {
        "call_close": AutoKind(
            condition=lambda conn, target, result: None if result.get("ok") else "조건 미달",
            apply=lambda conn, action: STATE.append(action["target"]),
            undo=lambda conn, action: STATE.remove(action["target"]),
            period=lambda conn, target, result: PERIOD[0],
        )
    }


def turn_on(conn):
    db.set_setting(conn, "auto_policy", {"call_close": True})


def apply(conn, k, target="c-1", result=None, **kw):
    with conn:
        return apply_auto_action(
            conn, k, "call_close", target, result=result or {"ok": True}, reason="이유", **kw
        )


def test_policy_off_by_default_applies_nothing(conn):
    k = kinds()
    d = apply(conn, k)
    assert not d.applied and d.why == "policy_off" and STATE == []
    assert conn.execute("SELECT COUNT(*) FROM ax_auto_actions").fetchone()[0] == 0


def test_applied_action_keeps_reason_and_changes_state(conn):
    k = kinds()
    turn_on(conn)
    d = apply(conn, k, run_id=None)
    assert d.applied and STATE == ["c-1"]
    row = conn.execute("SELECT * FROM ax_auto_actions").fetchone()
    assert row["reason"] == "이유" and row["kind"] == "call_close"


def test_condition_failure_applies_nothing(conn):
    k = kinds()
    turn_on(conn)
    d = apply(conn, k, result={"ok": False})
    assert not d.applied and d.why == "condition" and STATE == []


def test_unknown_kind_is_refused(conn):
    turn_on(conn)
    with conn:
        d = apply_auto_action(conn, {}, "call_close", "c-1", result={}, reason="이유")
    assert not d.applied and d.why == "unknown_kind"


def test_same_key_applies_once(conn):
    k = kinds()
    turn_on(conn)
    apply(conn, k)
    d = apply(conn, k)
    assert not d.applied and d.why == "duplicate" and STATE == ["c-1"]


def test_period_makes_a_new_key(conn):
    k = kinds()
    turn_on(conn)
    PERIOD[0] = "2026-10"
    apply(conn, k)
    PERIOD[0] = "2026-11"
    assert apply(conn, k).applied


def test_revert_restores_state_and_records_person(conn):
    k = kinds()
    turn_on(conn)
    d = apply(conn, k)
    with conn:
        assert revert_auto_action(conn, k, d.action_id, by="원장")
    assert STATE == []
    row = conn.execute("SELECT * FROM ax_auto_actions").fetchone()
    assert row["reverted_by"] == "원장" and row["reverted_at"]


def test_second_revert_changes_nothing(conn):
    k = kinds()
    turn_on(conn)
    d = apply(conn, k)
    with conn:
        revert_auto_action(conn, k, d.action_id, by="원장")
    with conn:
        assert not revert_auto_action(conn, k, d.action_id, by="데스크")
    assert conn.execute("SELECT reverted_by FROM ax_auto_actions").fetchone()[0] == "원장"


def test_reverted_key_is_not_applied_again(conn):
    k = kinds()
    turn_on(conn)
    d = apply(conn, k)
    with conn:
        revert_auto_action(conn, k, d.action_id, by="원장")
    again = apply(conn, k)
    assert not again.applied and again.why == "reverted" and STATE == []


def agent(conn, clock, gw, k):
    r = ToolRegistry()
    r.register(auto_action_tool(k, allowed=("call_close",)))
    spec = AgentSpec("x", "chat", "t", "v1", lambda v: "s", ("request_auto_action",), 5, 1.0)
    return Runner(spec, gw, RunStore(conn, clock), r, owner="t")


def test_llm_request_passes_through_policy(conn, clock):
    k = kinds()
    args = {"kind": "call_close", "target": "c-1", "result": {"ok": True}, "reason": "왜"}
    gw = Script(call(("request_auto_action", args)), answer())
    agent(conn, clock, gw, k).start("c-1", "입력")
    assert STATE == []  # 정책이 꺼져 있으면 LLM이 요청해도 0건


def test_resume_does_not_rerun_finished_request(conn, clock):
    k = kinds()
    turn_on(conn)
    conn.commit()
    args = {"kind": "call_close", "target": "c-1", "result": {"ok": True}, "reason": "왜"}
    gw = Script(call(("request_auto_action", args)), RuntimeError("죽음"))
    with pytest.raises(RuntimeError):
        agent(conn, clock, gw, k).start("c-1", "입력")
    run_id = conn.execute("SELECT id FROM ax_agent_runs").fetchone()[0]
    clock.advance(RunStore.lock_ttl + 1)
    agent(conn, clock, Script(answer()), k).resume(run_id)
    assert STATE == ["c-1"]
    assert conn.execute("SELECT COUNT(*) FROM ax_auto_actions").fetchone()[0] == 1


def test_action_records_run_id_from_tool(conn, clock):
    k = kinds()
    turn_on(conn)
    conn.commit()
    args = {"kind": "call_close", "target": "c-1", "result": {"ok": True}, "reason": "왜"}
    out = agent(conn, clock, Script(call(("request_auto_action", args)), answer()), k).start(
        "c-1", "입력"
    )
    assert conn.execute("SELECT run_id FROM ax_auto_actions").fetchone()[0] == out.run_id


def test_kind_outside_allowed_is_refused_by_tool(conn, clock):
    k = kinds()
    turn_on(conn)
    conn.commit()
    args = {"kind": "ledger_fix", "target": "c-1", "result": {}, "reason": "왜"}
    gw = Script(call(("request_auto_action", args)), answer())
    out = agent(conn, clock, gw, k).start("c-1", "입력")
    assert RunStore(conn, clock).steps(out.run_id)[2]["result"]["why"] == "unknown_kind"


def test_source_change_turns_policy_off(conn, paths):
    turn_on(conn)
    conn.commit()
    assert not check_source(conn, paths[1])  # 처음 — 지문만 저장
    assert db.get_setting(conn, "auto_policy") == {"call_close": True}
    assert not check_source(conn, paths[1])  # 같음
    paths[1].write_bytes(paths[1].read_bytes() + b"x")
    assert check_source(conn, paths[1])
    assert db.get_setting(conn, "auto_policy") == {}


def test_model_cannot_choose_period(conn, clock):
    k = kinds()
    turn_on(conn)
    d = apply(conn, k)
    with conn:
        revert_auto_action(conn, k, d.action_id, by="원장")
    conn.commit()
    args = {
        "kind": "call_close",
        "target": "c-1",
        "period": "again",
        "result": {"ok": True},
        "reason": "왜",
    }
    agent(conn, clock, Script(call(("request_auto_action", args)), answer()), k).start(
        "c-1", "입력"
    )
    assert STATE == []


def test_model_cannot_act_on_other_target(conn, clock):
    k = kinds()
    turn_on(conn)
    conn.commit()
    args = {"kind": "call_close", "target": "다른 환자", "result": {"ok": True}, "reason": "왜"}
    out = agent(conn, clock, Script(call(("request_auto_action", args)), answer()), k).start(
        "c-1", "입력"
    )
    assert STATE == []
    store = RunStore(conn, clock)
    assert store.steps(out.run_id)[2]["result"]["why"] == "wrong_target"
    assert store.block_counts() == {"auto.wrong_target": 1}


@pytest.mark.parametrize(
    "setup, rule",
    [
        ("off", "auto.policy_off"),
        ("condition", "auto.condition"),
        ("twice", "auto.duplicate"),
    ],
)
def test_refusals_are_counted_by_rule(conn, clock, setup, rule):
    k = kinds()
    if setup != "off":
        turn_on(conn)
    if setup == "twice":
        apply(conn, k)
    apply(conn, k, result={"ok": False} if setup == "condition" else None)
    assert RunStore(conn, clock).block_counts() == {rule: 1}


def test_condition_reason_is_returned(conn):
    k = kinds()
    turn_on(conn)
    assert apply(conn, k, result={"ok": False}).detail == "조건 미달"


def test_failed_apply_rolls_back_action(conn):
    def boom(conn, action):
        raise ZeroDivisionError

    k = {"call_close": AutoKind(lambda c, t, r: None, boom, lambda c, a: None)}
    turn_on(conn)
    with pytest.raises(ZeroDivisionError), conn:
        apply_auto_action(conn, k, "call_close", "c-1", result={}, reason="이유")
    assert conn.execute("SELECT COUNT(*) FROM ax_auto_actions").fetchone()[0] == 0


def test_empty_reason_is_refused(conn, clock):
    k = kinds()
    turn_on(conn)
    conn.commit()
    args = {"kind": "call_close", "target": "c-1", "result": {"ok": True}, "reason": ""}
    out = agent(conn, clock, Script(call(("request_auto_action", args)), answer()), k).start(
        "c-1", "입력"
    )
    assert STATE == []
    assert RunStore(conn, clock).steps(out.run_id)[2]["rule_id"] == "tool.bad_args"


def test_action_survives_deleting_run_history(conn, clock):
    """실행 기록은 재생성 때 다시 만들고 자동 처리 되돌림은 보존한다(앱 설계 §7.3)"""
    k = kinds()
    turn_on(conn)
    conn.commit()
    args = {"kind": "call_close", "target": "c-1", "result": {"ok": True}, "reason": "왜"}
    agent(conn, clock, Script(call(("request_auto_action", args)), answer()), k).start(
        "c-1", "입력"
    )
    conn.execute("DELETE FROM ax_agent_steps")
    conn.execute("DELETE FROM ax_agent_runs")
    assert conn.execute("SELECT COUNT(*) FROM ax_auto_actions").fetchone()[0] == 1
