"""랭그래프 비교 실험 — ⑧-1 불일치 조사 에이전트(설계 docs/superpowers/specs/2026-11-16-langgraph-recon-design.md, 앱 설계
§3b 하네스를 직접 구현한 이유). 같은 프롬프트(recon v3)·도구(backend/recon_tools.py)·근거 규칙·상한·게이트웨이를
랭그래프 StateGraph 위에서 돌려 하네스(backend/harness/loop.py) 결과와 나란히 잰다. 앱은 바꾸지 않고(backend/는 이
모듈을 import하지 않는다) app.sqlite에는 쓰지 않는다 — 상태는 랭그래프 체크포인터(evaluation/runs/), 결과는 jsonl.
하네스 저장소에 기대는 도구·규칙·화면 줄(ctx.store.steps)은 그래프 상태 위 어댑터(GraphStore)로 맞춘다.
uv run --env-file .env --group langgraph python -m evaluation.langgraph_recon run [--limit N] [--plan]   (과금, 이어 돎)
uv run --group langgraph python -m evaluation.langgraph_recon score                                    (무료)"""

from typing import TypedDict

from pydantic import ValidationError

from backend import prompts, recon_agent
from backend.harness.hooks import BAD_ARGS, NOT_ALLOWED, READ_ONLY, first_block
from backend.harness.loop import (
    DEFAULT_RESULT_LIMIT,
    TRUST_NOTICE,
    ToolContext,
    _tool_message,
    external,
    nudge_text,
)
from backend.recon_agent import ALIAS, PROMPT_VERSION, RECON_MAX_TOOL_CALLS, SUBMIT, TASK
from shared.llm import BudgetExceeded

RECON_MAX_USD = recon_agent.RECON_MAX_USD  # 테스트가 바꿀 수 있게 모듈 이름으로
END_TOOLS = (SUBMIT,)
RECURSION_LIMIT = (
    100  # 랭그래프 슈퍼스텝 상한 — 모델·도구 한 바퀴가 2스텝, 조사 상한 12 + 되돌리기보다 넉넉히
)
_STEP_KEYS = ("tool_use_id", "tool", "args", "response", "result", "raw_result", "hook_status",
              "rule_id")  # fmt: skip


class State(TypedDict):
    key: str
    messages: list[dict]  # 게이트웨이에 그대로 넘기는 Anthropic 형식
    steps: list[dict]  # 하네스 ax_agent_steps 한 행 모양(parse_step) — 도구·규칙·화면 줄이 읽는다
    pending: list[dict]  # 답이 없는 도구 호출
    model_calls: int
    tool_calls: int
    cost_usd: float
    input_tokens: int
    output_tokens: int
    status: str | None  # done · handed_off · failed, 도는 중이면 None
    reason: str | None


class GraphStore:
    """하네스 RunStore 자리 — 도구는 conn으로 조회하고 steps로 조회 번호를 센다. 그래프 상태가 원본이라 이 어댑터가
    필요했다는 것 자체가 '상태가 둘로 갈라지는가'(설계 §4-5)의 답이다"""

    def __init__(self, conn, steps: list[dict]):
        self.conn, self._steps = conn, steps

    def steps(self, run_id) -> list[dict]:
        return self._steps


def _registry():
    return recon_agent.registry()


def _step(seq: int, kind: str, **fields) -> dict:
    return {"seq": seq, "kind": kind, **dict.fromkeys(_STEP_KEYS), "is_error": 0,
            "truncated": 0} | fields  # fmt: skip


def initial_state(key: str, text: str) -> State:
    wrapped = external("input", text)
    return State(key=key, messages=[{"role": "user", "content": wrapped}],
                 steps=[_step(1, "input", result=wrapped)], pending=[], model_calls=0, tool_calls=0,
                 cost_usd=0.0, input_tokens=0, output_tokens=0, status=None, reason=None)  # fmt: skip


def call_tool(conn, steps: list[dict], key: str, call: dict) -> tuple[dict, dict]:
    """도구 하나 — (모델에 줄 tool_result 블록, 새 단계). 하네스 Runner._tool과 같은 순서: 허용 밖·인자 검증 실패·
    before 규칙은 막힘, 실행 뒤 읽기 전용 도구가 DB를 바꿨으면 막힘, 예외는 오류 결과로 모델에 돌려준다"""
    reg, names = _registry()
    ctx = ToolContext(0, recon_agent.AGENT, key, GraphStore(conn, steps))
    name, raw = call["name"], call["input"]

    def record(args, result, status, rule_id=None, is_error=False, raw_result=None):
        block, cut = _tool_message(
            call["id"], f"tool:{name}", result, DEFAULT_RESULT_LIMIT, is_error
        )
        step = _step(len(steps) + 1, "tool", tool_use_id=call["id"], tool=name, args=args, result=result,
                     raw_result=raw_result, hook_status=status, rule_id=rule_id,
                     is_error=int(is_error), truncated=int(cut))  # fmt: skip
        return block, step

    def block(args, rule_id, why, raw_result=None):
        return record(args, f"막힘: {why}", "blocked", rule_id, True, raw_result)

    tool = reg.tools.get(name)
    if tool is None or name not in names:
        return block(raw, NOT_ALLOWED, f"{name} 권한 없음")
    try:
        args = tool.args.model_validate(raw).model_dump(mode="json")
    except ValidationError as e:
        return block(raw, BAD_ARGS, str(e))
    if blocked := first_block(recon_agent.rules(), "before", ctx, name, args):
        return block(args, *blocked)
    changes = conn.total_changes
    try:
        result = tool.fn(ctx, args)
    except Exception as e:  # 도구 오류는 모델에 돌려 다른 길을 찾게 한다
        return record(args, f"{type(e).__name__}: {e}", "pass", is_error=True)
    if not tool.writes and conn.total_changes != changes:
        conn.rollback()
        return block(args, READ_ONLY, f"{name}은 쓰기 도구가 아님", {"value": result})
    if blocked := first_block(recon_agent.rules(), "after", ctx, name, args, result):
        return block(args, *blocked, {"value": result})
    return record(args, result, "pass")


def _ended(step: dict) -> str | None:
    reg, _ = _registry()
    tool = reg.tools.get(step["tool"])
    if tool is None or tool.ends_run is None or step["is_error"]:
        return None
    return tool.ends_run(step["args"])


def _nudged(steps: list[dict]) -> bool:
    return any(s["kind"] == "input" and s["result"] == nudge_text(END_TOOLS) for s in steps)


def build_graph(gw, conn, checkpointer=None):
    """노드 model(게이트웨이 호출) ↔ tools(도구 실행), 끝·상한이면 END. 하네스 루프의 분기를 그래프 간선으로 옮긴 것"""
    from langgraph.graph import END, START, StateGraph

    reg, names = _registry()
    schemas = reg.schemas(names)
    system = prompts.load(TASK, PROMPT_VERSION)["system"] + TRUST_NOTICE

    def model(st: State) -> dict:
        if st["cost_usd"] >= RECON_MAX_USD:
            return {"status": "handed_off", "reason": "cost_limit"}
        try:
            r = gw.complete(ALIAS, task=TASK, messages=st["messages"], system=system,
                            tools=schemas, prompt_version=PROMPT_VERSION,
                            ref=("langgraph", st["key"]))  # fmt: skip
        except BudgetExceeded:
            return {"status": "handed_off", "reason": "budget"}
        out = {
            "model_calls": st["model_calls"] + 1,
            "cost_usd": st["cost_usd"] + r.cost_usd,
            "input_tokens": st["input_tokens"] + r.input_tokens,
            "output_tokens": st["output_tokens"] + r.output_tokens,
        }
        if not r.ok:
            return out | {"status": "failed", "reason": r.error}
        steps = st["steps"] + [_step(len(st["steps"]) + 1, "model", response={
            "text": r.text, "stop_reason": r.stop_reason, "tool_calls": list(r.tool_calls),
            "content": r.content, "model": r.model, "cost_usd": r.cost_usd})]  # fmt: skip
        messages = st["messages"] + [{"role": "assistant", "content": r.content}]
        if r.tool_calls:
            return out | {"steps": steps, "messages": messages, "pending": list(r.tool_calls)}
        if _nudged(steps):  # 한 번만 — 두 번째 글은 그대로 끝낸다
            return out | {"steps": steps, "status": "done", "reason": None}
        nudge = nudge_text(END_TOOLS)
        return out | {
            "steps": steps + [_step(len(steps) + 1, "input", result=nudge)],
            "messages": messages + [{"role": "user", "content": nudge}],
        }

    def tools(st: State) -> dict:
        steps, results, n = list(st["steps"]), [], st["tool_calls"]
        out: dict = {}
        for c in st["pending"]:
            if n >= RECON_MAX_TOOL_CALLS:
                out = {"status": "handed_off", "reason": "call_limit"}
                break
            blk, step = call_tool(conn, steps, st["key"], c)
            steps.append(step)
            results.append(blk)
            n += 1
            if reason := _ended(step):
                out = {"status": "done", "reason": reason}
                break
        messages = st["messages"] + ([{"role": "user", "content": results}] if results else [])
        return out | {"steps": steps, "messages": messages, "pending": [], "tool_calls": n}

    def after_model(st: State) -> str:
        return END if st["status"] else "tools" if st["pending"] else "model"

    def after_tools(st: State) -> str:
        return END if st["status"] else "model"

    g = StateGraph(State)
    g.add_node("model", model)
    g.add_node("tools", tools)
    g.add_edge(START, "model")
    g.add_conditional_edges("model", after_model, ["tools", "model", END])
    g.add_conditional_edges("tools", after_tools, ["model", END])
    return g.compile(checkpointer=checkpointer)


def investigate(graph, key: str, text: str, resume: bool = False) -> State:
    """건 하나를 끝까지 — 체크포인터가 있으면 thread id = 건 키, resume이면 마지막 체크포인트에서 이어 간다"""
    config = {"configurable": {"thread_id": key}, "recursion_limit": RECURSION_LIMIT}
    return graph.invoke(None if resume else initial_state(key, text), config)


def summary(st: State) -> dict:
    """하네스 결과 요약(recon_agent._summary)과 같은 모양 — 채점이 같은 규칙으로 읽는다"""
    run = {"id": st["key"], "status": st["status"] or "running", "tool_calls": st["tool_calls"],
           "cost_usd": st["cost_usd"], "reason": st["reason"]}  # fmt: skip
    return recon_agent._summary(run, st["steps"])
