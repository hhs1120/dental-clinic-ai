"""⑧-1 조사 에이전트 (기능 정의 §5 ⑧-1, 설계 docs/superpowers/specs/2026-11-11-finance-recon-design.md §3).
불일치 건 하나마다 가설 → 조회 → 비교를 되풀이해 원인 1·2순위와 근거(조회 번호)를 낸다. 실행은 하네스(Runner), 끝은
submit_cause, 조회 도구는 backend/recon_tools.py(읽기 전용). 근거 번호가 그 실행의 조회가 아니면 훅이 막는다.
실행 기록(ax_agent_runs·ax_agent_steps, 대상 = 건 키)만이 상태다 — 화면 줄(lines)·확정·자동 마감(backend/recon_auto.py)·
채점이 모두 result()와 lines()로 읽는다. 조사 과정 줄은 기록을 나눠 보인다(설계 §3 "조사 과정 줄
종류"): 조회 도구 인자 checked → 비교·purpose → 가설, 조회 → 조회 번호 + 요약, submit_cause → 결론. 모델이 따로 쓴
글은 `가설:`·`비교:` 머리말로 가설·비교(머리말 없으면 생각). 잠금이 만료된 running(끊긴 조사)과 failed는 결과가 아니다 —
화면에서 다시 조사할 수 있다.
일괄(make recon)은 라운드 배치(backend/harness/rounds.py), 화면 `조사 시작`은 일반 호출(investigate)."""

import argparse
import re
import sqlite3
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from backend import db, prompts, recon_match, recon_tools
from backend.harness import rounds
from backend.harness.hooks import BAD_ARGS, Rule
from backend.harness.loop import AgentSpec, Runner
from backend.harness.store import RunLocked, RunStore, SqliteLlmLog, parse_step
from backend.harness.tools import Tool, ToolRegistry
from shared.clinic import load_clinic
from shared.llm import Gateway, load_config

ROOT = Path(__file__).resolve().parents[1]
AGENT, ALIAS, TASK = "recon_cause", "recon", "recon"
# v2: 상한 12, 구강용품 조합은 도구가 계산(조합이 없으면 진료 외 매출 아님)
# v3: 가설·비교를 모델 글 대신 조회 인자 purpose·checked로(v2 지시는 reasoning_extraction 거절, AI 실험 기록)
PROMPT_VERSION = "v3"
SUBMIT = "submit_cause"
EVIDENCE_RULE = "recon.evidence"
# 조사 단계 상한(끝내는 도구 포함, 설계 §3) — 시작값 8은 dev 시험 5건 중 2건이 걸려 12로(AI 실험 기록 §26)
RECON_MAX_TOOL_CALLS = 12
RECON_MAX_USD = 0.3  # 실행당 비용 상한 — 넘으면 원인 미정으로 끝난다
EST_USD = 0.08  # 건당 예상 비용(일반 호출, 추정) — 일괄 시작 때 찍는 값
STATE = ROOT / "data" / "recon_batch.json"
# 원인 9개(source.ax_master_values recon_causes 키 — tests/backend/test_recon_agent.py가 비교) + 근거 부족
CAUSES = (
    "split_payment",
    "cancel_reapprove",
    "fee_mismatch",
    "deposit_delay",
    "non_clinical_sale",
    "entry_missing",
    "amount_typo",
    "date_shift",
    "method_wrong",
    "unknown",
)
_PREFIX = re.compile(r"^[\s*#>-]*(가설|비교)\s*[:：]\**\s*")
_KIND = {"가설": "hypothesis", "비교": "compare"}
ENDED = ("done", "handed_off")  # 결과가 있는 끝 — failed는 원인 미정이 아니라 다시 조사할 건
_END_REASON = {
    "call_limit": "조사 상한에 걸려 원인 미정",
    "cost_limit": "비용 상한에 걸려 원인 미정",
    "budget": "하루 비용 상한에 걸려 원인 미정",
}


class AlreadyDone(RuntimeError):
    """이 건은 지금 버전으로 이미 조사했다 — 다시 조사하지 않는다(설계 §3)"""


class SubmitArgs(BaseModel):
    cause: Literal[CAUSES]  # type: ignore[valid-type]
    runner_up: Literal[CAUSES] | None = None  # type: ignore[valid-type]
    evidence: list[int] = Field(default_factory=list, max_length=RECON_MAX_TOOL_CALLS)
    reason: str = Field(min_length=1, max_length=200)


def registry() -> tuple[ToolRegistry, tuple[str, ...]]:
    reg = ToolRegistry()
    for t in recon_tools.tools(SUBMIT):
        reg.register(t)
    reg.register(
        Tool(
            SUBMIT,
            "원인을 낸다 — 1순위·2순위 원인, 근거 조회 번호, 한두 문장 이유. 호출하면 조사가 끝난다.",
            SubmitArgs,
            lambda ctx, args: args,
            ends_run=lambda args: "submitted",
        )
    )
    return reg, (*recon_tools.LOOKUPS, SUBMIT)


def spec(names: tuple[str, ...]) -> AgentSpec:
    return AgentSpec(
        name=AGENT,
        alias=ALIAS,
        task=TASK,
        prompt_version=PROMPT_VERSION,
        system=lambda v: prompts.load(TASK, v)["system"],
        tools=names,
        max_tool_calls=RECON_MAX_TOOL_CALLS,
        max_usd=RECON_MAX_USD,
        end_tools=(SUBMIT,),
    )


def _evidence(ctx, name: str, args: dict) -> str | None:
    """근거 = 그 실행의 조회 번호(설계 §3 — 조회하지 않은 사실로 원인을 고르지 않게)"""
    if name != SUBMIT:
        return None
    n = len(recon_tools.lookup_steps(ctx.store.steps(ctx.run_id), SUBMIT))
    if args["cause"] != "unknown" and not args["evidence"]:
        return f"근거 조회 번호가 없음 — 조회 번호 1~{n} 중에서 고른다"
    if bad := [e for e in args["evidence"] if not 1 <= e <= n]:
        return f"조회 번호 {bad}는 이 조사에 없음 — 1~{n} 중에서 고른다"
    return None


def rules() -> tuple[Rule, ...]:
    return (Rule(EVIDENCE_RULE, before=_evidence),)


def _submitted(steps: list[dict]) -> dict | None:
    for s in reversed(steps):
        if (
            s["kind"] == "tool"
            and s["tool"] == SUBMIT
            and s["hook_status"] == "pass"
            and not s["is_error"]
        ):
            return s["args"]
    return None


_LIVE = (
    "agent = ? AND prompt_version = ? AND (status IN ('done', 'handed_off')"
    " OR (status = 'running' AND heartbeat_at >= ?))"
)  # 결과로 읽는 실행 — 끝났거나 잠금이 살아 있는 running


def _latest(conn: sqlite3.Connection, key: str):
    return conn.execute(
        f"SELECT * FROM ax_agent_runs WHERE {_LIVE} AND target = ? ORDER BY id DESC LIMIT 1",
        (AGENT, PROMPT_VERSION, RunStore(conn).fresh_since(), key),
    ).fetchone()


def _summary(run, steps: list[dict]) -> dict:
    """실행 하나 요약 — 제출이 없으면 unknown(상한·비용·글로 끝남), 도는 중이면 cause None"""
    out = {
        "run_id": run["id"],
        "status": run["status"],
        "tool_calls": run["tool_calls"],
        "cost_usd": run["cost_usd"],
        "cause": None,
        "runner_up": None,
        "evidence": [],
        "reason": None,
    }
    if run["status"] == "running":
        return out
    args = _submitted(steps)
    if args is None:
        return out | {
            "cause": "unknown",
            "reason": _END_REASON.get(run["reason"], "원인을 내지 않음"),
        }
    return out | {k: args[k] for k in ("cause", "runner_up", "evidence", "reason")}


def result(conn: sqlite3.Connection, key: str) -> dict | None:
    """그 건의 지금 버전 최신 실행(done·handed_off·running) 요약 — 없으면 None"""
    run = _latest(conn, key)
    return None if run is None else _summary(run, RunStore(conn).steps(run["id"]))


def results(conn: sqlite3.Connection, keys: set[str]) -> dict[str, dict]:
    """result()와 조사 과정 줄(lines)을 여러 건에 — 쿼리 수가 건 수와 무관하다(일계표 화면)"""
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
    return {
        k: _summary(run, steps[run["id"]])
        | {"lines": lines(steps[run["id"]], finished=run["status"] in ENDED)}
        for k, run in latest.items()
    }


def _short_block(text: str) -> str:
    """인자 검증 오류(pydantic 원문) → 첫 오류 문구만 — 화면 막힘 줄"""
    msgs = [
        ln.strip().split(" [type=")[0]
        for ln in text.splitlines()[1:]
        if ln.startswith("  ") and not ln.strip().startswith("For further")
    ]
    return "막힘: " + (" · ".join(msgs) or text)


def lines(steps: list[dict], finished: bool = True) -> list[dict]:
    """조사 과정 줄 — 저장된 단계를 나눠 보인다. finished(실행이 done·handed_off로 끝남)면 제출이 없을 때 '원인 미정'
    결론을 붙인다. 인자 검증에 막힌 조회는 검증 전 purpose·checked를 줄로 내지 않는다"""
    nos = {s["seq"]: i + 1 for i, s in enumerate(recon_tools.lookup_steps(steps, SUBMIT))}
    out = []
    for s in steps:
        if s["kind"] == "model":
            for para in re.split(r"\n\s*\n", s["response"]["text"] or ""):
                if not (para := para.strip()):
                    continue
                m = _PREFIX.match(para)
                kind = _KIND[m.group(1)] if m else "note"
                out.append({"kind": kind, "text": para[m.end() :].strip() if m else para})
        elif s["kind"] == "tool":
            if s["tool"] != SUBMIT and s["rule_id"] != BAD_ARGS:
                for field, kind in (("checked", "compare"), ("purpose", "hypothesis")):
                    if text := (s["args"] or {}).get(field):
                        out.append({"kind": kind, "text": text})
            if s["seq"] in nos:
                out.append(
                    {
                        "kind": "lookup",
                        "tool": s["tool"],
                        "args": s["args"],
                        "lookup_no": nos[s["seq"]],
                        "text": s["result"]["summary"],
                    }
                )
            elif s["tool"] == SUBMIT and s["hook_status"] == "pass" and not s["is_error"]:
                a = s["args"]
                out.append(
                    {
                        "kind": "conclusion",
                        "cause": a["cause"],
                        "runner_up": a["runner_up"],
                        "evidence": a["evidence"],
                        "text": a["reason"],
                    }
                )
            else:
                text = str(s["result"])
                if s["rule_id"] == BAD_ARGS:
                    text = _short_block(text.removeprefix("막힘: "))
                out.append({"kind": "blocked", "tool": s["tool"], "text": text})
    if finished and _submitted(steps) is None:
        out.append(
            {
                "kind": "conclusion",
                "cause": "unknown",
                "runner_up": None,
                "evidence": [],
                "text": "원인 미정",
            }
        )
    return out


class _Live:
    """게이트웨이를 감싸 모델을 부르기 직전마다 아직 안 보낸 조사 줄을 내보낸다 — 앞 턴의 글·조회는 그때 다 저장돼 있다"""

    def __init__(self, inner, store: RunStore, emit):
        self.inner, self.store, self.emit, self.sent = inner, store, emit, 0

    def complete(self, alias, **kw):
        self.flush(kw["ref"][1])
        return self.inner.complete(alias, **kw)

    def flush(self, run_id: int, finished: bool = False) -> None:
        ls = lines(self.store.steps(run_id), finished)
        for line in ls[self.sent :]:
            self.emit(line)
        self.sent = len(ls)


def _close_stuck(store: RunStore, runner: Runner, key: str) -> bool:
    """끊긴 실행(잠금 만료)은 실패로 닫는다(backend/followup.py와 같은 처리). 살아 있는 다른 실행이면 False"""
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


def investigate(conn: sqlite3.Connection, gw, key: str, today: date, on_line=None) -> dict:
    """화면 `조사 시작` — 건 하나를 일반 호출로 끝까지, 줄은 on_line으로. 지금 탐지에 없는 키 ValueError,
    지금 버전으로 끝난 실행이 있으면 AlreadyDone, 다른 곳이 돌리는 중이면 RunLocked. 실패로 끝나면 None"""
    books = recon_match.load(conn, today)
    item = next((i for i in recon_match.detect(books) if i["key"] == key), None)
    if item is None:
        raise ValueError(f"불일치 건이 아님: {key}")
    r = result(conn, key)
    if r and r["status"] != "running":
        raise AlreadyDone(key)
    conn.commit()  # Runner.start가 거절되면 미커밋 쓰기가 되돌아가므로
    reg, names = registry()
    store = RunStore(conn)
    live = _Live(gw, store, on_line or (lambda line: None))
    runner = Runner(spec(names), live, store, reg, rules())
    if not _close_stuck(store, runner, key):
        raise RunLocked(f"{AGENT}/{key} 실행 중")
    out = runner.start(key, recon_tools.input_text(item, books, today))
    live.flush(out.run_id, finished=out.status in ENDED)
    return result(conn, key)


def targets(conn: sqlite3.Connection, today: date, months: list[str] | None = None) -> list[dict]:
    """지금 버전으로 끝난(done·handed_off) 실행이 없는 탐지 건 — 실패한 건은 다시 대상"""
    done = {
        r[0]
        for r in conn.execute(
            "SELECT target FROM ax_agent_runs WHERE agent = ? AND prompt_version = ?"
            " AND status IN ('done', 'handed_off')",
            (AGENT, PROMPT_VERSION),
        )
    }
    items = recon_match.detect(recon_match.load(conn, today))
    return [i for i in items if i["key"] not in done and (not months or i["date"][:7] in months)]


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
    """대상 건 전부를 라운드 배치로 — {status: 건수, "cost_usd"}. 끊기면 다시 돌려 이어 간다"""
    books = recon_match.load(conn, today)
    todo = targets(conn, today, months)
    reg, names = registry()
    store = RunStore(conn)
    runner = Runner(spec(names), gw, store, reg, rules())
    ids = []
    for item in todo:
        row = conn.execute(
            "SELECT id, prompt_version FROM ax_agent_runs WHERE agent = ? AND target = ?"
            " AND status = 'running'",
            (AGENT, item["key"]),
        ).fetchone()
        if row and row["prompt_version"] == PROMPT_VERSION:
            ids.append(row["id"])  # 끊긴 일괄을 이어 간다
            continue
        if row and not _close_stuck(store, runner, item["key"]):
            continue  # 옛 버전을 다른 곳이 아직 돌리는 중
        ids.append(rounds.create(runner, item["key"], recon_tools.input_text(item, books, today)))
    rounds.run_rounds(
        runner,
        gw,
        ids,
        state,
        min_batch=rounds.ROUND_BATCH_MIN if batch else len(ids) + 1,
        poll_seconds=poll_seconds,
    )
    from backend import recon_auto  # recon_auto가 result()를 읽으므로 순환을 피해 여기서

    counts: dict = {"cost_usd": 0.0}
    counts["auto"] = recon_auto.refresh(
        conn, today
    )  # 자동 마감 다시 계산(정책 꺼짐이면 아무것도 안 함)
    conn.commit()
    for rid in ids:
        run = store.run(rid)
        counts[run["status"]] = counts.get(run["status"], 0) + 1
        counts["cost_usd"] += run["cost_usd"] or 0
    return counts


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="python -m backend.recon_agent")
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
