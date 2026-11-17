"""② 개선 에이전트 — 재료 오류 통화를 읽고 유형으로 묶어 has_todo 규칙 한 줄 후보를 낸다(자동 개선 설계 §3,
평가 설계 §2.7). 앱과 같은 하네스(backend/harness/)를 쓰되 저장소는 STORE(앱 스키마로 만든 별도 파일, 커밋 안 함) —
골드가 들어 있어 app.sqlite에 쓰지 않는다(앱 설계 §3b 저장소 주입). 녹취는 재료 오류 통화만 읽을 수 있다."""

import sqlite3
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

from backend import db, prompts
from backend.harness.hooks import Rule
from backend.harness.loop import AgentSpec, Runner
from backend.harness.store import RunStore
from backend.harness.tools import Tool, ToolRegistry
from backend.recon_tools import submitted

STORE = Path("evaluation/runs/improve/harness.sqlite")
AGENT, ALIAS, TASK, PROMPT_VERSION = "improve_classify", "improve", "improve", "v1"
READ, TYPES, SUBMIT = "read_call", "submit_types", "submit_rule"
MATERIAL_RULE = "improve.material_only"
MAX_TOOL_CALLS = 40  # 재료 오류 수십 건을 읽을 여유(Claude 판단)
MAX_USD = 2.0
RULE_PREFIX = "- has_todo:"


class ReadArgs(BaseModel):
    call_uid: str


class ErrorType(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    n: int = Field(ge=1)


class TypesArgs(BaseModel):
    types: list[ErrorType] = Field(min_length=1, max_length=10)


class RuleArgs(BaseModel):
    rule: str = Field(max_length=300)

    @field_validator("rule")
    @classmethod
    def _one_rule_line(cls, v: str) -> str:
        if "\n" in v or not v.startswith(RULE_PREFIX):
            raise ValueError(f"'{RULE_PREFIX}'로 시작하는 한 줄이어야 한다")
        return v


def open_store(path: Path = STORE) -> sqlite3.Connection:
    """하네스 저장소 — 없으면 앱 스키마로 만든다(하네스 표만 쓴다)"""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    db.init_schema(conn)
    return conn


def _registry(errors: dict[str, dict]) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(Tool(READ, "재료 오류 통화 하나의 녹취·방향·모델 값·정답 값을 읽는다.", ReadArgs,
                      lambda ctx, a: {k: errors[a["call_uid"]][k]
                                      for k in ("transcript", "direction", "model", "gold")}))  # fmt: skip
    reg.register(Tool(TYPES, "오류 유형을 낸다 — 유형 이름(일반 표현, 녹취 문장 금지)과 해당 통화 수.",
                      TypesArgs, lambda ctx, a: "받음"))  # fmt: skip
    reg.register(Tool(SUBMIT, "has_todo 규칙 한 줄 후보를 낸다. 호출하면 끝난다.", RuleArgs,
                      lambda ctx, a: "받음", ends_run=lambda a: "submitted"))  # fmt: skip
    return reg


def _material_only(errors: dict[str, dict]):
    def check(ctx, name: str, args: dict) -> str | None:
        if name == READ and args["call_uid"] not in errors:
            return "재료 오류 목록 밖 통화 — 목록의 통화만 읽는다"
        return None

    return check


def input_text(base_rule: str, errors: list[dict], failures: list[dict]) -> str:
    """첫 입력 — 기준 규칙, 오류 통화 목록(id·모델 값·정답 값, 녹취는 도구로), 앞선 탈락 후보(바꾼 규칙·못 넘은
    조건 이름만 — 수치·건별 목록은 넘기지 않는다, 평가 설계 §2.7 실패 기록)"""
    lines = [f"기준 규칙: {base_rule}", "", f"재료 오류 통화 {len(errors)}건(id · 모델 → 정답):"]
    lines += [f"- {e['call_uid']} · {e['model']} → {e['gold']}" for e in errors]
    if failures:
        lines += ["", "앞선 탈락 후보(바꾼 규칙 · 못 넘은 조건):"]
        lines += [f"- {f['rule']} · {', '.join(f['failed'])}" for f in failures]
    return "\n".join(lines)


def propose(gw, store_conn, base_rule: str, errors: list[dict], failures: list[dict],
            target: str) -> dict:  # fmt: skip
    """에이전트 한 번 — {"status", "rule"(제출 없으면 None), "types", "cost_usd"}"""
    by_id = {e["call_uid"]: e for e in errors}
    spec = AgentSpec(
        name=AGENT, alias=ALIAS, task=TASK, prompt_version=PROMPT_VERSION,
        system=lambda v: prompts.load(TASK, v)["system"], tools=(READ, TYPES, SUBMIT),
        max_tool_calls=MAX_TOOL_CALLS, max_usd=MAX_USD, end_tools=(SUBMIT,),
    )  # fmt: skip
    store = RunStore(store_conn)
    runner = Runner(spec, gw, store, _registry(by_id),
                    rules=(Rule(MATERIAL_RULE, before=_material_only(by_id)),))  # fmt: skip
    out = runner.start(target, input_text(base_rule, errors, failures))
    steps = store.steps(out.run_id)
    rule = submitted(steps, SUBMIT)
    types = submitted(steps, TYPES)
    return {
        "status": out.status,
        "rule": rule["rule"] if rule else None,
        "types": types["types"] if types else [],
        "cost_usd": store.run(out.run_id)["cost_usd"],
    }
