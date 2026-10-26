"""make verify — 생성 데이터 검사. 검사 목록은 docs/치과 AX - 가상 데이터 생성 설계.md §5.3.
필수 검사 실패 시 exit 1, 경고는 위반만 보여 주고, 리포트(report_*)는 허용치 없이 수치만 출력한다(튜닝 때 눈으로 봄).

구현: 정형(emr_*)·ax_calls·통화 슬롯(6단계)·truth_claims(있을 때) 필수 검사 + 분포·치아·통화 분포 경고 + 리포트.
§5.3 중 아직 없는 것(해당 단계 구현 때 추가). RAG는 구현 전까지 [SKIP]으로 출력:
  필수 — 다형 참조(ax_llm_calls.ref_*·ax_chunks.source_*), effective_date ≤ TODAY,
         누출(풀 마킹 카테고리 추론 — 순번 셔플·행 순서는 검사함, 분류기 입력 덤프), RAG 전부(청구 기준 문서의 상세 사유마다 절 1개 포함),
         차트 본문의 근거 사실(소견 수치 등 — 9b 건별 검증과 함께)
  경고 — 문자, 통화(전사 — 7단계), RAG
결정성 2회 비교는 make verify가 아니라 tests/test_generator.py — canonical_hash로, 두 번은 서로 다른
PYTHONHASHSEED(0과 1)로 (같은 값이면 set 순회 순서에 기댄 버그가 가려짐)
검사 함수는 (conn, ctx) → 위반 행 목록(dict). 빈 목록이면 통과. 리포트는 (conn, ctx) → 수치 dict.
conn은 app.sqlite이고 truth.sqlite가 `truth` 스키마로 ATTACH되어 있다.
"""
import argparse
import hashlib
import json
import re
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

from generator.paths import output_paths
from generator.master import (ADJUST_REASONS, AGE_BANDS, ALERT_TYPES, CHART_REQUIREMENT, CONDITIONAL_INSURED_MIN_AGE,
                              EXTRACTION_CODES, FULL_MOUTH, IMPLANT_CODE, MAX_ALERTS_PER_PATIENT, MAX_LEAD_DAYS,
                              PERIO_LINK_DAYS, PREGNANCY_LEFT_DAYS, PROCEDURES, PROCEDURE_DIAGNOSES,
                              RETREATMENT_REQUIREMENT, SCALING_CODE, SCALING_MIN_AGE, SUBMITTED_MONTHS, VISIT_MIX,
                              add_years, age_band, age_on, daily_cap, insured_on, open_slots,
                              BOOKING_PURPOSES, CALL_COUNTERPART_SHARE, CALL_DURATION_SEC, CALL_FLAGS, CALL_NOISE_SHARE,
                              CALL_OUTBOUND_SHARE, CALL_PAIN_PURPOSES, CALL_PURPOSE_SHARE, CALL_SUBTOPICS,
                              EVAL_RARE_PURPOSES, EVAL_RARE_SHARE, EVAL_MIN_PER_SPLIT, HOURS, IMPLIED_FLAG,
                              SUBTOPIC_PURPOSE, URGENCIES)

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = 5

CHILD_MAX_AGE = next(hi for name, _, hi, _ in AGE_BANDS if name == "child")
PROCEDURE = {p["code"]: p for p in PROCEDURES}
REASON_BY_TEXT = {(r["category"], r["note"]): r for r in ADJUST_REASONS.values()}  # (adjust_code, adjust_note) → 사유

# 경고 허용치 (§5.3 경고 표)
VISIT_MIX_TOLERANCE = 0.04
PREP_SHEET_DAYS = 7  # 준비표 날짜 탭 = 최근 7일 + 오늘 (기능 정의 ④)
CHART_DECOY_RANGE = (1.0, 2.0)  # 디코이 / 특이 케이스 (§4.3 — 목표는 master.CHART_DECOY_RATIO)
CHART_FORBIDDEN_WORDS = ("청구 오류", "삭감", "조정")  # 차트 본문에 정답 어휘 금지 (§4.3 서술 규칙)


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


def load_ctx(config_path: Path, conn) -> Ctx:
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
    )


def _rows(conn, sql, *params) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params)]


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


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
    """submitted_at·sent_at·started_at·created_at·cancelled_at·registered_at·birth_date·recorded_at ≤ TODAY"""
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
        + _rows(conn, "SELECT 'emr_patient_alerts' AS tbl, id, recorded_at AS at FROM emr_patient_alerts"
                      " WHERE date(recorded_at) > ?", today)
    )


def check_last_month_claims_pending(conn, ctx):
    """TODAY 전월(11월) 이후 청구는 pending, 제출일 없음"""
    last_month = (ctx.today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    return _rows(conn, "SELECT id, claim_month, status, submitted_at FROM emr_claims"
                       " WHERE claim_month >= ? AND (status != 'pending' OR submitted_at IS NOT NULL)", last_month)


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
    return _rows(conn, "SELECT id, claim_month, status, paid_amount, adjust_code, adjust_note FROM emr_claims"
                       f" WHERE claim_month IN ({marks}) AND (status != 'submitted'"
                       " OR paid_amount IS NOT NULL OR adjust_code IS NOT NULL OR adjust_note IS NOT NULL)", *months)


def check_settled_claims(conn, ctx):
    """확정 월(제출 월 이전) 청구는 paid(금액 그대로·사유 없음) / adjusted(일부 지급·사유) / denied(0원·사유)"""
    first_submitted = min(_submitted_months(ctx))
    return _rows(conn, "SELECT id, claim_month, status, claim_amount, paid_amount, adjust_code, adjust_note FROM emr_claims"
                       " WHERE claim_month < ? AND NOT ("
                       " (status = 'paid' AND paid_amount = claim_amount AND adjust_code IS NULL AND adjust_note IS NULL)"
                       " OR (status = 'adjusted' AND paid_amount > 0 AND paid_amount < claim_amount AND adjust_code IS NOT NULL AND adjust_note IS NOT NULL)"
                       " OR (status = 'denied' AND paid_amount = 0 AND adjust_code IS NOT NULL AND adjust_note IS NOT NULL))", first_submitted)


# ── 충돌 ─────────────────────────────────────────────────────

def check_patient_same_day(conn, ctx):
    """같은 환자 같은 날 예약 중복 없음 (취소 제외)"""
    return _rows(conn, "SELECT patient_id, date(scheduled_at) AS day, group_concat(id) AS ids"
                       " FROM emr_appointments WHERE status != 'cancelled'"
                       " GROUP BY patient_id, day HAVING count(*) > 1")


# ── 예약 ────────────────────────────────────────────────────


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


def check_confirmed_reminded(conn, ctx):
    """confirmed는 리마인드 대상만 — 직전 진료일 전에 잡혔고(그날 잡은 예약은 대상 아님) 그날까지 취소되지 않은 예약"""
    bad = []
    for r in _rows(conn, "SELECT id, scheduled_at, created_at, cancelled_at FROM emr_appointments WHERE confirmed = 1"):
        prev = _prev_open_day(_dt(r["scheduled_at"]).date(), ctx.holidays)
        if _dt(r["created_at"]).date() >= prev or (r["cancelled_at"] and _dt(r["cancelled_at"]).date() <= prev):
            bad.append(r)
    return bad


def check_pre_window_visit(conn, ctx):
    """창 이전 내원(scheduled_at < range_start)은 환자당 completed 위생 1행, 등록일 < 내원일"""
    rows = _rows(conn, "SELECT a.id, a.patient_id, a.scheduled_at, a.type, a.status, p.registered_at"
                       " FROM emr_appointments a JOIN emr_patients p ON p.id = a.patient_id"
                       " WHERE date(a.scheduled_at) < ? ORDER BY a.patient_id, a.id", ctx.range_start.isoformat())
    bad, seen = [], set()
    for r in rows:
        if (r["type"], r["status"]) != ("hygiene", "completed") or r["patient_id"] in seen \
                or date.fromisoformat(r["registered_at"]) >= _dt(r["scheduled_at"]).date():
            bad.append(r)
        seen.add(r["patient_id"])
    return bad


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


def check_reschedule_pair(conn, ctx):
    """변경 쌍: 원래 예약은 cancelled, 새 예약 created_at = 원래 cancelled_at (변경 통화 시각의 근거)"""
    return _rows(conn, "SELECT n.id, n.rescheduled_from_id, o.status, o.cancelled_at, n.created_at"
                       " FROM emr_appointments n JOIN emr_appointments o ON o.id = n.rescheduled_from_id"
                       " WHERE o.status != 'cancelled' OR n.created_at IS NOT o.cancelled_at")


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
            or age_on(date.fromisoformat(r["birth_date"]), date.fromisoformat(r["service_date"])) < CONDITIONAL_INSURED_MIN_AGE]


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
    for r in _rows(conn, "SELECT t.id, t.procedure_code, t.performed_at, t.fee_total, t.insured_amount, t.patient_amount,"
                         " pt.birth_date FROM emr_treatments t JOIN emr_patients pt ON pt.id = t.patient_id"):
        p = PROCEDURE[r["procedure_code"]]
        insured = insured_on(p, age_on(date.fromisoformat(r["birth_date"]), _dt(r["performed_at"]).date()))
        expected = round(p["fee_total"] * (1 - p["patient_share_rate"])) if insured else 0
        if (r["fee_total"], r["insured_amount"], r["patient_amount"]) != (p["fee_total"], expected, p["fee_total"] - expected):
            bad.append({**r, "expected_insured": expected})
    return bad


def check_insured_claim_one_line(conn, ctx):
    """급여 completed 진료 ↔ 청구 정확히 1줄(환자·차트번호·코드·치아·금액 = 급여 금액·청구월 = 진료월 일치).
    창 안은 급여 진료 전부, 창 이전 내원은 TODAY와 같은 해 급여 스케일링만 paid 1줄. 그 밖의 진료는 청구 없음"""
    claims = defaultdict(list)
    for c in _rows(conn, "SELECT * FROM emr_claims"):
        claims[c["treatment_id"]].append(c)
    bad = []
    for t in _rows(conn, "SELECT t.id, t.patient_id, pt.chart_no, t.procedure_code, t.tooth_no, t.performed_at,"
                         " t.insured_amount, pt.birth_date FROM emr_treatments t JOIN emr_patients pt ON pt.id = t.patient_id"):
        served = _dt(t["performed_at"]).date()
        insured = insured_on(PROCEDURE[t["procedure_code"]], age_on(date.fromisoformat(t["birth_date"]), served))
        pre_window = served < ctx.range_start
        if pre_window:
            insured = insured and t["procedure_code"] == SCALING_CODE and served.year == ctx.today.year
        got = claims.pop(t["id"], [])
        if len(got) != insured:
            bad.append({"treatment_id": t["id"], "procedure_code": t["procedure_code"], "performed_at": t["performed_at"],
                        "claims": len(got), "expected": int(insured)})
            continue
        expected = {"patient_id": t["patient_id"], "chart_no": t["chart_no"], "procedure_code": t["procedure_code"],
                    "tooth_no": t["tooth_no"], "claim_amount": t["insured_amount"], "claim_month": served.strftime("%Y-%m")}
        if pre_window:
            expected["status"] = "paid"
        for c in got:
            mismatch = [k for k, v in expected.items() if c[k] != v]
            if mismatch:
                bad.append({"claim_id": c["id"], "treatment_id": t["id"], "mismatch": mismatch})
    return bad + [{"claim_id": c["id"], "treatment_id": tid, "mismatch": "진료 기록 없음"}
                  for tid, cs in claims.items() for c in cs]


def check_adjust_reason(conn, ctx):
    """조정·불능 줄의 사유(adjust_code·adjust_note)는 master.ADJUST_REASONS 상세 사유 하나와 범주 문자·서술이 일치하고
    그 시술에 맞음. 외부 이력 사유는 denied만"""
    return [r for r in _rows(conn, "SELECT id, procedure_code, status, adjust_code, adjust_note FROM emr_claims"
                                   " WHERE status IN ('adjusted', 'denied')")
            if (reason := REASON_BY_TEXT.get((r["adjust_code"], r["adjust_note"]))) is None
            or r["procedure_code"] not in reason["procedures"]
            or (reason["type"] == "external" and r["status"] != "denied")]


def check_extracted_tooth(conn, ctx):
    """치아 수명 — 발치한 치아에는 이후 임플란트 1차만, 임플란트 치아에는 이후 진료 없음, 임플란트 1차는 발치했거나
    장부에 이력이 없는 치아에만. 어기면 정답 목록에 없는 청구 오류가 장부에 생긴다"""
    bad = []
    extracted, implanted, history = defaultdict(set), defaultdict(set), defaultdict(set)
    for r in _rows(conn, "SELECT id, patient_id, procedure_code, tooth_no, performed_at FROM emr_treatments"
                         " ORDER BY patient_id, datetime(performed_at), id"):
        pid, tooth, code = r["patient_id"], r["tooth_no"], r["procedure_code"]
        if tooth == FULL_MOUTH:
            continue
        why = ("임플란트 치아" if tooth in implanted[pid]
               else "이력 있는 자연치에 임플란트" if code == IMPLANT_CODE and tooth in history[pid] - extracted[pid]
               else "발치한 치아" if code != IMPLANT_CODE and tooth in extracted[pid] else None)
        if why:
            bad.append({**r, "reason": why})
        history[pid].add(tooth)
        if code in EXTRACTION_CODES:
            extracted[pid].add(tooth)
        if code == IMPLANT_CODE:
            implanted[pid].add(tooth)
    return bad


def check_extra_claim_review_not_in_ledger(conn, ctx):
    """truth_claims의 extra_in_claim_review 대조 키가 emr_claims와 겹치지 않음"""
    return _rows(conn, "SELECT t.id, t.chart_no, t.service_date, t.procedure_code, t.tooth_no, c.id AS claim_id"
                       " FROM truth.truth_claims t JOIN emr_claims c USING (chart_no, service_date, procedure_code, tooth_no)"
                       " WHERE t.outcome = 'extra_in_claim_review'")


def check_truth_claims_match(conn, ctx):
    """9~10월 emr_claims마다 truth_claims 1행(claim_id·대조 키·금액 일치), 그 밖을 가리키는 truth 행 없음,
    extra_in_claim_review 줄도 9~10월 진료일. truth_claims는 심사결과 슬롯 착수 때 생성 — 비어 있으면 건너뜀"""
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
                        " WHERE t.outcome != 'extra_in_claim_review' AND c.id IS NULL", *months)
    extra_outside = _rows(conn, "SELECT id AS truth_id, service_date FROM truth.truth_claims"
                                f" WHERE outcome = 'extra_in_claim_review' AND substr(service_date, 1, 7) NOT IN ({marks})",
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


def check_payment_per_visit(conn, ctx):
    """창 안 completed 예약마다 visit 수납 정확히 1행, 창 이전 내원은 수납 없음, settlement는 amount_due 0"""
    bad = _rows(conn, "SELECT a.id, a.scheduled_at, count(p.id) AS visits FROM emr_appointments a"
                      " LEFT JOIN emr_payments p ON p.appointment_id = a.id AND p.kind = 'visit'"
                      " WHERE a.status = 'completed' GROUP BY a.id"
                      " HAVING (date(a.scheduled_at) >= ? AND visits != 1) OR (date(a.scheduled_at) < ? AND visits != 0)",
                ctx.range_start.isoformat(), ctx.range_start.isoformat())
    return bad + _rows(conn, "SELECT id, amount_due FROM emr_payments WHERE kind = 'settlement' AND amount_due != 0")


# ── 주의사항 ────────────────────────────────────────────────

def _alerts(conn) -> list[dict]:
    return _rows(conn, "SELECT al.*, p.birth_date, p.sex, p.registered_at FROM emr_patient_alerts al"
                       " JOIN emr_patients p ON p.id = al.patient_id ORDER BY al.id")


def check_alert_target(conn, ctx):
    """문진 주의사항은 master.ALERT_TYPES 대상 규칙(기록일 나이·성별) 중 하나에 맞음"""
    bad = []
    for r in _alerts(conn):
        rules = ALERT_TYPES.get(r["code"], {}).get("rules", [])
        age = age_on(date.fromisoformat(r["birth_date"]), date.fromisoformat(r["recorded_at"]))
        if r["source"] == "intake" and not any(lo <= age and (hi is None or age <= hi) and sex in (None, r["sex"])
                                               for lo, hi, sex, _ in rules):
            bad.append({**r, "age": age})
    return bad


def check_alert_source(conn, ctx):
    """출처가 master 정의와 같음. intake는 내원 없음, visit은 같은 환자의 창 안 completed 내원 중
    관련 시술이고 기록일 = 그 내원일"""
    appts = {r["id"]: r for r in _rows(conn, "SELECT id, patient_id, scheduled_at, status, planned_procedure_code"
                                             " FROM emr_appointments")}
    bad = []
    for r in _alerts(conn):
        a = ALERT_TYPES.get(r["code"])
        if a is None or a["source"] != r["source"]:
            bad.append({**r, "reason": "출처가 정의와 다름"})
        elif r["source"] == "intake" and r["appointment_id"] is not None:
            bad.append({**r, "reason": "문진에 내원 연결"})
        elif r["source"] == "visit":
            v = appts.get(r["appointment_id"])
            if v is None or v["patient_id"] != r["patient_id"] or v["status"] != "completed" \
                    or _dt(v["scheduled_at"]).date() < ctx.range_start or v["planned_procedure_code"] not in a["procedures"] \
                    or _dt(v["scheduled_at"]).date().isoformat() != r["recorded_at"]:
                bad.append({**r, "reason": "발견 내원이 맞지 않음", "appointment": v})
    return bad


def check_alert_recorded_at(conn, ctx):
    """기록일 ≥ 등록일. 문진 기록일 = 신환은 첫 completed 내원일(내원한 적 없으면 문진 없음),
    구환은 창 이전 마지막 completed 내원일(없으면 등록일)"""
    start = ctx.range_start.isoformat()
    pre = dict(conn.execute("SELECT patient_id, max(date(scheduled_at)) FROM emr_appointments"
                            " WHERE status = 'completed' AND date(scheduled_at) < ? GROUP BY patient_id", (start,)).fetchall())
    first = dict(conn.execute("SELECT patient_id, min(date(scheduled_at)) FROM emr_appointments"
                              " WHERE status = 'completed' GROUP BY patient_id").fetchall())
    bad = []
    for r in _alerts(conn):
        expected = first.get(r["patient_id"]) if r["registered_at"] >= start \
            else pre.get(r["patient_id"], r["registered_at"])
        if r["recorded_at"] < r["registered_at"] or (r["source"] == "intake" and r["recorded_at"] != expected):
            bad.append({**r, "expected": expected})
    return bad


def check_alert_ends(conn, ctx):
    """끝나는 날(ends_at): 임신은 기록일 + PREGNANCY_LEFT_DAYS 안, 협조 어려운 소아는 소아 구간을 벗어나는 생일,
    나머지는 없음"""
    lo, hi = PREGNANCY_LEFT_DAYS
    bad = []
    for r in _alerts(conn):
        if r["code"] == "PREGNANT":
            left = r["ends_at"] and (date.fromisoformat(r["ends_at"]) - date.fromisoformat(r["recorded_at"])).days
            ok = bool(left) and lo <= left <= hi
        elif r["code"] == "CHILD_COOP":
            ok = r["ends_at"] == add_years(date.fromisoformat(r["birth_date"]), CHILD_MAX_AGE + 1).isoformat()
        else:
            ok = r["ends_at"] is None
        if not ok:
            bad.append(r)
    return bad


def check_alerts_per_patient(conn, ctx):
    """환자당 주의사항 ≤ MAX_ALERTS_PER_PATIENT개"""
    return _rows(conn, "SELECT patient_id, count(*) AS n FROM emr_patient_alerts GROUP BY patient_id HAVING n > ?",
                 MAX_ALERTS_PER_PATIENT)


# ── 차트·청구 오류 (5b단계) ────────────────────────────────

def _month_bucket(ctx, month: str) -> str:
    """청구월 구간 — settled(확정) / submitted(제출 월) / pending(전월 이후)"""
    months = _submitted_months(ctx)
    return "pending" if month > max(months) else "submitted" if month >= min(months) else "settled"


def _chart_lines(conn, ctx) -> list[dict]:
    """진료마다 차트·차트 정답·청구·오류를 붙인 행. window = 창 안, cut = 확정 월 조정 사유(master 사유 dict)"""
    rows = _rows(conn, "SELECT t.id, t.patient_id, t.procedure_code, t.tooth_no, t.performed_at,"
                       " n.dx_code, n.note, pt.name AS patient_name,"
                       " tc.kind, tc.is_special, tc.requirement, tc.evidence_present, tc.is_decoy, tc.facts,"
                       " c.id AS claim_id, c.claim_month, c.status, c.adjust_code, c.adjust_note,"
                       " e.kind AS error, e.requirement AS error_requirement"
                       " FROM emr_treatments t JOIN emr_patients pt ON pt.id = t.patient_id"
                       " LEFT JOIN emr_chart_notes n ON n.treatment_id = t.id"
                       " LEFT JOIN truth.truth_chart_notes tc ON tc.treatment_id = t.id"
                       " LEFT JOIN emr_claims c ON c.treatment_id = t.id"
                       " LEFT JOIN truth.truth_claim_errors e ON e.treatment_id = t.id"
                       " ORDER BY t.patient_id, datetime(t.performed_at), t.id")
    for r in rows:
        r["day"] = _dt(r["performed_at"]).date()
        r["window"] = r["day"] >= ctx.range_start
        r["bucket"] = _month_bucket(ctx, r["claim_month"]) if r["claim_id"] else None
        r["cut"] = REASON_BY_TEXT.get((r["adjust_code"], r["adjust_note"])) if r["bucket"] == "settled" else None
    return rows


def _expected_requirement(rows: list[dict]) -> dict[int, str | None]:
    """진료 id → 요건 사유 키. 창 안 급여 줄만: 확정 월 요건 사유로 깎였으면 그 사유, 아니면 CHART_REQUIREMENT
    (잇몸치료는 직전 PERIO_LINK_DAYS일 안에 치석제거가 있으면 R02), 아말감/GI·신경치료는 같은 치아에 같은 시술을
    다시 할 때 R01 (같은 치아의 다른 시술 — 신경치료 뒤 충전 — 은 다음 단계 치료라 재시행이 아니다)"""
    expected, treated, scaled = {}, defaultdict(set), defaultdict(list)
    retreat_codes = ADJUST_REASONS[RETREATMENT_REQUIREMENT]["procedures"]
    key_of = {id(r): k for k, r in ADJUST_REASONS.items()}
    for r in rows:  # 환자·시각순
        key = None
        if r["window"] and r["claim_id"]:
            if r["cut"] is not None and r["cut"]["type"] == "requirement":
                key = key_of[id(r["cut"])]
            elif r["procedure_code"] in CHART_REQUIREMENT:
                key = CHART_REQUIREMENT[r["procedure_code"]]
                if key == "R03" and any(0 < (r["day"] - d).days <= PERIO_LINK_DAYS for d in scaled[r["patient_id"]]):
                    key = "R02"
            elif r["procedure_code"] in retreat_codes and (r["tooth_no"], r["procedure_code"]) in treated[r["patient_id"]]:
                key = RETREATMENT_REQUIREMENT
        expected[r["id"]] = key
        if r["tooth_no"] != FULL_MOUTH:
            treated[r["patient_id"]].add((r["tooth_no"], r["procedure_code"]))
        if r["procedure_code"] == SCALING_CODE:
            scaled[r["patient_id"]].append(r["day"])
    return expected


def check_chart_note_per_treatment(conn, ctx):
    """진료 1건당 emr_chart_notes·truth_chart_notes 정확히 1행 (emr 쪽은 UNIQUE, 남는 정답 행도 없음)"""
    return (_rows(conn, "SELECT t.id, n.id AS note_id, tc.treatment_id AS truth_id FROM emr_treatments t"
                        " LEFT JOIN emr_chart_notes n ON n.treatment_id = t.id"
                        " LEFT JOIN truth.truth_chart_notes tc ON tc.treatment_id = t.id"
                        " WHERE n.id IS NULL OR tc.treatment_id IS NULL")
            + _rows(conn, "SELECT tc.treatment_id FROM truth.truth_chart_notes tc"
                          " LEFT JOIN emr_treatments t ON t.id = tc.treatment_id WHERE t.id IS NULL"))


def check_chart_dx(conn, ctx):
    """차트 상병이 master.PROCEDURE_DIAGNOSES 허용 밖 ⇔ 상병 착오 오류 줄"""
    return [{k: r[k] for k in ("id", "procedure_code", "dx_code", "error")} for r in _chart_lines(conn, ctx)
            if r["dx_code"] is not None
            and (r["dx_code"] not in PROCEDURE_DIAGNOSES[r["procedure_code"]]) != (r["error"] == "diagnosis")]


def check_claim_errors(conn, ctx):
    """truth_claim_errors는 창 안 급여 청구 줄에만. 임상 요건형 오류 ⇔ 요건 줄의 근거 없음(요건 키 일치).
    확정 월은 심사 결과에서: 상병 사유로 깎인 줄 = 상병 착오, 요건 사유로 깎인 줄만 요건 오류 가능, 그 밖은 오류 없음"""
    bad = []
    for r in _chart_lines(conn, ctx):
        why = None
        if r["error"] and not (r["window"] and r["claim_id"]):
            why = "창 안 급여 청구 줄이 아님"
        elif (r["error"] == "requirement") != (r["evidence_present"] == 0):
            why = "요건 오류 ⇔ 근거 없음이 아님"
        elif r["error"] == "requirement" and r["error_requirement"] != r["requirement"]:
            why = "오류 요건 ≠ 차트 요건"
        elif r["bucket"] == "settled" and r["window"]:
            cut_type = r["cut"]["type"] if r["cut"] else None
            if (cut_type == "diagnosis") != (r["error"] == "diagnosis"):
                why = "상병 사유 조정 ⇔ 상병 착오가 아님"
            elif r["error"] == "requirement" and cut_type != "requirement":
                why = "요건 사유로 깎이지 않은 확정 월 요건 오류"
        if why:
            bad.append({"id": r["id"], "error": r["error"], "status": r["status"], "adjust_note": r["adjust_note"],
                        "reason": why})
    return bad


def check_chart_requirement(conn, ctx):
    """요건 줄(requirement)이 규칙대로 — _expected_requirement와 같고, 요건 줄만 evidence_present(0/1)를 가진다"""
    rows = _chart_lines(conn, ctx)
    expected = _expected_requirement(rows)
    return [{"id": r["id"], "procedure_code": r["procedure_code"], "requirement": r["requirement"],
             "expected": expected[r["id"]], "evidence_present": r["evidence_present"]}
            for r in rows if r["kind"] is not None and (r["requirement"] != expected[r["id"]]
                                                        or (r["requirement"] is None) != (r["evidence_present"] is None)
                                                        or r["evidence_present"] not in (None, 0, 1))]


def check_requirement_facts(conn, ctx):
    """요건 사실이 장부와 모순되지 않음 — 장부에 근거 사건이 있으면 그 날짜에서 계산한 값(other_clinic 아님),
    없으면 '타 기관' 이력(other_clinic). R01 = 같은 치아·같은 시술 이전 진료, R02 = 직전 PERIO_LINK_DAYS일 안
    치석제거, R06 = 같은 치아 발치(근거 있는 줄만)"""
    bad = []
    last, scaled, extracted = defaultdict(dict), defaultdict(list), defaultdict(dict)
    for r in _chart_lines(conn, ctx):  # 환자·시각순
        pid, day = r["patient_id"], r["day"]
        f = json.loads(r["facts"]).get("requirement") if r["facts"] else None
        if f:
            ledger, field = {
                "R01": (last[pid].get((r["tooth_no"], r["procedure_code"])), "prev_days"),
                "R02": (min([(day - d).days for d in scaled[pid] if 0 < (day - d).days <= PERIO_LINK_DAYS], default=None),
                        "scaling_days_ago"),
                "R06": (extracted[pid].get(r["tooth_no"]), "extracted_days_ago"),
            }.get(f["key"], (None, None))
            if isinstance(ledger, date):
                ledger = (day - ledger).days
            if field and not (f["key"] == "R06" and not f["evidence"]):
                ok = (not f.get("other_clinic") and f.get(field) == ledger) if ledger is not None else bool(f.get("other_clinic"))
                if not ok:
                    bad.append({"id": r["id"], "requirement": f, "ledger_days": ledger})
        if r["tooth_no"] != FULL_MOUTH:
            last[pid][(r["tooth_no"], r["procedure_code"])] = day
        if r["procedure_code"] == SCALING_CODE:
            scaled[pid].append(day)
        if r["procedure_code"] in EXTRACTION_CODES:
            extracted[pid][r["tooth_no"]] = day
    return bad


def check_chart_special(conn, ctx):
    """특이 ⇔ 오류 줄 ∪ 요건 줄, 서술 ⇔ 특이 ∪ 디코이, 특이와 디코이는 겹치지 않음, 창 이전 내원은 템플릿만"""
    bad = []
    for r in _chart_lines(conn, ctx):
        if r["kind"] is None:
            continue
        special = bool(r["error"]) or r["requirement"] is not None
        if bool(r["is_special"]) != special or (r["kind"] == "narrative") != bool(r["is_special"] or r["is_decoy"]) \
                or (r["is_special"] and r["is_decoy"]) or (not r["window"] and r["kind"] != "template"):
            bad.append({k: r[k] for k in ("id", "kind", "is_special", "is_decoy", "requirement", "error", "window")})
    return bad


def check_chart_text(conn, ctx):
    """차트 본문: 환자 이름·정답 어휘(CHART_FORBIDDEN_WORDS) 없음, 치아 단위 시술은 'NN번', 시술 이름이나 별칭 있음,
    그 내원에서 발견한 주의사항(visit 출처) 이름 있음(§2.2)"""
    names = {r["code"]: [r["name"], *json.loads(r["aliases"])] for r in _rows(conn, "SELECT code, name, aliases FROM emr_procedures")}
    discovered = defaultdict(list)
    for r in _rows(conn, "SELECT t.id, ty.label FROM emr_patient_alerts al JOIN emr_alert_types ty ON ty.code = al.code"
                         " JOIN emr_treatments t ON t.appointment_id = al.appointment_id WHERE al.source = 'visit'"):
        discovered[r["id"]].append(r["label"])
    bad = []
    for r in _chart_lines(conn, ctx):
        note = r["note"]
        if note is None:
            continue
        why = ("환자 이름" if r["patient_name"] in note
               else "정답 어휘" if any(w in note for w in CHART_FORBIDDEN_WORDS)
               else "치아 번호 없음" if r["tooth_no"] != FULL_MOUTH and f"{r['tooth_no']}번" not in note
               else "시술 이름 없음" if not any(n in note for n in names[r["procedure_code"]])
               else "발견한 주의사항 없음" if not all(label in note for label in discovered[r["id"]]) else None)
        if why:
            bad.append({"id": r["id"], "reason": why, "note": note[:60]})
    return bad


# emr_chart_notes 허용 컬럼 — 특이·디코이·요건 충족 컬럼은 정답 누출 (§5.3 누출)
CHART_NOTE_COLUMNS = {"id", "treatment_id", "dx_code", "note"}


def check_chart_note_columns(conn, ctx):
    """emr_chart_notes에 허용 컬럼만"""
    return [{"column": r["name"]} for r in conn.execute("PRAGMA main.table_info(emr_chart_notes)")
            if r["name"] not in CHART_NOTE_COLUMNS]


# ── 누출 ────────────────────────────────────────────────────

# shared/schema.sql의 ax_calls 컬럼(의미는 가상 데이터 생성 설계 §3). 이 밖의 컬럼(라벨·노이즈·카테고리 등)은 정답 누출
AX_CALLS_COLUMNS = {
    "id", "call_uid", "source", "patient_id", "caller_phone", "caller_name", "direction", "started_at",
    "duration_sec", "has_transcript", "audio_path", "transcript", "in_eval_pool", "eval_sort_order",
    "judge_status", "in_blind_pool", "blind_sort_order", "indexed_at",
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


# ── 통화 슬롯 (6단계, §4.1) ─────────────────────────────────

CALL_MIX_TOLERANCE = 0.02  # 목적·노이즈 분포 경고 허용치 (§5.3 경고 표)
OPEN_TIME = datetime.strptime(HOURS["open"], "%H:%M").time()
CLOSE_TIME = datetime.strptime(HOURS["weekday_close"], "%H:%M").time()
_SIDE = ("patient", "guardian")


def _calls(conn) -> list[dict]:
    """생성 통화 + 골드 라벨 (accepted·fact는 JSON 풀어서)"""
    rows = _rows(conn, "SELECT c.*, l.primary_purpose, l.accepted_purposes, l.counterpart, l.urgency,"
                       f" {', '.join('l.' + f for f in CALL_FLAGS)}, l.noise_type, l.used_fact, l.split"
                       " FROM ax_calls c JOIN truth.truth_call_labels l USING (call_uid) WHERE c.source = 'generated'"
                       " ORDER BY c.id")
    for r in rows:
        r["accepted"], r["fact"] = json.loads(r["accepted_purposes"]), json.loads(r["used_fact"])
    return rows


def check_call_labels_match(conn, ctx):
    """생성 통화 ↔ truth_call_labels 1:1 (call_uid)"""
    calls = {r[0] for r in conn.execute("SELECT call_uid FROM ax_calls WHERE source = 'generated'")}
    labels = {r[0] for r in conn.execute("SELECT call_uid FROM truth.truth_call_labels")}
    return ([{"call_uid": u, "problem": "라벨 없음"} for u in sorted(calls - labels)]
            + [{"call_uid": u, "problem": "통화 없음"} for u in sorted(labels - calls)])


def _label_problem(r) -> str | None:
    purpose = r["primary_purpose"]
    if purpose not in CALL_PURPOSE_SHARE or r["counterpart"] not in CALL_COUNTERPART_SHARE \
            or r["urgency"] not in URGENCIES or (r["noise_type"] is not None and r["noise_type"] not in CALL_NOISE_SHARE):
        return "enum 밖"
    implied = IMPLIED_FLAG.get(purpose)
    if implied and not r[implied]:
        return "주 목적이 함의하는 플래그 꺼짐"
    subs = [f for f in SUBTOPIC_PURPOSE if r[f] and f != implied]
    if r["accepted"][:1] != [purpose] or sorted(r["accepted"][1:]) != sorted(SUBTOPIC_PURPOSE[f] for f in subs):
        return "accepted_purposes ≠ [주 목적] + 부 주제 플래그"
    if len(subs) > 1 or not set(subs) <= set(CALL_SUBTOPICS[purpose]):
        return "부 주제가 CALL_SUBTOPICS 밖"
    if r["emergency"] and r["urgency"] != "urgent":
        return "응급인데 urgent 아님"
    if r["appt_confirmed"] and purpose not in ("book", "reschedule"):
        return "appt_confirmed는 book·reschedule만"
    if r["direction"] == "outbound" and purpose not in CALL_OUTBOUND_SHARE:
        return "outbound가 아닌 목적"
    noise = r["noise_type"]
    if (noise == "two_topics") != (r["direction"] == "inbound" and bool(subs)):
        return "inbound 복합 주제 ⇔ 노이즈 주제 2개"
    if noise == "guardian_confused" and r["counterpart"] != "guardian":
        return "보호자 노이즈인데 보호자 아님"
    if noise == "ambiguous_pain" and purpose not in CALL_PAIN_PURPOSES:
        return "애매한 통증 노이즈 대상 목적 아님"
    return None


def check_call_label_values(conn, ctx):
    """골드 라벨: enum, 함의 플래그, accepted = 주 목적 + 부 주제(CALL_SUBTOPICS 안 1개), 응급 → urgent,
    outbound 목적, inbound 복합 ⇔ 노이즈 '주제 2개', 노이즈 대상(보호자·통증 목적)"""
    return [{"call_uid": r["call_uid"], "problem": p} for r in _calls(conn) if (p := _label_problem(r))]


def check_call_slot_count(conn, ctx):
    """생성 통화 수 = calls_with_transcript"""
    n = conn.execute("SELECT count(*) FROM ax_calls WHERE source = 'generated'").fetchone()[0]
    return [] if n == ctx.call_slots else [{"calls": n, "expected": ctx.call_slots}]


def check_call_day(conn, ctx):
    """통화는 range_start ~ TODAY 전날의 진료일, 개원 ~ 평일 마감 (§2.6 사건 시각)"""
    bad = []
    for r in _rows(conn, "SELECT id, started_at FROM ax_calls WHERE source = 'generated'"):
        t = _dt(r["started_at"])
        if not (ctx.range_start <= t.date() < ctx.today and open_slots(t.weekday()) and t.date() not in ctx.holidays
                and OPEN_TIME <= t.time() < CLOSE_TIME):
            bad.append(r)
    return bad


def check_call_duration(conn, ctx):
    """duration_sec은 CALL_DURATION_SEC 안"""
    lo, hi = CALL_DURATION_SEC
    return _rows(conn, "SELECT id, duration_sec FROM ax_calls WHERE source = 'generated'"
                       " AND duration_sec NOT BETWEEN ? AND ?", lo, hi)


def check_call_row_order(conn, ctx):
    """행 순서(id) = 통화 시각순 — 생성 순서(목적별)가 id에 드러나지 않게"""
    rows = _rows(conn, "SELECT id, started_at FROM ax_calls WHERE source = 'generated' ORDER BY id")
    return [b for a, b in zip(rows, rows[1:]) if b["started_at"] < a["started_at"]]


def _booking_problem(r, a, origins, reminders, ctx) -> str | None:
    if a is None:
        return "appointment_id 없음"
    if a["patient_id"] != r["patient_id"]:
        return "예약 환자와 통화 환자 다름"
    if a["created_at"] < ctx.range_start.isoformat():
        return "창 이전 예약"
    t, purpose = r["started_at"], r["primary_purpose"]
    if purpose == "book" and not (a["booked_via"] == "phone" and a["rescheduled_from_id"] is None and t == a["created_at"]):
        return "예약 잡기 = 전화 예약(변경 새 예약 아님)의 created_at"
    if purpose == "reschedule" and not (a["rescheduled_from_id"] is not None and t == a["created_at"]):
        return "변경 = 변경 쌍 새 예약의 created_at"
    if purpose == "cancel" and not (a["status"] == "cancelled" and a["id"] not in origins and t == a["cancelled_at"]):
        return "취소 = 당일 취소의 cancelled_at"
    if purpose == "confirm":
        call = _dt(t)
        if not a["confirmed"] or call.date() != _prev_open_day(_dt(a["scheduled_at"]).date(), ctx.holidays) \
                or call.time() <= OPEN_TIME or any(s >= t for s in reminders.get(a["id"], [])):
            return "확인 = confirmed 예약의 리마인드 날 개원 뒤, 리마인드 문자 뒤"
    return None


def check_booking_call_event(conn, ctx):
    """예약류 통화 ↔ 예약 이벤트: 통화 시각 == 이벤트 시각(확인은 리마인드 날·문자 뒤), 같은 환자, 창 안 예약,
    이벤트 하나에 통화 최대 1건(= 목적별 이벤트 수 ≥ 통화 수)"""
    appts = {a["id"]: a for a in _rows(conn, "SELECT * FROM emr_appointments")}
    origins = {a["rescheduled_from_id"] for a in appts.values() if a["rescheduled_from_id"]}
    reminders = defaultdict(list)
    for m in _rows(conn, "SELECT related_appointment_id AS a, sent_at FROM emr_messages WHERE \"trigger\" = 'reminder'"):
        reminders[m["a"]].append(m["sent_at"])
    bad, used = [], defaultdict(list)
    for r in _calls(conn):
        if r["primary_purpose"] not in BOOKING_PURPOSES:
            continue
        aid = r["fact"].get("appointment_id")
        used[(r["primary_purpose"], aid)].append(r["call_uid"])
        if p := _booking_problem(r, appts.get(aid), origins, reminders, ctx):
            bad.append({"call_uid": r["call_uid"], "appointment_id": aid, "problem": p})
    return bad + [{"event": k, "calls": v, "problem": "이벤트 하나에 통화 여러 건"} for k, v in used.items() if len(v) > 1]


def _is_child(p: dict, day: date) -> bool:
    """보호자가 거는 환자 — 통화일에 소아이고 보호자 기록이 있음"""
    return p["guardian_patient_id"] is not None and age_on(date.fromisoformat(p["birth_date"]), day) <= CHILD_MAX_AGE


def check_call_counterpart(conn, ctx):
    """상대 ↔ 환자·발신자: 환자 측은 환자 연결·통화일 소아 ⇔ 보호자·번호/이름 = 말하는 사람(소아면 보호자), 처음 연락 예약은
    그날 등록한 환자, 그 밖의 처음 연락·보험사·업체·기타는 환자 없음·환자 번호 아님, outbound는 환자 측"""
    patients = {p["id"]: p for p in _rows(conn, "SELECT * FROM emr_patients")}
    phones = {p["phone"] for p in patients.values()}
    bad = []
    for r in _calls(conn):
        who, p = r["counterpart"], patients.get(r["patient_id"])
        day = _dt(r["started_at"]).date()
        if who in _SIDE or (who == "new_caller" and r["primary_purpose"] in BOOKING_PURPOSES):
            if p is None:
                problem = "환자 측인데 환자 없음"
            elif who == "new_caller" and (r["primary_purpose"] != "book" or p["registered_at"] != day.isoformat()):
                problem = "처음 연락 예약은 그날 등록한 환자의 예약 잡기만"
            elif r["primary_purpose"] == "book" and who != "new_caller" and p["registered_at"] == day.isoformat():
                problem = "그날 등록한 환자의 예약 잡기는 처음 연락"
            elif who != "new_caller" and (who == "guardian") != _is_child(p, day):
                problem = "통화일 소아 ⇔ 보호자"
            else:
                speaker = patients.get(p["guardian_patient_id"]) if _is_child(p, day) else p
                problem = None if speaker and (r["caller_phone"], r["caller_name"]) == (p["phone"], speaker["name"]) \
                    else "발신 번호·이름 ≠ 말하는 사람"
        elif r["patient_id"] is not None or r["caller_phone"] in phones:
            problem = "환자 측이 아닌데 환자 연결·환자 번호"
        else:
            problem = None
        if problem is None and r["direction"] == "outbound" and who not in _SIDE:
            problem = "outbound는 환자 측만"
        if problem:
            bad.append({"call_uid": r["call_uid"], "counterpart": who, "problem": problem})
    return bad


def _facts_at(conn, pid: int, t: str, birth: str) -> dict:
    """통화 시각 t 시점의 사실 — 생성기와 따로 계산 (§4.1 절차 2: created_at·performed_at·paid_at ≤ t)"""
    day = t[:10]
    visits = [{"date": v["performed_at"][:10], "procedure": PROCEDURE[v["procedure_code"]]["name"], "tooth": v["tooth_no"]}
              for v in _rows(conn, "SELECT performed_at, procedure_code, tooth_no FROM emr_treatments"
                                   " WHERE patient_id = ? AND performed_at <= ? ORDER BY performed_at DESC LIMIT 3", pid, t)]
    balance = conn.execute("SELECT coalesce(sum(amount_due - amount_paid), 0) FROM emr_payments"
                           " WHERE patient_id = ? AND paid_at <= ?", (pid, t)).fetchone()[0]
    nxt = conn.execute("SELECT id FROM emr_appointments WHERE patient_id = ? AND created_at <= ? AND scheduled_at > ?"
                       " AND (cancelled_at IS NULL OR cancelled_at > ?) ORDER BY scheduled_at LIMIT 1",
                       (pid, t, t, t)).fetchone()
    alerts = {r[0] for r in conn.execute(
        "SELECT t.label FROM emr_patient_alerts a JOIN emr_alert_types t USING (code) WHERE a.patient_id = ?"
        " AND a.recorded_at <= ? AND (a.ends_at IS NULL OR ? < a.ends_at)", (pid, day, day))}
    return {"age": age_on(date.fromisoformat(birth), date.fromisoformat(day)), "recent_visits": visits,
            "balance": balance, "next": nxt[0] if nxt else None, "alerts": alerts}


def check_call_facts_as_of(conn, ctx):
    """used_fact는 통화 시점 사실: 통화 날짜, 환자(통화 환자와 같음), 최근 진료 3건·잔액·다음 예약·보이는 주의사항이
    장부를 통화 시각까지만 읽은 값과 같음"""
    patients = {p["id"]: p for p in _rows(conn, "SELECT id, name, birth_date FROM emr_patients")}
    bad = []
    for r in _calls(conn):
        f, t = r["fact"], r["started_at"]
        if f.get("call_date") != t[:10]:
            bad.append({"call_uid": r["call_uid"], "problem": "call_date ≠ 통화 날짜"})
            continue
        if "patient" not in f:
            continue
        pid = f["patient"]["id"]
        if r["patient_id"] is not None and pid != r["patient_id"] or pid not in patients:
            bad.append({"call_uid": r["call_uid"], "problem": "사실의 환자 ≠ 통화 환자"})
            continue
        want = _facts_at(conn, pid, t, patients[pid]["birth_date"])
        nxt = f.get("next_appointment")
        got = {"age": f["patient"]["age"], "recent_visits": f["recent_visits"], "balance": f["balance"],
               "next": nxt["appointment_id"] if nxt else None, "alerts": set(f["alerts"])}
        if f["patient"]["name"] != patients[pid]["name"]:
            got["name"] = f["patient"]["name"]
        diff = sorted(k for k in got if got[k] != want.get(k))
        if diff:
            bad.append({"call_uid": r["call_uid"], "problem": f"통화 시점 장부와 다름: {diff}"})
    return bad


def check_eval_pool(conn, ctx):
    """판정 풀: 크기 = eval_pool_size, 순번 1..P, 앞 ⌈P/2⌉ = dev·나머지 test(라벨 split과 일치),
    풀만 judge_status pending, 비풀은 순번 없음·silver"""
    rows = _calls(conn)
    pool = [r for r in rows if r["in_eval_pool"]]
    bad = [] if len(pool) == ctx.eval_pool else [{"pool": len(pool), "expected": ctx.eval_pool}]
    if sorted(r["eval_sort_order"] or 0 for r in pool) != list(range(1, len(pool) + 1)):
        bad.append({"problem": "eval_sort_order가 1..P가 아님"})
    dev_n = (len(pool) + 1) // 2
    for r in rows:
        if r["in_eval_pool"]:
            want = ("dev" if (r["eval_sort_order"] or 0) <= dev_n else "test", "pending")
        else:
            want = ("silver", "none")
        if (r["split"], r["judge_status"]) != want or (not r["in_eval_pool"] and r["eval_sort_order"] is not None):
            bad.append({"call_uid": r["call_uid"], "split": r["split"], "judge_status": r["judge_status"],
                        "expected": want})
    return bad


def check_blind_pool(conn, ctx):
    """블라인드 풀: 판정 풀의 부분집합, 크기 = blind_pool_size, 순번 1..B, 비블라인드는 순번 없음"""
    rows = _rows(conn, "SELECT id, in_eval_pool, in_blind_pool, blind_sort_order FROM ax_calls WHERE source = 'generated'")
    blind = [r for r in rows if r["in_blind_pool"]]
    bad = [r for r in blind if not r["in_eval_pool"]] + [r for r in rows if not r["in_blind_pool"] and r["blind_sort_order"]]
    if len(blind) != ctx.blind_pool:
        bad.append({"blind": len(blind), "expected": ctx.blind_pool})
    if sorted(r["blind_sort_order"] or 0 for r in blind) != list(range(1, len(blind) + 1)):
        bad.append({"problem": "blind_sort_order가 1..B가 아님"})
    return bad


def check_eval_pool_strata(conn, ctx):
    """판정 풀 층화(평가 설계 §2.1, 카테고리 = 주 목적): 희귀 카테고리는 전사의 EVAL_RARE_SHARE(반올림),
    카테고리마다 dev·test 차이 ≤ 1, 전사가 충분한(≥ split당 최소 × 2) 일반 카테고리는 split마다 최소 건수 이상"""
    by = defaultdict(lambda: {"n": 0, "dev": 0, "test": 0})
    for r in _calls(conn):
        c = by[r["primary_purpose"]]
        c["n"] += 1
        if r["split"] in ("dev", "test"):
            c[r["split"]] += 1
    floor = 2 * EVAL_MIN_PER_SPLIT
    bad = []
    for purpose, c in sorted(by.items()):
        if abs(c["dev"] - c["test"]) > 1:
            bad.append({"purpose": purpose, **c, "problem": "dev·test 차이 > 1"})
        if purpose in EVAL_RARE_PURPOSES and c["dev"] + c["test"] != round(EVAL_RARE_SHARE * c["n"]):
            bad.append({"purpose": purpose, **c, "problem": "희귀 카테고리 과표집 비율"})
        if purpose not in EVAL_RARE_PURPOSES and c["n"] >= floor \
                and min(c["dev"], c["test"]) < EVAL_MIN_PER_SPLIT:
            bad.append({"purpose": purpose, **c, "problem": "split당 최소 건수 미달"})
    return bad


def _share(rows, key) -> dict:
    counts = defaultdict(int)
    for r in rows:
        counts[r[key]] += 1
    return {k: v / len(rows) for k, v in counts.items()} if rows else {}


def check_call_purpose_mix(conn, ctx):
    """통화 목적 분포가 §4.1 목표(CALL_PURPOSE_SHARE) ±CALL_MIX_TOLERANCE"""
    got = _share(_calls(conn), "primary_purpose")
    return [{"purpose": k, "share": round(got.get(k, 0), 3), "target": v} for k, v in CALL_PURPOSE_SHARE.items()
            if abs(got.get(k, 0) - v) > CALL_MIX_TOLERANCE]


def check_call_noise_mix(conn, ctx):
    """노이즈 분포가 §4.1 목표(CALL_NOISE_SHARE) ±CALL_MIX_TOLERANCE"""
    got = _share(_calls(conn), "noise_type")
    return [{"noise": k, "share": round(got.get(k, 0), 3), "target": v} for k, v in CALL_NOISE_SHARE.items()
            if abs(got.get(k, 0) - v) > CALL_MIX_TOLERANCE]


def report_call_mix(conn, ctx):
    """상대 분포(§4.1 근사 — 목표 아님)·outbound 비중·복합 주제 비중"""
    rows = _calls(conn)
    if not rows:
        return {}
    return {"counterpart": {k: round(v, 3) for k, v in sorted(_share(rows, "counterpart").items(), key=lambda kv: -kv[1])},
            "outbound": round(sum(r["direction"] == "outbound" for r in rows) / len(rows), 3),
            "multi_topic": round(sum(len(r["accepted"]) > 1 for r in rows) / len(rows), 3)}


# ── 가상 PII ────────────────────────────────────────────────

def check_fake_phone(conn, ctx):
    """환자 전화·통화 상대 번호는 010-0000-xxxx만"""
    fake = "'010-0000-[0-9][0-9][0-9][0-9]'"
    return (_rows(conn, f"SELECT 'emr_patients' AS tbl, id, phone FROM emr_patients WHERE phone NOT GLOB {fake}")
            + _rows(conn, "SELECT 'ax_calls' AS tbl, id, caller_phone AS phone FROM ax_calls"
                          f" WHERE caller_phone IS NOT NULL AND caller_phone NOT GLOB {fake}"))


# ── 경고: 분포 ──────────────────────────────────────────────


def report_daily_fill(conn, ctx):
    """하루 채움(비취소 예약 / 하루 상한) — range_start + MAX_LEAD_DAYS부터 TODAY 전날까지의 진료일
    (시작 직후는 미리 잡힌 예약이 없고, TODAY 이후는 덜 찬 게 정상)"""
    counts = defaultdict(int)
    for r in _rows(conn, "SELECT date(scheduled_at) AS d FROM emr_appointments WHERE status != 'cancelled'"):
        counts[date.fromisoformat(r["d"])] += 1
    fills, d = [], ctx.range_start + timedelta(days=MAX_LEAD_DAYS)
    while d <= min(ctx.range_end, ctx.today - timedelta(days=1)):
        if open_slots(d.weekday()) and d not in ctx.holidays:
            fills.append(counts[d] / daily_cap(ctx.visits_per_day, d.weekday()))
        d += timedelta(days=1)
    if not fills:
        return {"days": 0}
    return {"days": len(fills), "mean": round(sum(fills) / len(fills), 3), "min": round(min(fills), 3)}


def report_balance_holder_share(conn, ctx):
    """잔액 있음 비율 — 준비표 범위(최근 PREP_SHEET_DAYS일 + 오늘)의 비취소 예약(환자 × 날짜) 중 미수 칩이 켜지는 것.
    잔액 있음 = 그 예약일 개원 전 잔액 > 0 (정의 원본: 앱 설계 §3 has_unpaid_balance)"""
    since = ctx.today - timedelta(days=PREP_SHEET_DAYS)
    rows = _rows(conn, "SELECT DISTINCT patient_id, date(scheduled_at) AS d FROM emr_appointments"
                       " WHERE status != 'cancelled' AND date(scheduled_at) BETWEEN ? AND ?",
                 since.isoformat(), ctx.today.isoformat())
    if not rows:
        return {"n": 0}
    owing = sum(conn.execute("SELECT coalesce(sum(amount_due - amount_paid), 0) FROM emr_payments"
                             " WHERE patient_id = ? AND date(paid_at) < ?", (r["patient_id"], r["d"])).fetchone()[0] > 0
                for r in rows)
    return {"n": len(rows), "share": round(owing / len(rows), 3)}


def check_chart_decoy_ratio(conn, ctx):
    """디코이 / 특이 케이스(창 안)가 CHART_DECOY_RANGE 안 — 특이가 없으면 건너뜀"""
    specials, decoys = conn.execute("SELECT coalesce(sum(is_special), 0), coalesce(sum(is_decoy), 0)"
                                    " FROM truth.truth_chart_notes").fetchone()
    if not specials:
        return []
    ratio = decoys / specials
    lo, hi = CHART_DECOY_RANGE
    return [] if lo <= ratio <= hi else [{"specials": specials, "decoys": decoys, "ratio": round(ratio, 3)}]


def report_adjust_judgment(conn, ctx):
    """확정 월 깎인 줄(창 안)의 판정 분포 — 정당(오류 있음·외부 이력) / 다툴 만함(요건 사유·오류 없음) /
    보완 후 재청구(장부 밖 사유·오류 없음). 목표는 master.ADJUST_JUDGMENT_TARGET (생성 설계 §2.4)"""
    counts = defaultdict(int)
    for r in _chart_lines(conn, ctx):
        if r["cut"] is None or not r["window"]:
            continue
        judged = "justified" if r["error"] or r["cut"]["type"] == "external" \
            else "contestable" if r["cut"]["type"] == "requirement" else "resubmit"
        counts[judged] += 1
    n = sum(counts.values())
    if not n:
        return {"n": 0}
    return {"n": n, **{k: round(counts[k] / n, 3) for k in ("justified", "contestable", "resubmit")}}


def report_claim_errors(conn, ctx):
    """청구 오류 수 — 월 구간별(확정·제출·전월) + 전월 급여 줄 수(⑤-2 평가 모수)"""
    counts = defaultdict(int)
    for r in _chart_lines(conn, ctx):
        if r["bucket"] and r["window"]:
            counts[r["bucket"]] += bool(r["error"])
            counts["pending_lines"] += r["bucket"] == "pending"
    return {k: counts[k] for k in ("settled", "submitted", "pending", "pending_lines")}


def report_alert_holder_share(conn, ctx):
    """주의사항 1개 이상 보유 비율 — TODAY까지 등록한 환자 전체와 연령 구간별(TODAY 나이). 기록일과 무관하게 센다"""
    holders = {r[0] for r in conn.execute("SELECT DISTINCT patient_id FROM emr_patient_alerts")}
    by_band = defaultdict(list)
    for r in _rows(conn, "SELECT id, birth_date FROM emr_patients WHERE registered_at <= ?", ctx.today.isoformat()):
        by_band[age_band(age_on(date.fromisoformat(r["birth_date"]), ctx.today))].append(r["id"] in holders)
    flags = [f for fs in by_band.values() for f in fs]
    if not flags:
        return {"n": 0}
    return {"n": len(flags), "share": round(sum(flags) / len(flags), 3),
            **{band: round(sum(fs) / len(fs), 3) for band, fs in by_band.items()}}


def check_visit_mix(conn, ctx):
    """창 안 내원(completed) 유형 구성 master.VISIT_MIX ± VISIT_MIX_TOLERANCE — 창 이전 내원은 빼고"""
    counts = {r["type"]: r["n"] for r in _rows(conn, "SELECT type, count(*) AS n FROM emr_appointments"
                                                      " WHERE status = 'completed' AND date(scheduled_at) >= ?"
                                                      " GROUP BY type", ctx.range_start.isoformat())}
    total = sum(counts.values())
    if not total:
        return []
    return [{"type": t, "share": round(counts.get(t, 0) / total, 3), "target": target}
            for t, target in VISIT_MIX.items() if abs(counts.get(t, 0) / total - target) > VISIT_MIX_TOLERANCE]


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
        r["age"] = age_on(date.fromisoformat(r["birth_date"]), _dt(r["performed_at"]).date())
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
    check_settled_claims,
    check_patient_same_day,
    check_confirmed_not_future,
    check_confirmed_reminded,
    check_created_before_scheduled,
    check_cancel_time,
    check_cancelled_at_status,
    check_cancellation_kind,
    check_reschedule_pair,
    check_pre_window_visit,
    check_treatment_on_completed,
    check_completed_one_treatment,
    check_performed_at,
    check_service_date,
    check_uninsured_not_claimed,
    check_scaling_limit,
    check_extracted_tooth,
    check_treatment_amounts,
    check_insured_claim_one_line,
    check_adjust_reason,
    check_extra_claim_review_not_in_ledger,
    check_truth_claims_match,
    check_balance_non_negative,
    check_visit_payment_completed,
    check_payment_per_visit,
    check_alert_target,
    check_alert_source,
    check_alert_recorded_at,
    check_alert_ends,
    check_alerts_per_patient,
    check_chart_note_per_treatment,
    check_chart_dx,
    check_claim_errors,
    check_chart_requirement,
    check_requirement_facts,
    check_chart_special,
    check_chart_text,
    check_chart_note_columns,
    check_ax_calls_columns,
    check_call_uid_format,
    check_call_labels_match,
    check_call_label_values,
    check_call_slot_count,
    check_call_day,
    check_call_duration,
    check_call_row_order,
    check_booking_call_event,
    check_call_counterpart,
    check_call_facts_as_of,
    check_eval_pool,
    check_blind_pool,
    check_eval_pool_strata,
    check_fake_phone,
]
WARNINGS = [
    check_visit_mix,
    check_tooth_level,
    check_child_procedures,
    check_chart_decoy_ratio,
    check_call_purpose_mix,
    check_call_noise_mix,
]
REPORTS = [
    report_daily_fill,
    report_balance_holder_share,
    report_alert_holder_share,
    report_adjust_judgment,
    report_claim_errors,
    report_call_mix,
]


# ── 결정성 ──────────────────────────────────────────────────

# 실행할 때마다 바뀌는 컬럼 (§5.3 결정성: indexed_at 제외)
HASH_EXCLUDED = {("ax_calls", "indexed_at"), ("ax_chunk_vectors", "embedding")}


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
    # RAG 필수 검사는 9단계·앱 색인 코드와 함께 구현 — 그 전엔 건너뜀으로 표시(§5.1 파이프라인 10단계)
    # — 청크가 생겨도 검사 없이 통과한 것처럼 보이지 않게 항상 출력
    print("[SKIP] RAG — 미구현(9단계·앱 색인 코드 후)")
    for report in REPORTS:
        print(f"[INFO] {report.__name__}: {report(conn, ctx)}")
    return ok


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, help="경로를 직접 줄 때(테스트용). 기본은 generator/paths.py")
    parser.add_argument("--truth", type=Path)
    parser.add_argument("--config", type=Path, default=ROOT / "config" / "generator.yaml")
    args = parser.parse_args(argv)
    paths = output_paths()
    db, truth_db = args.db or paths["app"], args.truth or paths["truth"]
    for path in (db, truth_db):
        if not path.exists():
            print(f"DB 없음: {path} — make data 먼저")
            return 1
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("ATTACH DATABASE ? AS truth", (f"file:{truth_db}?mode=ro",))
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
