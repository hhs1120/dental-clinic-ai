"""통화 화면 조회·쓰기 (backend/calls.py, 통화·② 판정 화면 설계 "통화 화면")."""

import json
from datetime import datetime

import pytest

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


def test_todo_check_moves_to_done_and_back(cconn):
    b = calls.today_board(cconn, TODAY)
    item = b["internal"][0]
    calls.set_todo_done(
        cconn,
        item["classification_id"],
        item["item"],
        part="데스크",
        at="2026-11-05 10:00:00",
        done=True,
    )
    b2 = calls.today_board(cconn, TODAY)
    done = b2["done"][0]
    assert (done["call_uid"], done["done_by"]) == (item["call_uid"], "데스크")
    calls.set_todo_done(
        cconn, item["classification_id"], item["item"], part="데스크", at="x", done=False
    )
    assert calls.today_board(cconn, TODAY)["done"] == []


def test_todo_check_idempotent_and_missing_item(cconn):
    cid = calls.today_board(cconn, TODAY)["internal"][0]["classification_id"]
    for _ in range(2):
        calls.set_todo_done(cconn, cid, 0, part="데스크", at="2026-11-05 10:00:00", done=True)
    assert cconn.execute("SELECT COUNT(*) FROM ax_todo_checks").fetchone()[0] == 1
    with pytest.raises(KeyError):
        calls.set_todo_done(cconn, cid, 9, part="데스크", at="x", done=True)
    with pytest.raises(KeyError):
        calls.set_todo_done(cconn, 99999, 0, part="데스크", at="x", done=True)


def _gw(conn, fail=False):
    from dataclasses import replace

    from backend.harness.store import SqliteLlmLog
    from shared.llm import FakeGateway

    class G(FakeGateway):
        def complete(self, alias, **kw):
            r = super().complete(alias, **kw)
            return replace(r, ok=False, data=None, error="실패") if fail else r

    return G(SqliteLlmLog(conn), scope="runtime")


NOW = datetime(2026, 11, 5, 14, 30, 0)


def test_add_call_classifies_and_links_patient(cconn):
    uid = calls.add_call(
        cconn,
        _gw(cconn),
        direction="inbound",
        transcript="직원: 네\n환자: 예약 바꿀게요",
        phone="010-1111-2222",
        part="데스크",
        now=NOW,
        today=TODAY,
    )
    assert uid == "p-0001"
    d = calls.call_detail(cconn, uid)
    assert d["call"]["at"] == "2026-12-01 14:30:00" and d["call"]["chart_no"] == "C-00001"
    assert d["classification"]["status"] == "ok" and d["judge"] is not None
    assert (
        calls.add_call(
            cconn,
            _gw(cconn),
            direction="outbound",
            transcript="직원: 안내",
            phone=None,
            part="데스크",
            now=NOW,
            today=TODAY,
        )
        == "p-0002"
    )
    assert calls.call_detail(cconn, "p-0002")["call"]["chart_no"] is None


def test_add_call_rejects_empty(cconn):
    for direction, text in (("inbound", "   "), ("sideways", "직원: 네")):
        with pytest.raises(ValueError):
            calls.add_call(
                cconn,
                _gw(cconn),
                direction=direction,
                transcript=text,
                phone=None,
                part="데스크",
                now=NOW,
                today=TODAY,
            )
    assert cconn.execute("SELECT COUNT(*) FROM ax_pasted_calls").fetchone()[0] == 0


def test_add_call_failed_then_reclassify(cconn):
    uid = calls.add_call(
        cconn,
        _gw(cconn, fail=True),
        direction="inbound",
        transcript="직원: 네\n환자: 아파요",
        phone=None,
        part="데스크",
        now=NOW,
        today=TODAY,
    )
    assert calls.call_detail(cconn, uid)["classification"]["status"] == "failed"
    calls.classify_one(cconn, _gw(cconn), uid)
    assert calls.call_detail(cconn, uid)["classification"]["status"] == "ok"
