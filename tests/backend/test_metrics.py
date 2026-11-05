"""정확도 표 집계 (backend/metrics.py, 기능 정의 §4 ② "정확도 표", 평가 설계 §2.4)."""

import pytest

from backend import metrics
from tests.backend.call_db import BLIND_CLINIC, LABELS, judge_row, make_blind_db


@pytest.fixture
def mconn(tmp_path):
    c = make_blind_db(tmp_path)
    yield c
    c.close()


def _ids(conn, uid):
    return dict(
        conn.execute(
            "SELECT prompt_version, id FROM ax_call_classifications WHERE call_uid = ?", (uid,)
        )
    )


def test_agreement_dev_pool_only(mconn):
    """dev 판정 풀(b-1·b-2)만 — b-3·b-4(test)는 빠진다. v2는 b-2 분류가 없어 n = 1"""
    judge_row(mconn, "b-1", LABELS | {"urgency": "low"})
    judge_row(mconn, "b-2", LABELS)
    judge_row(mconn, "b-3", LABELS | {"complaint": 1})
    a = metrics.agreement(mconn, BLIND_CLINIC, ["v1", "v2"], "v2")
    assert a["v1"]["urgency"] == {"k": 1, "n": 2}
    assert a["v1"]["complaint"] == {"k": 2, "n": 2}
    assert a["v2"]["primary_purpose"] == {"k": 1, "n": 1}


def _llm_vote(conn, left, right, first, second, choice):
    conn.execute(
        "INSERT INTO ax_llm_calls (task, model_alias, model_actual, ok, created_at)"
        " VALUES ('blind_judge', 'blind_judge', 'h', 1, '2026-11-05 09:00:00')"
    )
    log = conn.execute("SELECT max(id) FROM ax_llm_calls").fetchone()[0]
    conn.execute(
        "INSERT INTO ax_model_blind_votes (left_classification_id, right_classification_id,"
        " model_alias, model_actual, prompt_version, first_choice, second_choice, choice,"
        " first_llm_call_id, second_llm_call_id, created_at)"
        " VALUES (?, ?, 'blind_judge', 'h', 'v1', ?, ?, ?, ?, ?, '2026-11-05 09:00:00')",
        (left, right, first, second, choice, log, log),
    )


def test_blind_rates_maps_sides_to_versions(mconn):
    """b-1: 왼쪽 v2를 LLM·사람 모두 골랐다 → b_better 1, 일치 1/1. b-3(test) 투표는 빠진다"""
    b1, b3 = _ids(mconn, "b-1"), _ids(mconn, "b-3")
    _llm_vote(mconn, b1["v2"], b1["v1"], "left", "left", "left")
    _llm_vote(mconn, b3["v1"], b3["v2"], "left", "left", "left")
    mconn.execute(
        "INSERT INTO ax_blind_votes (left_classification_id, right_classification_id, choice,"
        " voted_by, voted_at) VALUES (?, ?, 'left', '데스크', '2026-11-05 10:00:00')",
        (b1["v2"], b1["v1"]),
    )
    r = metrics.blind_rates(mconn, BLIND_CLINIC, "v1", "v2")
    assert r["llm"] == {
        "b_better": 1,
        "a_better": 0,
        "tie": 0,
        "both_wrong": 0,
        "n": 1,
        "left": 1,
        "right": 0,
    }
    assert r["human"]["b_better"] == 1 and r["human"]["n"] == 1
    assert r["agree"] == {"k": 1, "n": 1}


def test_human_share(cconn, monkeypatch):
    """판정 풀 밖 대상(c-0001·c-0003·c-0004) 중 엇갈림 또는 표본인 몫"""
    from backend import judge_queue

    monkeypatch.setattr(judge_queue, "is_sample", lambda uid: uid == "c-0003")
    cconn.execute("INSERT INTO ax_call_disagreements VALUES ('c-0001', 'v2', 'v2', 1, 0, 0, '[]')")
    assert metrics.human_share(cconn, "v2", "v2") == {"k": 2, "n": 3}
