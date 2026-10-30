"""verify 차트·청구 오류 검사(5b단계)가 위반을 잡는지 — 정상 DB를 한 곳씩 깨뜨린다 (가상 데이터 생성 설계 §4.3·§5.3).
시드 진료: 1 신경치료 26(10/06, 제출 월, 템플릿) / 2 스케일링(11/03, 전월, 템플릿) / 3 소아 레진 16(11/18, 비급여, 서술 디코이)"""

import json
from dataclasses import replace
from datetime import date

import pytest

from generator import verify
from tests.generator.verify_db import CTX, make_db

# 확정 월(8월) 줄이 창 안에 있도록 — 실제 데이터는 창(2월~)이 확정 월을 포함한다
CTX_AUG = replace(CTX, range_start=date(2026, 8, 1))

CHART_CHECKS = [
    verify.check_chart_note_per_treatment,
    verify.check_chart_dx,
    verify.check_claim_errors,
    verify.check_chart_special,
    verify.check_chart_requirement,
    verify.check_chart_text,
    verify.check_chart_note_columns,
]


def test_valid_db_passes_chart_checks(tmp_path):
    conn = make_db(tmp_path)
    assert {c.__name__: c(conn, CTX) for c in CHART_CHECKS if c(conn, CTX)} == {}


def _line(
    conn,
    code,
    tooth,
    day,
    *,
    claim=None,
    dx,
    kind="template",
    special=0,
    requirement=None,
    evidence=None,
    decoy=0,
    note=None,
    fee=(120000, 84000, 36000),
):
    """1번 환자(1980년생 남) completed 내원 + 진료 + (claim이 있으면) 청구 + 차트 + 차트 정답 1줄. 진료 id를 돌려준다.
    claim = (status, paid_amount, adjust_code, adjust_note)"""
    conn.execute(
        "INSERT INTO emr_appointments (patient_id, staff_id, scheduled_at, duration_min, type,"
        " planned_procedure_code, status, booked_via, created_at) VALUES (1, 1, ?, 30, 'treatment', ?,"
        " 'completed', 'phone', '2026-07-01 10:00:00')",
        (f"{day} 10:00:00", code),
    )
    appt_id = conn.execute("SELECT max(id) FROM emr_appointments").fetchone()[0]
    conn.execute(
        "INSERT INTO emr_treatments (appointment_id, patient_id, procedure_code, tooth_no, performed_at,"
        " fee_total, insured_amount, patient_amount) VALUES (?, 1, ?, ?, ?, ?, ?, ?)",
        (appt_id, code, tooth, f"{day} 10:00:00", *fee),
    )
    tid = conn.execute("SELECT max(id) FROM emr_treatments").fetchone()[0]
    if claim:
        status, paid, adjust_code, adjust_note = claim
        conn.execute(
            "INSERT INTO emr_claims (treatment_id, patient_id, chart_no, tooth_no, claim_month, service_date,"
            " procedure_code, claim_amount, submitted_at, status, paid_amount, adjust_code, adjust_note)"
            " VALUES (?, 1, 'C-00001', ?, ?, ?, ?, ?, '2026-09-01 09:00:00', ?, ?, ?, ?)",
            (tid, tooth, day[:7], day, code, fee[1], status, paid, adjust_code, adjust_note),
        )
    conn.execute(
        "INSERT INTO emr_chart_notes (treatment_id, dx_code, note) VALUES (?, ?, ?)",
        (
            tid,
            dx,
            note
            or f"{tooth}번 치아 시술을 진행했습니다. 신경치료 아말감/GI 잇몸치료 사랑니 발치 실란트 스케일링",
        ),
    )
    conn.execute(
        "INSERT INTO truth.truth_chart_notes VALUES (?, ?, ?, ?, ?, ?, '{}')",
        (tid, kind, special, requirement, evidence, decoy),
    )
    return tid


R01 = ("C", "동일 부위 재시행 간격 기준 미충족")
C01 = ("B", "상병과 시행 행위 불일치")
D03 = ("K", "부위·산정 내역 기재 누락")


def _error(conn, tid, kind, requirement=None):
    conn.execute(
        "INSERT INTO truth.truth_claim_errors (treatment_id, kind, requirement) VALUES (?, ?, ?)",
        (tid, kind, requirement),
    )


# ── 정상 사례 (통과해야 함) ─────────────────────────────────


def test_settled_requirement_cut_with_error(tmp_path):
    """확정 월 요건 사유로 깎인 줄: 요건 = 그 사유, 오류면 근거 없음"""
    conn = make_db(tmp_path)
    tid = _line(
        conn,
        "U0006",
        "36",
        "2026-08-04",
        claim=("adjusted", 60000, *R01),
        dx="D01",
        kind="narrative",
        special=1,
        requirement="R01",
        evidence=0,
    )
    _error(conn, tid, "requirement", "R01")
    assert [c.__name__ for c in CHART_CHECKS if c(conn, CTX_AUG)] == []


def test_settled_requirement_cut_contestable(tmp_path):
    """요건 사유로 깎였지만 오류가 아닌 줄(다툴 만함): 근거 있음"""
    conn = make_db(tmp_path)
    _line(
        conn,
        "U0006",
        "36",
        "2026-08-04",
        claim=("denied", 0, *R01),
        dx="D01",
        kind="narrative",
        special=1,
        requirement="R01",
        evidence=1,
    )
    assert [c.__name__ for c in CHART_CHECKS if c(conn, CTX_AUG)] == []


def test_settled_diagnosis_cut_is_diagnosis_error(tmp_path):
    conn = make_db(tmp_path)
    tid = _line(
        conn,
        "U0001",
        "00",
        "2026-08-04",
        claim=("adjusted", 30000, *C01),
        dx="D05",
        kind="narrative",
        special=1,
        fee=(60000, 42000, 18000),
    )
    _error(conn, tid, "diagnosis")
    assert [c.__name__ for c in CHART_CHECKS if c(conn, CTX_AUG)] == []


def test_pending_requirement_error(tmp_path):
    """전월 요건 오류 줄 — 심사 결과와 무관하게 5b가 비율로 고른다. 11/03 스케일링 뒤 잇몸치료라 요건은 R02"""
    conn = make_db(tmp_path)
    tid = _line(
        conn,
        "U0018",
        "00",
        "2026-11-10",
        claim=("pending", None, None, None),
        dx="D05",
        kind="narrative",
        special=1,
        requirement="R02",
        evidence=0,
        fee=(35000, 24500, 10500),
    )
    conn.execute("UPDATE emr_claims SET submitted_at = NULL WHERE treatment_id = ?", (tid,))
    _error(conn, tid, "requirement", "R02")
    assert [c.__name__ for c in CHART_CHECKS if c(conn, CTX)] == []


def test_next_step_on_same_tooth_is_not_retreatment(tmp_path):
    """같은 치아라도 다른 시술(신경치료 26 → 아말감 26)은 다음 단계 치료라 요건 줄이 아니다"""
    conn = make_db(tmp_path)
    _line(
        conn,
        "U0006",
        "26",
        "2026-11-10",
        claim=("pending", None, None, None),
        dx="D01",
        fee=(25000, 17500, 7500),
    )
    assert [c.__name__ for c in CHART_CHECKS if c(conn, CTX)] == []


def test_retreatment_is_requirement_line(tmp_path):
    """아말감·신경치료는 같은 치아에 같은 시술을 다시 할 때 요건 줄(R01) — 시드 1번 신경치료 26 뒤 신경치료 26"""
    conn = make_db(tmp_path)
    tid = _line(conn, "U0008", "26", "2026-11-10", claim=("pending", None, None, None), dx="D02")
    assert verify.check_chart_requirement(conn, CTX)  # 템플릿·요건 없음 → 위반
    conn.execute(
        "UPDATE truth.truth_chart_notes SET kind = 'narrative', is_special = 1, requirement = 'R01',"
        " evidence_present = 1 WHERE treatment_id = ?",
        (tid,),
    )
    assert [c.__name__ for c in CHART_CHECKS if c(conn, CTX)] == []


# ── 위반 (잡아야 함) ────────────────────────────────────────

BREAKS = {
    "plan_not_open_item": (  # 2번 환자는 계획이 없다
        "UPDATE truth.truth_chart_notes SET facts = json_set(facts, '$.plan',"
        ' json(\'{"items": [{"procedure": "크라운", "tooth": "26"}]}\'))'
        " WHERE treatment_id = 3",
        "check_chart_plan",
    ),
    "evidence_word_without_evidence": (
        "UPDATE truth.truth_chart_notes SET is_special = 1, requirement = 'R01',"
        " evidence_present = 0, is_decoy = 0, facts = json_set(facts, '$.requirement',"
        ' json(\'{"key": "R01", "evidence": false, "prev_days": 30, "reason": null}\'))'
        " WHERE treatment_id = 3; UPDATE emr_chart_notes SET note = note || ' 충전물 파절'"
        " WHERE treatment_id = 3",
        "check_chart_evidence_words",
    ),
    # 장부에 같은 치아·같은 시술이 있는데 '타 기관' 이력 — 1번 환자의 10/06 신경치료 26번을 같은 코스의 3번 줄로 다시
    "r01_other_clinic_with_ledger": (
        "INSERT INTO truth.truth_appointment_courses VALUES (1, 9), (4, 9);"
        " UPDATE emr_treatments SET patient_id = 1, tooth_no = '26', procedure_code = 'U0008'"
        " WHERE id = 3; UPDATE truth.truth_chart_notes SET facts = json_set(facts, '$.requirement',"
        ' json(\'{"key": "R01", "evidence": true, "other_clinic": true,'
        ' "prev_months": 5, "reason": "2차 우식"}\')) WHERE treatment_id = 3',
        "check_requirement_facts",
    ),
    "chart_missing": (
        "DELETE FROM emr_chart_notes WHERE treatment_id = 1",
        "check_chart_note_per_treatment",
    ),
    "chart_truth_missing": (
        "DELETE FROM truth.truth_chart_notes WHERE treatment_id = 1",
        "check_chart_note_per_treatment",
    ),
    "dx_unknown": (
        "UPDATE emr_chart_notes SET dx_code = 'D99' WHERE treatment_id = 1",
        "check_foreign_keys",
    ),
    "dx_not_allowed_without_error": (
        "UPDATE emr_chart_notes SET dx_code = 'D06' WHERE treatment_id = 1",
        "check_chart_dx",
    ),
    "dx_error_but_allowed": (
        "INSERT INTO truth.truth_claim_errors (treatment_id, kind, requirement) VALUES (1, 'diagnosis', NULL)",
        "check_chart_dx",
    ),
    "error_on_uninsured": (
        "INSERT INTO truth.truth_claim_errors (treatment_id, kind, requirement) VALUES (3, 'diagnosis', NULL)",
        "check_claim_errors",
    ),
    "requirement_error_without_requirement": (
        "INSERT INTO truth.truth_claim_errors (treatment_id, kind, requirement) VALUES (2, 'requirement', 'R02')",
        "check_claim_errors",
    ),
    "unknown_requirement": (
        "UPDATE truth.truth_chart_notes SET requirement = 'R99', is_special = 1, kind = 'narrative',"
        " evidence_present = 1, is_decoy = 0 WHERE treatment_id = 1",
        "check_chart_requirement",
    ),
    "requirement_wrong_procedure": (
        "UPDATE truth.truth_chart_notes SET requirement = 'R04', is_special = 1,"
        " kind = 'narrative', evidence_present = 1, is_decoy = 0 WHERE treatment_id = 1",
        "check_chart_requirement",
    ),
    "requirement_without_evidence_flag": (
        "UPDATE truth.truth_chart_notes SET requirement = 'R01', is_special = 1,"
        " kind = 'narrative', is_decoy = 0 WHERE treatment_id = 1",
        "check_chart_requirement",
    ),
    "special_without_reason": (
        "UPDATE truth.truth_chart_notes SET is_special = 1, kind = 'narrative', is_decoy = 0"
        " WHERE treatment_id = 2",
        "check_chart_special",
    ),
    "special_and_decoy": (
        "UPDATE truth.truth_chart_notes SET is_special = 1 WHERE treatment_id = 3",
        "check_chart_special",
    ),
    "narrative_neither": (
        "UPDATE truth.truth_chart_notes SET is_decoy = 0 WHERE treatment_id = 3",
        "check_chart_special",
    ),
    "decoy_as_template": (
        "UPDATE truth.truth_chart_notes SET kind = 'template' WHERE treatment_id = 3",
        "check_chart_special",
    ),
    "note_with_patient_name": (
        "UPDATE emr_chart_notes SET note = note || ' 김하늘 환자' WHERE treatment_id = 3",
        "check_chart_text",
    ),
    "note_with_answer_word": (
        "UPDATE emr_chart_notes SET note = note || ' 삭감 우려' WHERE treatment_id = 3",
        "check_chart_text",
    ),
    "note_without_tooth": (
        "UPDATE emr_chart_notes SET note = '레진 충전을 했습니다.' WHERE treatment_id = 3",
        "check_chart_text",
    ),
    "note_with_alias_only": (
        "UPDATE emr_chart_notes SET note = '26번 치아 신경 치료를 진행했습니다. 마취가 잘 안 됨'"
        " WHERE treatment_id = 1",
        "check_chart_text",
    ),  # 별칭(신경 치료)만 — 차트는 표준 이름
    "note_without_procedure": (
        "UPDATE emr_chart_notes SET note = '16번 치아를 치료했습니다.' WHERE treatment_id = 3",
        "check_chart_text",
    ),
    "note_without_discovered_alert": (
        "UPDATE emr_chart_notes SET note = '26번 치아 신경치료를 진행했습니다.'"
        " WHERE treatment_id = 1",
        "check_chart_text",
    ),  # 그날 발견한 '마취가 잘 안 됨'
    "chart_leak_column": (
        "ALTER TABLE emr_chart_notes ADD COLUMN is_special INTEGER",
        "check_chart_note_columns",
    ),
}


@pytest.mark.parametrize("case", BREAKS)
def test_break_is_caught(tmp_path, case):
    sql, check = BREAKS[case]
    conn = make_db(tmp_path)
    conn.executescript(sql)  # 여러 문장인 경우가 있다
    assert getattr(verify, check)(conn, CTX), f"{check}가 {case}를 못 잡음"


def test_always_requirement_procedure_must_be_requirement_line(tmp_path):
    """잇몸치료·실란트·사랑니·급여 임플란트 창 안 줄은 항상 요건 줄"""
    conn = make_db(tmp_path)
    _line(
        conn,
        "U0018",
        "00",
        "2026-11-10",
        claim=("pending", None, None, None),
        dx="D05",
        fee=(35000, 24500, 10500),
    )
    assert verify.check_chart_requirement(conn, CTX)


def test_settled_diagnosis_cut_without_error(tmp_path):
    """확정 월 상병 사유로 깎인 줄은 전부 상병 착오"""
    conn = make_db(tmp_path)
    _line(
        conn,
        "U0001",
        "00",
        "2026-08-04",
        claim=("adjusted", 30000, *C01),
        dx="D06",
        fee=(60000, 42000, 18000),
    )
    assert verify.check_claim_errors(conn, CTX_AUG)


def test_settled_requirement_cut_other_reason(tmp_path):
    """확정 월 요건 사유로 깎인 줄은 요건 = 그 사유 (R02로 깎였는데 차트 요건이 R03)"""
    conn = make_db(tmp_path)
    _line(
        conn,
        "U0018",
        "00",
        "2026-08-04",
        claim=("adjusted", 20000, "C", "치석제거 후 후속 치주치료 확인 불가"),
        dx="D05",
        kind="narrative",
        special=1,
        requirement="R03",
        evidence=1,
        fee=(35000, 24500, 10500),
    )
    assert verify.check_chart_requirement(conn, CTX_AUG)


def test_settled_error_on_paid_line(tmp_path):
    """확정 월 오류는 깎인 줄에서만 정한다 — paid 줄의 오류는 생성기 버그"""
    conn = make_db(tmp_path)
    tid = _line(
        conn,
        "U0018",
        "00",
        "2026-08-04",
        claim=("paid", 24500, None, None),
        dx="D05",
        kind="narrative",
        special=1,
        requirement="R03",
        evidence=0,
        fee=(35000, 24500, 10500),
    )
    _error(conn, tid, "requirement", "R03")
    assert verify.check_claim_errors(conn, CTX_AUG)


def test_settled_documentation_cut_has_no_error(tmp_path):
    """장부 밖 사유로 깎인 줄은 오류가 아니다(보완 후 재청구)"""
    conn = make_db(tmp_path)
    tid = _line(
        conn,
        "U0001",
        "00",
        "2026-08-04",
        claim=("adjusted", 30000, *D03),
        dx="D05",
        kind="narrative",
        special=1,
        fee=(60000, 42000, 18000),
    )
    _error(conn, tid, "diagnosis")
    assert verify.check_claim_errors(conn, CTX_AUG)


def test_pre_window_chart_is_template(tmp_path):
    conn = make_db(tmp_path)
    tid = _line(
        conn,
        "U0001",
        "00",
        "2026-09-15",
        dx="D06",
        kind="narrative",
        decoy=1,
        fee=(60000, 42000, 18000),
    )
    assert verify.check_chart_special(conn, CTX)
    conn.execute(
        "UPDATE truth.truth_chart_notes SET kind = 'template', is_decoy = 0 WHERE treatment_id = ?",
        (tid,),
    )
    assert verify.check_chart_special(conn, CTX) == []


def test_decoy_ratio_warning(tmp_path):
    """디코이 / 특이 케이스가 verify.CHART_DECOY_RANGE 밖이면 경고. 특이가 없으면 건너뜀"""
    conn = make_db(tmp_path)
    assert verify.check_chart_decoy_ratio(conn, CTX) == []  # 특이 0 — 비율 없음
    _line(
        conn,
        "U0018",
        "00",
        "2026-11-10",
        claim=("pending", None, None, None),
        dx="D05",
        kind="narrative",
        special=1,
        requirement="R02",
        evidence=1,
        fee=(35000, 24500, 10500),
    )
    assert verify.check_chart_decoy_ratio(conn, CTX) == []  # 디코이 1 / 특이 1
    conn.execute(
        "UPDATE truth.truth_chart_notes SET kind = 'template', is_decoy = 0 WHERE treatment_id = 3"
    )
    assert verify.check_chart_decoy_ratio(conn, CTX)  # 0 / 1


def test_adjust_judgment_report(tmp_path):
    """확정 월 깎인 줄의 판정 분포 — 정당(오류 있음·외부 이력) / 다툴 만함(요건 사유·오류 없음) / 보완 후 재청구(장부 밖)"""
    conn = make_db(tmp_path)
    assert verify.report_adjust_judgment(conn, CTX_AUG) == {"n": 0}
    tid = _line(
        conn,
        "U0006",
        "36",
        "2026-08-04",
        claim=("adjusted", 60000, *R01),
        dx="D01",
        kind="narrative",
        special=1,
        requirement="R01",
        evidence=0,
    )
    _error(conn, tid, "requirement", "R01")
    _line(
        conn,
        "U0006",
        "37",
        "2026-08-05",
        claim=("adjusted", 60000, *R01),
        dx="D01",
        kind="narrative",
        special=1,
        requirement="R01",
        evidence=1,
    )
    _line(
        conn,
        "U0001",
        "00",
        "2026-08-06",
        claim=("adjusted", 30000, *D03),
        dx="D06",
        fee=(60000, 42000, 18000),
    )
    _line(
        conn,
        "U0001",
        "00",
        "2026-08-07",
        claim=("denied", 0, "C", "연 1회 산정 횟수 초과(타 기관 이력)"),
        dx="D06",
        fee=(60000, 42000, 18000),
    )
    assert verify.report_adjust_judgment(conn, CTX_AUG) == {
        "n": 4,
        "justified": 0.5,
        "contestable": 0.25,
        "resubmit": 0.25,
    }


def test_claim_error_report(tmp_path):
    """월 구간별 오류 수 — ⑤-2 평가 모수(전월) 확인용"""
    conn = make_db(tmp_path)
    _error(conn, 2, "diagnosis")
    conn.execute("UPDATE emr_chart_notes SET dx_code = 'D05' WHERE treatment_id = 2")
    assert verify.report_claim_errors(conn, CTX) == {
        "settled": 0,
        "submitted": 0,
        "pending": 1,
        "pending_lines": 1,
    }


# ── 요건 사실 ↔ 장부: 장부에 사건이 있으면 그 날짜로, 없으면 '타 기관' 이력으로 (§4.3) ──


def _facts(conn, tid, requirement: dict):
    conn.execute(
        "UPDATE truth.truth_chart_notes SET facts = ? WHERE treatment_id = ?",
        (json.dumps({"requirement": requirement}, ensure_ascii=False), tid),
    )


def _retreat(conn):
    """시드 1번 신경치료 26(10/06) 뒤 11/10 신경치료 26 — 장부상 35일 전"""
    return _line(
        conn,
        "U0008",
        "26",
        "2026-11-10",
        claim=("pending", None, None, None),
        dx="D02",
        kind="narrative",
        special=1,
        requirement="R01",
        evidence=1,
    )


def test_requirement_facts_match_ledger(tmp_path):
    conn = make_db(tmp_path)
    tid = _retreat(conn)
    _facts(conn, tid, {"key": "R01", "evidence": True, "prev_days": 35, "reason": "2차 우식"})
    assert verify.check_requirement_facts(conn, CTX) == []


@pytest.mark.parametrize(
    "facts",
    [
        {
            "key": "R01",
            "evidence": True,
            "prev_days": 300,
            "reason": "2차 우식",
        },  # 장부(35일 전)와 다름
        {
            "key": "R01",
            "evidence": True,
            "other_clinic": True,
            "prev_months": 6,
            "reason": "2차 우식",
        },  # 장부에 있는데 타 기관
    ],
)
def test_requirement_facts_contradict_ledger(tmp_path, facts):
    conn = make_db(tmp_path)
    _facts(conn, _retreat(conn), facts)
    assert verify.check_requirement_facts(conn, CTX)


def test_requirement_facts_other_clinic_only_without_ledger(tmp_path):
    """장부에 이전 사건이 없는 요건 줄(확정 월 사유로 정해진 줄)은 '타 기관' 이력만 — 장부 날짜를 지어내지 않는다"""
    conn = make_db(tmp_path)
    tid = _line(
        conn,
        "U0006",
        "36",
        "2026-08-04",
        claim=("adjusted", 60000, *R01),
        dx="D01",
        kind="narrative",
        special=1,
        requirement="R01",
        evidence=1,
    )
    _facts(
        conn,
        tid,
        {
            "key": "R01",
            "evidence": True,
            "other_clinic": True,
            "prev_months": 8,
            "reason": "2차 우식",
        },
    )
    assert verify.check_requirement_facts(conn, CTX_AUG) == []
    _facts(conn, tid, {"key": "R01", "evidence": True, "prev_days": 40, "reason": "2차 우식"})
    assert verify.check_requirement_facts(conn, CTX_AUG)


def test_r02_and_r06_facts_follow_ledger(tmp_path):
    """R02 = 직전 치석제거(시드 11/03 스케일링 → 11/10 잇몸치료 7일), R06 = 같은 치아 발치일"""
    conn = make_db(tmp_path)
    perio = _line(
        conn,
        "U0018",
        "00",
        "2026-11-10",
        claim=("pending", None, None, None),
        dx="D05",
        kind="narrative",
        special=1,
        requirement="R02",
        evidence=1,
        fee=(35000, 24500, 10500),
    )
    _facts(conn, perio, {"key": "R02", "evidence": True, "scaling_days_ago": 7, "linked": True})
    assert verify.check_requirement_facts(conn, CTX) == []
    _facts(
        conn,
        perio,
        {
            "key": "R02",
            "evidence": True,
            "other_clinic": True,
            "scaling_days_ago": 30,
            "linked": True,
        },
    )
    assert verify.check_requirement_facts(conn, CTX)
    _facts(conn, perio, {"key": "R02", "evidence": True, "scaling_days_ago": 7, "linked": True})
    conn.execute(
        "UPDATE emr_treatments SET procedure_code = 'U0011' WHERE id = 1"
    )  # 10/06 단순발치 26
    implant = _line(
        conn,
        "U0013",
        "26",
        "2026-11-17",
        claim=("pending", None, None, None),
        dx="D08",
        kind="narrative",
        special=1,
        requirement="R06",
        evidence=1,
        fee=(1300000, 910000, 390000),
    )
    _facts(conn, implant, {"key": "R06", "evidence": True, "extracted_days_ago": 42})
    assert verify.check_requirement_facts(conn, CTX) == []
    _facts(
        conn,
        implant,
        {"key": "R06", "evidence": True, "other_clinic": True, "edentulous_months": 6},
    )
    assert verify.check_requirement_facts(conn, CTX)


def test_narrative_facts_required(tmp_path):
    """서술 줄 본문에 꼭 넣을 사실(9b 건별 검증과 같은 기준)이 없으면 실패"""
    conn = make_db(tmp_path)
    assert verify.check_chart_narrative_facts in verify.REQUIRED
    assert verify.check_chart_narrative_facts(conn, CTX) == []
    conn.execute(
        "UPDATE emr_chart_notes SET note = '레진 충전을 했습니다.' WHERE treatment_id = 3"
    )
    assert verify.check_chart_narrative_facts(conn, CTX) == [
        {"treatment_id": 3, "missing": ["16번"]}
    ]


# 추가 처치 누락(§4.3): 시드 2번 진료(1번 환자 11/03 스케일링, 11월분 pending 청구)에 같은 날 36번 아말감/GI를 차트에만.
# 1번 환자 장부의 치아는 26번(신경치료)뿐
EXTRA = '{"extra": {"procedure": "아말감/GI", "tooth": "36"}}'


def _omitted(conn, code="U0006", tooth="36", tid=2, facts=EXTRA):
    conn.execute(
        "INSERT INTO truth.truth_claim_errors (treatment_id, kind, extra_procedure_code, extra_tooth_no)"
        " VALUES (?, 'omitted_extra', ?, ?)",
        (tid, code, tooth),
    )
    conn.execute(
        "UPDATE truth.truth_chart_notes SET facts = ? WHERE treatment_id = ?", (facts, tid)
    )


def test_omitted_extra_valid(tmp_path):
    conn = make_db(tmp_path)
    _omitted(conn)
    assert verify.check_omitted_extra(conn, CTX) == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"tooth": "26", "facts": EXTRA.replace("36", "26")},  # 장부에 있는 치아
        {
            "code": "U0004",
            "tooth": "36",
            "facts": EXTRA.replace("아말감/GI", "실란트"),
        },  # 성인 실란트는 비급여
        {"code": "U0005", "facts": EXTRA.replace("아말감/GI", "레진")},  # 추가 처치 시술 아님
        {"facts": EXTRA.replace("36", "37")},  # 차트 사실 ≠ 정답
        {"tid": 3, "facts": EXTRA},  # 비급여 레진 줄(청구 없음)
    ],
)
def test_omitted_extra_break(tmp_path, kwargs):
    conn = make_db(tmp_path)
    _omitted(conn, **kwargs)
    assert verify.check_omitted_extra(conn, CTX)


def test_extra_fact_without_error(tmp_path):
    conn = make_db(tmp_path)
    conn.execute("UPDATE truth.truth_chart_notes SET facts = ? WHERE treatment_id = 2", (EXTRA,))
    assert verify.check_omitted_extra(conn, CTX)


def test_chart_rules_recheck_saved_notes(tmp_path):
    """저장된 서술 차트에 9b 검증의 금지 꼴(chart.narrative_checks의 금지 패턴 — 메모체가 아닌 끝맺음 등)이 없다 (경고)"""
    conn = make_db(tmp_path)
    assert verify.check_chart_rules(conn, CTX) == []
    assert verify.check_chart_rules in verify.WARNINGS
    conn.execute(
        "UPDATE emr_chart_notes SET note = '16번 치아 레진 시행했습니다.' WHERE treatment_id = 3"
    )
    assert [r["treatment_id"] for r in verify.check_chart_rules(conn, CTX)] == [3]
