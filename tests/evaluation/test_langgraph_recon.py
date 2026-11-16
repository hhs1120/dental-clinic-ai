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


# ── Task 3: 재개 ─────────────────────────────────────────────────


def _two_lookups_then_submit(conn):
    """첫 턴에 조회 두 개를 한꺼번에 부르는 대본 — 노드 안에서 끊길 자리를 만든다"""
    from dataclasses import replace

    from tests.backend.test_recon_agent import Script

    class Two(Script):
        def complete(self, alias, **kw):
            r = super().complete(alias, **kw)
            if self.pos[kw["ref"][1]] == 1:  # 첫 턴
                calls = tuple(
                    {"id": f"tu-{i}", "name": LOOK[1], "input": LOOK[2]} for i in range(2)
                )
                return replace(r, tool_calls=calls,
                               content=[{"type": "tool_use", **c} for c in calls])  # fmt: skip
            return r

    return Two(conn, LOOK, submit(evidence=(2,)))


def test_resume_after_interrupt(conn, tmp_path, monkeypatch):
    """도구 노드 안에서 두 번째 조회 중에 끊고 같은 thread로 이어 가면: 결과는 끊지 않은 실행과 같고 모델 호출은 다시
    나가지 않지만, 노드 단위로 다시 돌아 끊기 전에 끝난 첫 조회를 한 번 더 실행한다(하네스는 도구 단계마다 기록해
    다시 하지 않는다 — backend/harness/loop.py drive). 읽기 전용 조회라 결과는 같고, 쓰기 도구였다면 두 번 쓴다"""
    import sqlite3

    from langgraph.checkpoint.sqlite import SqliteSaver

    real = lg.call_tool
    ran = {"n": 0, "fail_at": 2}

    def counting(*a):  # 끝까지 실행된 조회만 센다
        if ran["n"] + 1 == ran["fail_at"]:
            raise RuntimeError("끊김")
        ran["n"] += 1
        return real(*a)

    monkeypatch.setattr(lg, "call_tool", counting)
    k = key(conn)

    base_gw = _two_lookups_then_submit(conn)
    ran["fail_at"] = -1
    base = lg.investigate(lg.build_graph(base_gw, conn), k, "입력")
    base_tools = ran["n"]

    ran.update(n=0, fail_at=2)
    gw = _two_lookups_then_submit(conn)
    saver = SqliteSaver(sqlite3.connect(tmp_path / "cp.sqlite", check_same_thread=False))
    graph = lg.build_graph(gw, conn, saver)
    with pytest.raises(RuntimeError, match="끊김"):
        lg.investigate(graph, k, "입력")
    ran["fail_at"] = -1
    st = lg.investigate(graph, k, "입력", resume=True)

    assert lg.summary(st)["cause"] == lg.summary(base)["cause"] == "amount_typo"
    assert st["model_calls"] == base["model_calls"] == 2
    assert sum(gw.pos.values()) == 2  # 모델은 다시 부르지 않았다
    assert st["cost_usd"] == base["cost_usd"]  # 비용 합도 끊지 않은 실행과 같다
    assert ran["n"] == base_tools + 1  # 끊기 전 끝난 조회 1번을 다시 실행


# ── Task 4: 실행·채점 ────────────────────────────────────────────


def test_run_skips_done_keys(conn, tmp_path):
    from backend import recon_match
    from tests.backend.test_recon_agent import Script
    from tests.backend.test_recon_match import DAY

    items = recon_match.detect(recon_match.load(conn, DAY))
    path = tmp_path / "runs.jsonl"
    gw = Script(conn, LOOK, submit())
    out = lg.run(gw, conn, items, DAY, path, tmp_path / "cp.sqlite")
    assert out == {"done": 1}
    [line] = lg.read_runs(path)
    assert line["key"] == items[0]["key"] and line["cause"] == "amount_typo"
    assert line["model_calls"] == 2 and line["tool_calls"] == 2
    gw2 = Script(conn, LOOK, submit())
    assert lg.run(gw2, conn, items, DAY, path, tmp_path / "cp.sqlite") == {}
    assert gw2.pos == {}


def test_failed_key_runs_again(conn, tmp_path):
    from backend import recon_match
    from tests.backend.test_recon_agent import Script
    from tests.backend.test_recon_match import DAY

    items = recon_match.detect(recon_match.load(conn, DAY))
    path = tmp_path / "runs.jsonl"
    cp = tmp_path / "cp.sqlite"  # 같은 체크포인터 — 실패로 끝난 thread는 지우고 처음부터
    assert lg.run(Script(conn, ("fail", "거절")), conn, items, DAY, path, cp) == {"failed": 1}
    assert lg.run(Script(conn, LOOK, submit()), conn, items, DAY, path, cp) == {"done": 1}
    assert lg.latest(lg.read_runs(path))[items[0]["key"]]["status"] == "done"


def test_plan_counts_without_gateway(conn):
    from backend import recon_match
    from tests.backend.test_recon_match import DAY

    items = recon_match.detect(recon_match.load(conn, DAY))
    got = lg.plan(items, {}, mean_usd=0.08)
    assert got == {"targets": 1, "todo": 1, "estimated_usd": 0.08}


# ── 리뷰 반영 ──────────────────────────────────────────────────────


def test_after_rule_exception_returned_as_error(conn, monkeypatch):
    """하네스는 실행 뒤 규칙의 예외도 도구 오류로 모델에 돌려준다(backend/harness/loop.py _tool)"""
    from backend.harness.hooks import Rule

    def boom(*a):
        raise ValueError("규칙 깨짐")

    monkeypatch.setattr(recon_agent, "rules", lambda: (Rule("x", after=boom),))
    block, step = lg.call_tool(conn, _state(), "K", call(LOOK[1], LOOK[2]))
    assert step["is_error"] == 1 and "ValueError: 규칙 깨짐" in step["result"]


def test_run_reuses_finished_checkpoint(conn, tmp_path):
    """끝난 thread인데 결과 줄을 쓰기 전에 죽었으면 체크포인트의 결과를 쓰고 다시 부르지 않는다"""
    from backend import recon_match
    from tests.backend.test_recon_agent import Script
    from tests.backend.test_recon_match import DAY

    items = recon_match.detect(recon_match.load(conn, DAY))
    path, cp = tmp_path / "runs.jsonl", tmp_path / "cp.sqlite"
    lg.run(Script(conn, LOOK, submit()), conn, items, DAY, path, cp)
    first = lg.read_runs(path)
    path.write_text("")  # 결과 줄을 못 쓴 것처럼
    gw = Script(conn, LOOK, submit())
    assert lg.run(gw, conn, items, DAY, path, cp) == {"done": 1}
    assert gw.pos == {} and lg.read_runs(path) == first


def test_scored_lines_drop_failed():
    """하네스 results()처럼 failed는 결과가 아니다 — 채점에서 결과 없음(missing)으로 센다"""
    lines = [{"key": "a", "status": "done"}, {"key": "b", "status": "failed"},
             {"key": "c", "status": "handed_off"}, {"key": "z", "status": "done"}]  # fmt: skip
    assert set(lg.scored_lines(lines, {"a", "b", "c"})) == {"a", "c"}


def test_harness_cost_by_call_type():
    """일반 호출은 그대로, Batch 호출만 두 배로 환산(라운드 배치는 남은 실행이 적으면 일반 호출로 바꾼다)"""
    import sqlite3

    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE ax_llm_calls (task, prompt_version, ref_type, input_tokens,"
              " output_tokens, cost_usd)")  # fmt: skip
    c.executemany("INSERT INTO ax_llm_calls VALUES (?, ?, ?, ?, ?, ?)", [
        ("recon", "v3", "agent_run", 10, 1, 1.0),
        ("recon", "v3", "batch", 20, 2, 1.0),
        ("recon", "v3", "batch", 20, 2, 1.0),
        ("recon", "v2", "batch", 99, 9, 9.0),
    ])  # fmt: skip
    got = lg.harness_v3_totals(c)
    assert got == {"plain_calls": 1, "batch_calls": 2, "cost_usd": 3.0, "cost_usd_plain": 5.0,
                   "input_tokens": 50, "output_tokens": 5}  # fmt: skip


def test_agent_connection_has_no_truth(tmp_path):
    import sqlite3

    for name in ("app", "source", "truth"):
        sqlite3.connect(tmp_path / f"{name}.sqlite").execute("CREATE TABLE t (x)")
    paths = {n: tmp_path / f"{n}.sqlite" for n in ("app", "source", "truth")}
    names = {r[1] for r in lg._connect(paths, truth=False).execute("PRAGMA database_list")}
    assert "truth" not in names and "source" in names
    names = {r[1] for r in lg._connect(paths).execute("PRAGMA database_list")}
    assert "truth" in names
