"""5단계 진료·수납·청구 — completed 예약에서 파생 (가상 데이터 생성 설계 §2.4·§2.6 (3) 5단계·(4)).
스케줄에 영향이 없고, 치아·수납·심사 추첨은 단계 이름(treatments·payments·claims)으로 난수를 나눈다 —
미수 파라미터를 튜닝해도 진료·청구·심사 결과가 흔들리지 않게 (§5.1).
- 진료 기록: completed마다 1행(창 이전 내원 포함), 예정 시술 그대로, 수가대로 급여·본인부담 금액. 치료계획 치아·적재(§2.3)
- 수납: 창 안만. visit 1행(paid_at = 내원 종료 시각), 미수 정산은 다음 completed 내원 종료 시각에 settlement 행
- 청구: 창 안 급여 진료마다 1줄 + 창 이전은 TODAY와 같은 해 급여 스케일링만 paid. 확정 월은 심사 결과 추첨
"""

from collections import defaultdict
from datetime import date, datetime, timedelta

from generator.master import (
    ADJUST_REASONS,
    ADJUST_TYPE_WEIGHTS,
    ADJUSTED_PAID_RANGE,
    EXTERNAL_SCALING_DENY_RATE,
    EXTRACTION_CODES,
    FULL_MOUTH,
    IMPLANT_CODE,
    PAYMENT_METHOD_WEIGHTS,
    PROCEDURES,
    REVIEW_OUTCOME_WEIGHTS,
    SCALING_CODE,
    SUBMITTED_MONTHS,
    TOOTH_CHOICES,
    UNPAID_PAID_RANGE,
    adjust_group,
    age_on,
    insured_on,
)

PROCEDURE = {p["code"]: p for p in PROCEDURES}
FMT = "%Y-%m-%d %H:%M:%S"


def _weighted(rng, weights: dict) -> str:
    names = list(weights)
    total = sum(weights.values())
    return names[int(rng.choice(len(names), p=[weights[n] / total for n in names]))]


def _floor10(x: float) -> int:
    return int(x) // 10 * 10


def _birth(conn) -> dict[int, date]:
    return {
        pid: date.fromisoformat(b)
        for pid, b in conn.execute("SELECT id, birth_date FROM emr_patients")
    }


def _est_fee(p: dict, age: int) -> int:
    """예상 금액 = 그 나이의 본인부담 (진료 기록 patient_amount와 같은 계산)"""
    return p["fee_total"] - (
        round(p["fee_total"] * (1 - p["patient_share_rate"])) if insured_on(p, age) else 0
    )


def stage_treatments(run, rng, fake) -> None:
    """completed마다 진료 기록 1행 + 치료계획 적재(§2.3). 치아 단위 시술은 TOOTH_CHOICES에서 뽑는다.
    치아 수명: 발치·임플란트 치아는 이후 자연치 시술 후보에서 빠지고, 임플란트 1차는 발치했거나 장부에 이력이 없는
    치아에만 한다(임플란트 치아에 다시 임플란트 없음).
    치료계획: 계획을 만든 내원에서 항목 치아를 모두 정한다 — 앞 항목 치아를 유지(그 시술의 후보에 있을 때, 앞 항목의
    발치·임플란트를 친 상태로), 계획 항목 예약은 항목 치아 그대로. 새로 뽑는 치아는 열린 항목 치아를 피하고(피할 수
    없으면 그대로), 다른 계획의 열린 항목 치아에 진료가 들어가면 그 항목은 대체됨. 항목마다 닫힌 시각(완료·대체)을
    state plan_items에 남긴다(5b 다음 계획·6단계 통화 사실). 후보가 바닥나면(사랑니 4개를 다 뽑은 뒤 또 사랑니 발치)
    생성이 실패한다"""
    birth = _birth(run.conn)
    plans_by_appt = {plan.appointment_id: plan for plan in run.state["plans"]}
    extracted: dict[int, set] = defaultdict(set)
    implanted: dict[int, set] = defaultdict(set)
    history: dict[int, set] = defaultdict(set)
    item_tooth: dict[int, str] = {}
    status: dict[int, str] = {}
    closed: dict[int, str] = {}
    item_plan = {i.id: plan.id for plan in run.state["plans"] for i in plan.items}
    open_teeth: dict[int, dict] = defaultdict(
        lambda: defaultdict(set)
    )  # 환자 → 치아 → 열린 항목 id

    def valid(code, pid, ext=frozenset(), imp=frozenset(), hist=frozenset()) -> tuple:
        choices = TOOTH_CHOICES[code]
        if code == IMPLANT_CODE:
            return tuple(
                t
                for t in choices
                if t not in implanted[pid] | imp
                and (t in extracted[pid] | ext or t not in history[pid] | hist)
            )
        return tuple(t for t in choices if t not in extracted[pid] | implanted[pid] | ext | imp)

    def pick(pid, choices: tuple) -> str:
        free = tuple(t for t in choices if not open_teeth[pid].get(t)) or choices
        return free[int(rng.integers(len(free)))]

    def plan_teeth(plan, pid, first: str | None) -> None:
        prev, ext, imp, hist = first, set(), set(), set()
        for item in plan.items:
            p = PROCEDURE[item.code]
            if not p["is_tooth_level"]:
                tooth = FULL_MOUTH
            elif item.seq == 1 and first is not None:
                tooth = first
            else:
                choices = valid(item.code, pid, ext, imp, hist)
                tooth = prev if prev in choices else pick(pid, choices)
            if tooth != FULL_MOUTH:
                prev = tooth
                hist.add(tooth)
                if item.code in EXTRACTION_CODES:
                    ext.add(tooth)
                if item.code == IMPLANT_CODE:
                    imp.add(tooth)
            item_tooth[item.id] = tooth
        for item in plan.items:  # 다 정한 뒤에 — 같은 계획의 앞 항목을 피하거나 대체하지 않게
            if item.id not in status:
                status[item.id] = "open"
                if item_tooth[item.id] != FULL_MOUTH:
                    open_teeth[pid][item_tooth[item.id]].add(item.id)

    rows, plan_rows, item_rows = [], [], []
    for appt_id, pid, start, code, item in run.conn.execute(
        "SELECT id, patient_id, scheduled_at, planned_procedure_code, plan_item_id FROM emr_appointments"
        " WHERE status = 'completed' ORDER BY scheduled_at, id"
    ):
        p = PROCEDURE[code]
        tooth = FULL_MOUTH
        if item in item_tooth:
            tooth = item_tooth[item]
        elif p["is_tooth_level"]:
            tooth = pick(pid, valid(code, pid))
        if tooth != FULL_MOUTH:
            for other in sorted(open_teeth[pid].get(tooth, ())):
                if item is None or item_plan[other] != item_plan[item]:
                    status[other], closed[other] = "replaced", start
                    open_teeth[pid][tooth].discard(other)
        if item is not None:
            status[item], closed[item] = "done", start
            open_teeth[pid].get(tooth, set()).discard(item)
        if tooth != FULL_MOUTH:
            history[pid].add(tooth)
            if code in EXTRACTION_CODES:
                extracted[pid].add(tooth)
            if code == IMPLANT_CODE:
                implanted[pid].add(tooth)
        if (plan := plans_by_appt.get(appt_id)) is not None:
            plan_teeth(plan, pid, tooth if item is not None else None)
            plan_rows.append((plan.id, pid, plan.created.strftime(FMT), appt_id))
        insured = 0
        if insured_on(p, age_on(birth[pid], datetime.fromisoformat(start).date())):
            insured = round(p["fee_total"] * (1 - p["patient_share_rate"]))
        rows.append(
            (appt_id, pid, code, tooth, start, p["fee_total"], insured, p["fee_total"] - insured)
        )
    for plan in run.state["plans"]:
        age = age_on(birth[plan.patient_id], plan.created.date())
        item_rows += [
            (
                i.id,
                plan.id,
                i.seq,
                i.code,
                item_tooth[i.id],
                _est_fee(PROCEDURE[i.code], age),
                status[i.id],
            )
            for i in plan.items
        ]
    run.state["plan_items"] = [
        {
            "patient_id": plan.patient_id,
            "created": plan.created.strftime(FMT),
            "procedure": PROCEDURE[i.code]["name"],
            "tooth": item_tooth[i.id],
            "closed": closed.get(i.id),
        }
        for plan in sorted(run.state["plans"], key=lambda plan: (plan.created, plan.id))
        for i in plan.items
    ]
    run.conn.executemany(
        "INSERT INTO emr_treatments (appointment_id, patient_id, procedure_code, tooth_no, performed_at,"
        " fee_total, insured_amount, patient_amount) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    run.conn.executemany(
        "INSERT INTO emr_treatment_plans (id, patient_id, created_at, created_appointment_id)"
        " VALUES (?, ?, ?, ?)",
        plan_rows,
    )
    run.conn.executemany(
        "INSERT INTO emr_treatment_plan_items (id, plan_id, seq, procedure_code, tooth_no, est_fee, status)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        item_rows,
    )


def open_plan_items(items: list[dict], pid: int, t: str) -> list[dict]:
    """시각 t에 열린 치료계획 항목(만든 시각 ≤ t, t 전에 닫히지 않음) — items = state plan_items (§2.3)"""
    return [
        {"procedure": i["procedure"], "tooth": i["tooth"]}
        for i in items
        if i["patient_id"] == pid
        and i["created"] <= t
        and (i["closed"] is None or i["closed"] >= t)
    ]


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
        (run.range_start.isoformat(),),
    ):
        end = (datetime.fromisoformat(start) + timedelta(minutes=duration)).strftime(FMT)
        paid, settle = due, False
        if due > 0 and rng.random() < unpaid_rate:
            paid = _floor10(due * rng.uniform(*UNPAID_PAID_RANGE))
            settle = rng.random() < settle_rate
        rows.append(
            (pid, appt_id, "visit", due, paid, end, _weighted(rng, PAYMENT_METHOD_WEIGHTS))
        )
        if owed[pid]:
            rows.append(
                (
                    pid,
                    appt_id,
                    "settlement",
                    0,
                    owed[pid],
                    end,
                    _weighted(rng, PAYMENT_METHOD_WEIGHTS),
                )
            )
            owed[pid] = 0
        if settle:
            owed[pid] += due - paid
    run.conn.executemany(
        "INSERT INTO emr_payments (patient_id, appointment_id, kind, amount_due, amount_paid, paid_at,"
        " method) VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )


def _months_before(today: date, month: str) -> int:
    y, m = map(int, month.split("-"))
    return today.year * 12 + today.month - (y * 12 + m)


def _submitted_at(month: str) -> str:
    """익월 1일 (시각·휴진일은 따지지 않음)"""
    y, m = map(int, month.split("-"))
    return f"{date(y + m // 12, m % 12 + 1, 1).isoformat()} 09:00:00"


def review(rng, code: str, amount: int, repeat: bool = False) -> tuple:
    """확정 월 심사 결과 (status, paid_amount, adjust_code, adjust_note). 급여 스케일링은 먼저 타 기관 이력 불능,
    그 밖은 시술군별 결과 가중 → 깎이면 사유 유형 가중 → 그 유형에서 그 시술에 맞는 상세 사유 균등.
    repeat(같은 코스에서 같은 치아·시술을 이미 함 — 이어지는 치료라 재시행이 아님)이면 요건 유형을 빼고 뽑는다"""
    if code == SCALING_CODE and rng.random() < EXTERNAL_SCALING_DENY_RATE:
        reason = next(r for r in ADJUST_REASONS.values() if r["type"] == "external")
        return "denied", 0, reason["category"], reason["note"]
    group = adjust_group(code)
    status = _weighted(rng, REVIEW_OUTCOME_WEIGHTS[group])
    if status == "paid":
        return "paid", amount, None, None
    adjust_type = _weighted(
        rng,
        {
            k: w
            for k, w in ADJUST_TYPE_WEIGHTS[group].items()
            if not (repeat and k == "requirement")
        },
    )
    reasons = [
        ADJUST_REASONS[k]
        for k in sorted(ADJUST_REASONS)
        if ADJUST_REASONS[k]["type"] == adjust_type and code in ADJUST_REASONS[k]["procedures"]
    ]
    reason = reasons[int(rng.integers(len(reasons)))]
    paid = 0 if status == "denied" else _floor10(amount * rng.uniform(*ADJUSTED_PAID_RANGE))
    return status, paid, reason["category"], reason["note"]


def stage_claims(run, rng, fake) -> None:
    """급여 진료마다 청구 1줄(claim_month = 진료월, 금액 = 급여 금액). 전월 이후 pending / 그 전 SUBMITTED_MONTHS개월
    submitted / 그 이전 확정(심사 추첨). 창 이전 내원은 TODAY와 같은 해 급여 스케일링만 paid 고정(심사 추첨 대상 아님)"""
    birth, episode = _birth(run.conn), run.state["episode"]
    rows, seen = [], set()  # seen = (환자, 치아, 시술, 코스) — 코스 안 반복
    for tid, appt_id, pid, chart_no, tooth, performed, code, amount in run.conn.execute(
        "SELECT t.id, t.appointment_id, t.patient_id, p.chart_no, t.tooth_no, t.performed_at,"
        " t.procedure_code, t.insured_amount FROM emr_treatments t"
        " JOIN emr_patients p ON p.id = t.patient_id ORDER BY t.id"
    ):
        key = (pid, tooth, code, episode.get(appt_id))
        repeat = tooth != FULL_MOUTH and key[3] is not None and key in seen
        seen.add(key)
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
            result = review(rng, code, amount, repeat)
        submitted = None if result[0] == "pending" else _submitted_at(month)
        rows.append(
            (
                tid,
                pid,
                chart_no,
                tooth,
                month,
                served.isoformat(),
                code,
                amount,
                submitted,
                *result,
            )
        )
    run.conn.executemany(
        "INSERT INTO emr_claims (treatment_id, patient_id, chart_no, tooth_no, claim_month, service_date,"
        " procedure_code, claim_amount, submitted_at, status, paid_amount, adjust_code, adjust_note)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
