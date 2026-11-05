"""shared/call_labels.py의 라벨 enum이 원본(generator/master.py)과 같은지 — backend가 generator를 import하지
못해 복사해 둔 값이다(분류기 설계 "분류기")."""

from generator import master
from shared import call_labels


def test_label_enums_match_master():
    assert call_labels.PURPOSES == tuple(master.CALL_PURPOSE_SHARE)
    assert call_labels.COUNTERPARTS == master.CALL_COUNTERPARTS
    assert call_labels.URGENCIES == master.URGENCIES
    assert call_labels.FLAGS == master.CALL_FLAGS


def test_label_schema_items():
    assert call_labels.LABEL_SCHEMA["required"] == list(call_labels.ITEMS)
    assert len(call_labels.ITEMS) == 9
    assert set(call_labels.CORE_ITEMS) <= set(call_labels.ITEMS)


def test_ko_names_cover_enums():
    assert set(call_labels.PURPOSE_KO) == set(call_labels.PURPOSES)
    assert set(call_labels.COUNTERPART_KO) == set(call_labels.COUNTERPARTS)
    assert set(call_labels.URGENCY_KO) == set(call_labels.URGENCIES)
    assert set(call_labels.FLAG_KO) == set(call_labels.FLAGS)
