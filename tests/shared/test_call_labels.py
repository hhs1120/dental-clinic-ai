"""라벨 비교 (shared/call_labels.py, 평가 설계 §2.5)."""

from shared.call_labels import differing_items

BASE = {
    "primary_purpose": "book",
    "counterpart": "patient",
    "urgency": "normal",
    "emergency": 0,
    "complaint": 0,
    "price_inquiry": 0,
    "wants_appt": 1,
    "appt_confirmed": 1,
    "has_todo": 0,
}


def test_urgency_compared_as_urgent_or_not():
    """급함은 urgent 여부만 비교한다 — normal/low는 골드가 추첨이라 같은 것으로 본다"""
    assert differing_items(BASE, BASE | {"urgency": "low"}) == []
    assert differing_items(BASE | {"urgency": "low"}, BASE | {"urgency": "urgent"}) == ["urgency"]


def test_other_items_compared_as_is():
    assert differing_items(BASE, BASE | {"complaint": 1, "counterpart": "guardian"}) == [
        "counterpart",
        "complaint",
    ]
    assert differing_items(BASE | {"has_todo": True}, BASE | {"has_todo": 1}) == []
