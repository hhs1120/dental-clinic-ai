"""재생 (앱 설계 §3b). 저장된 모델 응답·도구 결과로 실행을 다시 돌려 호출이 같은지 본다. 키 없이 돌고 실제 도구는
실행하지 않으며 기록은 메모리 DB에만 남는다. 응답이 저장값이라 잡는 것은 훅·권한·인자 스키마·상한의 변화이고,
프롬프트 변화는 잡지 못한다. 메모리 DB에는 앱 데이터가 없으므로 DB를 읽는 규칙은 재생할 수 없다."""

import json
import sqlite3
from collections import defaultdict
from dataclasses import replace

from backend import db
from backend.harness.hooks import READ_ONLY
from backend.harness.loop import AgentSpec, Runner
from backend.harness.store import RunStore
from backend.harness.tools import ToolRegistry
from shared.llm import LLMResult


class _Exhausted(Exception):
    """저장된 모델 응답이 끝남 — 실패로 끝난 실행은 여기까지만 비교한다"""


def export_run(store: RunStore, run_id: int) -> dict:
    """재생 입력 — tests/fixtures/agent_runs/에 저장할 수 있는 JSON 값"""
    return {"run": store.run(run_id), "steps": store.steps(run_id)}


class _Recorded:
    """Gateway.complete()와 같은 인터페이스 — 저장된 모델 응답을 순서대로 낸다"""

    def __init__(self, steps):
        self.models = [s["response"] for s in steps if s["kind"] == "model"]

    def complete(self, alias, **kwargs):
        if not self.models:
            raise _Exhausted
        r = self.models.pop(0)
        return LLMResult(
            True,
            r["text"],
            None,
            r["model"],
            False,
            0,
            0,
            r["cost_usd"],
            0,
            stop_reason=r["stop_reason"],
            tool_calls=tuple(r["tool_calls"]),
            content=r["content"],
        )


def _key(name: str, args) -> tuple:
    return name, json.dumps(args, sort_keys=True)


def _ran(step: dict) -> bool:
    """도구 함수까지 간 단계 — 실행 전에 막힌 건은 재생에서도 함수에 닿지 않는다"""
    return step["kind"] == "tool" and (
        step["hook_status"] == "pass" or step["raw_result"] is not None
    )


def _stubbed(registry: ToolRegistry, steps: list) -> ToolRegistry:
    """실제 함수 대신 저장된 결과를 호출 순서대로 돌려주는 도구"""
    saved = defaultdict(list)
    for s in filter(_ran, steps):
        saved[_key(s["tool"], s["args"])].append(s)

    def stub(name):
        def run(ctx, args):
            s = saved[_key(name, args)].pop(0)  # 없으면 IndexError — 기록에 없는 호출
            if s["rule_id"] == READ_ONLY:  # 쓰기까지 재현해야 같은 규칙이 다시 막는다
                ctx.store.conn.execute("INSERT OR REPLACE INTO ax_settings VALUES ('replay', '1')")
            if s["raw_result"] is not None:
                return s["raw_result"]["value"]
            if s["is_error"]:
                raise RuntimeError(s["result"])
            return s["result"]

        return run

    out = ToolRegistry()
    for name, tool in registry.tools.items():
        out.register(replace(tool, fn=stub(name)))
    return out


def _calls(steps: list) -> list[tuple]:
    return [
        (s["tool"], s["args"], s["hook_status"], s["rule_id"], s["is_error"])
        for s in steps
        if s["kind"] == "tool"
    ]


def replay(dump: dict, spec: AgentSpec, registry: ToolRegistry, rules: tuple = ()) -> list[str]:
    """달라진 점 설명 목록. 비면 같다"""
    steps, run = dump["steps"], dump["run"]
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA.read_text(encoding="utf-8"))
    store = RunStore(conn)
    run_spec = replace(spec, prompt_version=run["prompt_version"])
    stubs = _stubbed(registry, steps)
    runner = Runner(run_spec, _Recorded(steps), store, stubs, rules, "replay")
    with conn:
        run_id = store.create_run(spec.name, run["target"], run_spec.prompt_version, "replay")
        store.add_step(run_id, "input", result=steps[0]["result"])
    try:
        outcome = runner.drive(run_id)
    except _Exhausted:
        outcome = None
    before, after = _calls(steps), _calls(store.steps(run_id))
    conn.close()
    diffs = [f"{a} → {b}" for a, b in zip(before, after, strict=False) if a != b]
    if len(before) != len(after):
        diffs.append(f"호출 수 {len(before)} → {len(after)}")
    if outcome is None:  # 모델 응답 없이 끝난 실행만 여기서 멈추는 것이 정상이다
        if run["status"] not in ("failed", "running") and run["reason"] != "budget":
            diffs.append(f"저장된 응답이 끝남 — 원래 결과 {run['status']} {run['reason']}")
    elif (outcome.status, outcome.reason) != (run["status"], run["reason"]):
        diffs.append(f"결과 {run['status']} {run['reason']} → {outcome.status} {outcome.reason}")
    return diffs
