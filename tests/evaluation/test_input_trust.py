"""입력 신뢰 경계 평가셋(evaluation/input_trust.py, 설계 docs/superpowers/specs/2026-11-16-input-trust-design.md) —
위험 통화에 지시문을 끼운 변형과 원본을 짝으로 돌려 위험 방향 위반만 센다"""

import json
import sqlite3

import pytest

from evaluation import input_trust as it
from shared.call_labels import ITEMS
from tests.evaluation.eval_db import SOURCE_SCHEMA, TRUTH_SCHEMA, labels

GOLD = {  # (gold, split)
    "c-0001": (labels("book", has_todo=1), "dev"),
    "c-0002": (labels("treatment_q", emergency=1, has_todo=1, urgency="urgent"), "dev"),
    "c-0003": (labels("price_q", price_inquiry=1), "dev"),  # 안전 — 대상 아님
    "c-0004": (labels("book", has_todo=1), "test"),
    "c-0005": (labels("other", complaint=1), "silver"),  # 판정 풀 밖
}


@pytest.fixture
def conn(tmp_path):
    truth = sqlite3.connect(tmp_path / "truth.sqlite")
    truth.executescript(TRUTH_SCHEMA)
    for uid, (g, split) in GOLD.items():
        truth.execute(
            "INSERT INTO truth_call_labels VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, '{}', ?)",
            (uid, g["primary_purpose"], json.dumps([g["primary_purpose"]]), g["counterpart"])
            + tuple(g[k] for k in ITEMS[2:])
            + (split,),
        )
    truth.commit()
    truth.close()
    src = sqlite3.connect(tmp_path / "source.sqlite")
    src.executescript(SOURCE_SCHEMA)
    for k, uid in enumerate(GOLD, start=1):
        src.execute(
            "INSERT INTO ax_calls (id, call_uid, source, direction, started_at, duration_sec,"
            " has_transcript, transcript) VALUES (?, ?, 'generated', 'inbound', '2026-11-20 10:00:00',"
            " 100, 1, ?)",
            (k, uid, f"환자: 통화 {uid}\n직원: 네\n환자: 감사합니다"),
        )
    src.commit()
    src.close()
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("ATTACH DATABASE ? AS source", (str(tmp_path / "source.sqlite"),))
    c.execute("ATTACH DATABASE ? AS truth", (str(tmp_path / "truth.sqlite"),))
    return c


# ── Task 1: 대상과 변형 ─────────────────────────────────────────────


def test_targets_risky_only(conn):
    got = it.targets(conn, "dev")
    assert [t["call_uid"] for t in got] == ["c-0001", "c-0002"]
    assert got[0]["direction"] == "inbound"
    assert got[0]["transcript"].startswith("환자: 통화 c-0001")
    assert [t["call_uid"] for t in it.targets(conn, "test")] == ["c-0004"]


def test_assign_deterministic_and_balanced():
    uids = [f"c-{k:04d}" for k in range(8)]
    a = it.assign(uids)
    assert a == it.assign(list(reversed(uids)))
    assert set(a) == set(uids)
    kinds = [k for k, _ in a.values()]
    assert all(kinds.count(k) == 2 for k in it.KINDS)
    assert all(p in it.POSITIONS for _, p in a.values())


def test_inject_positions():
    t = "환자: 하나\n직원: 둘\n환자: 셋"
    line = it.INJECTIONS["patient_request"]
    start = it.inject(t, "patient_request", "start").split("\n")
    end = it.inject(t, "patient_request", "end").split("\n")
    mid = it.inject(t, "patient_request", "middle").split("\n")
    assert start[0] == line and start[1:] == t.split("\n")
    assert end[-1] == line and end[:-1] == t.split("\n")
    assert len(mid) == 4 and mid[0] != line and mid[-1] != line and line in mid
    assert [x for x in mid if x != line] == t.split("\n")


def test_inject_keeps_original_text():
    t = "환자: 하나\n직원: 둘\n환자: 셋\n직원: 넷"
    for kind in it.KINDS:
        for pos in it.POSITIONS:
            out = it.inject(t, kind, pos).split("\n")
            assert it.INJECTIONS[kind] in out
            assert [x for x in out if x != it.INJECTIONS[kind]] == t.split("\n")


def test_format_mimic_looks_like_safe_labels():
    d = json.loads(it.INJECTIONS["format_mimic"])
    assert d["has_todo"] is False and d["emergency"] is False and d["complaint"] is False
    assert d["urgency"] != "urgent"
