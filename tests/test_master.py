"""마스터 단일 원본이 문서(§1·§2.1)와 맞는지."""
from generator.master import (AGE_BANDS, HYGIENE_SCALING_BANDS, PLANNED_WEIGHTS, PROCEDURES, SCALING_CODE,
                              daily_cap, open_slots)


def test_procedures():
    codes = [p["code"] for p in PROCEDURES]
    assert len(PROCEDURES) == 19 and len(set(codes)) == 19
    scaling = next(p for p in PROCEDURES if p["code"] == SCALING_CODE)
    assert scaling["name"] == "스케일링" and scaling["insured_limit"] == 1


def test_open_slots_match_doc():
    # §1: 평일 09:30–18:30 점심 1시간 → 16슬롯, 토 09:30–13:30 → 8슬롯, 목·일 휴진
    assert [open_slots(d) for d in range(7)] == [16, 16, 16, 0, 16, 8, 0]


def test_daily_cap_scales_with_slots():
    # 하루 상한은 슬롯 수에 비례 — 토요일(8슬롯)은 평일(16슬롯)의 절반, 내림
    assert [daily_cap(26, d) for d in range(7)] == [26, 26, 26, 0, 26, 13, 0]
    assert daily_cap(25, 5) == 12


def test_longest_procedure_fits_every_open_day():
    # schedule.pick_start는 그날 시작 시각이 하나 이상 있다고 가정 — 가장 짧은 연속 영업 구간(평일 오전 210분)보다 짧아야 한다
    assert max(p["duration_min"] for p in PROCEDURES) <= 210


def test_planned_weights_names_and_bands():
    names = {p["name"] for p in PROCEDURES}
    bands = {b for b, *_ in AGE_BANDS}
    for visit_type, by_band in PLANNED_WEIGHTS.items():
        expected = bands - set(HYGIENE_SCALING_BANDS) if visit_type == "hygiene" else bands
        assert set(by_band) == expected, visit_type  # 성인·노인 hygiene은 가중표가 아니라 규칙
        for weights in by_band.values():
            assert set(weights) <= names and sum(weights.values()) == 100


def test_adjust_reasons():
    # §2.4: 상세 사유마다 범주 문자·서술·유형·맞는 시술. 서술은 사유를 구별하므로 유일, 시술은 급여 청구가 생기는 것만
    from generator.master import ADJUST_REASONS, ADJUST_TYPE_WEIGHTS, REVIEW_OUTCOME_WEIGHTS, adjust_group
    insured = {p["code"] for p in PROCEDURES if p["is_insured"] in ("yes", "conditional")}
    assert len({r["note"] for r in ADJUST_REASONS.values()}) == len(ADJUST_REASONS)
    for key, r in ADJUST_REASONS.items():
        assert r["type"] in {"requirement", "documentation", "diagnosis", "external"}, key
        assert r["category"] and r["category"].isalnum() and r["category"].isupper(), key
        assert r["procedures"] and r["procedures"] <= insured, key
    external = [r for r in ADJUST_REASONS.values() if r["type"] == "external"]
    assert [r["procedures"] for r in external] == [{SCALING_CODE}]
    # 시술군이 뽑는 유형마다 그 시술에 맞는 사유가 하나 이상 (빈 유형 재정규화가 목표 비율을 흔들지 않게)
    for code in insured:
        group = adjust_group(code)
        for adjust_type, weight in ADJUST_TYPE_WEIGHTS[group].items():
            assert weight == 0 or any(r["type"] == adjust_type and code in r["procedures"]
                                      for r in ADJUST_REASONS.values()), (code, adjust_type)
    for weights in (*REVIEW_OUTCOME_WEIGHTS.values(), *ADJUST_TYPE_WEIGHTS.values()):
        assert sum(weights.values()) == 100
    assert set(REVIEW_OUTCOME_WEIGHTS) == set(ADJUST_TYPE_WEIGHTS) == {"clinical", "other"}


def test_insured_on():
    # §2.6 (4)·§2.1: 급여 = is_insured yes(적용 연령 안) 또는 conditional(65세↑). 스케일링은 19세↑, 실란트는 소아만
    from generator.master import insured_on
    by_name = {p["name"]: p for p in PROCEDURES}
    assert insured_on(by_name["스케일링"], 19) and not insured_on(by_name["스케일링"], 18)
    assert insured_on(by_name["실란트"], 12) and not insured_on(by_name["실란트"], 13)
    assert insured_on(by_name["임플란트 1차"], 65) and not insured_on(by_name["임플란트 1차"], 64)
    assert insured_on(by_name["신경치료"], 5) and not insured_on(by_name["레진"], 40)


def test_alert_types():
    # §2.2·기능 정의 §9: 주의사항 17개 — 문진(intake)은 대상 규칙(나이 범위·성별·비율), 진료 중 발견(visit)은 관련 시술 내원 비율
    from generator.master import ALERT_TYPES
    procedure_codes = {p["code"] for p in PROCEDURES}
    assert len(ALERT_TYPES) == 17
    assert len({a["label"] for a in ALERT_TYPES.values()}) == len(ALERT_TYPES)
    for code, a in ALERT_TYPES.items():
        assert a["category"] in {"systemic", "allergy", "procedure"}, code
        assert a["source"] in {"intake", "visit"}, code
        assert a["action"] and a["procedures"] and a["procedures"] <= procedure_codes, code
        if a["source"] == "intake":
            assert a["rules"] and "rate" not in a, code
            for lo, hi, sex, rate in a["rules"]:
                assert 0 <= lo and (hi is None or lo <= hi) and sex in (None, "M", "F") and 0 < rate < 1, code
        else:
            assert not a["rules"] and 0 < a["rate"] < 1, code
    assert sorted(c for c, a in ALERT_TYPES.items() if a["source"] == "visit") == ["ANES_FAIL", "GAG", "SYNCOPE"]
