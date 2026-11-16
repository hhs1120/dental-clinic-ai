"""랭그래프 비교 실험(evaluation/langgraph_recon.py, 설계 docs/superpowers/specs/2026-11-16-langgraph-recon-design.md) —
같은 도구·규칙·프롬프트를 그래프 위에서 돌릴 때 하네스(backend/harness/loop.py)와 같은 동작인지 본다.
langgraph 그룹이 없으면(CI 기본 설치) 건너뛴다."""

import pytest

pytest.importorskip("langgraph")

from backend import recon_agent  # noqa: E402
from backend.harness.hooks import BAD_ARGS, NOT_ALLOWED  # noqa: E402
from backend.harness.loop import DEFAULT_RESULT_LIMIT, _tool_message  # noqa: E402
from evaluation import langgraph_recon as lg  # noqa: E402
from tests.backend.test_recon_agent import LOOK, conn, key, submit  # noqa: E402, F401

__all__ = ["conn"]  # 픽스처 재사용


def call(name, args, i=0):
    return {"id": f"tu{i}", "name": name, "input": args}


def _state(steps=None):
    return lg.initial_state("K", "입력")["steps"] if steps is None else steps


# ── Task 1: 도구 실행 ──────────────────────────────────────────────


def test_lookup_numbers_follow_steps(conn):
    steps = _state()
    for i in range(2):
        _, step = lg.call_tool(conn, steps, "K", call(LOOK[1], LOOK[2], i))
        steps.append(step)
    assert [s["result"]["lookup_no"] for s in steps[1:]] == [1, 2]


def test_evidence_rule_blocks_unknown_number(conn):
    steps = _state()
    _, step = lg.call_tool(conn, steps, "K", call(LOOK[1], LOOK[2]))
    steps.append(step)
    block, step = lg.call_tool(conn, steps, "K", call(*submit(evidence=(3,))[1:], i=1))
    assert step["hook_status"] == "blocked" and step["rule_id"] == recon_agent.EVIDENCE_RULE
    assert step["is_error"] == 1 and block["is_error"] is True


def test_not_allowed_and_bad_args_blocked(conn):
    _, step = lg.call_tool(conn, _state(), "K", call("drop_table", {}))
    assert step["rule_id"] == NOT_ALLOWED
    _, step = lg.call_tool(conn, _state(), "K", call(LOOK[1], {"day": 3}))
    assert step["rule_id"] == BAD_ARGS


def test_tool_error_returned_to_model(conn, monkeypatch):
    reg, _ = recon_agent.registry()
    tool = reg.tools[LOOK[1]]

    def boom(ctx, args):
        raise ValueError("깨짐")

    monkeypatch.setitem(reg.tools, LOOK[1], tool.__class__(**{**tool.__dict__, "fn": boom}))
    monkeypatch.setattr(lg, "_registry", lambda: (reg, recon_agent.registry()[1]))
    block, step = lg.call_tool(conn, _state(), "K", call(LOOK[1], LOOK[2]))
    assert step["is_error"] == 1 and step["hook_status"] == "pass"
    assert "ValueError: 깨짐" in step["result"] and block["is_error"] is True


def test_result_block_like_harness(conn):
    block, step = lg.call_tool(conn, _state(), "K", call(LOOK[1], LOOK[2]))
    want, cut = _tool_message(
        "tu0", f"tool:{LOOK[1]}", step["result"], DEFAULT_RESULT_LIMIT, False
    )
    assert block == want and step["truncated"] == int(cut)


# ── Task 2: 그래프 ─────────────────────────────────────────────────


def _invoke(conn, *script, checkpointer=None):
    from tests.backend.test_recon_agent import Script

    gw = Script(conn, *script)
    graph = lg.build_graph(gw, conn, checkpointer)
    return lg.investigate(graph, key(conn), "불일치 건 입력"), gw


def test_submit_ends_done(conn):
    st, _ = _invoke(conn, LOOK, submit())
    assert st["status"] == "done" and st["reason"] == "submitted"
    s = lg.summary(st)
    assert s["cause"] == "amount_typo" and s["evidence"] == [1]
    assert st["model_calls"] == 2 and st["tool_calls"] == 2


def test_text_answer_nudged_once(conn):
    st, gw = _invoke(conn, ("text", "아마 오입력"))
    assert st["status"] == "done" and st["model_calls"] == 2
    assert sum(s["kind"] == "input" for s in st["steps"]) == 2  # 입력 + 되돌리기 한 번
    assert lg.summary(st)["cause"] == "unknown"


def test_call_limit_hands_off(conn):
    st, _ = _invoke(conn, LOOK)
    assert st["status"] == "handed_off" and st["reason"] == "call_limit"
    assert st["tool_calls"] == recon_agent.RECON_MAX_TOOL_CALLS


def test_cost_limit_hands_off(conn, monkeypatch):
    monkeypatch.setattr(lg, "RECON_MAX_USD", 0.0)
    st, _ = _invoke(conn, LOOK)
    assert st["status"] == "handed_off" and st["reason"] == "cost_limit"
    assert st["model_calls"] == 0


def test_failed_model_call(conn):
    st, _ = _invoke(conn, ("fail", "거절"))
    assert st["status"] == "failed" and st["reason"] == "거절"


def test_lines_from_graph_steps(conn):
    st, _ = _invoke(conn, LOOK, submit())
    kinds = [line["kind"] for line in recon_agent.lines(st["steps"])]
    assert "lookup" in kinds and kinds[-1] == "conclusion"


def test_same_steps_as_harness(conn):
    """같은 대본이면 하네스와 같은 단계(종류·도구·인자·막힘)를 남긴다"""
    script = (LOOK, submit(evidence=(5,)), submit())
    st, _ = _invoke(conn, *script)
    from tests.backend.test_recon_agent import run

    r, _ = run(conn, *script)
    from backend.harness.store import RunStore

    harness = RunStore(conn).steps(r["run_id"])

    def shape(steps):
        return [(s["kind"], s["tool"], s["args"], s["hook_status"], s["rule_id"]) for s in steps]

    assert shape(st["steps"])[1:] == shape(harness)[1:]  # 입력 글만 다르다
