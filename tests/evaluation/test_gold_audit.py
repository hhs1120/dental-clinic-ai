"""골드 점검 (평가 설계 §2.3, 골드 점검 설계) — fixture는 eval_db 머리말(판정기 v1: 통화 2 dev·3 test가 골드와 다름)."""

from pathlib import Path

import pytest

from evaluation import gold_audit
from tests.evaluation.eval_db import make_db

ROOT = Path(__file__).resolve().parents[2]
HIDDEN = ("골드", "gold", "정답", "판정기", "judge")


@pytest.fixture
def conn(tmp_path):
    return make_db(tmp_path)


def _set_judge(conn, uid, **values):
    for k, v in values.items():
        conn.execute(
            "UPDATE ax_model_judgments SET labels = json_set(labels, ?, ?) WHERE call_uid = ?",
            (f"$.{k}", v, uid),
        )


def _target():
    return {
        "call_uid": "c-0002",
        "direction": "inbound",
        "transcript": "직원: 네\n환자: 불만이에요",
        "transcript_sha": "x",
        "items": {
            "primary_purpose": {"gold": "complaint", "judge": "book"},
            "complaint": {"gold": 1, "judge": 0},
        },
    }


def test_targets_skip_test_pool(conn):
    """c-0003은 test 판정 풀 — 평가 설계 §1 원칙 2"""
    ts = gold_audit.targets(conn, "v1")
    assert [t["call_uid"] for t in ts] == ["c-0002"]
    assert ts[0]["items"] == {
        "primary_purpose": {"gold": "complaint", "judge": "book"},
        "complaint": {"gold": 1, "judge": 0},
    }


def test_targets_compare_urgency_as_urgent_or_not(conn):
    """골드 normal ↔ low는 대상이 아니고 normal ↔ urgent만 대상(엇갈림과 같은 규칙)"""
    _set_judge(conn, "c-0004", urgency="low")
    assert "c-0004" not in {t["call_uid"] for t in gold_audit.targets(conn, "v1")}
    _set_judge(conn, "c-0004", urgency="urgent")
    [t] = [t for t in gold_audit.targets(conn, "v1") if t["call_uid"] == "c-0004"]
    assert t["items"] == {"urgency": {"gold": "normal", "judge": "urgent"}}


def test_request_hides_gold():
    msg, schema = gold_audit.arbiter_request(_target())
    for word in HIDDEN:
        assert word not in msg
    assert "직원: 네" in msg
    assert schema["properties"]["complaint"]["enum"] == ["가", "나"]
    assert schema["properties"]["primary_purpose"]["enum"] == ["가", "나", "둘 다 아님"]
    assert sorted(schema["required"]) == ["complaint", "primary_purpose"]


def test_request_shows_urgency_as_two_values():
    t = dict(_target(), items={"urgency": {"gold": "low", "judge": "urgent"}})
    msg, schema = gold_audit.arbiter_request(t)
    assert "급하지 않음" in msg and "낮음" not in msg
    assert schema["properties"]["urgency"]["enum"] == ["가", "나"]


def test_sides_are_mixed():
    sides = {gold_audit.gold_side(f"c-{i:04d}", "has_todo") for i in range(20)}
    assert sides == {"가", "나"}
    assert gold_audit.gold_side("c-0001", "has_todo") == gold_audit.gold_side("c-0001", "has_todo")


def test_prompt_file_hides_gold():
    text = (ROOT / "prompts" / "gold_audit" / "v1.md").read_text(encoding="utf-8")
    for word in HIDDEN[:4]:
        assert word not in text


def test_decode_maps_back():
    t = _target()
    side = gold_audit.gold_side("c-0002", "primary_purpose")
    other = "나" if side == "가" else "가"
    out = gold_audit.decode(t, {"primary_purpose": side, "complaint": other})
    assert out == {"primary_purpose": "gold", "complaint": "judge"}
    out = gold_audit.decode(t, {"primary_purpose": "둘 다 아님", "complaint": side})
    assert out == {"primary_purpose": "neither", "complaint": "gold"}
    with pytest.raises(ValueError):
        gold_audit.decode(t, {"primary_purpose": side, "complaint": "둘 다 아님"})
    with pytest.raises(ValueError):
        gold_audit.decode(t, {"primary_purpose": side})


def test_sample_constant():
    from evaluation.metrics import GOLD_AUDIT_SAMPLE

    assert GOLD_AUDIT_SAMPLE == 50
