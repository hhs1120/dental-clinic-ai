"""블라인드 비교 — 사람 투표와 LLM 쌍 비교 (backend/blind.py, 평가 설계 §2.4, 통화·② 판정 화면 설계 "블라인드 비교")."""

import json
import sqlite3
from dataclasses import replace
from datetime import date

import pytest

from backend import blind, db
from backend.harness.store import SqliteLlmLog
from shared.clinic import Clinic
from shared.llm import FakeGateway
from tests.backend.call_db import classify_row
from tests.backend.conftest import SOURCE_SCHEMA

CLINIC = Clinic(today=date(2026, 12, 1), range_start=date(2026, 1, 1), eval_pool_size=4)
V2_TODO = [{"kind": "contact", "text": "회신 전화", "who": "환자", "after": -1}]


@pytest.fixture
def bconn(tmp_path):
    """판정 풀 4건(정렬 1~4, dev = 1·2) — 블라인드는 b-1(dev)·b-3(test)·b-2(dev, v2 없음)"""
    source = tmp_path / "source.sqlite"
    s = sqlite3.connect(source)
    s.executescript(SOURCE_SCHEMA.read_text(encoding="utf-8"))
    for uid, order, blind_ in (("b-1", 1, 1), ("b-2", 2, 1), ("b-3", 3, 1), ("b-4", 4, 0)):
        s.execute(
            "INSERT INTO ax_calls (call_uid, source, direction, started_at, duration_sec,"
            " has_transcript, transcript, in_eval_pool, eval_sort_order, in_blind_pool)"
            " VALUES (?, 'generated', 'inbound', '2026-11-28 10:00:00', 60, 1, ?, 1, ?, ?)",
            (uid, f"직원: 네\n환자: {uid} 문의", order, blind_),
        )
    s.commit()
    s.close()
    c = db.connect(tmp_path / "app.sqlite", source)
    db.init_schema(c)
    db.set_setting(c, "blind_versions", ["v1", "v2"])
    for uid in ("b-1", "b-2", "b-3", "b-4"):
        classify_row(c, uid, version="v1", todo=["원장님께 전달"], summary=f"{uid} v1 요약")
    for uid in ("b-1", "b-3", "b-4"):
        classify_row(c, uid, version="v2", todo=V2_TODO, summary=f"{uid} v2 요약")
    yield c
    c.close()


def test_next_pair_dev_only(bconn):
    """dev 블라인드 통화만, 두 버전 분류가 모두 있을 때 — b-3(test)·b-2(v2 없음)는 안 나온다"""
    p = blind.next_pair(bconn, CLINIC)
    assert p["call_uid"] == "b-1" and p["transcript"].startswith("직원")
    texts = {p["left"]["text"], p["right"]["text"]}
    assert texts == {"b-1 v1 요약 할 일: 원장님께 전달", "b-1 v2 요약 할 일: 회신 전화"}
    assert "version" not in json.dumps(p)
    out = blind.save_vote(
        bconn,
        p["left"]["classification_id"],
        p["right"]["classification_id"],
        "left",
        part="데스크",
        at="2026-11-05 10:00:00",
    )
    assert sorted(out.values()) == ["v1", "v2"]
    assert blind.next_pair(bconn, CLINIC) is None


def test_next_pair_none_without_setting(bconn):
    bconn.execute("DELETE FROM ax_settings WHERE key = 'blind_versions'")
    assert blind.next_pair(bconn, CLINIC) is None


def test_left_right_stable():
    t = "직원: 네\n환자: 문의"
    assert blind.first_on_left(t) == blind.first_on_left(t)
    share = sum(blind.first_on_left(f"통화 {i}") for i in range(1000)) / 1000
    assert 0.45 < share < 0.55


def test_summary_text_no_todo():
    assert blind.summary_text("요약", []) == "요약 할 일: 없음"


def test_combine_tie_when_disagree():
    assert blind.combine("left", "right") == "tie"
    assert blind.combine("left", "left") == "left"
    assert blind.combine("both_wrong", "both_wrong") == "both_wrong"
    assert blind.unswap("left") == "right" and blind.unswap("tie") == "tie"


def test_pair_request_inputs_only():
    """입력은 녹취록과 두 요약뿐 — 버전·판정기·골드 없음"""
    r = blind.pair_request("녹취록 본문", "요약 A", "요약 B", "v1")
    (m,) = r["messages"]
    assert "녹취록 본문" in m["content"] and "요약 A" in m["content"] and "요약 B" in m["content"]
    assert r["schema"]["properties"]["choice"]["enum"] == ["left", "right", "tie", "both_wrong"]
    assert r["system"]


def test_run_pairs_saves_and_skips_existing(bconn):
    gw = FakeGateway(SqliteLlmLog(bconn), scope="runtime")
    assert blind.run_pairs(bconn, gw, CLINIC, "v1", "v2", "v1") == {"done": 1, "failed": 0}
    row = bconn.execute("SELECT * FROM ax_model_blind_votes").fetchone()
    assert row["model_alias"] == "blind_judge" and row["prompt_version"] == "v1"
    assert row["choice"] == blind.combine(row["first_choice"], row["second_choice"])
    assert blind.run_pairs(bconn, gw, CLINIC, "v1", "v2", "v1") == {"done": 0, "failed": 0}


def test_run_pairs_failed_call_not_saved(bconn):
    class Bad(FakeGateway):
        def complete(self, alias, **kw):
            return replace(super().complete(alias, **kw), ok=False, data=None)

    gw = Bad(SqliteLlmLog(bconn), scope="runtime")
    assert blind.run_pairs(bconn, gw, CLINIC, "v1", "v2", "v1") == {"done": 0, "failed": 1}
    assert bconn.execute("SELECT COUNT(*) FROM ax_model_blind_votes").fetchone()[0] == 0


def test_run_pairs_uses_classifier_max_tokens(bconn):
    """Haiku 기본 생각 토큰도 상한 안 — 분류기와 같은 MAX_TOKENS(300이면 잘림)"""
    from backend import classify as cl

    seen = []

    class Rec(FakeGateway):
        def complete(self, alias, **kw):
            seen.append(kw["max_tokens"])
            return super().complete(alias, **kw)

    blind.run_pairs(bconn, Rec(SqliteLlmLog(bconn), scope="runtime"), CLINIC, "v1", "v2", "v1")
    assert seen == [cl.MAX_TOKENS, cl.MAX_TOKENS]
