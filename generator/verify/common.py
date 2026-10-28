"""verify 공용 — 검사 맥락(Ctx)·행 읽기·참조·기준일 검사 (가상 데이터 생성 설계 §5.3 참조·기준일)"""

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

from generator.master import (
    ADJUST_REASONS,
    AGE_BANDS,
    PROCEDURES,
)

ROOT = Path(__file__).resolve().parents[2]


EXAMPLES = 5


CHILD_MAX_AGE = next(hi for name, _, hi, _ in AGE_BANDS if name == "child")


PROCEDURE = {p["code"]: p for p in PROCEDURES}


REASON_BY_TEXT = {
    (r["category"], r["note"]): r for r in ADJUST_REASONS.values()
}  # (adjust_code, adjust_note) → 사유


@dataclass(frozen=True)
class Ctx:
    today: date
    holidays: frozenset
    visits_per_day: int
    range_start: date  # 생성 기간 — truth_gen_meta에서 (검사하는 DB 자신의 값 — verify 테스트 DB는 config와 다름)
    range_end: date
    call_slots: int  # 통화 슬롯·판정 풀·블라인드 풀 크기 — truth_gen_meta에서
    eval_pool: int
    blind_pool: int
    documents_core: int  # RAG 문서 수(핵심·방해) — truth_gen_meta에서
    documents_distractor: int
    link_failure_days: int  # 연동 실패일 수·장부 실수 종류별 건수 — truth_gen_meta에서 (§2.4·§2.7)
    ledger_errors: dict
    rag_qa: Path | None = (
        None  # 이 DB와 짝인 ground_truth/rag_qa.json (make verify만 — verify 테스트 DB는 None)
    )


def load_ctx(config_path: Path, conn, rag_qa: Path | None = None) -> Ctx:
    """설정 + truth_gen_meta(생성기가 남긴 기간·통화 건수). 메타 키가 없으면 KeyError."""
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    holidays = json.loads((ROOT / cfg["holidays"]).read_text(encoding="utf-8"))
    meta = dict(conn.execute("SELECT key, value FROM truth.truth_gen_meta").fetchall())
    return Ctx(
        today=cfg["today"],
        holidays=frozenset(date.fromisoformat(d) for d in holidays),
        visits_per_day=cfg["visits_per_day"],
        range_start=date.fromisoformat(meta["range_start"]),
        range_end=date.fromisoformat(meta["range_end"]),
        call_slots=int(meta["call_slots"]),
        eval_pool=int(meta["eval_pool"]),
        blind_pool=int(meta["blind_pool"]),
        documents_core=int(meta["documents_core"]),
        documents_distractor=int(meta["documents_distractor"]),
        link_failure_days=int(meta["link_failure_days"]),
        ledger_errors=json.loads(meta["ledger_errors"]),
        rag_qa=rag_qa,
    )


def _rows(conn, sql, *params) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params)]


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _end(a: dict) -> str:
    """예약 종료 시각 — 계획을 만든 시각(내원 종료)"""
    return (_dt(a["scheduled_at"]) + timedelta(minutes=a["duration_min"])).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


# ── 참조 ────────────────────────────────────────────────────


def check_foreign_keys(conn, ctx):
    """FK 전부 유효 (스키마에 선언된 emr_*·ax_* 참조)"""
    return [
        {"table": t, "rowid": rowid, "parent": parent}
        for t, rowid, parent, _ in conn.execute("PRAGMA foreign_key_check")
    ]


# ── 기준일 ───────────────────────────────────────────────────


def check_status_from_today(conn, ctx):
    """TODAY 이후 예약은 scheduled/cancelled만"""
    return _rows(
        conn,
        "SELECT id, scheduled_at, status FROM emr_appointments"
        " WHERE date(scheduled_at) >= ? AND status NOT IN ('scheduled', 'cancelled')",
        ctx.today.isoformat(),
    )


def check_no_records_after_today(conn, ctx):
    """TODAY 다음 날부터는 예약만 (treatments·payments·claims 없음)"""
    today = ctx.today.isoformat()
    return (
        _rows(
            conn,
            "SELECT 'emr_treatments' AS tbl, id, performed_at AS at FROM emr_treatments"
            " WHERE date(performed_at) > ?",
            today,
        )
        + _rows(
            conn,
            "SELECT 'emr_payments' AS tbl, id, paid_at AS at FROM emr_payments"
            " WHERE date(paid_at) > ?",
            today,
        )
        + _rows(
            conn,
            "SELECT 'emr_claims' AS tbl, id, service_date AS at FROM emr_claims"
            " WHERE date(service_date) > ?",
            today,
        )
    )


def check_timestamps_not_future(conn, ctx):
    """submitted_at·sent_at·started_at·created_at·cancelled_at·registered_at·birth_date·recorded_at·effective_date ≤ TODAY"""
    today = ctx.today.isoformat()
    return (
        _rows(
            conn,
            "SELECT 'emr_appointments.created_at' AS tbl, id, created_at AS at FROM emr_appointments"
            " WHERE date(created_at) > ?",
            today,
        )
        + _rows(
            conn,
            "SELECT 'emr_appointments.cancelled_at' AS tbl, id, cancelled_at AS at FROM emr_appointments"
            " WHERE cancelled_at IS NOT NULL AND date(cancelled_at) > ?",
            today,
        )
        + _rows(
            conn,
            "SELECT 'emr_patients.registered_at' AS tbl, id, registered_at AS at FROM emr_patients"
            " WHERE date(registered_at) > ?",
            today,
        )
        + _rows(
            conn,
            "SELECT 'emr_patients.birth_date' AS tbl, id, birth_date AS at FROM emr_patients"
            " WHERE date(birth_date) > ?",
            today,
        )
        + _rows(
            conn,
            "SELECT 'emr_claims' AS tbl, id, submitted_at AS at FROM emr_claims"
            " WHERE submitted_at IS NOT NULL AND date(submitted_at) > ?",
            today,
        )
        + _rows(
            conn,
            "SELECT 'emr_messages' AS tbl, id, sent_at AS at FROM emr_messages"
            " WHERE date(sent_at) > ?",
            today,
        )
        + _rows(
            conn,
            "SELECT 'ax_calls' AS tbl, id, started_at AS at FROM ax_calls"
            " WHERE date(started_at) > ?",
            today,
        )
        + _rows(
            conn,
            "SELECT 'emr_patient_alerts' AS tbl, id, recorded_at AS at FROM emr_patient_alerts"
            " WHERE date(recorded_at) > ?",
            today,
        )
        + _rows(
            conn,
            "SELECT 'ax_documents' AS tbl, id, effective_date AS at FROM ax_documents"
            " WHERE effective_date IS NOT NULL AND date(effective_date) > ?",
            today,
        )
    )
