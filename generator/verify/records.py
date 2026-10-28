"""진료·청구·치료계획·수납 검사, 치아·나이 검사, 잔액·열린 치료계획 리포트
(가상 데이터 생성 설계 §2.3·§2.4·§2.6·§5.3 시술·청구·수납)"""

from collections import defaultdict
from datetime import date, timedelta

from generator.master import (
    ADJUST_REASONS,
    AMOUNT_TYPO_MIN,
    CONDITIONAL_INSURED_MIN_AGE,
    EXTRACTION_CODES,
    FULL_MOUTH,
    HOURS,
    IMPLANT_CODE,
    LEDGER_ERROR_KINDS,
    MIN_VISIT_AGE,
    RECON_MONTHS,
    REVIEW_NOTICE_DAYS,
    SATURDAY,
    SCALING_CODE,
    SCALING_MIN_AGE,
    SUBMITTED_MONTHS,
    TOOTH_CHOICES,
    TOOTH_TREATMENT_MIN_AGE,
    age_on,
    insured_on,
    open_slots,
)
from generator.verify.common import CHILD_MAX_AGE, PROCEDURE, REASON_BY_TEXT, _dt, _end, _rows

PREP_SHEET_DAYS = 7  # 준비표 날짜 탭 = 최근 7일 + 오늘 (기능 정의 ④)


# ── 청구 상태 ─────────────────────────────────────────────────


def check_last_month_claims_pending(conn, ctx):
    """TODAY 전월(11월) 이후 청구는 pending, 제출일 없음"""
    last_month = (ctx.today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    return _rows(
        conn,
        "SELECT id, claim_month, status, submitted_at FROM emr_claims"
        " WHERE claim_month >= ? AND (status != 'pending' OR submitted_at IS NOT NULL)",
        last_month,
    )


def _submitted_months(ctx) -> list[str]:
    """전월(pending) 이전 SUBMITTED_MONTHS개월 — 9~10월"""
    months, first = [], ctx.today.replace(day=1)
    for _ in range(SUBMITTED_MONTHS + 1):
        first = (first - timedelta(days=1)).replace(day=1)
        months.append(first.strftime("%Y-%m"))
    return months[1:]


def check_submitted_months_no_result(conn, ctx):
    """전월 이전 SUBMITTED_MONTHS개월(9~10월) 청구는 submitted, 결과(paid_amount·adjust_code·adjust_note) 없음 — 정답 누출 방지"""
    months = _submitted_months(ctx)
    marks = ",".join("?" * len(months))
    return _rows(
        conn,
        "SELECT id, claim_month, status, paid_amount, adjust_code, adjust_note FROM emr_claims"
        f" WHERE claim_month IN ({marks}) AND (status != 'submitted'"
        " OR paid_amount IS NOT NULL OR adjust_code IS NOT NULL OR adjust_note IS NOT NULL)",
        *months,
    )


def check_settled_claims(conn, ctx):
    """확정 월(제출 월 이전) 청구는 paid(금액 그대로·사유 없음) / adjusted(일부 지급·사유) / denied(0원·사유)"""
    first_submitted = min(_submitted_months(ctx))
    return _rows(
        conn,
        "SELECT id, claim_month, status, claim_amount, paid_amount, adjust_code, adjust_note FROM emr_claims"
        " WHERE claim_month < ? AND NOT ("
        " (status = 'paid' AND paid_amount = claim_amount AND adjust_code IS NULL AND adjust_note IS NULL)"
        " OR (status = 'adjusted' AND paid_amount > 0 AND paid_amount < claim_amount AND adjust_code IS NOT NULL AND adjust_note IS NOT NULL)"
        " OR (status = 'denied' AND paid_amount = 0 AND adjust_code IS NOT NULL AND adjust_note IS NOT NULL))",
        first_submitted,
    )


def check_parallel_external(conn, ctx):
    """타 기관 이력 사유(master.ADJUST_REASONS의 external)로 깎인 청구 ⇔ 병행 환자(truth_patient_churn parallel) —
    병행 환자마다 한 줄 이상 (§2.2)"""
    external = [
        (r["category"], r["note"]) for r in ADJUST_REASONS.values() if r["type"] == "external"
    ]
    hit = {
        pid
        for pid, code, note in conn.execute(
            "SELECT patient_id, adjust_code, adjust_note FROM emr_claims WHERE adjust_code IS NOT NULL"
        )
        if (code, note) in external
    }
    parallel = {
        r[0]
        for r in conn.execute(
            "SELECT patient_id FROM truth.truth_patient_churn WHERE kind = 'parallel'"
        )
    }
    return [
        {
            "patient_id": pid,
            "problem": "병행 환자가 아닌데 타 기관 이력"
            if pid in hit
            else "병행 환자인데 타 기관 이력 없음",
        }
        for pid in sorted(hit ^ parallel)
    ]


def check_reviewed_at(conn, ctx):
    """심사결과 통보일은 확정 월만 = 제출일 + master.REVIEW_NOTICE_DAYS(청구 월마다 하루), 제출 월·전월은 없음.
    제출 월의 제출일 + REVIEW_NOTICE_DAYS는 TODAY 뒤(아직 결과가 올 때가 아님 — 상수 범위 확인)"""
    bad = []
    first_submitted = min(_submitted_months(ctx))
    for r in _rows(
        conn, "SELECT id, claim_month, status, submitted_at, reviewed_at FROM emr_claims"
    ):
        notice = (
            (_dt(r["submitted_at"]).date() + timedelta(days=REVIEW_NOTICE_DAYS)).isoformat()
            if r["submitted_at"]
            else None
        )
        if r["claim_month"] < first_submitted and r["status"] in ("paid", "adjusted", "denied"):
            if r["reviewed_at"] != notice or notice > ctx.today.isoformat():
                bad.append(r)
        elif r["reviewed_at"] is not None or (
            r["status"] == "submitted" and notice <= ctx.today.isoformat()
        ):
            bad.append(r)
    return bad


# ── 시술·청구 ─────────────────────────────────────────────────


def check_performed_at(conn, ctx):
    """performed_at == 예약 일시"""
    return _rows(
        conn,
        "SELECT t.id, t.appointment_id, t.performed_at, a.scheduled_at"
        " FROM emr_treatments t JOIN emr_appointments a ON a.id = t.appointment_id"
        " WHERE datetime(t.performed_at) IS NOT datetime(a.scheduled_at)",
    )


def check_treatment_on_completed(conn, ctx):
    """진료 기록은 completed 예약에만"""
    return _rows(
        conn,
        "SELECT t.id, t.appointment_id, a.status FROM emr_treatments t"
        " LEFT JOIN emr_appointments a ON a.id = t.appointment_id"
        " WHERE a.status IS NOT 'completed'",
    )


def check_completed_one_treatment(conn, ctx):
    """completed 예약은 진료 기록 정확히 1행"""
    return _rows(
        conn,
        "SELECT a.id, a.scheduled_at, count(t.id) AS treatments FROM emr_appointments a"
        " LEFT JOIN emr_treatments t ON t.appointment_id = a.id"
        " WHERE a.status = 'completed' GROUP BY a.id HAVING count(t.id) != 1",
    )


def check_service_date(conn, ctx):
    """service_date == 진료일"""
    return _rows(
        conn,
        "SELECT c.id, c.treatment_id, c.service_date, t.performed_at"
        " FROM emr_claims c JOIN emr_treatments t ON t.id = c.treatment_id"
        " WHERE date(c.service_date) IS NOT date(t.performed_at)",
    )


def check_uninsured_not_claimed(conn, ctx):
    """비급여 시술은 claims에 없음 (conditional은 시술일 65세 미만이면 비급여)"""
    rows = _rows(
        conn,
        "SELECT c.id, c.chart_no, c.service_date, c.procedure_code, p.name, p.is_insured, pt.birth_date"
        " FROM emr_claims c JOIN emr_procedures p ON p.code = c.procedure_code"
        " JOIN emr_patients pt ON pt.id = c.patient_id WHERE p.is_insured IN ('no', 'conditional')",
    )
    return [
        r
        for r in rows
        if r["is_insured"] == "no"
        or age_on(date.fromisoformat(r["birth_date"]), date.fromisoformat(r["service_date"]))
        < CONDITIONAL_INSURED_MIN_AGE
    ]


def check_scaling_limit(conn, ctx):
    """스케일링 급여: 19세↑, 환자당 연 insured_limit회"""
    rows = _rows(
        conn,
        "SELECT c.id, c.patient_id, c.chart_no, c.service_date, c.procedure_code,"
        " p.insured_limit, pt.birth_date"
        " FROM emr_claims c JOIN emr_procedures p ON p.code = c.procedure_code"
        " JOIN emr_patients pt ON pt.id = c.patient_id"
        " WHERE c.procedure_code = ? ORDER BY c.service_date, c.id",
        SCALING_CODE,
    )
    bad, count = [], defaultdict(int)
    for r in rows:
        served = date.fromisoformat(r["service_date"])
        if age_on(date.fromisoformat(r["birth_date"]), served) < SCALING_MIN_AGE:
            bad.append({**r, "reason": f"{SCALING_MIN_AGE}세 미만"})
        key = (r["patient_id"], r["procedure_code"], served.year)
        count[key] += 1
        if count[key] > r["insured_limit"]:
            bad.append({**r, "reason": f"연 {r['insured_limit']}회 초과"})
    return bad


def check_treatment_amounts(conn, ctx):
    """진료 금액은 수가대로(master fee_total) — 급여면 급여 금액 = fee × (1 − 본인부담률), 비급여면 전액 본인부담"""
    bad = []
    for r in _rows(
        conn,
        "SELECT t.id, t.procedure_code, t.performed_at, t.fee_total, t.insured_amount, t.patient_amount,"
        " pt.birth_date FROM emr_treatments t JOIN emr_patients pt ON pt.id = t.patient_id",
    ):
        p = PROCEDURE[r["procedure_code"]]
        insured = insured_on(
            p, age_on(date.fromisoformat(r["birth_date"]), _dt(r["performed_at"]).date())
        )
        expected = round(p["fee_total"] * (1 - p["patient_share_rate"])) if insured else 0
        if (r["fee_total"], r["insured_amount"], r["patient_amount"]) != (
            p["fee_total"],
            expected,
            p["fee_total"] - expected,
        ):
            bad.append({**r, "expected_insured": expected})
    return bad


def check_insured_claim_one_line(conn, ctx):
    """급여 completed 진료 ↔ 청구 정확히 1줄(환자·차트번호·코드·치아·금액 = 급여 금액·청구월 = 진료월 일치).
    창 안은 급여 진료 전부, 창 이전 내원은 TODAY와 같은 해 급여 스케일링만 paid 1줄. 그 밖의 진료는 청구 없음"""
    claims = defaultdict(list)
    for c in _rows(conn, "SELECT * FROM emr_claims"):
        claims[c["treatment_id"]].append(c)
    bad = []
    for t in _rows(
        conn,
        "SELECT t.id, t.patient_id, pt.chart_no, t.procedure_code, t.tooth_no, t.performed_at,"
        " t.insured_amount, pt.birth_date FROM emr_treatments t JOIN emr_patients pt ON pt.id = t.patient_id",
    ):
        served = _dt(t["performed_at"]).date()
        insured = insured_on(
            PROCEDURE[t["procedure_code"]], age_on(date.fromisoformat(t["birth_date"]), served)
        )
        pre_window = served < ctx.range_start
        if pre_window:
            insured = (
                insured and t["procedure_code"] == SCALING_CODE and served.year == ctx.today.year
            )
        got = claims.pop(t["id"], [])
        if len(got) != insured:
            bad.append(
                {
                    "treatment_id": t["id"],
                    "procedure_code": t["procedure_code"],
                    "performed_at": t["performed_at"],
                    "claims": len(got),
                    "expected": int(insured),
                }
            )
            continue
        expected = {
            "patient_id": t["patient_id"],
            "chart_no": t["chart_no"],
            "procedure_code": t["procedure_code"],
            "tooth_no": t["tooth_no"],
            "claim_amount": t["insured_amount"],
            "claim_month": served.strftime("%Y-%m"),
        }
        if pre_window:
            expected["status"] = "paid"
        for c in got:
            mismatch = [k for k, v in expected.items() if c[k] != v]
            if mismatch:
                bad.append({"claim_id": c["id"], "treatment_id": t["id"], "mismatch": mismatch})
    return bad + [
        {"claim_id": c["id"], "treatment_id": tid, "mismatch": "진료 기록 없음"}
        for tid, cs in claims.items()
        for c in cs
    ]


def check_adjust_reason(conn, ctx):
    """조정·불능 줄의 사유(adjust_code·adjust_note)는 master.ADJUST_REASONS 상세 사유 하나와 범주 문자·서술이 일치하고
    그 시술에 맞음. 외부 이력 사유는 denied만"""
    return [
        r
        for r in _rows(
            conn,
            "SELECT id, procedure_code, status, adjust_code, adjust_note FROM emr_claims"
            " WHERE status IN ('adjusted', 'denied')",
        )
        if (reason := REASON_BY_TEXT.get((r["adjust_code"], r["adjust_note"]))) is None
        or r["procedure_code"] not in reason["procedures"]
        or (reason["type"] == "external" and r["status"] != "denied")
    ]


def check_extracted_tooth(conn, ctx):
    """치아 수명 — 발치한 치아에는 이후 임플란트 1차만, 임플란트 치아에는 이후 진료 없음, 임플란트 1차는 발치했거나
    장부에 이력이 없는 치아에만. 어기면 정답 목록에 없는 청구 오류가 장부에 생긴다"""
    bad = []
    extracted, implanted, history = defaultdict(set), defaultdict(set), defaultdict(set)
    for r in _rows(
        conn,
        "SELECT id, patient_id, procedure_code, tooth_no, performed_at FROM emr_treatments"
        " ORDER BY patient_id, datetime(performed_at), id",
    ):
        pid, tooth, code = r["patient_id"], r["tooth_no"], r["procedure_code"]
        if tooth == FULL_MOUTH:
            continue
        why = (
            "임플란트 치아"
            if tooth in implanted[pid]
            else "이력 있는 자연치에 임플란트"
            if code == IMPLANT_CODE and tooth in history[pid] - extracted[pid]
            else "발치한 치아"
            if code != IMPLANT_CODE and tooth in extracted[pid]
            else None
        )
        if why:
            bad.append({**r, "reason": why})
        history[pid].add(tooth)
        if code in EXTRACTION_CODES:
            extracted[pid].add(tooth)
        if code == IMPLANT_CODE:
            implanted[pid].add(tooth)
    return bad


# ── 치아·나이 ─────────────────────────────────────────────────


def check_tooth_level(conn, ctx):
    """치아 단위 시술은 '00' 금지, 치아 단위가 아닌 시술은 '00'만"""
    return _rows(
        conn,
        "SELECT t.id, t.procedure_code, p.name, p.is_tooth_level, t.tooth_no"
        " FROM emr_treatments t JOIN emr_procedures p ON p.code = t.procedure_code"
        " WHERE (p.is_tooth_level = 1) = (t.tooth_no = ?)",
        FULL_MOUTH,
    )


def _treatments_with_age(conn) -> list[dict]:
    rows = _rows(
        conn,
        "SELECT t.id, t.patient_id, t.procedure_code, p.age_group, t.tooth_no, t.performed_at,"
        " pt.birth_date FROM emr_treatments t"
        " JOIN emr_procedures p ON p.code = t.procedure_code"
        " JOIN emr_patients pt ON pt.id = t.patient_id",
    )
    for r in rows:
        r["age"] = age_on(date.fromisoformat(r["birth_date"]), _dt(r["performed_at"]).date())
    return rows


def check_tooth_treatment_age(conn, ctx):
    """치아 단위 시술은 시술일 기준 TOOTH_TREATMENT_MIN_AGE세 이상 — 치아 번호가 영구치뿐"""
    tooth_level = {
        code
        for (code,) in conn.execute("SELECT code FROM emr_procedures WHERE is_tooth_level = 1")
    }
    return [
        r
        for r in _treatments_with_age(conn)
        if r["procedure_code"] in tooth_level and r["age"] < TOOTH_TREATMENT_MIN_AGE
    ]


def check_registration_age(conn, ctx):
    """등록일 기준 MIN_VISIT_AGE세 이상"""
    return [
        r
        for r in _rows(conn, "SELECT id, birth_date, registered_at FROM emr_patients")
        if age_on(date.fromisoformat(r["birth_date"]), date.fromisoformat(r["registered_at"]))
        < MIN_VISIT_AGE
    ]


def check_child_procedures(conn, ctx):
    """소아 시술(age_group='child': 불소도포·실란트)은 시술일 기준 0–12세만"""
    return [
        r
        for r in _treatments_with_age(conn)
        if r["age_group"] == "child" and r["age"] > CHILD_MAX_AGE
    ]


# ── 치료계획 ──────────────────────────────────────────────────


def _plan_est_fee(code: str, birth: str, on: str) -> int:
    """예상 금액 = 계획을 만든 날 나이의 본인부담 (check_treatment_amounts와 같은 계산)"""
    p = PROCEDURE[code]
    insured = insured_on(p, age_on(date.fromisoformat(birth), _dt(on).date()))
    return p["fee_total"] - (
        round(p["fee_total"] * (1 - p["patient_share_rate"])) if insured else 0
    )


def _plan_items(conn) -> list[dict]:
    """계획 항목 + 계획(환자·만든 시각) + 닫힌 시각 — 완료(그 항목의 completed 예약 시작) 또는 대체(다른 계획 밖 진료가
    그 치아에 처음 들어간 시각, 치아 단위만). 둘 다 없으면 None. 생성기와 따로 계산 (생성 설계 §2.3)"""
    items = _rows(
        conn,
        "SELECT i.id, i.plan_id, i.seq, i.procedure_code, i.tooth_no, i.est_fee, i.status,"
        " p.patient_id, p.created_at FROM emr_treatment_plan_items i"
        " JOIN emr_treatment_plans p ON p.id = i.plan_id ORDER BY p.created_at, p.id, i.seq",
    )
    done = dict(
        conn.execute(
            "SELECT plan_item_id, min(scheduled_at) FROM emr_appointments"
            " WHERE status = 'completed' AND plan_item_id IS NOT NULL GROUP BY plan_item_id"
        ).fetchall()
    )
    treated = defaultdict(list)  # (환자, 치아) → [(시각, 계획 id 또는 None)]
    for pid, tooth, at, plan in conn.execute(
        "SELECT t.patient_id, t.tooth_no, t.performed_at, i.plan_id FROM emr_treatments t"
        " JOIN emr_appointments a ON a.id = t.appointment_id"
        " LEFT JOIN emr_treatment_plan_items i ON i.id = a.plan_item_id"
        " WHERE t.tooth_no != ? ORDER BY t.performed_at",
        (FULL_MOUTH,),
    ):
        treated[(pid, tooth)].append((at, plan))
    for r in items:
        r["done_at"] = done.get(r["id"])
        r["replaced_at"] = next(
            (
                at
                for at, plan in treated[(r["patient_id"], r["tooth_no"])]
                if at > r["created_at"] and plan != r["plan_id"]
            ),
            None,
        )
        r["closed_at"] = r["done_at"] or r["replaced_at"]
    return items


def _open_items_at(items: list[dict], pid: int, t: str) -> list[dict]:
    """시각 t에 열린 항목(만든 시각 ≤ t, t 전에 닫히지 않음) — 차트 다음 계획·통화 사실이 보는 값"""
    return [
        {"procedure": PROCEDURE[r["procedure_code"]]["name"], "tooth": r["tooth_no"]}
        for r in items
        if r["patient_id"] == pid
        and r["created_at"] <= t
        and (r["closed_at"] is None or r["closed_at"] >= t)
    ]


def check_plan_origin(conn, ctx):
    """계획은 completed 진료·상담·위생 내원이 만든다 — 같은 환자, created_at = 그 내원 종료 시각.
    진료 내원이 만든 계획은 코스 첫 내원이라 그 예약이 항목 1"""
    bad = []
    for r in _rows(
        conn,
        "SELECT p.id, p.patient_id, p.created_at, a.patient_id AS appt_patient, a.status, a.type,"
        " a.scheduled_at, a.duration_min, a.plan_item_id, i.plan_id AS item_plan, i.seq AS item_seq"
        " FROM emr_treatment_plans p JOIN emr_appointments a ON a.id = p.created_appointment_id"
        " LEFT JOIN emr_treatment_plan_items i ON i.id = a.plan_item_id",
    ):
        if (
            r["status"] != "completed"
            or r["appt_patient"] != r["patient_id"]
            or r["created_at"] != _end(r)
            or (r["type"] == "treatment" and (r["item_plan"], r["item_seq"]) != (r["id"], 1))
        ):
            bad.append(r)
    return bad


def check_plan_item_appointments(conn, ctx):
    """예약 시술·치아 = 계획 항목: plan_item_id가 있는 예약은 진료(treatment)·같은 환자·예정 시술 = 항목 시술,
    completed면 진료 기록 치아 = 항목 치아. 위생·상담 예약은 plan_item_id 없음"""
    return _rows(
        conn,
        "SELECT a.id, a.type, a.planned_procedure_code, i.procedure_code, i.tooth_no, t.tooth_no AS treated"
        " FROM emr_appointments a JOIN emr_treatment_plan_items i ON i.id = a.plan_item_id"
        " JOIN emr_treatment_plans p ON p.id = i.plan_id"
        " LEFT JOIN emr_treatments t ON t.appointment_id = a.id"
        " WHERE a.type != 'treatment' OR a.patient_id != p.patient_id"
        " OR a.planned_procedure_code != i.procedure_code"
        " OR (t.id IS NOT NULL AND t.tooth_no != i.tooth_no)",
    )


def check_plan_item_status(conn, ctx):
    """완료 항목 ⇔ 그 항목의 completed 예약 있음. 대체됨 = 완료 전에 다른 진료가 그 치아에 들어감(치아 단위만),
    열림은 그런 진료 없음. 완료 항목은 순서 앞부분(완료 뒤에 열린·대체된 항목만 옴)"""
    bad, last_open = [], {}
    for r in _plan_items(conn):
        want = "done" if r["done_at"] else "replaced" if r["replaced_at"] else "open"
        if r["status"] != want:
            bad.append({"item_id": r["id"], "status": r["status"], "expected": want})
        elif want == "done" and last_open.get(r["plan_id"], 0) > 0:
            bad.append({"item_id": r["id"], "problem": "열린·대체된 항목 뒤의 완료 항목"})
        if want != "done":
            last_open[r["plan_id"]] = r["seq"]
    return bad


def check_plan_item_values(conn, ctx):
    """항목 순서 1..n, 치아 = 치아 단위 시술은 그 시술 후보(master.TOOTH_CHOICES)·아니면 FULL_MOUTH,
    예상 금액 = 계획을 만든 날 나이의 본인부담"""
    bad, seqs = [], defaultdict(list)
    for r in _rows(
        conn,
        "SELECT i.id, i.plan_id, i.seq, i.procedure_code, i.tooth_no, i.est_fee, p.created_at,"
        " pt.birth_date FROM emr_treatment_plan_items i JOIN emr_treatment_plans p ON p.id = i.plan_id"
        " JOIN emr_patients pt ON pt.id = p.patient_id",
    ):
        seqs[r["plan_id"]].append(r["seq"])
        code = r["procedure_code"]
        tooth_ok = (
            r["tooth_no"] in TOOTH_CHOICES[code]
            if PROCEDURE[code]["is_tooth_level"]
            else r["tooth_no"] == FULL_MOUTH
        )
        if not tooth_ok or r["est_fee"] != _plan_est_fee(code, r["birth_date"], r["created_at"]):
            bad.append(r)
    bad += [
        {"plan_id": k, "seqs": sorted(v)}
        for k, v in seqs.items()
        if sorted(v) != list(range(1, len(v) + 1))
    ]
    return bad


# ── 수납 ────────────────────────────────────────────────────


def check_balance_non_negative(conn, ctx):
    """환자별 시점 누적 잔액 ≥ 0"""
    bad, balance = [], defaultdict(int)
    for r in _rows(
        conn,
        "SELECT id, patient_id, kind, amount_due, amount_paid, paid_at"
        " FROM emr_payments ORDER BY patient_id, datetime(paid_at), id",
    ):
        balance[r["patient_id"]] += r["amount_due"] - r["amount_paid"]
        if balance[r["patient_id"]] < 0:
            bad.append({**r, "balance": balance[r["patient_id"]]})
    return bad


def check_visit_payment_completed(conn, ctx):
    """visit 수납 행은 completed 예약에만"""
    return _rows(
        conn,
        "SELECT p.id, p.patient_id, p.appointment_id, a.status FROM emr_payments p"
        " LEFT JOIN emr_appointments a ON a.id = p.appointment_id"
        " WHERE p.kind = 'visit' AND a.status IS NOT 'completed'",
    )


def check_payment_per_visit(conn, ctx):
    """창 안 completed 예약마다 visit 수납 정확히 1행, 창 이전 내원은 수납 없음, settlement는 amount_due 0"""
    bad = _rows(
        conn,
        "SELECT a.id, a.scheduled_at, count(p.id) AS visits FROM emr_appointments a"
        " LEFT JOIN emr_payments p ON p.appointment_id = a.id AND p.kind = 'visit'"
        " WHERE a.status = 'completed' GROUP BY a.id"
        " HAVING (date(a.scheduled_at) >= ? AND visits != 1) OR (date(a.scheduled_at) < ? AND visits != 0)",
        ctx.range_start.isoformat(),
        ctx.range_start.isoformat(),
    )
    return bad + _rows(
        conn,
        "SELECT id, amount_due FROM emr_payments WHERE kind = 'settlement' AND amount_due != 0",
    )


def _close(d: date) -> str:
    return HOURS["saturday_close" if d.weekday() == SATURDAY else "weekday_close"]


def _next_open_day(d: date, holidays) -> date:
    d += timedelta(days=1)
    while not open_slots(d.weekday()) or d in holidays:
        d += timedelta(days=1)
    return d


def check_approval_no(conn, ctx):
    """승인번호 = 연동 단말기 카드 수납(받은 금액 > 0)에만, 서로 다름. 연동 실패일 = 받은 카드 수납에 승인번호가
    하나도 없는 날 — 그날은 전부 없음, master.RECON_MONTHS 안 평일, 날 수 = 메타 link_failure_days (§2.4·§2.7)"""
    bad, days = [], defaultdict(set)
    seen = defaultdict(int)
    for r in _rows(conn, "SELECT id, method, amount_paid, paid_at, approval_no FROM emr_payments"):
        if r["approval_no"] is not None:
            seen[r["approval_no"]] += 1
        if r["method"] == "card" and r["amount_paid"] > 0:
            days[r["paid_at"][:10]].add(r["approval_no"] is not None)
        elif r["approval_no"] is not None:
            bad.append({**r, "problem": "카드 수납이 아니거나 받은 금액 0인데 승인번호"})
    bad += [{"approval_no": k, "problem": "승인번호 중복"} for k, n in seen.items() if n > 1]
    failed = sorted(d for d, flags in days.items() if flags == {False})
    bad += [
        {"day": d, "problem": "같은 날 연동·수기 카드 수납이 섞임"}
        for d, f in days.items()
        if len(f) > 1
    ]
    bad += [
        {"day": d, "problem": "연동 실패일이 평가 달 밖"}
        for d in failed
        if d[:7] not in RECON_MONTHS
    ]
    bad += [
        {"day": d, "problem": "연동 실패일이 토요일"}
        for d in failed
        if date.fromisoformat(d).weekday() == SATURDAY
    ]
    if len(failed) != ctx.link_failure_days:
        bad.append({"failed_days": failed, "expected": ctx.link_failure_days})
    return bad


def check_ledger_errors(conn, ctx):
    """장부 실수(truth_ledger_errors)는 창 안 visit 수납, 실제 일시 = 내원 종료·master.RECON_MONTHS 안·다음 진료일 < TODAY,
    실제 금액 = 진료비(전액 수납). 입력 누락 = 받은 금액 0 / 금액 오입력 = 0 하나 빠뜨림(master.AMOUNT_TYPO_MIN 이상) /
    날짜 밀림 = 다음 진료일 영업시간에 입력 / 결제수단 오입력 = 실제 카드가 연동 실패일에 다른 수단으로.
    종류별 건수 = 메타 ledger_errors, 수납 1행에 실수 1개 (§2.4)"""
    bad, counts, seen = [], defaultdict(int), set()
    linked = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT date(paid_at) FROM emr_payments WHERE approval_no IS NOT NULL"
        )
    }
    for r in _rows(
        conn,
        "SELECT e.*, p.kind AS pay_kind, p.amount_due, p.amount_paid, p.paid_at, p.method, p.approval_no,"
        " a.scheduled_at, a.duration_min FROM truth.truth_ledger_errors e"
        " JOIN emr_payments p ON p.id = e.payment_id JOIN emr_appointments a ON a.id = p.appointment_id",
    ):
        counts[r["kind"]] += 1
        true_day = _dt(r["true_paid_at"]).date()
        nxt = _next_open_day(true_day, ctx.holidays)
        shifted = _dt(r["paid_at"])
        ok = {
            "entry_missing": r["amount_paid"] == 0 and r["paid_at"] == r["true_paid_at"],
            "amount_typo": r["true_amount"] >= AMOUNT_TYPO_MIN
            and r["amount_paid"] == r["true_amount"] // 100 * 10
            and r["paid_at"] == r["true_paid_at"],
            "date_shift": r["amount_paid"] == r["true_amount"]
            and shifted.date() == nxt
            and HOURS["open"] <= shifted.strftime("%H:%M") <= _close(nxt),
            "method_wrong": r["amount_paid"] == r["true_amount"]
            and r["true_method"] == "card"
            and r["method"] != "card"
            and true_day.isoformat() not in linked,
        }[r["kind"]]
        if (
            not ok
            or r["pay_kind"] != "visit"
            or r["payment_id"] in seen
            or r["true_paid_at"] != _end(r)
            or r["true_amount"] != r["amount_due"]
            or r["true_amount"] <= 0
            or true_day.strftime("%Y-%m") not in RECON_MONTHS
            or nxt >= ctx.today
            or (r["kind"] != "method_wrong" and r["true_method"] != r["method"])
        ):
            bad.append(r)
        seen.add(r["payment_id"])
    want = {k: ctx.ledger_errors.get(k, 0) for k in LEDGER_ERROR_KINDS}
    if dict(counts) | {k: counts.get(k, 0) for k in want} != want:
        bad.append({"counts": dict(counts), "expected": want})
    return bad


def check_ledger_error_unsettled(conn, ctx):
    """입력 누락·금액 오입력으로 생긴 미수는 정산되지 않음 — 그 환자의 TODAY 잔액 ≥ 실수로 생긴 미수 합 (§2.4)"""
    owed = dict(
        conn.execute(
            "SELECT p.patient_id, sum(e.true_amount - p.amount_paid) FROM truth.truth_ledger_errors e"
            " JOIN emr_payments p ON p.id = e.payment_id"
            " WHERE e.kind IN ('entry_missing', 'amount_typo') GROUP BY p.patient_id"
        )
    )
    balance = dict(
        conn.execute(
            "SELECT patient_id, sum(amount_due - amount_paid) FROM emr_payments GROUP BY patient_id"
        )
    )
    return [
        {"patient_id": pid, "error_debt": d, "balance": balance.get(pid, 0)}
        for pid, d in sorted(owed.items())
        if balance.get(pid, 0) < d
    ]


# ── 리포트 ───────────────────────────────────────────────────


def report_balance_holder_share(conn, ctx):
    """잔액 있음 비율 — 준비표 범위(최근 PREP_SHEET_DAYS일 + 오늘)의 비취소 예약(환자 × 날짜) 중 미수 칩이 켜지는 것.
    잔액 있음 = 그 예약일 개원 전 잔액 > 0 (정의 원본: 앱 설계 §3 has_unpaid_balance)"""
    since = ctx.today - timedelta(days=PREP_SHEET_DAYS)
    rows = _rows(
        conn,
        "SELECT DISTINCT patient_id, date(scheduled_at) AS d FROM emr_appointments"
        " WHERE status != 'cancelled' AND date(scheduled_at) BETWEEN ? AND ?",
        since.isoformat(),
        ctx.today.isoformat(),
    )
    if not rows:
        return {"n": 0}
    owing = sum(
        conn.execute(
            "SELECT coalesce(sum(amount_due - amount_paid), 0) FROM emr_payments"
            " WHERE patient_id = ? AND date(paid_at) < ?",
            (r["patient_id"], r["d"]),
        ).fetchone()[0]
        > 0
        for r in rows
    )
    return {"n": len(rows), "share": round(owing / len(rows), 3)}


def report_open_plans(conn, ctx):
    """TODAY 개원 전 열린 치료계획 항목 — 항목 수·계획 수·환자 수, 그중 다음 예약이 없는 환자(⑨ 진료계획 미예약 규모)"""
    t = f"{ctx.today.isoformat()} 00:00:00"
    open_items = [r for r in _plan_items(conn) if r["closed_at"] is None]
    booked = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT patient_id FROM emr_appointments WHERE scheduled_at > ? AND status = 'scheduled'",
            (t,),
        )
    }
    patients = {r["patient_id"] for r in open_items}
    return {
        "items": len(open_items),
        "plans": len({r["plan_id"] for r in open_items}),
        "patients": len(patients),
        "patients_unbooked": len(patients - booked),
    }
