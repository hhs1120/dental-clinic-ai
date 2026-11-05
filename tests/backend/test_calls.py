"""통화 화면 조회·쓰기 (backend/calls.py, 통화·② 판정 화면 설계 "통화 화면")."""

import json

from backend import calls
from tests.backend.call_db import LABELS, TODAY, judge_row


def test_today_window_and_pool(cconn):
    """7일 안·판정 풀 밖 통화의 할 일만, 종류별 칸으로"""
    b = calls.today_board(cconn, TODAY)
    assert {i["call_uid"] for i in b["internal"] + b["contact"]} == {"c-0001", "c-0004"}
    assert all(i["kind"] == "internal" for i in b["internal"])
    assert all(i["kind"] == "contact" for i in b["contact"])
    first = next(i for i in b["contact"] if i["call_uid"] == "c-0001")
    assert first["name"] == "김철수" and first["who"] == "김철수" and first["after"] == 0
    assert b["done"] == []


def test_today_order_urgent_first(cconn):
    b = calls.today_board(cconn, TODAY)
    assert [i["call_uid"] for i in b["internal"]] == ["c-0004", "c-0001"]
    assert b["internal"][0]["urgent"] is True


def test_list_calls_groups_by_day_and_filters(cconn):
    got = calls.list_calls(cconn, flt=None, q=None, before=None)
    assert [d["date"] for d in got["days"]] == ["2026-11-30", "2026-11-28", "2026-11-20"]
    assert got["days"][0]["count"] == 1 and got["next"] is None
    card = got["days"][1]["calls"][0]
    assert card["call_uid"] == "c-0001" and card["chart_no"] == "C-00001"
    assert card["todo_total"] == 2 and card["todo_left"] == 2
    assert (
        calls.list_calls(cconn, flt=None, q="C-00001", before=None)["days"][0]["calls"][0][
            "call_uid"
        ]
        == "c-0001"
    )
    page = calls.list_calls(cconn, flt=None, q=None, before=None, limit=1)
    assert page["next"] == "2026-11-30 09:00:00"
    nxt = calls.list_calls(cconn, flt=None, q=None, before=page["next"], limit=1)
    assert nxt["days"][0]["calls"][0]["call_uid"] == "c-0001"


def test_list_calls_disagree_filter(cconn):
    cconn.execute(
        "INSERT INTO ax_call_disagreements VALUES ('c-0003', 'v2', 'v2', 1, 0, 0, ?)",
        (json.dumps(["primary_purpose"]),),
    )
    got = calls.list_calls(cconn, flt="disagree", q=None, before=None)
    assert [c["call_uid"] for d in got["days"] for c in d["calls"]] == ["c-0003"]


def test_detail_has_judge_and_disagreement(cconn):
    judge_row(cconn, "c-0001", LABELS | {"primary_purpose": "treatment_q"})
    cconn.execute(
        "INSERT INTO ax_call_disagreements VALUES ('c-0001', 'v2', 'v2', 1, 0, 0, ?)",
        (json.dumps(["primary_purpose"]),),
    )
    d = calls.call_detail(cconn, "c-0001")
    assert d["call"]["name"] == "김철수" and d["call"]["transcript"].startswith("직원")
    assert d["judge"]["primary_purpose"] == "treatment_q"
    assert d["disagree"] == {"items": ["primary_purpose"], "is_core": True}
    assert [t["done"] for t in d["classification"]["todo"]] == [False, False]


def test_detail_v1_strings_as_internal(cconn):
    cconn.execute(
        "UPDATE ax_call_classifications SET todo = ? WHERE call_uid = 'c-0001'",
        (json.dumps(["원장님께 전달"]),),
    )
    d = calls.call_detail(cconn, "c-0001")
    assert d["classification"]["todo"][0]["kind"] == "internal"
    assert d["judge"] is None and d["disagree"] is None


def test_detail_pool_call_is_none(cconn):
    assert calls.call_detail(cconn, "c-0002") is None
    assert calls.call_detail(cconn, "c-9999") is None
