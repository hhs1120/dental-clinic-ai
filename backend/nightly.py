"""야간 실행 `make nightly` (앱 설계 §3b "스케줄러", 설계 docs/superpowers/specs/2026-11-16-nightly-briefing-design.md §3).
고정 순서 단계를 차례로 돌려 모든 화면의 AI 결과를 최신으로 만든다 — 운영 에이전트 착수 전이라 순서는 코드가 정한다.
각 단계는 기존 모듈 함수를 기본(화면) 범위로 부르고, 모듈마다의 캐시(해시·버전·모델이 같으면 다시 부르지 않음)를 그대로 쓴다.
끊기면 다시 돌리면 된다 — 이미 한 건은 캐시가 건너뛴다. 기록은 ax_jobs kind='nightly' 한 행(단계별 detail)."""

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from backend import (
    autoclose,
    calls,
    claim_check,
    claim_evidence,
    claim_omission,
    classify_batch,
    db,
    disagreements,
    expense_agent,
    expense_auto,
    followup,
    judge_queue,
    prep,
    rag_index,
    recon_agent,
    recon_auto,
    report,
)
from backend.embeddings import make_embedder
from backend.harness import policy
from backend.rag_search import DEFAULT_EMBEDDING
from shared.clinic import load_clinic
from shared.llm import BudgetExceeded, load_config

ROOT = Path(__file__).resolve().parents[1]

KIND = "nightly"
# 이보다 오래 running이면 죽은 실행 — Batch 대기·Qwen 판정이 길어 화면 작업(api.JOB_STALE)보다 길게
NIGHTLY_STALE = timedelta(hours=8)
BUDGET_NOTE = "상한으로 중단"


@dataclass
class Ctx:
    conn: sqlite3.Connection
    gw: object
    today: date
    use_batch: bool


@dataclass
class Step:
    name: str
    # → {"new": 새로 만든 건수, "failed": 실패 건수, "note": 한 줄 | None}
    run: Callable[[Ctx], dict]
    plan: Callable[[Ctx], int | None]  # 새로 부를 건수(LLM 없이), 셀 수 없으면 None


def over_budget(gw) -> bool:
    """하루 상한에 닿았는지 — 하네스·calls.add_call이 BudgetExceeded를 안에서 삼키므로 단계 뒤에 직접 본다"""
    check = getattr(gw, "_check_budget", None)  # 가짜 게이트웨이는 상한이 없다
    if check is None:
        return False
    try:
        check()
    except BudgetExceeded:
        return True
    return False


def _job(conn, job_id: int) -> dict:
    r = conn.execute("SELECT * FROM ax_jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(r) | {"detail": json.loads(r["detail"] or "[]")}


def last_run(conn: sqlite3.Connection) -> dict | None:
    """가장 최근 야간 실행 행(detail은 목록) — 오늘 화면 "AI가 한 일"·브리핑 "밤사이"가 읽는다"""
    r = conn.execute(
        "SELECT id FROM ax_jobs WHERE kind = ? ORDER BY id DESC LIMIT 1", (KIND,)
    ).fetchone()
    return _job(conn, r[0]) if r else None


def _write(conn, job_id: int, detail: list[dict], **cols) -> None:
    sets = ", ".join(f"{k} = ?" for k in ("detail", *cols))
    conn.execute(
        f"UPDATE ax_jobs SET {sets} WHERE id = ?",
        (json.dumps(detail, ensure_ascii=False), *cols.values(), job_id),
    )
    conn.commit()


def run(ctx: Ctx, steps: list[Step], clock=datetime.now) -> dict | None:
    """단계를 차례로 — 살아 있는 야간 실행이 있으면 None. 단계 예외는 그 단계에 적고 다음 단계로,
    하루 상한에 닿으면 남은 단계를 건너뛰고 failed"""
    conn, now = ctx.conn, clock()
    since = (now - NIGHTLY_STALE).isoformat(timespec="seconds")
    if conn.execute(
        "SELECT 1 FROM ax_jobs WHERE kind = ? AND status = 'running' AND started_at > ?",
        (KIND, since),
    ).fetchone():
        return None
    job_id = conn.execute(
        "INSERT INTO ax_jobs (kind, status, total, started_at, detail)"
        " VALUES (?, 'running', ?, ?, '[]')",
        (KIND, len(steps), now.isoformat(timespec="seconds")),
    ).lastrowid
    conn.commit()
    detail, status = [], "done"
    for i, step in enumerate(steps):
        try:
            got = step.run(ctx)
        except KeyboardInterrupt:
            raise
        except BaseException as e:  # noqa: BLE001 — SystemExit(판정기 모델 다름)도 그 단계 실패로
            conn.rollback()
            got = {"new": 0, "failed": 1, "note": f"{type(e).__name__}: {e}"}
        detail.append({"step": step.name} | got)
        _write(conn, job_id, detail, done=i + 1)
        if i + 1 < len(steps) and over_budget(ctx.gw):
            detail.append({"step": steps[i + 1].name, "new": 0, "failed": 0, "note": BUDGET_NOTE})
            status = "failed"
            break
    _write(conn, job_id, detail, status=status, finished_at=clock().isoformat(timespec="seconds"))
    return _job(conn, job_id)


def plan(ctx: Ctx, steps: list[Step]) -> list[tuple[str, int | None]]:
    """단계별 새로 부를 건수 — LLM을 부르지 않고 ax_jobs에 쓰지 않는다(make nightly PLAN=1)"""
    return [(s.name, s.plan(ctx)) for s in steps]


# ── 단계 (설계 §3 표 — 각 모듈 main이 하는 일을 기본 범위로) ──
SOURCE = ROOT / "data" / "source.sqlite"
AFTER_PREVIOUS = (
    "judge",
    "disagreements",
    "autoclose",
    "followup",
)  # PLAN 수가 앞 단계 뒤 늘 수 있음


def _ok(n: int = 0, failed: int = 0, note: str | None = None) -> dict:
    return {"new": n, "failed": failed, "note": note}


def _pasted_todo(conn, version: str) -> list[str]:
    """활성 분류 ok가 없는 붙여넣은 통화 — 추가 때 분류가 상한·오류로 실패한 것(calls.add_call)"""
    return [
        r[0]
        for r in conn.execute(
            "SELECT call_uid FROM ax_pasted_calls WHERE call_uid NOT IN"
            " (SELECT call_uid FROM ax_call_classifications WHERE model_alias = 'classify'"
            "  AND prompt_version = ? AND status = 'ok') ORDER BY call_uid",
            (version,),
        )
    ]


def _source(ctx: Ctx) -> dict:
    changed = policy.check_source(ctx.conn, SOURCE)
    ctx.conn.commit()
    return _ok(note="원본이 바뀌어 자동 처리 정책 끔" if changed else None)


def _classify(ctx: Ctx) -> dict:
    a, _ = calls.active_versions(ctx.conn)
    out = classify_batch.run(ctx.conn, ctx.gw, "classify", a, use_batch=ctx.use_batch)
    n, failed = out["done"], out["failed"]
    for uid in _pasted_todo(ctx.conn, a):
        calls.classify_one(ctx.conn, ctx.gw, uid)
        n += 1
    return _ok(n, failed)


def _classify_plan(ctx: Ctx) -> int:
    a, _ = calls.active_versions(ctx.conn)
    return len(classify_batch.targets(ctx.conn, "classify", a, None)) + len(
        _pasted_todo(ctx.conn, a)
    )


def _judge(ctx: Ctx) -> dict:
    _, b = calls.active_versions(ctx.conn)
    if not b:
        return _ok(note="판정기 버전 없음")
    out = classify_batch.run(ctx.conn, ctx.gw, "judge", b, use_batch=ctx.use_batch)
    return _ok(out["done"], out["failed"])


def _judge_plan(ctx: Ctx) -> int | None:
    _, b = calls.active_versions(ctx.conn)
    return len(classify_batch.targets(ctx.conn, "judge", b, None)) if b else None


def _disagreements(ctx: Ctx) -> dict:
    a, b = calls.active_versions(ctx.conn)
    if not b:
        return _ok(note="판정기 버전 없음")
    n = disagreements.compute(ctx.conn, a, b)
    judge_queue.refresh_if_active(ctx.conn, a, b)
    judge_queue.prune(ctx.conn, a, b)
    ctx.conn.commit()
    return _ok(n)


def _autoclose(ctx: Ctx) -> dict:
    out = autoclose.run(ctx.conn)
    return _ok(note="정책 꺼짐") if out.get("off") else _ok(out["closed"])


def _followup(ctx: Ctx) -> dict:
    clinic = load_clinic()
    searcher = followup.app_searcher(ctx.conn)
    n = failed = 0
    for uid in followup.targets(ctx.conn, ctx.today):
        try:
            r = followup.run_for_call(ctx.conn, ctx.gw, uid, clinic, searcher)
        except Exception:  # noqa: BLE001 — 한 통화 때문에 나머지가 멈추지 않게(make followups와 같다)
            ctx.conn.rollback()
            failed += 1
            continue
        if r:
            n += r["status"] == "done"
            failed += r["status"] == "failed"
    return _ok(n, failed)


def _has_index(conn) -> bool:
    return conn.execute("SELECT 1 FROM ax_chunks LIMIT 1").fetchone() is not None


def _index(ctx: Ctx) -> dict:
    if not _has_index(ctx.conn):
        return _ok(note="건너뜀: 색인 없음")
    return _ok(rag_index.build_index(ctx.conn, make_embedder(DEFAULT_EMBEDDING)))


def _prep(ctx: Ctx) -> dict:
    out = prep.build(
        ctx.conn, ctx.gw, prep.tab_days(ctx.today), ctx.today, use_batch=ctx.use_batch
    )
    return _ok(out["called"], out["failed"])


def _prep_plan(ctx: Ctx) -> int:
    return prep.todo_count(ctx.conn, prep.tab_days(ctx.today), ctx.today, _model(ctx, prep.ALIAS))


def _claim(mod) -> Step:
    def run(ctx: Ctx) -> dict:
        rows = mod.targets(ctx.conn, ctx.today, scope="app")
        out = mod.build(ctx.conn, ctx.gw, rows, use_batch=ctx.use_batch)
        return _ok(out.get("called", 0), out.get("failed", 0))

    return Step(
        mod.__name__.rsplit(".", 1)[1], run, lambda ctx: len(mod.todo_ids(ctx.conn, ctx.today))
    )


def _agent(name: str, mod, auto) -> Step:
    def run(ctx: Ctx) -> dict:
        if not mod.targets(
            ctx.conn, ctx.today
        ):  # 조사할 것이 없어도 자동 처리는 다시 계산(make recon과 같다)
            auto.refresh(ctx.conn, ctx.today)
            ctx.conn.commit()
            return _ok()
        out = mod.run_batch(ctx.conn, ctx.gw, ctx.today, None, batch=ctx.use_batch)
        return _ok(out.get("done", 0) + out.get("handed_off", 0), out.get("failed", 0))

    return Step(name, run, lambda ctx: len(mod.targets(ctx.conn, ctx.today)))


def _report(ctx: Ctx) -> dict:
    out = report.build(ctx.conn, ctx.gw, ctx.today, use_batch=ctx.use_batch)
    return _ok(out["called"], out["failed"])


def _model(ctx: Ctx, alias: str) -> str:
    cfg = getattr(ctx.gw, "config", None) or load_config()
    return cfg["aliases"][alias][0]


def _none(ctx: Ctx) -> None:
    return None


STEPS = [
    Step("source", _source, _none),
    Step("classify", _classify, _classify_plan),
    Step("judge", _judge, _judge_plan),
    Step("disagreements", _disagreements, _none),
    Step("autoclose", _autoclose, _none),
    Step("followup", _followup, lambda ctx: len(followup.targets(ctx.conn, ctx.today))),
    Step("index", _index, lambda ctx: None if _has_index(ctx.conn) else 0),
    Step("prep", _prep, _prep_plan),
    _claim(claim_check),
    _claim(claim_omission),
    _claim(claim_evidence),
    _agent("recon", recon_agent, recon_auto),
    _agent("expense", expense_agent, expense_auto),
    Step(
        "report",
        _report,
        lambda ctx: report.todo_count(ctx.conn, ctx.today, _model(ctx, report.ALIAS)),
    ),
]


def main(argv=None) -> None:
    import argparse
    import time

    from backend.harness.llm import make_gateway

    p = argparse.ArgumentParser(prog="python -m backend.nightly")
    p.add_argument("command", choices=("run",))
    p.add_argument("--plan", action="store_true", help="LLM 없이 단계별 새로 부를 건수만")
    p.add_argument("--no-batch", action="store_true", help="Batch 없이 일반 호출만")
    args = p.parse_args(argv)
    conn = db.connect(ROOT / "data" / "app.sqlite", SOURCE)
    db.init_schema(conn)
    ctx = Ctx(conn, make_gateway(conn), load_clinic().today, use_batch=not args.no_batch)
    if args.plan:
        for name, n in plan(ctx, STEPS):
            tail = " (앞 단계 뒤 늘 수 있음)" if name in AFTER_PREVIOUS else ""
            print(f"{name}: {'—' if n is None else f'{n}건'}{tail}")
        print("Batch는 하루 상한 밖 — 이 건수로 확인하고 돌린다")
        return
    start = datetime.now().isoformat(timespec="seconds")
    timed = []
    for s in STEPS:

        def run_timed(c, s=s):
            t = time.monotonic()
            got = s.run(c)
            print(
                f"{s.name}: 새로 {got['new']} · 실패 {got['failed']} · {time.monotonic() - t:.0f}초"
                + (f" · {got['note']}" if got["note"] else "")
            )
            return got

        timed.append(Step(s.name, run_timed, s.plan))
    job = run(ctx, timed)
    if job is None:
        print("야간 실행이 이미 돌고 있다 — 끝나거나 오래되면 다시")
        return
    cost = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) FROM ax_llm_calls WHERE created_at >= ?", (start,)
    ).fetchone()[0]
    print(f"{job['status']} · {job['finished_at']} 끝남 · 비용 ${cost:.4f}")


if __name__ == "__main__":
    main()
