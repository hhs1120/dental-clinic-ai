"""③ 질문하기 에이전트 (기능 정의 §4 ③, 앱 설계 §3, 설계 docs/superpowers/specs/2026-11-09-chat-agent-design.md).
질문 하나를 도구 6개(search_docs·query_patient·query_schedule·query_claims·query_charts·refuse)로 조사해 확인한
사실만 출처와 함께 답한다. 끝은 answer(text, cited_ids) 또는 refuse. answer의 인용이 이번 질문의 검색·조회 결과 밖이거나
숫자가 도구 결과·질문에 없으면 after 훅이 첫 번째만 막아 같은 루프에서 다시 답하게 하고, 두 번째도 어긋나면 고정 문구로
끝낸다. 검색·챗 1회 = ax_search_logs 1행."""

import json
import sqlite3
from datetime import datetime

from pydantic import BaseModel, Field

from backend import prompts
from backend.harness.hooks import Rule
from backend.harness.loop import AgentSpec, Runner, external
from backend.harness.store import RunStore
from backend.harness.tools import Tool, ToolRegistry
from backend.query_tools import query_tools
from backend.rag_search import Searcher, search_tool
from backend.usage import record_voyage
from backend.verify import allowed_numbers, numbers_ok
from shared.clinic import Clinic

AGENT, ALIAS, TASK = "chat", "chat", "chat"
PROMPT_VERSION = "v1"
BASELINE_TASK, BASELINE_VERSION = (
    "chat_baseline",
    "v1",
)  # 단순 RAG 기준선(평가 설계 §3.3) — 같은 답변 모델
BASELINE_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "cited_ids": {"type": "array", "items": {"type": "string"}},
        "refused": {"type": "boolean"},
    },
    "required": ["text", "cited_ids", "refused"],
    "additionalProperties": False,
}
# 평가의 질문당 도구 호출 상한과 같게(평가 설계 §3.2 — tests/backend/test_chat.py가 비교)
CHAT_MAX_TOOL_CALLS = 6
CHAT_MAX_USD = 0.5  # 질문당 비용 상한 — dev 실측 뒤 다시 정한다
ANSWER, REFUSE = "answer", "refuse"
DATA_TOOLS = ("query_patient", "query_schedule", "query_claims")  # 청크 없이 답할 수 있는 조회
REFUSED_TEXT = "문서함에 없음"
FAILED_TEXT = "확인된 근거로 답하지 못했습니다"
CALL_LIMIT, CITE, NUMBER = "chat.call_limit", "chat.cite_outside", "chat.number_unverified"
RETRY_HINT = " 이번 질문의 검색·조회 결과만 인용하고, 숫자는 결과에 있는 그대로 쓴다. 건수는 필터를 주고 count를 읽는다."


class AnswerArgs(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    cited_ids: list[str] = Field(default_factory=list)


def _chart_plain(conn: sqlite3.Connection, chunk: dict) -> dict:
    """차트 청크 text의 검색용 괄호 꼬리를 떼고 서술 원문을 준다 — 화면 카드와 같은 기준(backend/search_cards.py)"""
    if chunk["source_type"] != "chart_note":
        return chunk
    prefix = chunk["text"].split("] ", 1)[0] + "]"
    note = conn.execute(
        "SELECT note FROM source.emr_chart_notes WHERE treatment_id = ?",
        (int(chunk["id"].split(":")[1]),),
    ).fetchone()
    return {**chunk, "text": f"{prefix} {note[0]}" if note else prefix}


def _search(searcher: Searcher) -> Tool:
    """챗 전용 search_docs — 공용 도구는 그대로 두고(후속 에이전트 입력이 바뀌지 않게) 차트 text만 바꾼다"""
    base = search_tool(searcher)

    def run(ctx, args):
        found = base.fn(ctx, args)
        return {
            b: [_chart_plain(ctx.store.conn, c) for c in chunks] for b, chunks in found.items()
        }

    return Tool(base.name, base.description, base.args, run)


def _with_event(tool: Tool, on_event) -> Tool:
    def run(ctx, args):
        on_event({"type": "step", "tool": tool.name})
        return tool.fn(ctx, args)

    return Tool(tool.name, tool.description, tool.args, run, tool.writes, tool.ends_run)


def registry(clinic: Clinic, searcher: Searcher, on_event=None) -> tuple[ToolRegistry, tuple]:
    reg = ToolRegistry()
    tools = [_search(searcher), *query_tools(clinic)]
    for t in tools:
        reg.register(_with_event(t, on_event) if on_event and t.name != REFUSE else t)
    reg.register(
        Tool(
            ANSWER,
            "최종 답을 낸다 — 확인한 사실만, 근거 청크·차트 id를 cited_ids에. 데이터 조회만으로 답하면 비워도 된다."
            " 호출하면 끝난다.",
            AnswerArgs,
            lambda ctx, args: args,
            ends_run=lambda args: "answered",
        )
    )
    return reg, (*(t.name for t in tools), ANSWER)


def spec(names: tuple) -> AgentSpec:
    return AgentSpec(
        name=AGENT,
        alias=ALIAS,
        task=TASK,
        prompt_version=PROMPT_VERSION,
        system=lambda v: prompts.load(TASK, v)["system"],
        tools=names,
        max_tool_calls=CHAT_MAX_TOOL_CALLS
        + 2,  # 답변 두 번 몫 — 다른 도구 상한은 훅 chat.call_limit
        max_usd=CHAT_MAX_USD,
    )


def _ok_tool_steps(steps: list[dict]) -> list[dict]:
    return [s for s in steps if s["kind"] == "tool" and not s["is_error"]]


def _retrieved(steps: list[dict]) -> list[str]:
    """이번 질문의 search_docs 청크 id + query_charts 차트 id(오류·막힌 단계 제외)"""
    out = []
    for s in _ok_tool_steps(steps):
        if s["tool"] == "search_docs":
            out += [c["id"] for chunks in s["result"].values() for c in chunks]
        elif s["tool"] == "query_charts":
            out += [r["id"] for r in s["result"]["results"]]
    return list(dict.fromkeys(out))


def _check(steps: list[dict], args: dict) -> str | None:
    """answer 인자 검사 — 어긋난 규칙 id 또는 None"""
    done = [s for s in _ok_tool_steps(steps) if s["tool"] not in (ANSWER, REFUSE)]
    cited = args["cited_ids"]
    if not set(cited) <= set(_retrieved(steps)):
        return CITE
    if not cited and not any(s["tool"] in DATA_TOOLS for s in done):
        return CITE  # 검색만 하고 인용 없이 답함 — 데이터 조회만 쓴 답은 비어도 된다(앱 설계 §3)
    sources = [str(s["result"]) for s in steps if s["kind"] == "input"]
    sources += [json.dumps(s["args"], ensure_ascii=False, default=str) for s in done]
    if not numbers_ok(args["text"], allowed_numbers(sources, [s["result"] for s in done], True)):
        return NUMBER
    return None


def _rules(today: str) -> tuple:
    def call_limit(ctx, name, args):
        if name in (ANSWER, REFUSE):
            return None
        used = [s for s in ctx.store.steps(ctx.run_id) if s["kind"] == "tool"]
        if sum(s["tool"] not in (ANSWER, REFUSE) for s in used) >= CHAT_MAX_TOOL_CALLS:
            return f"도구 호출 {CHAT_MAX_TOOL_CALLS}회 상한 — 지금까지의 결과로 answer 또는 refuse"
        return None

    def first_answer(ctx, name, args, result, rule):
        if name != ANSWER:
            return None
        steps = ctx.store.steps(ctx.run_id)
        if any(s["tool"] == ANSWER and s["hook_status"] == "blocked" for s in steps):
            return None  # 두 번째 답은 통과시켜 끝내고 chat()이 상태로 거른다
        steps = [*steps, {"kind": "input", "result": f"오늘 {today}"}]
        if _check(steps, args) != rule:
            return None
        return {
            CITE: "인용이 이번 질문의 검색·조회 결과 밖이거나 비어 있음.",
            NUMBER: "답의 숫자가 도구 결과·질문에 없음.",
        }[rule] + RETRY_HINT

    return (
        Rule(CALL_LIMIT, before=call_limit),
        Rule(CITE, after=lambda *a: first_answer(*a, CITE)),
        Rule(NUMBER, after=lambda *a: first_answer(*a, NUMBER)),
    )


def _status(store: RunStore, outcome, steps: list[dict], today: str) -> tuple[str, dict | None]:
    """(상태, 정상 answer 인자) — 설계 "상태" 순서"""
    if outcome.status == "handed_off" and outcome.reason in ("budget", "cost_limit"):
        return outcome.reason, None
    if outcome.reason == "call_limit" or any(s["rule_id"] == CALL_LIMIT for s in steps):
        return "call_limit", None
    ok = _ok_tool_steps(steps)
    if ok and ok[-1]["tool"] == REFUSE:
        return "refused", None
    if ok and ok[-1]["tool"] == ANSWER:
        args = ok[-1]["args"]
        rule = _check([*steps, {"kind": "input", "result": f"오늘 {today}"}], args)
        if rule is None:
            return "answered", args
        with store.conn:
            store.add_block(rule)
        return {CITE: "cite_failed", NUMBER: "number_failed"}[rule], None
    return "failed", None


def chat(
    conn: sqlite3.Connection,
    gw,
    searcher: Searcher,
    clinic: Clinic,
    question: str,
    *,
    is_eval: bool = False,
    on_event=None,
) -> dict:
    """질문 1개 → {log_id, status, tool_calls, retrieved_ids, answer_text, cited_ids, refuse_reason, fallback_used}.
    tool_calls는 answer를 뺀 도구 호출([{name, args}] — 평가 채점 형식)"""
    now = datetime.now().isoformat(sep=" ", timespec="seconds")
    log_id = conn.execute(
        "INSERT INTO ax_search_logs (kind, question, status, is_eval, created_at)"
        " VALUES ('chat', ?, 'running', ?, ?)",
        (question, int(is_eval), now),
    ).lastrowid
    conn.commit()  # Runner.start가 거절되면 미커밋 쓰기가 되돌아가므로
    today = clinic.today.isoformat()
    reg, names = registry(clinic, searcher, on_event)
    store = RunStore(conn)
    outcome = Runner(spec(names), gw, store, reg, rules=_rules(today)).start(
        str(log_id), f"오늘: {today}\n질문: {question}"
    )
    voyage = record_voyage(
        conn, searcher.drain_usage(), is_eval=is_eval, ref=("agent_run", outcome.run_id)
    )
    steps = store.steps(outcome.run_id)
    status, answer = _status(store, outcome, steps, today)
    tools = [s for s in steps if s["kind"] == "tool" and s["tool"] != ANSWER]
    refuse = next((s for s in reversed(tools) if s["tool"] == REFUSE and not s["is_error"]), None)
    fallback = conn.execute(
        "SELECT COALESCE(MAX(fallback_used), 0) FROM ax_llm_calls WHERE ref_type = 'agent_run'"
        " AND ref_id = ?",
        (str(outcome.run_id),),
    ).fetchone()[0]
    out = {
        "log_id": log_id,
        "status": status,
        "tool_calls": [{"name": s["tool"], "args": s["args"]} for s in tools],
        "retrieved_ids": _retrieved(steps),
        "answer_text": answer["text"]
        if answer
        else REFUSED_TEXT
        if status == "refused"
        else FAILED_TEXT,
        "cited_ids": answer["cited_ids"] if answer else [],
        "refuse_reason": refuse["args"]["reason"] if refuse and status == "refused" else None,
        "fallback_used": bool(fallback),
        "cost_usd": store.run(outcome.run_id)["cost_usd"] + voyage,
    }
    conn.execute(
        "UPDATE ax_search_logs SET tool_calls = ?, retrieved_ids = ?, answer_text = ?, cited_ids = ?,"
        " status = ?, refuse_reason = ?, run_id = ?, fallback_used = ? WHERE id = ?",
        (
            json.dumps(out["tool_calls"], ensure_ascii=False, default=str),
            json.dumps(out["retrieved_ids"]),
            out["answer_text"],
            json.dumps(out["cited_ids"]),
            status,
            out["refuse_reason"],
            outcome.run_id,
            int(out["fallback_used"]),
            log_id,
        ),
    )
    conn.commit()
    return out


def baseline(
    conn: sqlite3.Connection, gw, searcher: Searcher, question: str, *, is_eval: bool = False
) -> dict:
    """단순 RAG 기준선(평가 설계 §3.3) — 질문 원문으로 검색 1회(필터 없음), 같은 답변 모델이 도구 없이 그 결과만으로
    {text, cited_ids, refused}. 인용 확인·숫자 후검증은 붙이지 않는다(단순 RAG 그대로와 비교)"""
    found = searcher.search(question)
    chunks = [_chart_plain(conn, c) for b in found.values() for c in b]
    context = "\n\n".join(external(c["id"], c["text"]) for c in chunks)
    r = gw.complete(
        ALIAS,
        task=BASELINE_TASK,
        system=prompts.load(BASELINE_TASK, BASELINE_VERSION)["system"],
        messages=[{"role": "user", "content": f"질문: {question}\n\n검색 결과:\n{context}"}],
        schema=BASELINE_SCHEMA,
        prompt_version=BASELINE_VERSION,
    )
    voyage = record_voyage(conn, searcher.drain_usage(), is_eval=is_eval, ref=None)
    data = r.data if r.ok and r.data else {"text": "", "cited_ids": [], "refused": True}
    return {
        "retrieved_ids": [c["id"] for c in chunks],
        "answer_text": data["text"],
        "cited_ids": data["cited_ids"],
        "refused": data["refused"],
        "cost_usd": r.cost_usd + voyage,
        "fallback_used": r.fallback_used,
    }
