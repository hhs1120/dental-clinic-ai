"""판정 큐 (backend/judge_queue.py, 앱 설계 §6 "판정 큐 다음 건")."""

import json

from backend import judge_queue as jq
from tests.backend.call_db import LABELS, judge_row


def _disagree(conn, uid, core):
    conn.execute(
        "INSERT INTO ax_call_disagreements VALUES (?, 'v2', 'v2', ?, 0, 0, ?)",
        (uid, int(core), json.dumps(["urgency"])),
    )


def _uids(conn):
    return dict(conn.execute("SELECT call_uid, state FROM ax_judge_state"))


def test_is_sample_share():
    share = sum(jq.is_sample(f"c-{i:04d}") for i in range(10000)) / 10000
    assert abs(share - jq.SAMPLE_SHARE) < 0.01


def test_refresh_targets_disagreements_and_sample(cconn, monkeypatch):
    """엇갈림 통화와 표본만 pending, 판정 풀 통화(c-0002)는 엇갈림이 있어도 빠진다"""
    monkeypatch.setattr(jq, "is_sample", lambda uid: uid == "c-0003")
    _disagree(cconn, "c-0001", True)
    _disagree(cconn, "c-0002", True)
    assert jq.refresh(cconn, "v2", "v2") == 2
    assert _uids(cconn) == {"c-0001": "pending", "c-0003": "pending"}
    assert jq.refresh(cconn, "v2", "v2") == 0


def test_next_order(cconn, monkeypatch):
    """핵심 엇갈림 → 나머지 엇갈림 → 표본"""
    monkeypatch.setattr(jq, "is_sample", lambda uid: uid == "c-0001")
    _disagree(cconn, "c-0003", False)
    _disagree(cconn, "c-0004", True)
    jq.refresh(cconn, "v2", "v2")
    seen = []
    while (n := jq.next_call(cconn, "2026-11-05")) is not None:
        seen.append(n["call_uid"])
        assert "labels" not in n and n["transcript"]
        jq.save(cconn, n["call_uid"], LABELS, None, part="데스크", at="2026-11-05 10:00:00")
    assert seen == ["c-0004", "c-0003", "c-0001"]


def test_save_reveals_models_and_marks_done(cconn):
    judge_row(cconn, "c-0001", LABELS | {"urgency": "urgent"})
    _disagree(cconn, "c-0001", True)
    jq.refresh(cconn, "v2", "v2")
    out = jq.save(cconn, "c-0001", LABELS, "애매", part="데스크", at="2026-11-05 10:00:00")
    assert out["classifier"]["urgency"] == "normal" and out["judge"]["urgency"] == "urgent"
    assert _uids(cconn)["c-0001"] == "done"
    row = cconn.execute("SELECT note, judged_by FROM ax_judgments").fetchone()
    assert tuple(row) == ("애매", "데스크")
    assert jq.next_call(cconn, "2026-11-05") is None


def test_skip_goes_last(cconn, monkeypatch):
    monkeypatch.setattr(jq, "is_sample", lambda uid: False)
    _disagree(cconn, "c-0001", True)
    _disagree(cconn, "c-0003", False)
    jq.refresh(cconn, "v2", "v2")
    jq.skip(cconn, "c-0001", "2026-11-05 10:00:00")
    assert jq.next_call(cconn, "2026-11-05")["call_uid"] == "c-0003"


def test_counts_and_empty_queue(cconn, monkeypatch):
    monkeypatch.setattr(jq, "is_sample", lambda uid: False)
    assert jq.next_call(cconn, "2026-11-05") is None
    _disagree(cconn, "c-0001", True)
    _disagree(cconn, "c-0003", True)
    jq.refresh(cconn, "v2", "v2")
    jq.save(cconn, "c-0001", LABELS, None, part="데스크", at="2026-11-04 10:00:00")
    n = jq.next_call(cconn, "2026-11-05")
    assert (n["left"], n["done_today"], n["done_total"]) == (1, 0, 1)
