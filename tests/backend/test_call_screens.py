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
    assert page["next"] == "2026-11-30 09:00:00|c-0004"
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


def _src(conn, sql, params=()):
    """읽기 전용으로 붙인 source.sqlite에 테스트 행을 넣는다"""
    import sqlite3 as s3

    path = conn.execute("PRAGMA database_list").fetchall()
    src = next(r[2] for r in path if r[1] == "source")
    c = s3.connect(src)
    c.execute(sql, params)
    c.commit()
    c.close()


def test_add_call_shared_phone_not_linked(cconn):
    """가족이 같이 쓰는 번호는 누구인지 모르므로 연결하지 않는다 — 한 명일 때만"""
    _src(
        cconn,
        "INSERT INTO emr_patients (id, chart_no, name, birth_date, sex, phone, registered_at)"
        " VALUES (2, 'C-00002', '김영희', '1982-01-01', 'F', '010-1111-2222', '2024-01-01')",
    )
    uid = calls.add_call(
        cconn,
        _gw(cconn),
        direction="inbound",
        transcript="직원: 네",
        phone="010-1111-2222",
        part="데스크",
        now=NOW,
        today=TODAY,
    )
    assert calls.call_detail(cconn, uid)["call"]["chart_no"] is None


def test_add_call_phone_digits_only(cconn):
    """하이픈 없이 넣어도 같은 번호로 본다"""
    uid = calls.add_call(
        cconn,
        _gw(cconn),
        direction="inbound",
        transcript="직원: 네",
        phone="01011112222",
        part="데스크",
        now=NOW,
        today=TODAY,
    )
    assert calls.call_detail(cconn, uid)["call"]["chart_no"] == "C-00001"


def test_add_call_survives_gateway_exception(cconn):
    """하루 상한·키 없음처럼 게이트웨이가 예외를 던져도 통화 번호를 돌려준다 — 상세에서 다시 분류"""

    class Boom:
        def complete(self, alias, **kw):
            raise RuntimeError("상한")

    uid = calls.add_call(
        cconn,
        Boom(),
        direction="inbound",
        transcript="직원: 네",
        phone=None,
        part="데스크",
        now=NOW,
        today=TODAY,
    )
    assert calls.call_detail(cconn, uid)["classification"]["status"] != "ok"
    calls.classify_one(cconn, _gw(cconn), uid)
    assert calls.call_detail(cconn, uid)["classification"]["status"] == "ok"


def test_add_call_number_after_delete(cconn):
    """번호는 지금까지의 최대 + 1 — 행이 지워져도 겹치지 않는다"""
    for _ in range(2):
        calls.add_call(
            cconn,
            _gw(cconn),
            direction="inbound",
            transcript="직원: 네",
            phone=None,
            part="데스크",
            now=NOW,
            today=TODAY,
        )
    cconn.execute("DELETE FROM ax_pasted_calls WHERE call_uid = 'p-0001'")
    uid = calls.add_call(
        cconn,
        _gw(cconn),
        direction="inbound",
        transcript="직원: 네",
        phone=None,
        part="데스크",
        now=NOW,
        today=TODAY,
    )
    assert uid == "p-0003"


def test_list_calls_same_time_page_boundary(cconn):
    """같은 시각 통화가 쪽 경계에 걸려도 빠지지 않는다"""
    _src(
        cconn,
        "INSERT INTO ax_calls (call_uid, source, direction, started_at, duration_sec,"
        " has_transcript, transcript) VALUES ('c-0005', 'generated', 'inbound',"
        " '2026-11-30 09:00:00', 60, 1, '직원: 네')",
    )
    seen, before = [], None
    while True:
        page = calls.list_calls(cconn, flt=None, q=None, before=before, limit=1)
        seen += [c["call_uid"] for d in page["days"] for c in d["calls"]]
        if not page["next"]:
            break
        before = page["next"]
    assert sorted(seen) == ["c-0001", "c-0003", "c-0004", "c-0005"]


def test_list_calls_search_wildcards_literal(cconn):
    """검색어의 %·_는 글자 그대로"""
    for q in ("_", "%"):
        assert calls.list_calls(cconn, flt=None, q=q, before=None)["days"] == []


def test_reclassify_clears_old_checks(cconn):
    """다시 분류하면 할 일 목록이 바뀌므로 그 분류 행의 체크를 지운다"""
    item = calls.today_board(cconn, TODAY)["internal"][0]
    calls.set_todo_done(
        cconn, item["classification_id"], item["item"], part="데스크", at="x", done=True
    )
    calls.classify_one(cconn, _gw(cconn), item["call_uid"])
    assert cconn.execute("SELECT COUNT(*) FROM ax_todo_checks").fetchone()[0] == 0
