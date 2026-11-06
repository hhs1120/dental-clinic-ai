"""골드 점검 (평가 설계 §2.3, 골드 점검 설계) — fixture는 eval_db 머리말(판정기 v1: 통화 2 dev·3 test가 골드와 다름)."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from evaluation import gold_audit
from shared.llm import FakeGateway, JsonlLog
from tests.evaluation.eval_db import labels, make_db

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


class Fake(FakeGateway):
    """fail_uids의 통화는 실패, answers가 있으면 그 답을 돌려준다(통화 → {항목: 선택})"""

    def __init__(self, tmp_path, fail_uids=(), answers=None):
        super().__init__(JsonlLog(tmp_path / "llm.jsonl"), scope="runtime")
        self.fail_uids, self.answers, self.seen = set(fail_uids), answers or {}, []

    def complete(self, alias, **kw):
        msg = kw["messages"][0]["content"]
        uid = next(u for u in ("c-0002", "c-0004", "c-0005", "c-0001") if f"통화 {u[-1]}" in msg)
        self.seen.append((alias, uid))
        r = super().complete(alias, **kw)
        if uid in self.fail_uids:
            return replace(r, ok=False, data=None, error="실패")
        if uid in self.answers:
            return replace(r, data=self.answers[uid])
        return r


def test_arbitrate_writes_records(conn, tmp_path):
    p = tmp_path / "a.jsonl"
    gw = Fake(tmp_path)
    r = gold_audit.arbitrate(gw, conn, p, "v1")
    assert (r["done"], r["failed"]) == (1, 0)
    assert gw.seen == [("gold_audit", "c-0002")]
    [rec] = gold_audit.load(p)
    assert (rec["call_uid"], rec["judge_version"], rec["prompt_version"]) == ("c-0002", "v1", "v1")
    assert rec["transcript_sha"] == gold_audit.transcript_sha("환자: 통화 2")
    assert set(rec["items"]) == {"primary_purpose", "complaint"}
    pp = rec["items"]["primary_purpose"]
    assert (pp["gold"], pp["judge"], pp["human"]) == ("complaint", "book", None)
    assert pp["gold_side"] == gold_audit.gold_side("c-0002", "primary_purpose")
    assert all(v["arbiter"] in ("gold", "judge", "neither") for v in rec["items"].values())


def test_failed_calls_retry_only_missing(conn, tmp_path):
    p = tmp_path / "a.jsonl"
    _set_judge(conn, "c-0004", urgency="urgent")
    r = gold_audit.arbitrate(Fake(tmp_path, fail_uids={"c-0004"}), conn, p, "v1")
    assert (r["done"], r["failed"]) == (1, 1)
    assert [x["call_uid"] for x in gold_audit.load(p)] == ["c-0002"]
    gw = Fake(tmp_path)
    assert gold_audit.arbitrate(gw, conn, p, "v1")["done"] == 1
    assert gw.seen == [("gold_audit", "c-0004")]
    assert [x["call_uid"] for x in gold_audit.load(p)] == ["c-0002", "c-0004"]


def test_invalid_choice_is_failure(conn, tmp_path):
    p = tmp_path / "a.jsonl"
    bad = {"c-0002": {"primary_purpose": "가", "complaint": "둘 다 아님"}}
    r = gold_audit.arbitrate(Fake(tmp_path, answers=bad), conn, p, "v1")
    assert (r["done"], r["failed"]) == (0, 1)
    assert gold_audit.load(p) == []


def test_new_judge_version_gets_new_lines(conn, tmp_path):
    p = tmp_path / "a.jsonl"
    gold_audit.arbitrate(Fake(tmp_path), conn, p, "v1")
    for cid, uid in ((4, "c-0004"), (2, "c-0002")):
        conn.execute(
            "INSERT INTO ax_llm_calls (id, task, model_alias, model_actual, prompt_version, ok, created_at)"
            " VALUES (?, 'judge', 'judge', 'solar-mini4', 'v2', 1, '2026-12-02 09:00:00')",
            (300 + cid,),
        )
        conn.execute(
            "INSERT INTO ax_model_judgments (call_uid, model_alias, model_actual, prompt_version, status,"
            " labels, llm_call_id) VALUES (?, 'judge', 'solar-mini4', 'v2', 'ok', ?, ?)",
            (uid, json.dumps(labels("reschedule")), 300 + cid),
        )
    gw = Fake(tmp_path)
    assert gold_audit.arbitrate(gw, conn, p, "v2")["done"] == 2
    recs = gold_audit.load(p)
    assert [(x["call_uid"], x["judge_version"]) for x in recs] == [
        ("c-0002", "v1"),
        ("c-0002", "v2"),
        ("c-0004", "v2"),
    ]


def test_arbitrate_does_not_write_app_db(conn, tmp_path):
    before = conn.execute("SELECT COUNT(*) FROM ax_llm_calls").fetchone()[0]
    gold_audit.arbitrate(Fake(tmp_path), conn, tmp_path / "a.jsonl", "v1")
    assert conn.execute("SELECT COUNT(*) FROM ax_llm_calls").fetchone()[0] == before
    assert (tmp_path / "llm.jsonl").exists()
