"""야간 실행 `make nightly` (앱 설계 §3b "스케줄러", 설계 docs/superpowers/specs/2026-11-16-nightly-briefing-design.md §3).
고정 순서 단계를 차례로 돌려 모든 화면의 AI 결과를 최신으로 만든다 — 운영 에이전트 착수 전이라 순서는 코드가 정한다.
각 단계는 기존 모듈 함수를 기본(화면) 범위로 부르고, 모듈마다의 캐시(해시·버전·모델이 같으면 다시 부르지 않음)를 그대로 쓴다.
끊기면 다시 돌리면 된다 — 이미 한 건은 캐시가 건너뛴다. 기록은 ax_jobs kind='nightly' 한 행(단계별 detail)."""

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from shared.llm import BudgetExceeded

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
