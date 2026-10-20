"""make verify — 생성 데이터 검사. 검사 목록은 docs/치과 AX - 가상 데이터 생성 설계.md §5.3.
필수 검사 실패 시 exit 1, 경고는 리포트만.

지금은 정형(emr_*)·ax_calls·truth_claims 필수 검사 + 분포·치아 경고. RAG·LLM 검사는 아직 없고,
결정성은 canonical_hash만 있다(같은 시드 2회 비교는 생성기 구현 후).
검사 함수는 (conn, ctx) → 위반 행 목록(dict). 빈 목록이면 통과.
conn은 app.sqlite이고 truth.sqlite가 `truth` 스키마로 ATTACH되어 있다.
"""
import argparse
import hashlib
import json
import math
import re
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

import yaml

from generator.master import (AGE_BANDS, DOCTOR_CHAIRS, FULL_MOUTH, HOURS, MAX_CHAIR_WORK, PEAK_HOURS,
                              PERMANENT_TEETH, PRIMARY_TEETH, SATURDAY_VISIT_RATIO, SCALING_CODE,
                              SCALING_MIN_AGE, SUBMITTED_MONTHS, open_slots)

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
PEAK = [tuple(time.fromisoformat(t) for t in w) for w in PEAK_HOURS]
CHILD_MAX_AGE = next(hi for name, _, hi, _ in AGE_BANDS if name == "child")

# 경고 허용치 (§5.3 경고 표)
AGE_TOLERANCE = 0.02
DAILY_VISIT_TOLERANCE = 0.30
LONG_PEAK_MAX = 0.40


@dataclass(frozen=True)
class Ctx:
    today: date
    holidays: frozenset
    reschedule_rate: float
    visits_per_day: int


def load_ctx(config_path: Path) -> Ctx:
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    holidays = json.loads((ROOT / cfg["holidays"]).read_text(encoding="utf-8"))
    return Ctx(
        today=cfg["today"],
        holidays=frozenset(date.fromisoformat(d) for d in holidays),
        reschedule_rate=cfg["reschedule_rate"],
        visits_per_day=cfg["visits_per_day"],
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
    """submitted_at·sent_at·started_at ≤ TODAY"""
    today = ctx.today.isoformat()
    return (
        _rows(conn, "SELECT 'emr_claims' AS tbl, id, submitted_at AS at FROM emr_claims"
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


def check_extra_eob_not_in_ledger(conn, ctx):
    """truth_claims의 extra_in_eob 대조 키가 emr_claims와 겹치지 않음"""
    return _rows(conn, "SELECT t.id, t.chart_no, t.service_date, t.procedure_code, t.tooth_no, c.id AS claim_id"
                       " FROM truth.truth_claims t JOIN emr_claims c USING (chart_no, service_date, procedure_code, tooth_no)"
                       " WHERE t.outcome = 'extra_in_eob'")


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

# 가상 데이터 생성 설계 §3.1의 ax_calls 컬럼. 이 밖의 컬럼(라벨·노이즈·카테고리 등)은 정답 누출
AX_CALLS_COLUMNS = {
    "id", "call_uid", "source", "patient_id", "caller_phone", "caller_name", "direction", "started_at",
    "duration_sec", "has_transcript", "audio_path", "transcript", "in_eval_pool", "eval_sort_order",
    "judge_status", "in_blind_pool", "blind_sort_order", "blind_status", "indexed_at",
}
CALL_UID = re.compile(r"c-\d{4,}")


def check_ax_calls_columns(conn, ctx):
    """ax_calls에 §3.1 밖의 컬럼(라벨·노이즈·카테고리) 없음"""
    return [{"column": r["name"]} for r in conn.execute("PRAGMA main.table_info(ax_calls)")
            if r["name"] not in AX_CALLS_COLUMNS]


def check_call_uid_format(conn, ctx):
    """call_uid는 'c-' + 순번 숫자만 (목적 enum 등 의미 있는 문자열 누출 방지)"""
    return [r for r in _rows(conn, "SELECT id, call_uid FROM ax_calls")
            if not CALL_UID.fullmatch(r["call_uid"])]


# ── 가상 PII ────────────────────────────────────────────────

def check_fake_phone(conn, ctx):
    """환자 전화는 010-0000-xxxx만"""
    return _rows(conn, "SELECT id, chart_no, phone FROM emr_patients"
                       " WHERE phone NOT GLOB '010-0000-[0-9][0-9][0-9][0-9]'")


# ── 경고: 분포 ──────────────────────────────────────────────

def _band(age: int) -> str:
    return next(name for name, lo, hi, _ in AGE_BANDS if lo <= age and (hi is None or age <= hi))


def check_age_distribution(conn, ctx):
    """연령 구간 비중 ±2%p (TODAY 기준 나이)"""
    ages = [_age(date.fromisoformat(r["birth_date"]), ctx.today)
            for r in _rows(conn, "SELECT birth_date FROM emr_patients")]
    if not ages:
        return []
    counts = defaultdict(int)
    for a in ages:
        counts[_band(a)] += 1
    bad = []
    for name, _, _, target in AGE_BANDS:
        share = counts[name] / len(ages)
        if abs(share - target) > AGE_TOLERANCE:
            bad.append({"band": name, "share": round(share, 3), "target": target})
    return bad


def check_daily_visits(conn, ctx):
    """진료일별 비취소 예약 수가 visits_per_day(토 ×0.5) ±30% — 첫 예약일~마지막 예약일 사이 모든 진료일"""
    counts = defaultdict(int)
    for r in _rows(conn, "SELECT date(scheduled_at) AS d FROM emr_appointments WHERE status != 'cancelled'"):
        counts[date.fromisoformat(r["d"])] += 1
    if not counts:
        return []
    bad, d = [], min(counts)
    while d <= max(counts):
        if open_slots(d.weekday()) and d not in ctx.holidays:
            target = ctx.visits_per_day * (SATURDAY_VISIT_RATIO if d.weekday() == SATURDAY else 1)
            if abs(counts[d] - target) > DAILY_VISIT_TOLERANCE * target:
                bad.append({"date": d.isoformat(), "visits": counts[d], "target": target})
        d += timedelta(days=1)
    return bad


def _in_peak(t: time) -> bool:
    return any(start <= t < end for start, end in PEAK)


def _peak_time_share(weekday: int) -> float:
    """그 요일 영업시간 중 피크 시간의 비율 (토요일은 15–17시가 영업 밖)"""
    close = SATURDAY_CLOSE if weekday == SATURDAY else WEEKDAY_CLOSE
    minutes = lambda t: t.hour * 60 + t.minute
    peak = sum(max(0, minutes(min(end, close)) - minutes(max(start, OPEN))) for start, end in PEAK)
    return peak / (open_slots(weekday) * 30)


def check_peak_pattern(conn, ctx):
    """피크(10–11·15–17시) 시작 비중이 피크의 영업시간 비중보다 큼"""
    starts = [a["start"] for a in _active_appointments(conn)]
    if not starts:
        return []
    actual = sum(_in_peak(s.time()) for s in starts) / len(starts)
    expected = sum(_peak_time_share(s.weekday()) for s in starts) / len(starts)
    return [] if actual > expected else [{"peak_share": round(actual, 3), "time_share": round(expected, 3)}]


def check_long_procedures_peak(conn, ctx):
    """60분↑ 예약의 피크 시작 비중 < 40% (비피크 우선 배치)"""
    starts = [a["start"] for a in _active_appointments(conn) if a["duration_min"] >= 60]
    if not starts:
        return []
    share = sum(_in_peak(s.time()) for s in starts) / len(starts)
    return [] if share < LONG_PEAK_MAX else [{"peak_share": round(share, 3), "n": len(starts)}]


# ── 경고: 치아 ──────────────────────────────────────────────

def check_tooth_fdi(conn, ctx):
    """tooth_no는 FDI 영구치·유치 번호 또는 '00'"""
    valid = PERMANENT_TEETH | PRIMARY_TEETH | {FULL_MOUTH}
    rows = (_rows(conn, "SELECT 'emr_treatments' AS tbl, id, tooth_no FROM emr_treatments")
            + _rows(conn, "SELECT 'emr_claims' AS tbl, id, tooth_no FROM emr_claims"))
    return [r for r in rows if r["tooth_no"] is not None and r["tooth_no"] not in valid]


def check_tooth_level(conn, ctx):
    """치아 단위 시술은 '00' 금지, 전악 시술은 '00'만"""
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


def check_primary_teeth_child(conn, ctx):
    """유치 번호는 소아(시술일 기준 0–12세)만"""
    return [r for r in _treatments_with_age(conn) if r["tooth_no"] in PRIMARY_TEETH and r["age"] > CHILD_MAX_AGE]


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
    check_extra_eob_not_in_ledger,
    check_balance_non_negative,
    check_visit_payment_completed,
    check_ax_calls_columns,
    check_call_uid_format,
    check_fake_phone,
]
WARNINGS = [
    check_age_distribution,
    check_daily_visits,
    check_peak_pattern,
    check_long_procedures_peak,
    check_tooth_fdi,
    check_tooth_level,
    check_primary_teeth_child,
    check_child_procedures,
]


# ── 결정성 ──────────────────────────────────────────────────

# 실행할 때마다 바뀌는 컬럼 (§5.3 결정성: indexed_at 제외)
HASH_EXCLUDED = {("ax_calls", "indexed_at")}


def canonical_hash(conn) -> str:
    """main 스키마 전 테이블의 내용 해시 — 테이블·컬럼 이름순, 행은 정렬. 같은 시드 2회 비교용"""
    h = hashlib.sha256()
    tables = [r[0] for r in conn.execute("SELECT name FROM main.sqlite_master WHERE type = 'table'"
                                         " AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    for table in tables:
        cols = sorted(r[1] for r in conn.execute(f"PRAGMA main.table_info({table})")
                      if (table, r[1]) not in HASH_EXCLUDED)
        rows = sorted(repr(tuple(r)) for r in conn.execute(
            f"SELECT {', '.join(f'[{c}]' for c in cols)} FROM main.[{table}]"))
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
    ok = run(conn, load_ctx(args.config))
    print("verify 통과" if ok else "verify 실패 — 필수 검사 위반")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
