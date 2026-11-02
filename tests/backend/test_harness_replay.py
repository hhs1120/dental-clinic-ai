"""재생 (앱 설계 §3b) — 저장된 실행을 키 없이 다시 돌려 호출이 같은지 본다."""

import json
from dataclasses import replace
from pathlib import Path

from pydantic import BaseModel

from backend.harness.hooks import Rule
from backend.harness.loop import AgentSpec, Runner
from backend.harness.replay import export_run, replay
from backend.harness.store import RunStore
from backend.harness.tools import Tool, ToolRegistry
from shared.llm import LLMResult
from tests.backend.script import Script, answer, call

FIXTURE = Path(__file__).parents[1] / "fixtures" / "agent_runs" / "lookup_then_write.json"


class Args(BaseModel):
    chart_no: str


def registry(calls):
    def touch(ctx, args):
        calls.append(args["chart_no"])
        return {"seen": args["chart_no"]}

    r = ToolRegistry()
    r.register(Tool("lookup", "조회", Args, touch))
    r.register(Tool("write", "쓰기", Args, touch, writes=True))
    return r


def record(conn, clock, *turns, spec=None, tools=None, rules=()):
    out = Runner(
        spec or SPEC, Script(*turns), RunStore(conn, clock), tools or registry([]), rules, "t"
    ).start("c-1", "입력")
    return export_run(RunStore(conn, clock), out.run_id)


SPEC = AgentSpec("a", "chat", "t", "v1", lambda v: "s", ("lookup", "write"), 5, 1.0)


def recorded(conn, clock):
    gw = Script(
        call(("lookup", {"chart_no": "C-1"})), call(("write", {"chart_no": "C-1"})), answer()
    )
    out = Runner(SPEC, gw, RunStore(conn, clock), registry([]), owner="t").start("c-1", "입력")
    return export_run(RunStore(conn, clock), out.run_id)


def test_replay_matches_and_runs_no_real_tool(conn, clock):
    dump = recorded(conn, clock)
    calls = []
    assert replay(dump, SPEC, registry(calls)) == []
    assert calls == []


def test_dump_survives_json(conn, clock):
    dump = json.loads(json.dumps(recorded(conn, clock), ensure_ascii=False))
    assert replay(dump, SPEC, registry([])) == []


def test_new_rule_blocking_a_recorded_call_shows_as_difference(conn, clock):
    dump = recorded(conn, clock)
    rule = Rule("no.write", before=lambda ctx, name, args: "금지" if name == "write" else None)
    diffs = replay(dump, SPEC, registry([]), rules=(rule,))
    assert diffs and "no.write" in diffs[0]


def test_committed_fixture_replays_without_key():
    dump = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert replay(dump, SPEC, registry([])) == []


LOOKUP = ("lookup", {"chart_no": "C-1"})


def test_call_limit_run_replays(conn, clock):
    spec = replace(SPEC, max_tool_calls=1)
    dump = record(conn, clock, call(LOOKUP), call(LOOKUP), spec=spec)
    assert replay(dump, spec, registry([])) == []


def test_cost_limit_run_replays(conn, clock):
    spec = replace(SPEC, max_usd=0.05)
    dump = record(conn, clock, call(LOOKUP, cost=0.06), spec=spec)
    assert replay(dump, spec, registry([])) == []


def test_failed_run_replays(conn, clock):
    bad = LLMResult(False, "", None, "m", False, 0, 0, 0.0, 1, error="오류")
    dump = record(conn, clock, call(LOOKUP), bad)
    assert replay(dump, SPEC, registry([])) == []


def test_same_after_rule_replays_without_difference(conn, clock):
    rule = Rule(
        "verify",
        after=lambda ctx, name, args, result: "실패" if result == {"seen": "C-1"} else None,
    )
    dump = record(conn, clock, call(LOOKUP), answer(), rules=(rule,))
    assert dump["steps"][2]["hook_status"] == "blocked"
    assert replay(dump, SPEC, registry([]), rules=(rule,)) == []


def test_same_call_with_different_results_replays_in_order(conn, clock):
    results = iter([{"n": 1}, {"n": 2}])
    r = ToolRegistry()
    r.register(Tool("lookup", "조회", Args, lambda ctx, args: next(results)))
    r.register(Tool("write", "쓰기", Args, lambda ctx, args: None, writes=True))
    dump = record(conn, clock, call(LOOKUP), call(LOOKUP), answer(), tools=r)
    only_second = Rule("n2", after=lambda ctx, n, a, result: "막음" if result["n"] == 3 else None)
    assert replay(dump, SPEC, registry([]), rules=(only_second,)) == []
    first_only = Rule("n1", after=lambda ctx, n, a, result: "막음" if result["n"] == 1 else None)
    assert len(replay(dump, SPEC, registry([]), rules=(first_only,))) == 1


def test_error_step_stays_error_on_replay(conn, clock):
    def fail(ctx, args):
        raise ValueError("망가짐")

    r = ToolRegistry()
    r.register(Tool("lookup", "조회", Args, fail))
    r.register(Tool("write", "쓰기", Args, fail, writes=True))
    dump = record(conn, clock, call(LOOKUP), answer(), tools=r)
    rule = Rule("after", after=lambda ctx, name, args, result: "막음")
    assert replay(dump, SPEC, registry([]), rules=(rule,)) == []


def test_changed_arguments_are_a_difference(conn, clock):
    dump = record(conn, clock, call(LOOKUP), answer())
    dump["steps"][2]["args"] = {"chart_no": "C-9"}
    assert replay(dump, SPEC, registry([]))


def test_changed_outcome_is_a_difference(conn, clock):
    dump = record(conn, clock, call(LOOKUP), answer())
    diffs = replay(dump, replace(SPEC, max_tool_calls=0), registry([]))
    assert any("handed_off" in d for d in diffs)


def test_extra_recorded_call_is_a_difference(conn, clock):
    dump = record(conn, clock, call(LOOKUP), answer())
    dump["steps"].insert(3, {**dump["steps"][2], "seq": 99})
    assert any("호출 수" in d for d in replay(dump, SPEC, registry([])))


def test_changed_arguments_of_blocked_call_are_a_difference(conn, clock):
    """실행 전에 막힌 호출은 저장된 결과가 없어 인자 비교로만 달라진 것이 보인다"""
    rule = Rule("no.lookup", before=lambda ctx, name, args: "금지")
    dump = record(conn, clock, call(LOOKUP), answer(), rules=(rule,))
    dump["steps"][2]["args"] = {"chart_no": "C-9"}
    assert replay(dump, SPEC, registry([]), rules=(rule,))
