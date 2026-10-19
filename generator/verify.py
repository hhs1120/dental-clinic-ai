"""make verify — 생성 데이터 검사. 검사 목록은 docs/치과 AX - 데이터 설계.md §5.3.
필수 검사 실패 시 exit 1, 경고는 리포트만.

지금은 정형(emr_*) 필수 검사만. ax_*·RAG·LLM·누출·결정성 검사는 아직 없다.
검사 함수는 (conn, ctx) → 위반 행 목록(dict). 빈 목록이면 통과.
"""
import argparse
import json
import math
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

import yaml

from generator.master import (DOCTOR_CHAIRS, HOURS, MAX_CHAIR_WORK, SCALING_CODE, SCALING_MIN_AGE,
                              SUBMITTED_MONTHS, open_slots)

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = 5

# 영업시간 (§1) — generator/master.py의 HOURS에서
OPEN = time.fromisoformat(HOURS["open"])
WEEKDAY_CLOSE = time.fromisoformat(HOURS["weekday_close"])
SATURDAY_CLOSE = time.fromisoformat(HOURS["saturday_close"])
LUNCH = tuple(time.fromisoformat(t) for t in HOURS["lunch"])
CLOSED_WEEKDAYS = set(HOURS["closed_weekdays"])
SATURDAY = 5
SLOT = timedelta(minutes=30)


@dataclass(frozen=True)
class Ctx:
    today: date
    holidays: frozenset
    reschedule_rate: float


def load_ctx(config_path: Path) -> Ctx:
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    holidays = json.loads((ROOT / cfg["holidays"]).read_text(encoding="utf-8"))
    return Ctx(
        today=cfg["today"],
        holidays=frozenset(date.fromisoformat(d) for d in holidays),
        reschedule_rate=cfg["reschedule_rate"],
    )


def _rows(conn, sql, *params) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params)]


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _active_appointments(conn) -> list[dict]:
    """취소되지 않은 예약 — 체어·원장 시간을 실제로 차지한 것 (노쇼 포함)."""
    rows = _rows(conn, "SELECT id, patient_id, chair_no, type, scheduled_at, duration_min"
                       " FROM emr_appointments WHERE status != 'cancelled'")
    for r in rows:
        r["start"] = _dt(r["scheduled_at"])
        r["end"] = r["start"] + timedelta(minutes=r["duration_min"])
    return rows


# ── 참조 ────────────────────────────────────────────────────

def check_foreign_keys(conn, ctx):
    """FK 전부 유효 (스키마에 선언된 emr_* 참조)"""
    return [{"table": t, "rowid": rowid, "parent": parent}
            for t, rowid, parent, _ in conn.execute("PRAGMA foreign_key_check")
            if t.startswith("emr_")]


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
    """submitted_at·sent_at ≤ TODAY"""
    today = ctx.today.isoformat()
    return (
        _rows(conn, "SELECT 'emr_claims' AS tbl, id, submitted_at AS at FROM emr_claims"
                    " WHERE submitted_at IS NOT NULL AND date(submitted_at) > ?", today)
        + _rows(conn, "SELECT 'emr_messages' AS tbl, id, sent_at AS at FROM emr_messages"
                      " WHERE date(sent_at) > ?", today)
    )


def check_last_month_claims_pending(conn, ctx):
    """TODAY 전월(11월) 이후 청구는 pending"""
    last_month = (ctx.today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    return _rows(conn, "SELECT id, claim_month, status, submitted_at FROM emr_claims"
                       " WHERE claim_month >= ? AND status != 'pending'", last_month)


def check_submitted_months_no_result(conn, ctx):
    """전월 이전 SUBMITTED_MONTHS개월(9~10월) 청구는 submitted, 결과(paid_amount·adjust_code) 없음 — 정답 누출 방지"""
    months, first = [], ctx.today.replace(day=1)
    for _ in range(SUBMITTED_MONTHS + 1):
        first = (first - timedelta(days=1)).replace(day=1)
        months.append(first.strftime("%Y-%m"))
    months = months[1:]  # 전월(pending)은 제외
    marks = ",".join("?" * len(months))
    return _rows(conn, "SELECT id, claim_month, status, paid_amount, adjust_code FROM emr_claims"
                       f" WHERE claim_month IN ({marks}) AND (status != 'submitted'"
                       " OR paid_amount IS NOT NULL OR adjust_code IS NOT NULL)", *months)


# ── 용량·충돌 ───────────────────────────────────────────────

def check_doctor_slots(conn, ctx):
    """날짜별 원장 슬롯 ≤ 2체어 × 영업 슬롯 (평일 32, 토 16)"""
    used = defaultdict(int)
    for a in _active_appointments(conn):
        if a["type"] == "treatment":
            used[a["start"].date()] += math.ceil(a["duration_min"] / 30)
    return [{"date": d.isoformat(), "slots": n, "limit": _doctor_limit(d)}
            for d, n in sorted(used.items()) if n > _doctor_limit(d)]


def _doctor_limit(d: date) -> int:
    return DOCTOR_CHAIRS * open_slots(d.weekday())


def check_chair_overlap(conn, ctx):
    """같은 체어·같은 시간 중복 없음"""
    by_chair = defaultdict(list)
    for a in _active_appointments(conn):
        if a["chair_no"] is not None:
            by_chair[a["chair_no"]].append(a)
    bad = []
    for chair, appts in by_chair.items():
        appts.sort(key=lambda a: a["start"])
        for prev, cur in zip(appts, appts[1:]):
            if cur["start"] < prev["end"]:
                bad.append({"chair_no": chair, "id": prev["id"], "other_id": cur["id"],
                            "at": cur["scheduled_at"]})
    return bad


def check_concurrency(conn, ctx):
    """30분 슬롯마다 동시 treatment ≤ 2, hygiene + treatment ≤ 3"""
    treat, chair_work = defaultdict(int), defaultdict(int)
    for a in _active_appointments(conn):
        if a["type"] == "consult":
            continue
        t = a["start"]
        while t < a["end"]:
            chair_work[t] += 1
            treat[t] += a["type"] == "treatment"
            t += SLOT
    return [{"slot": t.isoformat(sep=" "), "treatment": treat[t], "hygiene+treatment": n}
            for t, n in sorted(chair_work.items()) if treat[t] > DOCTOR_CHAIRS or n > MAX_CHAIR_WORK]


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


def check_reschedule_ratio(conn, ctx):
    """변경 쌍 ≥ reschedule_rate × 예약 수"""
    total, pairs = conn.execute(
        "SELECT count(*), count(rescheduled_from_id) FROM emr_appointments").fetchone()
    need = ctx.reschedule_rate * total
    return [] if pairs >= need else [{"pairs": pairs, "appointments": total, "need": need}]


# ── 시술·청구 ───────────────────────────────────────────────

def check_performed_at(conn, ctx):
    """performed_at == 예약 일시"""
    return _rows(conn, "SELECT t.id, t.appointment_id, t.performed_at, a.scheduled_at"
                       " FROM emr_treatments t JOIN emr_appointments a ON a.id = t.appointment_id"
                       " WHERE datetime(t.performed_at) IS NOT datetime(a.scheduled_at)")


def check_service_date(conn, ctx):
    """service_date == 진료일"""
    return _rows(conn, "SELECT c.id, c.treatment_id, c.service_date, t.performed_at"
                       " FROM emr_claims c JOIN emr_treatments t ON t.id = c.treatment_id"
                       " WHERE date(c.service_date) IS NOT date(t.performed_at)")


def check_uninsured_not_claimed(conn, ctx):
    """비급여 시술은 claims에 없음"""
    return _rows(conn, "SELECT c.id, c.chart_no, c.service_date, c.procedure_code, p.name"
                       " FROM emr_claims c JOIN emr_procedures p ON p.code = c.procedure_code"
                       " WHERE p.is_insured = 'no'")


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
    """tooth_no NULL 없음 (전악은 '00')"""
    return (_rows(conn, "SELECT 'emr_treatments' AS tbl, id FROM emr_treatments WHERE tooth_no IS NULL")
            + _rows(conn, "SELECT 'emr_claims' AS tbl, id FROM emr_claims WHERE tooth_no IS NULL"))


def check_claim_key_unique(conn, ctx):
    """대조 키 (chart_no, service_date, procedure_code, tooth_no) UNIQUE"""
    return _rows(conn, "SELECT chart_no, service_date, procedure_code, tooth_no, group_concat(id) AS ids"
                       " FROM emr_claims GROUP BY chart_no, service_date, procedure_code, tooth_no"
                       " HAVING count(*) > 1")


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


# ── 가짜 PII ────────────────────────────────────────────────

def check_fake_phone(conn, ctx):
    """환자 전화는 010-0000-xxxx만"""
    return _rows(conn, "SELECT id, chart_no, phone FROM emr_patients"
                       " WHERE phone NOT GLOB '010-0000-[0-9][0-9][0-9][0-9]'")


REQUIRED = [
    check_foreign_keys,
    check_status_from_today,
    check_no_records_after_today,
    check_timestamps_not_future,
    check_last_month_claims_pending,
    check_submitted_months_no_result,
    check_doctor_slots,
    check_chair_overlap,
    check_concurrency,
    check_patient_same_day,
    check_business_hours,
    check_planned_procedure,
    check_reschedule_ratio,
    check_performed_at,
    check_service_date,
    check_uninsured_not_claimed,
    check_scaling_limit,
    check_tooth_no_not_null,
    check_claim_key_unique,
    check_balance_non_negative,
    check_visit_payment_completed,
    check_fake_phone,
]
WARNINGS = []


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
    parser.add_argument("--config", type=Path, default=ROOT / "config" / "generator.yaml")
    args = parser.parse_args(argv)
    if not args.db.exists():
        print(f"DB 없음: {args.db} — make data 먼저")
        return 1
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    ok = run(conn, load_ctx(args.config))
    print("verify 통과" if ok else "verify 실패 — 필수 검사 위반")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
