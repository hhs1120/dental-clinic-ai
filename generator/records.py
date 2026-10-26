"""5단계 진료·수납·청구 — completed 예약에서 파생 (가상 데이터 생성 설계 §2.4·§2.6 (3) 5단계·(4)).
스케줄에 영향이 없고, 치아·수납·심사 추첨은 단계 이름(treatments·payments·claims)으로 난수를 나눈다 —
미수 파라미터를 튜닝해도 진료·청구·심사 결과가 흔들리지 않게 (§5.1).
- 진료 기록: completed마다 1행(창 이전 내원 포함), 예정 시술 그대로, 수가대로 급여·본인부담 금액
- 수납: 창 안만. visit 1행(paid_at = 내원 종료 시각), 미수 정산은 다음 completed 내원 종료 시각에 settlement 행
- 청구: 창 안 급여 진료마다 1줄 + 창 이전은 TODAY와 같은 해 급여 스케일링만 paid. 확정 월은 심사 결과 추첨
"""
from collections import defaultdict
from datetime import date, datetime, timedelta

from generator.master import (ADJUST_REASONS, ADJUST_TYPE_WEIGHTS, ADJUSTED_PAID_RANGE, EXTERNAL_SCALING_DENY_RATE,
                              EXTRACTION_CODES, FULL_MOUTH, IMPLANT_CODE, PAYMENT_METHOD_WEIGHTS, PROCEDURES, REVIEW_OUTCOME_WEIGHTS, SCALING_CODE,
                              SUBMITTED_MONTHS, TOOTH_CHOICES, UNPAID_PAID_RANGE, adjust_group, age_on, insured_on)

PROCEDURE = {p["code"]: p for p in PROCEDURES}
FMT = "%Y-%m-%d %H:%M:%S"


def _weighted(rng, weights: dict) -> str:
    names = list(weights)
    total = sum(weights.values())
    return names[int(rng.choice(len(names), p=[weights[n] / total for n in names]))]


def _floor10(x: float) -> int:
    return int(x) // 10 * 10


def _birth(conn) -> dict[int, date]:
    return {pid: date.fromisoformat(b) for pid, b in conn.execute("SELECT id, birth_date FROM emr_patients")}


def stage_treatments(run, rng, fake) -> None:
    """completed마다 진료 기록 1행. 치아 단위 시술은 TOOTH_CHOICES에서 뽑되 코스(같은 에피소드) 안에서는 첫 치아를 유지
    (그 시술의 후보에 있을 때). 발치한 치아는 환자의 이후 후보에서 빠진다 — 임플란트 1차만 그 자리에 한다.
    후보가 바닥나면(사랑니 4개를 다 뽑은 뒤 또 사랑니 발치) 생성이 실패한다"""
    episode, birth = run.state["episode"], _birth(run.conn)
    course_tooth: dict[int, str] = {}
    extracted: dict[int, set] = defaultdict(set)
    rows = []
    for appt_id, pid, start, code in run.conn.execute(
            "SELECT id, patient_id, scheduled_at, planned_procedure_code FROM emr_appointments"
            " WHERE status = 'completed' ORDER BY scheduled_at, id"):
        p = PROCEDURE[code]
        tooth = FULL_MOUTH
        if p["is_tooth_level"]:
            choices = TOOTH_CHOICES[code]
            if code != IMPLANT_CODE:
                choices = tuple(t for t in choices if t not in extracted[pid])
            ep = episode.get(appt_id)
            kept = course_tooth.get(ep)
            tooth = kept if kept in choices else choices[int(rng.integers(len(choices)))]
            if ep is not None:
                course_tooth.setdefault(ep, tooth)
            if code in EXTRACTION_CODES:
                extracted[pid].add(tooth)
        insured = 0
        if insured_on(p, age_on(birth[pid], datetime.fromisoformat(start).date())):
            insured = round(p["fee_total"] * (1 - p["patient_share_rate"]))
        rows.append((appt_id, pid, code, tooth, start, p["fee_total"], insured, p["fee_total"] - insured))
    run.conn.executemany("INSERT INTO emr_treatments (appointment_id, patient_id, procedure_code, tooth_no, performed_at,"
                         " fee_total, insured_amount, patient_amount) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)


def stage_payments(run, rng, fake) -> None:
    """창 안 completed마다 visit 1행. 미수(amount_due > 0 중 config unpaid_rate)는 UNPAID_PAID_RANGE만 수납하고,
    그중 config settle_next_visit은 다음 completed 내원 종료 시각에 settlement 행으로 정산, 나머지는 잔류"""
    unpaid_rate, settle_rate = run.cfg["unpaid_rate"], run.cfg["settle_next_visit"]
    owed: dict[int, int] = defaultdict(int)  # 환자 → 다음 내원 때 정산할 미수
    rows = []
    for appt_id, pid, start, duration, due in run.conn.execute(
            "SELECT a.id, a.patient_id, a.scheduled_at, a.duration_min, t.patient_amount FROM emr_appointments a"
            " JOIN emr_treatments t ON t.appointment_id = a.id"
            " WHERE a.status = 'completed' AND date(a.scheduled_at) >= ? ORDER BY a.scheduled_at, a.id",
            (run.range_start.isoformat(),)):
        end = (datetime.fromisoformat(start) + timedelta(minutes=duration)).strftime(FMT)
        paid, settle = due, False
        if due > 0 and rng.random() < unpaid_rate:
            paid = _floor10(due * rng.uniform(*UNPAID_PAID_RANGE))
            settle = rng.random() < settle_rate
        rows.append((pid, appt_id, "visit", due, paid, end, _weighted(rng, PAYMENT_METHOD_WEIGHTS)))
        if owed[pid]:
            rows.append((pid, appt_id, "settlement", 0, owed[pid], end, _weighted(rng, PAYMENT_METHOD_WEIGHTS)))
            owed[pid] = 0
        if settle:
            owed[pid] += due - paid
    run.conn.executemany("INSERT INTO emr_payments (patient_id, appointment_id, kind, amount_due, amount_paid, paid_at,"
                         " method) VALUES (?, ?, ?, ?, ?, ?, ?)", rows)


def _months_before(today: date, month: str) -> int:
    y, m = map(int, month.split("-"))
    return today.year * 12 + today.month - (y * 12 + m)


def _submitted_at(month: str) -> str:
    """익월 1일 (시각·휴진일은 따지지 않음)"""
    y, m = map(int, month.split("-"))
    return f"{date(y + m // 12, m % 12 + 1, 1).isoformat()} 09:00:00"


def review(rng, code: str, amount: int) -> tuple:
    """확정 월 심사 결과 (status, paid_amount, adjust_code, adjust_note). 급여 스케일링은 먼저 타 기관 이력 불능,
    그 밖은 시술군별 결과 가중 → 깎이면 사유 유형 가중 → 그 유형에서 그 시술에 맞는 상세 사유 균등"""
    if code == SCALING_CODE and rng.random() < EXTERNAL_SCALING_DENY_RATE:
        reason = next(r for r in ADJUST_REASONS.values() if r["type"] == "external")
        return "denied", 0, reason["category"], reason["note"]
    group = adjust_group(code)
    status = _weighted(rng, REVIEW_OUTCOME_WEIGHTS[group])
    if status == "paid":
        return "paid", amount, None, None
    adjust_type = _weighted(rng, ADJUST_TYPE_WEIGHTS[group])
    reasons = [ADJUST_REASONS[k] for k in sorted(ADJUST_REASONS)
               if ADJUST_REASONS[k]["type"] == adjust_type and code in ADJUST_REASONS[k]["procedures"]]
    reason = reasons[int(rng.integers(len(reasons)))]
    paid = 0 if status == "denied" else _floor10(amount * rng.uniform(*ADJUSTED_PAID_RANGE))
    return status, paid, reason["category"], reason["note"]


def stage_claims(run, rng, fake) -> None:
    """급여 진료마다 청구 1줄(claim_month = 진료월, 금액 = 급여 금액). 전월 이후 pending / 그 전 SUBMITTED_MONTHS개월
    submitted / 그 이전 확정(심사 추첨). 창 이전 내원은 TODAY와 같은 해 급여 스케일링만 paid 고정(심사 추첨 대상 아님)"""
    birth = _birth(run.conn)
    rows = []
    for tid, pid, chart_no, tooth, performed, code, amount in run.conn.execute(
            "SELECT t.id, t.patient_id, p.chart_no, t.tooth_no, t.performed_at, t.procedure_code, t.insured_amount"
            " FROM emr_treatments t JOIN emr_patients p ON p.id = t.patient_id ORDER BY t.id"):
        served = datetime.fromisoformat(performed).date()
        if not insured_on(PROCEDURE[code], age_on(birth[pid], served)):
            continue
        month = served.strftime("%Y-%m")
        before = _months_before(run.today, month)
        if served < run.range_start:
            if code != SCALING_CODE or served.year != run.today.year:
                continue
            result = ("paid", amount, None, None)
        elif before <= 1:
            result = ("pending", None, None, None)
        elif before <= 1 + SUBMITTED_MONTHS:
            result = ("submitted", None, None, None)
        else:
            result = review(rng, code, amount)
        submitted = None if result[0] == "pending" else _submitted_at(month)
        rows.append((tid, pid, chart_no, tooth, month, served.isoformat(), code, amount, submitted, *result))
    run.conn.executemany("INSERT INTO emr_claims (treatment_id, patient_id, chart_no, tooth_no, claim_month, service_date,"
                         " procedure_code, claim_amount, submitted_at, status, paid_amount, adjust_code, adjust_note)"
                         " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
