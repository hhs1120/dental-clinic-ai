"""⑧-2 증빙 탐색 에이전트 (기능 정의 §5 ⑧-2, 설계 docs/superpowers/specs/2026-11-11-finance-recon-design.md §4).
출금마다 증빙을 찾고 품목을 읽어 계정 과목 하나와 확신도·근거(조회 번호)를 낸다 — 규칙이 못 푼 줄은 찾기, 규칙이 푼
줄은 검산(입력에 규칙 답을 넣지 않는다). 실행은 하네스(Runner), 끝은 submit_match, 조회 도구는 backend/expense_tools.py
(읽기 전용). 제출 훅이 과목·후보 범위·근거 조회 번호를 막는다. 실행 기록(ax_agent_runs·ax_agent_steps, 대상 = 출금 키
expense.outflow_key)만이 상태 — 화면·규칙 비교·자동 확정(backend/expense_auto.py)·채점이 result()·results()로 읽는다.
조사 과정 줄은 ⑧-1과 같다(recon_tools.trace_lines). 일괄은 라운드 배치(make expense-ai), 화면은 결과만."""

import argparse
import sqlite3
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from backend import db, expense, expense_tools, prompts, recon_tools
from backend.harness import rounds
from backend.harness.hooks import Rule
from backend.harness.loop import AgentSpec, Runner
from backend.harness.store import RunLocked, RunStore, SqliteLlmLog, parse_step
from backend.harness.tools import Tool, ToolRegistry
from shared.clinic import load_clinic
from shared.llm import Gateway, load_config

ROOT = Path(__file__).resolve().parents[1]
AGENT, ALIAS, TASK = "expense_match", "expense", "expense"
PROMPT_VERSION = "v1"
SUBMIT = "submit_match"
EVIDENCE_RULE = "expense.evidence"
EXPENSE_MAX_TOOL_CALLS = 10  # 조사 상한(끝내는 도구 포함) — 시작값, dev 분포로 다시 본다(설계 §4)
EXPENSE_MAX_USD = 0.3  # 실행당 비용 상한 — 넘으면 결과 없이 끝난다
EST_USD = 0.06  # 출금당 예상 비용(일반 호출, 추정) — 일괄 시작 때 찍는 값
STATE = ROOT / "data" / "expense_batch.json"
NOTE_MAX = recon_tools.NOTE_MAX
ENDED = ("done", "handed_off")  # 결과로 읽는 끝 — failed는 다시 조사할 출금


class Reason(BaseModel):
    text: str = Field(min_length=1, max_length=NOTE_MAX)
    lookups: list[int] = Field(min_length=1, max_length=EXPENSE_MAX_TOOL_CALLS)


class SubmitArgs(BaseModel):
    document_ids: list[int] = Field(
        max_length=40
    )  # 빈 목록 = 증빙 없음, 월말 이체는 명세서 여러 장
    account: str
    confidence: Literal["high", "mid", "low"]
    reasons: list[Reason] = Field(min_length=1, max_length=3)
    see: str | None = Field(default=None, max_length=NOTE_MAX)  # 직원이 볼 것


def registry() -> tuple[ToolRegistry, tuple[str, ...]]:
    reg = ToolRegistry()
    for t in expense_tools.tools(SUBMIT):
        reg.register(t)
    reg.register(
        Tool(
            SUBMIT,
            "증빙과 계정 과목을 낸다 — 증빙 id들(없으면 빈 목록), 과목 하나, 확신도, 근거 글머리(조회 번호),"
            " 직원이 볼 것. 호출하면 조사가 끝난다.",
            SubmitArgs,
            lambda ctx, args: args,
            ends_run=lambda args: "submitted",
        )
    )
    return reg, (*expense_tools.LOOKUPS, SUBMIT)


def spec(names: tuple[str, ...]) -> AgentSpec:
    return AgentSpec(
        name=AGENT,
        alias=ALIAS,
        task=TASK,
        prompt_version=PROMPT_VERSION,
        system=lambda v: prompts.load(TASK, v)["system"],
        tools=names,
        max_tool_calls=EXPENSE_MAX_TOOL_CALLS,
        max_usd=EXPENSE_MAX_USD,
        end_tools=(SUBMIT,),
    )


def _evidence(ctx, name: str, args: dict) -> str | None:
    """과목은 목록 안, 증빙은 그 출금의 후보 안(확정이 후보 밖을 막으므로), 근거는 그 실행의 조회 번호(설계 §4)"""
    if name != SUBMIT:
        return None
    conn = ctx.store.conn
    accounts = expense.master_values(conn)["expense_accounts"]
    if args["account"] not in accounts:
        return f"모르는 계정 과목: {args['account']} — {', '.join(accounts)} 중 하나"
    cand = {d["id"] for d in expense_tools._candidates(conn, ctx.target)}
    if bad := [d for d in args["document_ids"] if d not in cand]:
        return f"증빙 {bad}는 이 출금의 후보가 아님 — search_docs로 찾은 증빙 중에서 고른다"
    n = len(recon_tools.lookup_steps(ctx.store.steps(ctx.run_id), SUBMIT))
    used = [no for r in args["reasons"] for no in r["lookups"]]
    if bad := [no for no in used if not 1 <= no <= n]:
        return f"조회 번호 {bad}는 이 조사에 없음 — 1~{n} 중에서 고른다"
    return None


def rules() -> tuple[Rule, ...]:
    return (Rule(EVIDENCE_RULE, before=_evidence),)


def _conclusion(a: dict | None) -> dict:
    if a is None:
        return {"kind": "conclusion", "document_ids": None, "account": None, "text": "결과 없음"}
    return {
        "kind": "conclusion",
        "document_ids": a["document_ids"],
        "account": a["account"],
        "confidence": a["confidence"],
        "text": " · ".join(r["text"] for r in a["reasons"]),
    }


def lines(steps: list[dict], finished: bool = True) -> list[dict]:
    """조사 과정 줄(recon_tools.trace_lines) — 제출 없이 끝났으면 '결과 없음'"""
    return recon_tools.trace_lines(steps, SUBMIT, _conclusion, finished)


_LIVE = (
    "agent = ? AND prompt_version = ? AND (status IN ('done', 'handed_off')"
    " OR (status = 'running' AND heartbeat_at >= ?))"
)  # 결과로 읽는 실행 — 끝났거나 잠금이 살아 있는 running


def _summary(run, steps: list[dict]) -> dict:
    """실행 하나 요약 — 제출이 없으면(상한·비용·글로 끝남, 도는 중) 증빙·과목 None"""
    a = recon_tools.submitted(steps, SUBMIT) if run["status"] != "running" else None
    return {
        "run_id": run["id"],
        "status": run["status"],
        "tool_calls": run["tool_calls"],
        "cost_usd": run["cost_usd"],
        "document_ids": sorted(set(a["document_ids"])) if a else None,
        "account": a["account"] if a else None,
        "confidence": a["confidence"] if a else None,
        "reasons": a["reasons"] if a else [],
        "see": a["see"] if a else None,
        "lines": lines(steps, finished=run["status"] in ENDED),
    }


def result(conn: sqlite3.Connection, key: str) -> dict | None:
    """그 출금의 지금 버전 최신 실행 요약 — 없으면 None"""
    return results(conn, {key}).get(key)


def results(conn: sqlite3.Connection, keys: set[str]) -> dict[str, dict]:
    """여러 출금의 최신 실행 요약 — 쿼리 수가 출금 수와 무관하다(지출 증빙 화면)"""
    latest = {}
    for run in conn.execute(
        f"SELECT * FROM ax_agent_runs WHERE {_LIVE} ORDER BY id",
        (AGENT, PROMPT_VERSION, RunStore(conn).fresh_since()),
    ):
        if run["target"] in keys:
            latest[run["target"]] = run
    steps: dict[int, list[dict]] = {r["id"]: [] for r in latest.values()}
    for row in conn.execute(
        "SELECT s.* FROM ax_agent_steps s JOIN ax_agent_runs r ON r.id = s.run_id"
        " WHERE r.agent = ? AND r.prompt_version = ? ORDER BY s.run_id, s.seq",
        (AGENT, PROMPT_VERSION),
    ):
        if row["run_id"] in steps:
            steps[row["run_id"]].append(parse_step(row))
    return {k: _summary(run, steps[run["id"]]) for k, run in latest.items()}


def targets(conn: sqlite3.Connection, today: date, months: list[str] | None = None) -> list[dict]:
    """기준일 전 출금 중 지금 버전으로 끝난(done·handed_off) 실행이 없는 것 — 실패한 출금은 다시 대상"""
    done = {
        r[0]
        for r in conn.execute(
            "SELECT target FROM ax_agent_runs WHERE agent = ? AND prompt_version = ?"
            " AND status IN ('done', 'handed_off')",
            (AGENT, PROMPT_VERSION),
        )
    }
    return [
        o
        for o in expense._load(conn, today)["outflows"]
        if expense.outflow_key(o) not in done and (not months or o["at"][:7] in months)
    ]


def _close_stuck(store: RunStore, runner: Runner, key: str) -> bool:
    """끊긴 실행(잠금 만료)은 실패로 닫는다(⑧-1 recon_agent와 같은 처리). 살아 있는 다른 실행이면 False"""
    row = store.conn.execute(
        "SELECT id FROM ax_agent_runs WHERE agent = ? AND target = ? AND status = 'running'",
        (AGENT, key),
    ).fetchone()
    if row is None:
        return True
    try:
        with store.conn:
            store.take_over(row[0], runner.owner)
            store.finish(row[0], "failed", runner.owner, reason="끊긴 실행")
    except RunLocked:
        return False
    return True


def run_batch(
    conn: sqlite3.Connection,
    gw,
    today: date,
    months: list[str] | None = None,
    *,
    batch: bool = True,
    state: Path = STATE,
    poll_seconds: int = 60,
) -> dict:
    """대상 출금 전부를 라운드 배치로 — {status: 건수, "cost_usd"}. 끊기면 다시 돌려 이어 간다"""
    reg, names = registry()
    store = RunStore(conn)
    runner = Runner(spec(names), gw, store, reg, rules())
    ids = []
    for o in targets(conn, today, months):
        key = expense.outflow_key(o)
        row = conn.execute(
            "SELECT id, prompt_version FROM ax_agent_runs WHERE agent = ? AND target = ?"
            " AND status = 'running'",
            (AGENT, key),
        ).fetchone()
        if row and row["prompt_version"] == PROMPT_VERSION:
            ids.append(row["id"])  # 끊긴 일괄을 이어 간다
            continue
        if row and not _close_stuck(store, runner, key):
            continue  # 옛 버전을 다른 곳이 아직 돌리는 중
        ids.append(rounds.create(runner, key, expense_tools.input_text(o)))
    rounds.run_rounds(
        runner,
        gw,
        ids,
        state,
        min_batch=rounds.ROUND_BATCH_MIN if batch else len(ids) + 1,
        poll_seconds=poll_seconds,
    )
    from backend import expense_auto  # expense_auto가 results()를 읽으므로 순환을 피해 여기서

    counts: dict = {"cost_usd": 0.0}
    counts["auto"] = expense_auto.refresh(conn, today)  # 정책 꺼짐이면 아무것도 안 함
    conn.commit()
    for rid in ids:
        run = store.run(rid)
        counts[run["status"]] = counts.get(run["status"], 0) + 1
        counts["cost_usd"] += run["cost_usd"] or 0
    return counts


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="python -m backend.expense_agent")
    p.add_argument("command", choices=("run",))
    p.add_argument("--month", action="append", help="YYYY-MM — 여러 번 줄 수 있다(없으면 전 기간)")
    p.add_argument("--no-batch", action="store_true", help="Batch 없이 일반 호출만")
    args = p.parse_args(argv)
    conn = db.connect(ROOT / "data" / "app.sqlite", ROOT / "data" / "source.sqlite")
    today = load_clinic().today
    todo = targets(conn, today, args.month)
    print(
        f"조사 대상 {len(todo)}건 · 예상 비용 약 ${len(todo) * EST_USD:.2f}(일반 호출 기준, Batch면 줄어듦)"
    )
    if not todo:
        return
    gw = Gateway(load_config(), SqliteLlmLog(conn), "runtime")
    counts = run_batch(conn, gw, today, args.month, batch=not args.no_batch)
    print(f"조사 {counts}")


if __name__ == "__main__":
    main()
