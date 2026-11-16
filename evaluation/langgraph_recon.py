"""랭그래프 비교 실험 — ⑧-1 불일치 조사 에이전트(설계 docs/superpowers/specs/2026-11-16-langgraph-recon-design.md, 앱 설계
§3b 하네스를 직접 구현한 이유). 같은 프롬프트(recon v3)·도구(backend/recon_tools.py)·근거 규칙·상한·게이트웨이를
랭그래프 StateGraph 위에서 돌려 하네스(backend/harness/loop.py) 결과와 나란히 잰다. 앱은 바꾸지 않고(backend/는 이
모듈을 import하지 않는다) app.sqlite에는 쓰지 않는다 — 상태는 랭그래프 체크포인터(evaluation/runs/), 결과는 jsonl.
하네스 저장소에 기대는 도구·규칙·화면 줄(ctx.store.steps)은 그래프 상태 위 어댑터(GraphStore)로 맞춘다.
uv run --env-file .env --group langgraph python -m evaluation.langgraph_recon run [--limit N] [--plan]   (과금, 이어 돎)
uv run --group langgraph python -m evaluation.langgraph_recon score                                    (무료)"""

import argparse
import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import TypedDict

from pydantic import ValidationError

from backend import prompts, recon_agent, recon_match, recon_tools
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

ROOT = Path(__file__).resolve().parents[1]
RUNS = (
    ROOT / "evaluation" / "langgraph_recon_runs.jsonl"
)  # 건마다 결과 한 줄(끝난 것만 다시 돌지 않는다)
LLM_LOG = (
    ROOT / "evaluation" / "langgraph_recon_llm.jsonl"
)  # 게이트웨이 기록 — app.sqlite에 쓰지 않는다
CHECKPOINTS = ROOT / "evaluation" / "runs" / "langgraph_recon.sqlite"  # 랭그래프 체크포인터
BATCH_DISCOUNT = 0.5  # 하네스 실행은 라운드 배치(Batch 단가 절반) — 일반 호출 환산에 쓴다

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


# ── 실행·채점(설계 §5) ──────────────────────────────────────────────


def read_runs(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x]


def latest(lines: list[dict]) -> dict[str, dict]:
    """건 키 → 마지막 결과 줄"""
    return {r["key"]: r for r in lines}


def _line(st: State) -> dict:
    s = summary(st)
    keep = (
        "model_calls",
        "tool_calls",
        "input_tokens",
        "output_tokens",
        "cost_usd",
        "status",
        "reason",
    )
    return {"key": st["key"], **{k: st[k] for k in keep}, "cost_usd": round(st["cost_usd"], 6),
            **{k: s[k] for k in ("cause", "runner_up", "evidence")}}  # fmt: skip


def run(gw, conn, items: list[dict], today: date, path: Path, checkpoints: Path) -> dict:
    """끝난(done·handed_off) 결과 줄이 없는 건만 — 체크포인트에 끊긴 상태가 있으면 거기서 이어 간다. {상태: 건수}"""
    from langgraph.checkpoint.sqlite import SqliteSaver

    done = {k for k, r in latest(read_runs(path)).items() if r["status"] in recon_agent.ENDED}
    books = recon_match.load(conn, today)
    checkpoints.parent.mkdir(parents=True, exist_ok=True)
    saver = SqliteSaver(sqlite3.connect(checkpoints, check_same_thread=False))
    graph = build_graph(gw, conn, saver)
    out: dict = {}
    with path.open("a", encoding="utf-8") as f:
        for item in items:
            if item["key"] in done:
                continue
            cfg = {"configurable": {"thread_id": item["key"]}}
            snap = graph.get_state(cfg)
            resume = bool(snap.next) and not snap.values.get("status")
            if snap.values and not resume:  # 끝난 thread(실패 등)는 지우고 처음부터
                saver.delete_thread(item["key"])
            st = investigate(
                graph, item["key"], recon_tools.input_text(item, books, today), resume
            )
            f.write(json.dumps(_line(st), ensure_ascii=False) + "\n")
            f.flush()
            out[st["status"]] = out.get(st["status"], 0) + 1
    return out


def plan(items: list[dict], lines: dict, harness_mean_usd: float) -> dict:
    """나갈 건 수와 예상 비용(무료) — 하네스 v3 건당 평균(Batch)의 일반 호출 환산"""
    todo = [i for i in items if lines.get(i["key"], {}).get("status") not in recon_agent.ENDED]
    est = round(len(todo) * harness_mean_usd / BATCH_DISCOUNT, 2)
    return {"targets": len(items), "todo": len(todo), "estimated_usd": est}


def _connect() -> sqlite3.Connection:
    """앱·생성·정답 DB 모두 읽기 전용 — 이 실험은 app.sqlite에 쓰지 않는다"""

    def uri(p: Path) -> str:
        return f"{p.resolve().as_uri()}?mode=ro"

    conn = sqlite3.connect(uri(ROOT / "data" / "app.sqlite"), uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("ATTACH DATABASE ? AS source", (uri(ROOT / "data" / "source.sqlite"),))
    conn.execute("ATTACH DATABASE ? AS truth", (uri(ROOT / "ground_truth" / "truth.sqlite"),))
    return conn


def dev_items(conn, today: date) -> list[dict]:
    """dev 정답에 대응하는 탐지 건(evaluation/recon.py match와 같은 대응) — 하네스 v3 dev 채점과 같은 건"""
    from evaluation.recon import match

    items = recon_match.detect(recon_match.load(conn, today))
    truth = conn.execute(
        "SELECT fin_ids, payment_ids FROM truth.truth_recon_mismatches WHERE split = 'dev'"
    ).fetchall()
    keys = {m["key"] for t in truth if (m := match(dict(t), items))}
    return [i for i in items if i["key"] in keys]


def harness_stats(conn, keys: set[str]) -> dict[str, dict]:
    """하네스 v3 실행(앱 DB, 지금 결과로 읽히는 것)의 건별 호출·비용. 토큰은 비우다 — 라운드 배치 호출은 기록이
    배치 단위(ref_type batch)라 실행별로 나눌 수 없다"""
    results = recon_agent.results(conn, keys)
    out = {}
    for k, r in results.items():
        run = conn.execute("SELECT model_calls FROM ax_agent_runs WHERE id = ?", (r["run_id"],))
        out[k] = {"model_calls": run.fetchone()[0], "tool_calls": r["tool_calls"],
                  "input_tokens": None, "output_tokens": None,
                  "cost_usd": r["cost_usd"] or 0.0, "status": r["status"]}  # fmt: skip
    return out


def _calls(rows: list[dict]) -> dict:
    n = len(rows) or 1

    def mean_max(k):
        vals = [r[k] for r in rows]
        return {"mean": round(sum(vals) / n, 2), "max": max(vals, default=0)}

    return {
        "runs": len(rows),
        "model_calls": mean_max("model_calls"),
        "tool_calls": mean_max("tool_calls"),
        "input_tokens": None if None in (t := [r["input_tokens"] for r in rows]) else sum(t),
        "output_tokens": None if None in (t := [r["output_tokens"] for r in rows]) else sum(t),
        "cost_usd": round(sum(r["cost_usd"] for r in rows), 4),
        "status": {s: sum(r["status"] == s for r in rows) for s in {r["status"] for r in rows}},
    }


def compare(conn, today: date, path: Path = RUNS) -> dict:
    """하네스(앱 DB)와 랭그래프(결과 줄)를 같은 채점 규칙(evaluation/recon.py score)으로 나란히(설계 §4)"""
    from evaluation.recon import score

    items = dev_items(conn, today)
    keys = {i["key"] for i in items}
    lg_lines = {k: r for k, r in latest(read_runs(path)).items() if k in keys}
    agents = {k: {"run_id": k, "status": r["status"], "tool_calls": r["tool_calls"],
                  "cost_usd": r["cost_usd"], "cause": r["cause"]} for k, r in lg_lines.items()}  # fmt: skip
    h = harness_stats(conn, keys)
    hs, ls = score(conn, "dev", today), score(conn, "dev", today, agents=agents)
    pick = ("agent", "agent_by_group", "abstain", "missing", "pair", "steps_mean")
    harness_calls = _calls(list(h.values()))
    harness_calls["cost_usd_plain"] = round(harness_calls["cost_usd"] / BATCH_DISCOUNT, 4)
    return {
        "targets": len(items),
        "rule": hs["rule"],
        "harness": {k: hs[k] for k in pick} | {"calls": harness_calls},
        "langgraph": {k: ls[k] for k in pick} | {"calls": _calls(list(lg_lines.values()))},
    }


def main(argv=None) -> None:
    from shared.clinic import load_clinic
    from shared.llm import Gateway, JsonlLog, load_config

    p = argparse.ArgumentParser(prog="python -m evaluation.langgraph_recon")
    p.add_argument("command", choices=("run", "score"))
    p.add_argument("--limit", type=int, help="앞에서 N건만")
    p.add_argument("--plan", action="store_true", help="LLM 없이 건 수·예상 비용만")
    args = p.parse_args(argv)
    conn, today = _connect(), load_clinic().today
    if args.command == "score":
        print(json.dumps(compare(conn, today), ensure_ascii=False, indent=1))
        return
    items = dev_items(conn, today)[: args.limit]
    if args.plan:
        h = harness_stats(conn, {i["key"] for i in items})
        mean = sum(r["cost_usd"] for r in h.values()) / (len(h) or 1)
        print(json.dumps(plan(items, latest(read_runs(RUNS)), mean)))
        return
    gw = Gateway(load_config(), JsonlLog(LLM_LOG), scope="generator")
    print(run(gw, conn, items, today, RUNS, CHECKPOINTS))


if __name__ == "__main__":
    main()
