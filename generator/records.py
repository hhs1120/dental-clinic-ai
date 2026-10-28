"""5단계 진료·수납·청구 — completed 예약에서 파생 (가상 데이터 생성 설계 §2.4·§2.6 (3) 5단계·(4)).
스케줄에 영향이 없고, 치아·수납·심사 추첨은 단계 이름(treatments·payments·claims)으로 난수를 나눈다 —
미수 파라미터를 튜닝해도 진료·청구·심사 결과가 흔들리지 않게 (§5.1).
- 진료 기록: completed마다 1행(창 이전 내원 포함), 예정 시술 그대로, 수가대로 급여·본인부담 금액. 치료계획 치아·적재(§2.3)
- 수납: 창 안만. visit 1행(paid_at = 내원 종료 시각), 미수 정산은 다음 completed 내원 종료 시각에 settlement 행
- 청구: 창 안 급여 진료마다 1줄 + 창 이전은 TODAY와 같은 해 급여 스케일링만 paid. 확정 월은 심사 결과 추첨
"""

from collections import defaultdict
from datetime import date, datetime, time, timedelta

from generator.master import (
    ADJUST_REASONS,
    ADJUST_TYPE_WEIGHTS,
    ADJUSTED_PAID_RANGE,
    AMOUNT_TYPO_MIN,
    EXTRACTION_CODES,
    FULL_MOUTH,
    HOURS,
    IMPLANT_CODE,
    LINK_FAILURE_DAYS,
    MAX_SETTLE_DAYS,
    PARALLEL_SHARE,
    PAYMENT_METHOD_WEIGHTS,
    PROCEDURES,
    RECON_MISMATCH_COUNTS,
    REVIEW_NOTICE_DAYS,
    REVIEW_OUTCOME_WEIGHTS,
    SATURDAY,
    SCALING_CODE,
    SUBMITTED_MONTHS,
    TOOTH_CHOICES,
    UNPAID_PAID_RANGE,
    add_bank_days,
    adjust_group,
    age_on,
    cash_deposit_day,
    insured_on,
    open_slots,
    recon_months,
)

PROCEDURE = {p["code"]: p for p in PROCEDURES}
OPEN = time.fromisoformat(HOURS["open"])
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


def _next_open(run, d: date) -> date:
    d += timedelta(days=1)
    while not open_slots(d.weekday()) or d in run.holidays:
        d += timedelta(days=1)
    return d


def _visible(run, d: date) -> bool:
    """그날 받은 돈이 TODAY 전에 통장에 보임 — 카드 정산(최대 MAX_SETTLE_DAYS)·현금 입금·다음 진료일 입력 모두 (§2.7)"""
    nxt = _next_open(run, d)
    return (
        add_bank_days(d, MAX_SETTLE_DAYS, run.holidays) < run.today
        and cash_deposit_day(nxt, run.holidays) < run.today
    )


def _pick_ledger_errors(
    run, rng, visits: list[dict]
) -> tuple[list[date], dict[int, str], dict[int, str]]:
    """연동 실패일(평가 달(recon_months) 안 평일 진료일 중 LINK_FAILURE_DAYS일, 다음 진료일 < TODAY — 토요일은 반나절이라 카드
    수납이 적어 결제수단 오입력이 몰리지 않게)과 장부 실수(내원 index → 종류).
    실수 후보 = 평가 달·입금이 TODAY 전에 보이는 날(_visible)의 전액 수납(진료비 > 0) 중 손으로 입력한 것 — 현금·이체,
    연동 실패일 카드. 결제수단 오입력은 연동 실패일 카드만, 날짜 밀림은 이체와 입금 경계를 넘는 현금만(밀린 날이 연동일이면
    수기 카드가 섞인다), 같은 현금 입금에는 실수 1건만, 금액 오입력은
    AMOUNT_TYPO_MIN 이상. 좁은 종류부터 뽑는다. 건수는 RECON_MISMATCH_COUNTS (§2.4·§2.7)"""
    ok_day = {
        v["day"]
        for v in visits
        if v["day"].strftime("%Y-%m") in recon_months(run.today)
        and _next_open(run, v["day"]) < run.today
        and _visible(run, v["day"])
    }
    days = sorted(d for d in ok_day if d.weekday() != SATURDAY)
    failure = sorted(
        days[int(i)] for i in rng.choice(len(days), size=LINK_FAILURE_DAYS, replace=False)
    )
    base = [i for i, v in enumerate(visits) if v["day"] in ok_day and v["paid"] == v["due"] > 0]
    card_fail = {i for i in base if visits[i]["method"] == "card" and visits[i]["day"] in failure}
    hand = {i for i in base if visits[i]["method"] != "card"}
    # 현금 날짜 밀림은 다음 진료일이 다음 현금 입금에 들어갈 때만 — 같은 입금 안이면 통장과 어긋나지 않는다
    crosses = {
        i
        for i in hand
        if visits[i]["method"] == "transfer"
        or cash_deposit_day(visits[i]["day"], run.holidays)
        != cash_deposit_day(_next_open(run, visits[i]["day"]), run.holidays)
    }
    pools = {
        "method_wrong": card_fail,
        "amount_typo": {i for i in hand | card_fail if visits[i]["due"] >= AMOUNT_TYPO_MIN},
        "date_shift": crosses,
        "entry_missing": hand | card_fail,
    }

    def cash_periods(i: int, kind: str) -> set:
        """그 실수가 건드리는 현금 입금 — 같은 입금에 실수가 둘이면 통장엔 합계 차이 하나라 가려낼 수 없다.
        결제수단 오입력은 장부에 적을 수단을 따로 정한다(아래 wrong)"""
        v = visits[i]
        if v["method"] != "cash":
            return set()
        out = {cash_deposit_day(v["day"], run.holidays)}
        if kind == "date_shift":
            out.add(cash_deposit_day(_next_open(run, v["day"]), run.holidays))
        return out

    errors: dict[int, str] = {}
    taken: set = set()  # 실수가 이미 든 현금 입금일
    for kind in ("method_wrong", "amount_typo", "date_shift", "entry_missing"):
        free = sorted(pools[kind] - set(errors))
        n = RECON_MISMATCH_COUNTS[kind]
        got = 0
        for k in rng.permutation(len(free)).tolist():
            touch = cash_periods(free[k], kind)
            if touch & taken:
                continue
            errors[free[k]] = kind
            taken |= touch
            got += 1
            if got == n:
                break
        if got < n:
            raise ValueError(f"장부 실수 {kind} 후보가 모자람 — {got}건 < {n}건")
    # 결제수단 오입력의 장부 수단 — 현금·이체 균등, 그 주 현금 입금에 이미 실수가 있으면 이체
    wrong = {}
    for i in sorted(k for k, kind in errors.items() if kind == "method_wrong"):
        period = cash_deposit_day(visits[i]["day"], run.holidays)
        method = ("cash", "transfer")[int(rng.integers(2))]
        if method == "cash" and period in taken:
            method = "transfer"
        if method == "cash":
            taken.add(period)
        wrong[i] = method
    return failure, errors, wrong


def stage_payments(run, rng, fake) -> None:
    """창 안 completed마다 visit 1행. 미수(amount_due > 0 중 config unpaid_rate)는 UNPAID_PAID_RANGE만 수납하고,
    그중 config settle_next_visit은 다음 completed 내원 종료 시각에 settlement 행으로 정산, 나머지는 잔류.
    연동 실패일·장부 실수는 자기 난수로(§2.4) — 실수 행은 전액 수납이라 정산 대상이 아니고, 실수로 생긴 미수는 남는다.
    승인번호는 받은 금액이 있는 연동일 카드 수납마다 서로 다른 8자리. 정답은 truth_ledger_errors"""
    unpaid_rate, settle_rate = run.cfg["unpaid_rate"], run.cfg["settle_next_visit"]
    visits = []
    for appt_id, pid, start, duration, due in run.conn.execute(
        "SELECT a.id, a.patient_id, a.scheduled_at, a.duration_min, t.patient_amount FROM emr_appointments a"
        " JOIN emr_treatments t ON t.appointment_id = a.id"
        " WHERE a.status = 'completed' AND date(a.scheduled_at) >= ? ORDER BY a.scheduled_at, a.id",
        (run.range_start.isoformat(),),
    ):
        end = datetime.fromisoformat(start) + timedelta(minutes=duration)
        paid, settle = due, False
        if due > 0 and rng.random() < unpaid_rate:
            paid = _floor10(due * rng.uniform(*UNPAID_PAID_RANGE))
            settle = rng.random() < settle_rate
        visits.append(
            {
                "appt": appt_id,
                "pid": pid,
                "end": end,
                "day": end.date(),
                "due": due,
                "paid": paid,
                "settle": settle,
                "method": _weighted(rng, PAYMENT_METHOD_WEIGHTS),
            }
        )
    error_rng, approval_rng = rng.spawn(2)
    failure, errors, wrong = _pick_ledger_errors(run, error_rng, visits)
    run.state["link_failure_days"] = failure
    owed: dict[int, int] = defaultdict(int)  # 환자 → 다음 내원 때 정산할 미수
    rows, truth = [], []
    for i, v in enumerate(visits):
        pid, paid, paid_at, method = v["pid"], v["paid"], v["end"], v["method"]
        kind = errors.get(i)
        if kind == "entry_missing":
            paid = 0
        elif kind == "amount_typo":
            paid = v["due"] // 100 * 10  # 0 하나 빠뜨림
        elif kind == "date_shift":
            nxt = _next_open(run, v["day"])
            close = HOURS["saturday_close" if nxt.weekday() == SATURDAY else "weekday_close"]
            span = (
                datetime.combine(nxt, time.fromisoformat(close)) - datetime.combine(nxt, OPEN)
            ).seconds // 60
            paid_at = datetime.combine(nxt, OPEN) + timedelta(
                minutes=int(error_rng.integers(span + 1))
            )
        elif kind == "method_wrong":
            method = wrong[i]
        if kind:
            truth.append((kind, len(rows) + 1, v["due"], v["end"].strftime(FMT), v["method"]))
        rows.append([pid, v["appt"], "visit", v["due"], paid, paid_at.strftime(FMT), method])
        if owed[pid]:
            rows.append(
                [
                    pid,
                    v["appt"],
                    "settlement",
                    0,
                    owed[pid],
                    v["end"].strftime(FMT),
                    _weighted(rng, PAYMENT_METHOD_WEIGHTS),
                ]
            )
            owed[pid] = 0
        if v["settle"]:
            owed[pid] += v["due"] - v["paid"]
    failed = {d.isoformat() for d in failure}
    used = set()
    for r in rows:
        r.append(None)
        if r[6] == "card" and r[4] > 0 and r[5][:10] not in failed:
            while (no := f"{int(approval_rng.integers(10**7, 10**8))}") in used:
                pass
            used.add(no)
            r[7] = no
    run.conn.executemany(
        "INSERT INTO emr_payments (id, patient_id, appointment_id, kind, amount_due, amount_paid, paid_at,"
        " method, approval_no) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(k, *r) for k, r in enumerate(rows, 1)],
    )
    run.conn.executemany(
        "INSERT INTO truth.truth_ledger_errors (kind, payment_id, true_amount, true_paid_at, true_method)"
        " VALUES (?, ?, ?, ?, ?)",
        truth,
    )


def _months_before(today: date, month: str) -> int:
    y, m = map(int, month.split("-"))
    return today.year * 12 + today.month - (y * 12 + m)


def _submitted_at(month: str) -> str:
    """익월 1일 (시각·휴진일은 따지지 않음)"""
    y, m = map(int, month.split("-"))
    return f"{date(y + m // 12, m % 12 + 1, 1).isoformat()} 09:00:00"


def review(rng, code: str, amount: int, repeat: bool = False) -> tuple:
    """확정 월 심사 결과 (status, paid_amount, adjust_code, adjust_note). 시술군별 결과 가중 → 깎이면 사유 유형 가중 →
    그 유형에서 그 시술에 맞는 상세 사유 균등. 타 기관 이력은 여기서 뽑지 않는다(병행 환자 줄 — stage_claims).
    repeat(같은 코스에서 같은 치아·시술을 이미 함 — 이어지는 치료라 재시행이 아님)이면 요건 유형을 빼고 뽑는다"""
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
    submitted / 그 이전 확정(심사 추첨). 창 이전 내원은 TODAY와 같은 해 급여 스케일링만 paid 고정(심사 추첨 대상 아님).
    병행(§2.2): 확정 월 창 안 급여 스케일링 줄이 있는 환자(이탈 환자 제외) 중 PARALLEL_SHARE — 그 환자의 첫 그 줄은
    타 기관 이력 거절(자기 난수). 정답은 truth_patient_churn"""
    birth, episode = _birth(run.conn), run.state["episode"]
    lines = run.conn.execute(
        "SELECT t.id, t.appointment_id, t.patient_id, p.chart_no, t.tooth_no, t.performed_at,"
        " t.procedure_code, t.insured_amount FROM emr_treatments t"
        " JOIN emr_patients p ON p.id = t.patient_id ORDER BY t.id"
    ).fetchall()

    def settled(served: date) -> bool:
        return (
            served >= run.range_start
            and _months_before(run.today, served.strftime("%Y-%m")) > 1 + SUBMITTED_MONTHS
        )

    first_scaling: dict[int, int] = {}  # 병행 후보 환자 → 그 첫 줄(진료 id)
    for tid, _, pid, _, _, performed, code, _ in lines:
        served = datetime.fromisoformat(performed).date()
        if (
            code == SCALING_CODE
            and pid not in run.state["churn"]
            and settled(served)
            and insured_on(PROCEDURE[code], age_on(birth[pid], served))
        ):
            first_scaling.setdefault(pid, tid)
    (parallel_rng,) = rng.spawn(1)
    pool = sorted(first_scaling)
    parallel = sorted(
        int(i)
        for i in parallel_rng.choice(pool, size=round(PARALLEL_SHARE * len(pool)), replace=False)
    )
    external_lines = {first_scaling[pid] for pid in parallel}
    external = next(r for r in ADJUST_REASONS.values() if r["type"] == "external")
    rows, seen = [], set()  # seen = (환자, 치아, 시술, 코스) — 코스 안 반복
    for tid, appt_id, pid, chart_no, tooth, performed, code, amount in lines:
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
        elif tid in external_lines:
            result = ("denied", 0, external["category"], external["note"])
        else:
            result = review(rng, code, amount, repeat)
        submitted = None if result[0] == "pending" else _submitted_at(month)
        reviewed = (
            None  # 통보일 — 확정 월(창 이전 스케일링 포함)만, 제출일 + REVIEW_NOTICE_DAYS (§2.4)
        )
        if result[0] in ("paid", "adjusted", "denied"):
            reviewed = (
                date.fromisoformat(submitted[:10]) + timedelta(days=REVIEW_NOTICE_DAYS)
            ).isoformat()
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
                reviewed,
            )
        )
    run.conn.executemany(
        "INSERT INTO truth.truth_patient_churn VALUES (?, 'parallel', NULL)",
        [(pid,) for pid in parallel],
    )
    run.conn.executemany(
        "INSERT INTO emr_claims (treatment_id, patient_id, chart_no, tooth_no, claim_month, service_date,"
        " procedure_code, claim_amount, submitted_at, status, paid_amount, adjust_code, adjust_note,"
        " reviewed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
