"""마스터 단일 원본이 문서(§1·§2.1)와 맞는지."""
from generator.master import PROCEDURES, SCALING_CODE, open_slots


def test_procedures():
    codes = [p["code"] for p in PROCEDURES]
    assert len(PROCEDURES) == 19 and len(set(codes)) == 19
    scaling = next(p for p in PROCEDURES if p["code"] == SCALING_CODE)
    assert scaling["name"] == "스케일링" and scaling["insured_limit"] == 1


def test_open_slots_match_doc():
    # §1: 평일 09:30–18:30 점심 1시간 → 16슬롯, 토 09:30–13:30 → 8슬롯, 목·일 휴진
    assert [open_slots(d) for d in range(7)] == [16, 16, 16, 0, 16, 8, 0]
