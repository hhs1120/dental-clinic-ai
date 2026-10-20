"""make verify — 생성 데이터 검사. 검사 목록은 docs/치과 AX - 가상 데이터 생성 설계.md §5.3.
필수 검사 실패 시 exit 1, 경고는 리포트만.

구현: 정형(emr_*)·ax_calls·truth_claims(있을 때) 필수 검사 + 분포·치아 경고.
§5.3 중 아직 없는 것(해당 단계 구현 때 추가):
  필수 — 다형 참조(ax_llm_calls.ref_*·ax_chunks.source_*), effective_date ≤ TODAY, 예약류 통화 시각 == 이벤트 시각,
         누출(풀 마킹 카테고리 추론·분류기 입력 덤프), RAG 전부, 결정성 2회 비교(canonical_hash만 있음)
  경고 — 목적·노이즈 분포, 문자, 통화, RAG
검사 함수는 (conn, ctx) → 위반 행 목록(dict). 빈 목록이면 통과.
conn은 app.sqlite이고 truth.sqlite가 `truth` 스키마로 ATTACH되어 있다.
"""
import argparse
import calendar
import hashlib
import json
import re
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

import yaml

from generator.master import (ACTIVE_MONTHS, ACTIVE_SHARE, AGE_BANDS, CONDITIONAL_INSURED_MIN_AGE, FULL_MOUTH, HOURS,
                              MAX_APPOINTMENTS_PER_SLOT, NEW_PATIENT_SHARE, PEAK_HOURS, SATURDAY,
                              SATURDAY_VISIT_RATIO, SCALING_CODE, SCALING_MIN_AGE, SLOT_MIN, SUBMITTED_MONTHS,
                              VISIT_MIX, open_slots)

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = 5

# 영업시간 (§1) — generator/master.py의 HOURS에서
OPEN = time.fromisoformat(HOURS["open"])
WEEKDAY_CLOSE = time.fromisoformat(HOURS["weekday_close"])
SATURDAY_CLOSE = time.fromisoformat(HOURS["saturday_close"])
LUNCH = tuple(time.fromisoformat(t) for t in HOURS["lunch"])
CLOSED_WEEKDAYS = set(HOURS["closed_weekdays"])
SLOT = timedelta(minutes=SLOT_MIN)
PEAK = [tuple(time.fromisoformat(t) for t in w) for w in PEAK_HOURS]
CHILD_MAX_AGE = next(hi for name, _, hi, _ in AGE_BANDS if name == "child")

# 경고 허용치 (§5.3 경고 표)
SHARE_TOLERANCE = 0.02  # 연령 비중·노쇼·당일 취소 ±2%p
ACTIVE_TOLERANCE = 0.05
NEW_PATIENT_TOLERANCE = 0.03
VISIT_MIX_TOLERANCE = 0.03
NEW_PATIENT_MIN_VISITS = 50  # 그달 completed가 이보다 적으면 신환 비중을 보지 않음
DAILY_MIN_FILL = 0.70  # TODAY 이전 진료일의 비취소 예약 ≥ 상한 × 이 비율


@dataclass(frozen=True)
class Ctx:
    today: date
    holidays: frozenset
    reschedule_rate: float
    noshow_rate: float
    sameday_cancel_rate: float
    confirm_rate: float
    unpaid_rate: float
    visits_per_day: int
    range_start: date  # 생성 기간 — truth_gen_meta에서 (규모별 시작일)
    range_end: date


def load_ctx(config_path: Path, conn) -> Ctx:
    """설정 + truth_gen_meta(생성기가 남긴 규모별 기간). 메타 키가 없으면 KeyError."""
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    holidays = json.loads((ROOT / cfg["holidays"]).read_text(encoding="utf-8"))
    meta = dict(conn.execute("SELECT key, value FROM truth.truth_gen_meta").fetchall())
    return Ctx(
        today=cfg["today"],
        holidays=frozenset(date.fromisoformat(d) for d in holidays),
        reschedule_rate=cfg["reschedule_rate"],
        noshow_rate=cfg["noshow_rate"],
        sameday_cancel_rate=cfg["sameday_cancel_rate"],
        confirm_rate=cfg["confirm_rate"],
        unpaid_rate=cfg["unpaid_rate"],
        visits_per_day=cfg["visits_per_day"],
        range_start=date.fromisoformat(meta["range_start"]),
        range_end=date.fromisoformat(meta["range_end"]),
    )


def _rows(conn, sql, *params) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params)]


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _active_appointments(conn) -> list[dict]:
    """취소되지 않은 예약 — 그 시간대를 실제로 차지한 것 (노쇼 포함)."""
    rows = _rows(conn, "SELECT id, scheduled_at, duration_min"
                       " FROM emr_appointments WHERE status != 'cancelled'")
    for r in rows:
        r["start"] = _dt(r["scheduled_at"])
        r["end"] = r["start"] + timedelta(minutes=r["duration_min"])
    return rows


# ── 참조 ────────────────────────────────────────────────────

def check_foreign_keys(conn, ctx):
    """FK 전부 유효 (스키마에 선언된 emr_*·ax_* 참조)"""
    return [{"table": t, "rowid": rowid, "parent": parent}
            for t, rowid, parent, _ in conn.execute("PRAGMA foreign_key_check")]


# ── 기준일 ──────────────────────────────────────────────────

def check_status_from_today(conn, ctx):
    """TODAY 이후 예약은 scheduled/cancelled만"""
    return _rows(conn, "SELECT id, scheduled_at, status FROM emr_appointments"
                       " WHERE date(scheduled_at) >= ? AND status NOT IN ('scheduled', 'cancelled')",
                 ctx.today.isoformat())


def check_no_records_after_today(conn, ctx):
    """TODAY 다음 날부터는 예약만 (treatments·payments·claims 없음)"""
    today = ctx.today.isoformat()
    return (
        _rows(conn, "SELECT 'emr_treatments' AS tbl, id, performed_at AS at FROM emr_treatments"
                    " WHERE date(performed_at) > ?", today)
        + _rows(conn, "SELECT 'emr_payments' AS tbl, id, paid_at AS at FROM emr_payments"
                      " WHERE date(paid_at) > ?", today)
        + _rows(conn, "SELECT 'emr_claims' AS tbl, id, service_date AS at FROM emr_claims"
                      " WHERE date(service_date) > ?", today)
    )


def check_timestamps_not_future(conn, ctx):
    """submitted_at·sent_at·started_at·created_at·cancelled_at·registered_at·birth_date ≤ TODAY"""
    today = ctx.today.isoformat()
    return (
        _rows(conn, "SELECT 'emr_appointments.created_at' AS tbl, id, created_at AS at FROM emr_appointments"
                    " WHERE date(created_at) > ?", today)
        + _rows(conn, "SELECT 'emr_appointments.cancelled_at' AS tbl, id, cancelled_at AS at FROM emr_appointments"
                      " WHERE cancelled_at IS NOT NULL AND date(cancelled_at) > ?", today)
        + _rows(conn, "SELECT 'emr_patients.registered_at' AS tbl, id, registered_at AS at FROM emr_patients"
                      " WHERE date(registered_at) > ?", today)
        + _rows(conn, "SELECT 'emr_patients.birth_date' AS tbl, id, birth_date AS at FROM emr_patients"
                      " WHERE date(birth_date) > ?", today)
        + _rows(conn, "SELECT 'emr_claims' AS tbl, id, submitted_at AS at FROM emr_claims"
                    " WHERE submitted_at IS NOT NULL AND date(submitted_at) > ?", today)
        + _rows(conn, "SELECT 'emr_messages' AS tbl, id, sent_at AS at FROM emr_messages"
                      " WHERE date(sent_at) > ?", today)
        + _rows(conn, "SELECT 'ax_calls' AS tbl, id, started_at AS at FROM ax_calls"
                      " WHERE date(started_at) > ?", today)
    )


def check_last_month_claims_pending(conn, ctx):
    """TODAY 전월(11월) 이후 청구는 pending"""
    last_month = (ctx.today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    return _rows(conn, "SELECT id, claim_month, status, submitted_at FROM emr_claims"
                       " WHERE claim_month >= ? AND status != 'pending'", last_month)


def _submitted_months(ctx) -> list[str]:
    """전월(pending) 이전 SUBMITTED_MONTHS개월 — 9~10월"""
    months, first = [], ctx.today.replace(day=1)
    for _ in range(SUBMITTED_MONTHS + 1):
        first = (first - timedelta(days=1)).replace(day=1)
        months.append(first.strftime("%Y-%m"))
    return months[1:]


def check_submitted_months_no_result(conn, ctx):
    """전월 이전 SUBMITTED_MONTHS개월(9~10월) 청구는 submitted, 결과(paid_amount·adjust_code) 없음 — 정답 누출 방지"""
    months = _submitted_months(ctx)
    marks = ",".join("?" * len(months))
    return _rows(conn, "SELECT id, claim_month, status, paid_amount, adjust_code FROM emr_claims"
                       f" WHERE claim_month IN ({marks}) AND (status != 'submitted'"
                       " OR paid_amount IS NOT NULL OR adjust_code IS NOT NULL)", *months)


# ── 용량·충돌 ───────────────────────────────────────────────

def _daily_cap(ctx, d: date) -> float:
    return ctx.visits_per_day * (SATURDAY_VISIT_RATIO if d.weekday() == SATURDAY else 1)


def check_daily_cap(conn, ctx):
    """하루 비취소 예약 ≤ 상한(평일 visits_per_day, 토 ×0.5)"""
    return [{"date": r["d"], "appointments": r["n"], "cap": _daily_cap(ctx, date.fromisoformat(r["d"]))}
            for r in _rows(conn, "SELECT date(scheduled_at) AS d, count(*) AS n FROM emr_appointments"
                                 " WHERE status != 'cancelled' GROUP BY d")
            if r["n"] > _daily_cap(ctx, date.fromisoformat(r["d"]))]


def check_slot_cap(conn, ctx):
    """같은 시작 시각의 비취소 예약 ≤ MAX_APPOINTMENTS_PER_SLOT"""
    return _rows(conn, "SELECT scheduled_at AS at, count(*) AS n FROM emr_appointments WHERE status != 'cancelled'"
                       " GROUP BY scheduled_at HAVING count(*) > ?", MAX_APPOINTMENTS_PER_SLOT)


def check_slot_grid(conn, ctx):
    """예약 시작은 SLOT_MIN분 격자(:00·:30), 소요는 SLOT_MIN의 배수 — 시각대 상한 검사의 전제"""
    bad = []
    for r in _rows(conn, "SELECT id, scheduled_at, duration_min FROM emr_appointments"):
        start = _dt(r["scheduled_at"])
        if start.minute % SLOT_MIN or start.second or r["duration_min"] % SLOT_MIN:
            bad.append(r)
    return bad


def check_patient_same_day(conn, ctx):
    """같은 환자 같은 날 예약 중복 없음 (취소 제외)"""
    return _rows(conn, "SELECT patient_id, date(scheduled_at) AS day, group_concat(id) AS ids"
                       " FROM emr_appointments WHERE status != 'cancelled'"
                       " GROUP BY patient_id, day HAVING count(*) > 1")


def check_business_hours(conn, ctx):
    """영업시간·휴진일 밖 예약 없음 (취소 포함 — 잡을 때 이미 영업시간이어야 함)"""
    bad = []
    for r in _rows(conn, "SELECT id, scheduled_at, duration_min FROM emr_appointments"):
        start = _dt(r["scheduled_at"])
        end = start + timedelta(minutes=r["duration_min"])
        reason = _hours_violation(start, end, ctx.holidays)
        if reason:
            bad.append({**r, "reason": reason})
    return bad


def _hours_violation(start: datetime, end: datetime, holidays) -> str | None:
    d = start.date()
    if d.weekday() in CLOSED_WEEKDAYS:
        return "휴진 요일"
    if d in holidays:
        return "공휴일"
    if end.date() != d:
        return "자정 넘김"
    close = SATURDAY_CLOSE if d.weekday() == SATURDAY else WEEKDAY_CLOSE
    if start.time() < OPEN or end.time() > close:
        return "영업시간 밖"
    if d.weekday() != SATURDAY and start.time() < LUNCH[1] and end.time() > LUNCH[0]:
        return "점심시간"
    return None


# ── 예약 ────────────────────────────────────────────────────

def check_planned_procedure(conn, ctx):
    """전 예약에 planned_procedure_code"""
    return _rows(conn, "SELECT id, scheduled_at, type FROM emr_appointments"
                       " WHERE planned_procedure_code IS NULL")


def _prev_open_day(d: date, holidays) -> date:
    """d 직전의 진료일 (휴진 요일·공휴일 건너뜀)"""
    d -= timedelta(days=1)
    while not open_slots(d.weekday()) or d in holidays:
        d -= timedelta(days=1)
    return d


def check_confirmed_not_future(conn, ctx):
    """confirmed는 직전 진료일 리마인드 응답 — 직전 진료일이 TODAY 이후인 예약은 0"""
    return [r for r in _rows(conn, "SELECT id, scheduled_at, confirmed FROM emr_appointments WHERE confirmed = 1")
            if _prev_open_day(_dt(r["scheduled_at"]).date(), ctx.holidays) > ctx.today]


def check_created_before_scheduled(conn, ctx):
    """예약은 잡은 뒤에 열린다 — created_at < scheduled_at"""
    return _rows(conn, "SELECT id, created_at, scheduled_at FROM emr_appointments"
                       " WHERE datetime(created_at) >= datetime(scheduled_at)")


def check_cancel_time(conn, ctx):
    """취소 시각은 예약을 잡은 뒤, 예약 시각 전 — created_at ≤ cancelled_at < scheduled_at"""
    return _rows(conn, "SELECT id, created_at, cancelled_at, scheduled_at FROM emr_appointments"
                       " WHERE cancelled_at IS NOT NULL AND (datetime(cancelled_at) < datetime(created_at)"
                       " OR datetime(cancelled_at) >= datetime(scheduled_at))")


def check_cancelled_at_status(conn, ctx):
    """cancelled_at은 cancelled 예약에만, cancelled 예약은 cancelled_at 필수"""
    return _rows(conn, "SELECT id, status, cancelled_at FROM emr_appointments"
                       " WHERE (status = 'cancelled') != (cancelled_at IS NOT NULL)")


def _future_new_patients(conn, ctx) -> list[int]:
    """예정 신환 — 창 안에서 등록했고(registered_at ≥ range_start) 비취소 예약이 전부 TODAY 이후인 환자.
    구환은 리콜·치료로 미래 예약만 있어도 정상이라 제외"""
    return [r["patient_id"] for r in _rows(
        conn, "SELECT a.patient_id FROM emr_appointments a JOIN emr_patients p ON p.id = a.patient_id"
              " WHERE a.status != 'cancelled' AND date(p.registered_at) >= ?"
              " GROUP BY a.patient_id HAVING min(date(a.scheduled_at)) >= ?",
        ctx.range_start.isoformat(), ctx.today.isoformat())]


def check_future_new_patient_single(conn, ctx):
    """예정 신환은 비취소 예약 1건뿐 (코스 다음 내원은 직전 내원 completed 뒤에야 잡힌다 — §2.6 ③)"""
    ids = _future_new_patients(conn, ctx)
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    return _rows(conn, "SELECT patient_id, count(*) AS appointments FROM emr_appointments"
                       f" WHERE status != 'cancelled' AND patient_id IN ({marks})"
                       " GROUP BY patient_id HAVING count(*) > 1", *ids)


def check_future_new_patient_registered(conn, ctx):
    """예정 신환의 registered_at = 첫 예약 created_at의 날짜 (전화 예약 때 차트 생성)"""
    ids = _future_new_patients(conn, ctx)
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    return _rows(conn, "SELECT p.id AS patient_id, p.registered_at, min(date(a.created_at)) AS first_booked"
                       " FROM emr_patients p JOIN emr_appointments a ON a.patient_id = p.id"
                       f" WHERE p.id IN ({marks}) GROUP BY p.id"
                       " HAVING date(p.registered_at) IS NOT min(date(a.created_at))", *ids)


def check_reschedule_pair(conn, ctx):
    """변경 쌍: 새 예약은 원래 날짜 뒤 1~14일, 같은 유형·예정 시술, 새 created_at = 원래 cancelled_at"""
    return _rows(conn, "SELECT n.id, n.rescheduled_from_id, o.scheduled_at AS original_at, n.scheduled_at,"
                       " o.cancelled_at, n.created_at FROM emr_appointments n"
                       " JOIN emr_appointments o ON o.id = n.rescheduled_from_id"
                       " WHERE julianday(date(n.scheduled_at)) - julianday(date(o.scheduled_at)) NOT BETWEEN 1 AND 14"
                       " OR n.type IS NOT o.type OR n.planned_procedure_code IS NOT o.planned_procedure_code"
                       " OR n.created_at IS NOT o.cancelled_at")


def check_reschedule_once(conn, ctx):
    """변경은 예약당 최대 1회 — 원래 예약은 새 예약 하나만, 변경으로 생긴 새 예약은 다시 변경되지 않음"""
    return (_rows(conn, "SELECT rescheduled_from_id AS original_id, count(*) AS n FROM emr_appointments"
                        " WHERE rescheduled_from_id IS NOT NULL GROUP BY rescheduled_from_id HAVING count(*) > 1")
            + _rows(conn, "SELECT m.id AS appointment_id FROM emr_appointments m"
                          " JOIN emr_appointments n ON n.rescheduled_from_id = m.id WHERE m.rescheduled_from_id IS NOT NULL"))


def check_cancellation_kind(conn, ctx):
    """취소는 변경 쌍의 원래 예약 또는 당일 취소(TODAY 이전 날짜)뿐"""
    return _rows(conn, "SELECT a.id, a.scheduled_at, a.cancelled_at FROM emr_appointments a"
                       " WHERE a.status = 'cancelled'"
                       " AND NOT EXISTS (SELECT 1 FROM emr_appointments n WHERE n.rescheduled_from_id = a.id)"
                       " AND NOT (date(a.cancelled_at) = date(a.scheduled_at) AND date(a.scheduled_at) < ?)",
                 ctx.today.isoformat())


# ── 시술·청구 ───────────────────────────────────────────────

def check_performed_at(conn, ctx):
    """performed_at == 예약 일시"""
    return _rows(conn, "SELECT t.id, t.appointment_id, t.performed_at, a.scheduled_at"
                       " FROM emr_treatments t JOIN emr_appointments a ON a.id = t.appointment_id"
                       " WHERE datetime(t.performed_at) IS NOT datetime(a.scheduled_at)")


def check_treatment_on_completed(conn, ctx):
    """진료 기록은 completed 예약에만"""
    return _rows(conn, "SELECT t.id, t.appointment_id, a.status FROM emr_treatments t"
                       " LEFT JOIN emr_appointments a ON a.id = t.appointment_id"
                       " WHERE a.status IS NOT 'completed'")


def check_completed_one_treatment(conn, ctx):
    """completed 예약은 진료 기록 정확히 1행"""
    return _rows(conn, "SELECT a.id, a.scheduled_at, count(t.id) AS treatments FROM emr_appointments a"
                       " LEFT JOIN emr_treatments t ON t.appointment_id = a.id"
                       " WHERE a.status = 'completed' GROUP BY a.id HAVING count(t.id) != 1")


def check_service_date(conn, ctx):
    """service_date == 진료일"""
    return _rows(conn, "SELECT c.id, c.treatment_id, c.service_date, t.performed_at"
                       " FROM emr_claims c JOIN emr_treatments t ON t.id = c.treatment_id"
                       " WHERE date(c.service_date) IS NOT date(t.performed_at)")


def check_uninsured_not_claimed(conn, ctx):
    """비급여 시술은 claims에 없음 (conditional은 시술일 65세 미만이면 비급여)"""
    rows = _rows(conn, "SELECT c.id, c.chart_no, c.service_date, c.procedure_code, p.name, p.is_insured, pt.birth_date"
                       " FROM emr_claims c JOIN emr_procedures p ON p.code = c.procedure_code"
                       " JOIN emr_patients pt ON pt.id = c.patient_id WHERE p.is_insured IN ('no', 'conditional')")
    return [r for r in rows if r["is_insured"] == "no"
            or _age(date.fromisoformat(r["birth_date"]), date.fromisoformat(r["service_date"])) < CONDITIONAL_INSURED_MIN_AGE]


def check_scaling_limit(conn, ctx):
    """스케일링 급여: 19세↑, 환자당 연 insured_limit회"""
    rows = _rows(conn, "SELECT c.id, c.patient_id, c.chart_no, c.service_date, c.procedure_code,"
                       " p.insured_limit, pt.birth_date"
                       " FROM emr_claims c JOIN emr_procedures p ON p.code = c.procedure_code"
                       " JOIN emr_patients pt ON pt.id = c.patient_id"
                       " WHERE c.procedure_code = ? ORDER BY c.service_date, c.id", SCALING_CODE)
    bad, count = [], defaultdict(int)
    for r in rows:
        served = date.fromisoformat(r["service_date"])
        if _age(date.fromisoformat(r["birth_date"]), served) < SCALING_MIN_AGE:
            bad.append({**r, "reason": f"{SCALING_MIN_AGE}세 미만"})
        key = (r["patient_id"], r["procedure_code"], served.year)
        count[key] += 1
        if count[key] > r["insured_limit"]:
            bad.append({**r, "reason": f"연 {r['insured_limit']}회 초과"})
    return bad


def _age(birth: date, on: date) -> int:
    return on.year - birth.year - ((on.month, on.day) < (birth.month, birth.day))


def check_tooth_no_not_null(conn, ctx):
    """tooth_no NULL 없음 (치아 단위가 아닌 시술은 '00')"""
    return (_rows(conn, "SELECT 'emr_treatments' AS tbl, id FROM emr_treatments WHERE tooth_no IS NULL")
            + _rows(conn, "SELECT 'emr_claims' AS tbl, id FROM emr_claims WHERE tooth_no IS NULL"))


def check_claim_key_unique(conn, ctx):
    """대조 키 (chart_no, service_date, procedure_code, tooth_no) UNIQUE"""
    return _rows(conn, "SELECT chart_no, service_date, procedure_code, tooth_no, group_concat(id) AS ids"
                       " FROM emr_claims GROUP BY chart_no, service_date, procedure_code, tooth_no"
                       " HAVING count(*) > 1")


def check_extra_eob_not_in_ledger(conn, ctx):
    """truth_claims의 extra_in_eob 대조 키가 emr_claims와 겹치지 않음"""
    return _rows(conn, "SELECT t.id, t.chart_no, t.service_date, t.procedure_code, t.tooth_no, c.id AS claim_id"
                       " FROM truth.truth_claims t JOIN emr_claims c USING (chart_no, service_date, procedure_code, tooth_no)"
                       " WHERE t.outcome = 'extra_in_eob'")


def check_truth_claims_match(conn, ctx):
    """9~10월 emr_claims마다 truth_claims 1행(claim_id·대조 키·금액 일치), 그 밖을 가리키는 truth 행 없음,
    extra_in_eob 줄도 9~10월 진료일. truth_claims는 EOB 슬롯 착수 때 생성 — 비어 있으면 건너뜀"""
    if not conn.execute("SELECT 1 FROM truth.truth_claims LIMIT 1").fetchone():
        return []
    months = _submitted_months(ctx)
    marks = ",".join("?" * len(months))
    unmatched = _rows(conn, "SELECT c.id AS claim_id, c.claim_month, t.id AS truth_id FROM emr_claims c"
                            " LEFT JOIN truth.truth_claims t ON t.claim_id = c.id"
                            f" WHERE c.claim_month IN ({marks}) AND (t.id IS NULL"
                            " OR t.chart_no IS NOT c.chart_no OR t.service_date IS NOT c.service_date"
                            " OR t.procedure_code IS NOT c.procedure_code OR t.tooth_no IS NOT c.tooth_no"
                            " OR t.claim_amount IS NOT c.claim_amount)", *months)
    stray = _rows(conn, "SELECT t.id AS truth_id, t.claim_id FROM truth.truth_claims t"
                        f" LEFT JOIN emr_claims c ON c.id = t.claim_id AND c.claim_month IN ({marks})"
                        " WHERE t.outcome != 'extra_in_eob' AND c.id IS NULL", *months)
    extra_outside = _rows(conn, "SELECT id AS truth_id, service_date FROM truth.truth_claims"
                                f" WHERE outcome = 'extra_in_eob' AND substr(service_date, 1, 7) NOT IN ({marks})",
                          *months)
    return unmatched + stray + extra_outside


# ── 수납 ────────────────────────────────────────────────────

def check_balance_non_negative(conn, ctx):
    """환자별 시점 누적 잔액 ≥ 0"""
    bad, balance = [], defaultdict(int)
    for r in _rows(conn, "SELECT id, patient_id, kind, amount_due, amount_paid, paid_at"
                         " FROM emr_payments ORDER BY patient_id, datetime(paid_at), id"):
        balance[r["patient_id"]] += r["amount_due"] - r["amount_paid"]
        if balance[r["patient_id"]] < 0:
            bad.append({**r, "balance": balance[r["patient_id"]]})
    return bad


def check_visit_payment_completed(conn, ctx):
    """visit 수납 행은 completed 예약에만"""
    return _rows(conn, "SELECT p.id, p.patient_id, p.appointment_id, a.status FROM emr_payments p"
                       " LEFT JOIN emr_appointments a ON a.id = p.appointment_id"
                       " WHERE p.kind = 'visit' AND a.status IS NOT 'completed'")


# ── 누출 ────────────────────────────────────────────────────

# shared/schema.sql의 ax_calls 컬럼(의미는 가상 데이터 생성 설계 §3). 이 밖의 컬럼(라벨·노이즈·카테고리 등)은 정답 누출
AX_CALLS_COLUMNS = {
    "id", "call_uid", "source", "patient_id", "caller_phone", "caller_name", "direction", "started_at",
    "duration_sec", "has_transcript", "audio_path", "transcript", "in_eval_pool", "eval_sort_order",
    "judge_status", "in_blind_pool", "blind_sort_order", "blind_status", "indexed_at",
}
CALL_UID = re.compile(r"c-\d{4,}")


def check_ax_calls_columns(conn, ctx):
    """ax_calls에 정해진 컬럼 밖(라벨·노이즈·카테고리) 없음"""
    return [{"column": r["name"]} for r in conn.execute("PRAGMA main.table_info(ax_calls)")
            if r["name"] not in AX_CALLS_COLUMNS]


def check_call_uid_format(conn, ctx):
    """call_uid는 'c-' + 순번 숫자만 (목적 enum 등 의미 있는 문자열 누출 방지)"""
    return [r for r in _rows(conn, "SELECT id, call_uid FROM ax_calls")
            if not CALL_UID.fullmatch(r["call_uid"])]


# ── 가상 PII ────────────────────────────────────────────────

def check_fake_phone(conn, ctx):
    """환자 전화·통화 상대 번호는 010-0000-xxxx만"""
    fake = "'010-0000-[0-9][0-9][0-9][0-9]'"
    return (_rows(conn, f"SELECT 'emr_patients' AS tbl, id, phone FROM emr_patients WHERE phone NOT GLOB {fake}")
            + _rows(conn, "SELECT 'ax_calls' AS tbl, id, caller_phone AS phone FROM ax_calls"
                          f" WHERE caller_phone IS NOT NULL AND caller_phone NOT GLOB {fake}"))


# ── 경고: 분포 ──────────────────────────────────────────────

def _band(age: int) -> str | None:
    """나이가 음수(미래 출생)면 None — 필수 검사가 따로 잡는다"""
    return next((name for name, lo, hi, _ in AGE_BANDS if lo <= age and (hi is None or age <= hi)), None)


def check_age_distribution(conn, ctx):
    """연령 구간 비중 ±2%p (TODAY 기준 나이)"""
    bands = [_band(_age(date.fromisoformat(r["birth_date"]), ctx.today))
             for r in _rows(conn, "SELECT birth_date FROM emr_patients")]
    bands = [b for b in bands if b is not None]
    if not bands:
        return []
    counts = defaultdict(int)
    for b in bands:
        counts[b] += 1
    bad = []
    for name, _, _, target in AGE_BANDS:
        share = counts[name] / len(bands)
        if abs(share - target) > SHARE_TOLERANCE:
            bad.append({"band": name, "share": round(share, 3), "target": target})
    return bad


def check_daily_fill(conn, ctx):
    """생성 기간 안 TODAY 이전 진료일의 비취소 예약 ≥ 하루 상한의 70% (TODAY 이후는 덜 찬 게 정상)"""
    counts = defaultdict(int)
    for r in _rows(conn, "SELECT date(scheduled_at) AS d FROM emr_appointments WHERE status != 'cancelled'"):
        counts[date.fromisoformat(r["d"])] += 1
    bad, d = [], ctx.range_start
    while d <= min(ctx.range_end, ctx.today - timedelta(days=1)):
        if open_slots(d.weekday()) and d not in ctx.holidays and counts[d] < DAILY_MIN_FILL * _daily_cap(ctx, d):
            bad.append({"date": d.isoformat(), "appointments": counts[d], "cap": _daily_cap(ctx, d)})
        d += timedelta(days=1)
    return bad


def check_new_patient_share(conn, ctx):
    """월별 신환(창 안 등록 — registered_at ≥ range_start이고 = 첫 예약 created_at 날짜) 첫 내원 / 그달 내원(completed) = 12% ±3%p"""
    first_visit = {r["patient_id"]: r["d"][:7] for r in _rows(
        conn, "SELECT a.patient_id, min(date(a.scheduled_at)) AS d FROM emr_appointments a"
              " JOIN emr_patients p ON p.id = a.patient_id WHERE a.status = 'completed'"
              " AND date(p.registered_at) >= ?"
              " AND date(p.registered_at) = (SELECT min(date(b.created_at)) FROM emr_appointments b"
              "                              WHERE b.patient_id = a.patient_id)"
              " GROUP BY a.patient_id", ctx.range_start.isoformat())}
    new_by_month = defaultdict(int)
    for month in first_visit.values():
        new_by_month[month] += 1
    bad = []
    for r in _rows(conn, "SELECT substr(scheduled_at, 1, 7) AS month, count(*) AS n FROM emr_appointments"
                         " WHERE status = 'completed' GROUP BY month ORDER BY month"):
        if r["n"] < NEW_PATIENT_MIN_VISITS:
            continue
        share = new_by_month[r["month"]] / r["n"]
        if abs(share - NEW_PATIENT_SHARE) > NEW_PATIENT_TOLERANCE:
            bad.append({"month": r["month"], "share": round(share, 3), "target": NEW_PATIENT_SHARE})
    return bad


def check_status_rates(conn, ctx):
    """노쇼 6%·당일 취소 4% ±2%p — 분모는 TODAY 이전 전체 예약(취소 포함)"""
    today = ctx.today.isoformat()
    base = conn.execute("SELECT count(*) FROM emr_appointments WHERE date(scheduled_at) < ?", (today,)).fetchone()[0]
    if not base:
        return []
    noshow = conn.execute("SELECT count(*) FROM emr_appointments WHERE status = 'noshow'"
                          " AND date(scheduled_at) < ?", (today,)).fetchone()[0]
    sameday = conn.execute("SELECT count(*) FROM emr_appointments a WHERE a.status = 'cancelled'"
                           " AND date(a.scheduled_at) < ? AND date(a.cancelled_at) = date(a.scheduled_at)"
                           " AND NOT EXISTS (SELECT 1 FROM emr_appointments n WHERE n.rescheduled_from_id = a.id)",
                           (today,)).fetchone()[0]
    return [{"status": status, "rate": round(n / base, 3), "target": target}
            for status, n, target in (("noshow", noshow, ctx.noshow_rate), ("sameday_cancel", sameday, ctx.sameday_cancel_rate))
            if abs(n / base - target) > SHARE_TOLERANCE]


def check_confirm_rate(conn, ctx):
    """리마인드 응답(confirmed) 85% ±2%p — TODAY 이전 비취소 예약 기준"""
    n, confirmed = conn.execute("SELECT count(*), coalesce(sum(confirmed), 0) FROM emr_appointments"
                                " WHERE status != 'cancelled' AND date(scheduled_at) < ?",
                                (ctx.today.isoformat(),)).fetchone()
    if not n:
        return []
    return [] if abs(confirmed / n - ctx.confirm_rate) <= SHARE_TOLERANCE else [
        {"rate": round(confirmed / n, 3), "target": ctx.confirm_rate}]


def check_unpaid_rate(conn, ctx):
    """미수 발생(당일 수납 < 청구) 15% ±2%p — visit 수납 행 기준"""
    n, unpaid = conn.execute("SELECT count(*), coalesce(sum(amount_paid < amount_due), 0) FROM emr_payments"
                             " WHERE kind = 'visit'").fetchone()
    if not n:
        return []
    return [] if abs(unpaid / n - ctx.unpaid_rate) <= SHARE_TOLERANCE else [
        {"rate": round(unpaid / n, 3), "target": ctx.unpaid_rate}]


def check_visit_mix(conn, ctx):
    """내원(completed) 유형 구성 진료 70 / 위생 25 / 상담 5 ±3%p"""
    counts = {r["type"]: r["n"] for r in _rows(conn, "SELECT type, count(*) AS n FROM emr_appointments"
                                                      " WHERE status = 'completed' GROUP BY type")}
    total = sum(counts.values())
    if not total:
        return []
    return [{"type": t, "share": round(counts.get(t, 0) / total, 3), "target": target}
            for t, target in VISIT_MIX.items() if abs(counts.get(t, 0) / total - target) > VISIT_MIX_TOLERANCE]


def _active_from(today: date) -> date:
    """활동 기준일 = TODAY − ACTIVE_MONTHS개월 (2026-12-01 → 2026-06-01)"""
    months = today.year * 12 + today.month - 1 - ACTIVE_MONTHS
    year, month = months // 12, months % 12 + 1
    return date(year, month, min(today.day, calendar.monthrange(year, month)[1]))


def _visit_split(conn, ctx) -> tuple[set, set]:
    """(분모 환자, 활동) — 분모는 예정 신환 제외. 내원 = completed 예약"""
    future_new = set(_future_new_patients(conn, ctx))
    patients = {r["id"] for r in _rows(conn, "SELECT id FROM emr_patients")} - future_new
    since = _active_from(ctx.today)
    last = {r["patient_id"]: date.fromisoformat(r["d"]) for r in _rows(
        conn, "SELECT patient_id, max(date(scheduled_at)) AS d FROM emr_appointments"
              " WHERE status = 'completed' GROUP BY patient_id")}
    active = {p for p in patients if p in last and since <= last[p] < ctx.today}
    return patients, active


def check_active_ratio(conn, ctx):
    """활동(TODAY 전 6개월 안 completed 내원) 비중 70% ±5%p — 예정 신환 제외.
    생성 기간이 6개월보다 짧으면(small) 활동 창을 다 덮지 못해 건너뜀"""
    if ctx.range_start > _active_from(ctx.today):
        return []
    patients, active = _visit_split(conn, ctx)
    if not patients:
        return []
    share = len(active) / len(patients)
    return [] if abs(share - ACTIVE_SHARE) <= ACTIVE_TOLERANCE else [{"share": round(share, 3), "target": ACTIVE_SHARE}]


def _in_peak(t: time) -> bool:
    return any(start <= t < end for start, end in PEAK)


def _peak_time_share(weekday: int) -> float:
    """그 요일 영업시간 중 피크 시간의 비율 (토요일은 15–17시가 영업 밖)"""
    close = SATURDAY_CLOSE if weekday == SATURDAY else WEEKDAY_CLOSE
    minutes = lambda t: t.hour * 60 + t.minute
    peak = sum(max(0, minutes(min(end, close)) - minutes(max(start, OPEN))) for start, end in PEAK)
    return peak / (open_slots(weekday) * SLOT_MIN)


def check_reschedule_rate(conn, ctx):
    """변경 쌍 / 변경 전 예약 수(전체 행 − 변경으로 생긴 새 예약) = reschedule_rate ±2%p"""
    total, pairs = conn.execute(
        "SELECT count(*), count(rescheduled_from_id) FROM emr_appointments").fetchone()
    if total == pairs:
        return []
    rate = pairs / (total - pairs)
    return [] if abs(rate - ctx.reschedule_rate) <= SHARE_TOLERANCE else [
        {"pairs": pairs, "rate": round(rate, 3), "target": ctx.reschedule_rate}]


def check_peak_pattern(conn, ctx):
    """피크(10–11·15–17시) 시작 비중이 피크의 영업시간 비중보다 큼 (휴진 요일 예약은 필수 검사 몫이라 제외)"""
    starts = [a["start"] for a in _active_appointments(conn) if open_slots(a["start"].weekday())]
    if not starts:
        return []
    actual = sum(_in_peak(s.time()) for s in starts) / len(starts)
    expected = sum(_peak_time_share(s.weekday()) for s in starts) / len(starts)
    return [] if actual > expected else [{"peak_share": round(actual, 3), "time_share": round(expected, 3)}]


# ── 경고: 치아 ──────────────────────────────────────────────

def check_tooth_level(conn, ctx):
    """치아 단위 시술은 '00' 금지, 치아 단위가 아닌 시술은 '00'만"""
    return _rows(conn, "SELECT t.id, t.procedure_code, p.name, p.is_tooth_level, t.tooth_no"
                       " FROM emr_treatments t JOIN emr_procedures p ON p.code = t.procedure_code"
                       " WHERE (p.is_tooth_level = 1) = (t.tooth_no = ?)", FULL_MOUTH)


def _treatments_with_age(conn) -> list[dict]:
    rows = _rows(conn, "SELECT t.id, t.patient_id, t.procedure_code, p.age_group, t.tooth_no, t.performed_at,"
                       " pt.birth_date FROM emr_treatments t"
                       " JOIN emr_procedures p ON p.code = t.procedure_code"
                       " JOIN emr_patients pt ON pt.id = t.patient_id")
    for r in rows:
        r["age"] = _age(date.fromisoformat(r["birth_date"]), _dt(r["performed_at"]).date())
    return rows


def check_child_procedures(conn, ctx):
    """소아 시술(age_group='child': 불소도포·실란트)은 시술일 기준 0–12세만"""
    return [r for r in _treatments_with_age(conn) if r["age_group"] == "child" and r["age"] > CHILD_MAX_AGE]


REQUIRED = [
    check_foreign_keys,
    check_status_from_today,
    check_no_records_after_today,
    check_timestamps_not_future,
    check_last_month_claims_pending,
    check_submitted_months_no_result,
    check_slot_grid,
    check_daily_cap,
    check_slot_cap,
    check_patient_same_day,
    check_business_hours,
    check_planned_procedure,
    check_confirmed_not_future,
    check_created_before_scheduled,
    check_cancel_time,
    check_cancelled_at_status,
    check_cancellation_kind,
    check_future_new_patient_single,
    check_future_new_patient_registered,
    check_reschedule_pair,
    check_reschedule_once,
    check_treatment_on_completed,
    check_completed_one_treatment,
    check_performed_at,
    check_service_date,
    check_uninsured_not_claimed,
    check_scaling_limit,
    check_tooth_no_not_null,
    check_claim_key_unique,
    check_extra_eob_not_in_ledger,
    check_truth_claims_match,
    check_balance_non_negative,
    check_visit_payment_completed,
    check_ax_calls_columns,
    check_call_uid_format,
    check_fake_phone,
]
WARNINGS = [
    check_age_distribution,
    check_daily_fill,
    check_active_ratio,
    check_new_patient_share,
    check_status_rates,
    check_reschedule_rate,
    check_confirm_rate,
    check_unpaid_rate,
    check_visit_mix,
    check_peak_pattern,
    check_tooth_level,
    check_child_procedures,
]


# ── 결정성 ──────────────────────────────────────────────────

# 실행할 때마다 바뀌는 컬럼 (§5.3 결정성: indexed_at 제외)
HASH_EXCLUDED = {("ax_calls", "indexed_at")}


def canonical_hash(conn, schema: str = "main") -> str:
    """한 스키마(main = app.sqlite, truth = truth.sqlite) 전 테이블의 내용 해시 — 테이블·컬럼 이름순, 행은 정렬.
    같은 시드 2회 비교용. 결정성은 두 스키마 모두 본다"""
    h = hashlib.sha256()
    tables = [r[0] for r in conn.execute(f"SELECT name FROM {schema}.sqlite_master WHERE type = 'table'"
                                         " AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    for table in tables:
        cols = sorted(r[1] for r in conn.execute(f"PRAGMA {schema}.table_info({table})")
                      if (table, r[1]) not in HASH_EXCLUDED)
        rows = sorted(repr(tuple(r)) for r in conn.execute(
            f"SELECT {', '.join(f'[{c}]' for c in cols)} FROM {schema}.[{table}]"))
        h.update(json.dumps([table, cols, rows], ensure_ascii=False).encode())
    return h.hexdigest()


def run(conn, ctx) -> bool:
    """모든 검사를 돌려 리포트를 출력. 필수 검사가 하나라도 실패하면 False."""
    ok = True
    for level, checks in (("FAIL", REQUIRED), ("WARN", WARNINGS)):
        for check in checks:
            violations = check(conn, ctx)
            name = getattr(check, "__name__", "check")
            if not violations:
                print(f"[ OK ] {name}")
                continue
            ok = ok and level != "FAIL"
            print(f"[{level}] {name}: {len(violations)}건 — {(check.__doc__ or '').strip()}")
            for v in violations[:EXAMPLES]:
                print(f"         {v}")
    return ok


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "app.sqlite")
    parser.add_argument("--truth", type=Path, default=ROOT / "ground_truth" / "truth.sqlite")
    parser.add_argument("--config", type=Path, default=ROOT / "config" / "generator.yaml")
    args = parser.parse_args(argv)
    for path in (args.db, args.truth):
        if not path.exists():
            print(f"DB 없음: {path} — make data 먼저")
            return 1
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("ATTACH DATABASE ? AS truth", (f"file:{args.truth}?mode=ro",))
    try:
        ctx = load_ctx(args.config, conn)
    except (KeyError, sqlite3.OperationalError) as e:
        print(f"truth_gen_meta 읽기 실패({e}) — make data 먼저")
        return 1
    ok = run(conn, ctx)
    print("verify 통과" if ok else "verify 실패 — 필수 검사 위반")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
