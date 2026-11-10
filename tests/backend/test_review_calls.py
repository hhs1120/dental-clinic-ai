"""확인할 통화 — 사람이 일이 남았다고 표시했는데 오늘 할 일에 안 뜨는 통화와 확인함
(backend/calls.py, 기능 정의 §4 오늘 화면·①)."""

import json

import pytest

from backend import calls
from tests.backend.call_db import LABELS, TODAY, classify_row

OFF = LABELS | {"has_todo": 0}


def _reclassify(conn, uid, **kw):
    """활성 분류 행을 지우고 다시 넣는다"""
    conn.execute("DELETE FROM ax_call_classifications WHERE call_uid = ?", (uid,))
    return classify_row(conn, uid, **kw)


def judge_human(conn, uid, at="2026-11-10T09:00:00", **on):
    conn.execute(
        "INSERT INTO ax_judgments (call_uid, labels, judged_by, judged_at) VALUES (?, ?, '데스크', ?)"
        " ON CONFLICT (call_uid) DO UPDATE SET labels = excluded.labels,"
        " judged_at = excluded.judged_at",
        (uid, json.dumps(OFF | on), at),
    )
    conn.commit()


def reopened(conn, uid, at="2026-11-10 09:00:00"):
    conn.execute(
        "INSERT INTO ax_auto_actions (kind, target, result, reason, reverted_by, reverted_at,"
        " created_at) VALUES ('call_autoclose', ?, ?, '이유', '데스크', ?, '2026-11-09 03:00:00')",
        (uid, json.dumps({"versions": ["v2", "v2"]}), at),
    )
    conn.commit()


def _brief(r):
    return [(x["call_uid"], x["ai"], x["reopened"], x["flags"]) for x in r]


def test_judged_flag_on_empty_todo_is_review(cconn):
    _reclassify(cconn, "c-0001", todo=[])
    judge_human(cconn, "c-0001", emergency=1)
    r = calls.review_calls(cconn, TODAY)
    assert _brief(r) == [("c-0001", "none", False, ["emergency"])]
    assert r[0]["name"] == "김철수" and r[0]["at"] == "2026-11-28 10:00:00"


@pytest.mark.parametrize(
    "flag, on",
    [
        ("emergency", {"emergency": 1}),
        ("urgent", {"urgency": "urgent"}),
        ("complaint", {"complaint": 1}),
        ("has_todo", {"has_todo": 1}),
    ],
)
def test_each_human_flag(cconn, flag, on):
    _reclassify(cconn, "c-0001", todo=[])
    judge_human(cconn, "c-0001", **on)
    assert [x["flags"] for x in calls.review_calls(cconn, TODAY)] == [[flag]]


def test_flags_in_severity_order(cconn):
    _reclassify(cconn, "c-0001", todo=[])
    judge_human(cconn, "c-0001", has_todo=1, complaint=1, emergency=1, urgency="urgent")
    assert calls.review_calls(cconn, TODAY)[0]["flags"] == [
        "emergency",
        "urgent",
        "complaint",
        "has_todo",
    ]


def test_judgment_without_flag_is_not_review(cconn):
    _reclassify(cconn, "c-0001", todo=[])
    judge_human(cconn, "c-0001")
    assert calls.review_calls(cconn, TODAY) == []


def test_reopened_autoclose_is_review(cconn):
    _reclassify(cconn, "c-0001", todo=[])
    reopened(cconn, "c-0001")
    assert _brief(calls.review_calls(cconn, TODAY)) == [("c-0001", "closed", True, [])]


def test_reopened_and_judged_one_row(cconn):
    """둘 다 해당하면 한 줄 — 사람 판정 항목과 다시 열림을 같이 싣는다"""
    _reclassify(cconn, "c-0001", todo=[])
    reopened(cconn, "c-0001")
    judge_human(cconn, "c-0001", complaint=1)
    assert _brief(calls.review_calls(cconn, TODAY)) == [("c-0001", "closed", True, ["complaint"])]


def test_old_call_with_todo_and_flag_is_review(cconn):
    """7일 창 밖이라 오늘 할 일에 안 뜨는 통화 — 날짜 제한 없음"""
    judge_human(cconn, "c-0003", emergency=1)
    assert _brief(calls.review_calls(cconn, TODAY)) == [
        ("c-0003", "old_todo", False, ["emergency"])
    ]


def test_on_board_call_is_not_review(cconn):
    judge_human(cconn, "c-0001", emergency=1)
    assert calls.review_calls(cconn, TODAY) == []


def test_pool_call_is_not_review(cconn):
    _reclassify(cconn, "c-0002", todo=[])
    judge_human(cconn, "c-0002", emergency=1)
    assert calls.review_calls(cconn, TODAY) == []


def test_review_skips_failed_classification(cconn):
    """분류가 ok가 아니면 확인할 통화가 아니라 "AI가 한 일"의 분류 실패 줄이 맡는다"""
    _reclassify(cconn, "c-0001", status="failed")
    judge_human(cconn, "c-0001", emergency=1)
    reopened(cconn, "c-0001")
    assert calls.review_calls(cconn, TODAY) == []


def test_order_latest_call_first(cconn):
    _reclassify(cconn, "c-0001", todo=[])
    judge_human(cconn, "c-0001", emergency=1)
    judge_human(cconn, "c-0003", complaint=1)
    _reclassify(cconn, "c-0004", todo=[])
    reopened(cconn, "c-0004")
    assert [x["call_uid"] for x in calls.review_calls(cconn, TODAY)] == [
        "c-0004",
        "c-0001",
        "c-0003",
    ]


def test_check_removes_from_review(cconn):
    _reclassify(cconn, "c-0001", todo=[])
    judge_human(cconn, "c-0001", emergency=1)
    assert calls.check_call(cconn, "c-0001", today=TODAY, by="데스크", at="2026-11-10 10:00:00")
    cconn.commit()
    assert calls.review_calls(cconn, TODAY) == []
    assert not calls.check_call(
        cconn, "c-0004", today=TODAY, by="데스크", at="2026-11-10 10:00:00"
    )
    assert not calls.check_call(
        cconn, "c-0001", today=TODAY, by="데스크", at="2026-11-10 11:00:00"
    )  # 이미 확인


def test_check_then_rejudge_shows_again(cconn):
    _reclassify(cconn, "c-0001", todo=[])
    judge_human(cconn, "c-0001", emergency=1)
    calls.check_call(cconn, "c-0001", today=TODAY, by="데스크", at="2026-11-10 10:00:00")
    judge_human(cconn, "c-0001", at="2026-11-10T11:00:00", complaint=1)  # 형식이 섞여도 시각으로
    assert _brief(calls.review_calls(cconn, TODAY)) == [("c-0001", "none", False, ["complaint"])]


def test_check_then_reopen_again_shows_again(cconn):
    _reclassify(cconn, "c-0001", todo=[])
    reopened(cconn, "c-0001", at="2026-11-10 09:00:00")
    calls.check_call(cconn, "c-0001", today=TODAY, by="데스크", at="2026-11-10 10:00:00")
    assert calls.review_calls(cconn, TODAY) == []
    cconn.execute("UPDATE ax_auto_actions SET reverted_at = '2026-11-10 12:00:00'")
    assert [x["call_uid"] for x in calls.review_calls(cconn, TODAY)] == ["c-0001"]


def test_detail_has_review(cconn):
    _reclassify(cconn, "c-0001", todo=[])
    judge_human(cconn, "c-0001", emergency=1)
    d = calls.call_detail(cconn, "c-0001", TODAY)
    assert d["review"] == {"ai": "none", "reopened": False, "flags": ["emergency"]}
    assert calls.call_detail(cconn, "c-0004", TODAY)["review"] is None
