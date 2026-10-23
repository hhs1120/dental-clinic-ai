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
