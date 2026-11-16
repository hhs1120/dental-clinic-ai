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


def test_add_call_judges_and_marks_disagreement(cconn):
    """새 통화도 판정 1행 ok, 라벨이 다르면 엇갈림 행(기능 정의 §4 ① 완료 기준)"""
    from dataclasses import replace

    gw = _gw(cconn)

    class Flip:
        """판정기 라벨 = 분류기 라벨에서 complaint만 뒤집은 것"""

        labels = None

        def complete(self, alias, **kw):
            r = gw.complete(alias, **kw)
            if alias == "classify":
                Flip.labels = r.data
            if alias == "judge":
                return replace(r, data={**Flip.labels, "complaint": not Flip.labels["complaint"]})
            return r

    uid = calls.add_call(cconn, Flip(), direction="inbound", transcript="직원: 네\n환자: 예약",
                         phone="010-1111-2222", part="데스크", now=NOW, today=TODAY)  # fmt: skip
    rows = cconn.execute("SELECT status FROM ax_model_judgments WHERE call_uid = ?", (uid,))
    assert [r["status"] for r in rows] == ["ok"]
    d = calls.call_detail(cconn, uid)
    assert d["disagree"] == {"items": ["complaint"], "is_core": False}


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


def _pasted(conn, uid, at, patient=1):
    conn.execute(
        "INSERT INTO ax_pasted_calls (call_uid, direction, started_at, transcript, caller_phone,"
        " patient_id, added_by, added_at) VALUES (?, 'outbound', ?, '직원: 네', '010-1111-2222', ?,"
        " '데스크', '2026-12-01 09:00:00')",
        (uid, at, patient),
    )
    conn.commit()


def test_patient_calls_skips_pool_and_current(cconn):
    """그 환자의 통화 — 판정 풀 통화(c-0002)는 통화 화면처럼 빠지고, 지금 연 통화는 뺀다"""
    got = calls.patient_calls(cconn, "C-00001")
    assert [c["call_uid"] for c in got["calls"]] == ["c-0001"] and got["total"] == 1
    # total은 지금 연 통화까지 — "전체 보기"로 연 통화 기록의 건수와 같게
    assert calls.patient_calls(cconn, "C-00001", exclude="c-0001") == {"calls": [], "total": 1}
    assert calls.patient_calls(cconn, "C-99999") == {"calls": [], "total": 0}


def test_patient_calls_newest_first_limit_and_pasted(cconn):
    """새로 추가한 통화도 들고, 최신순 PATIENT_CALLS_MAX건, total은 전체 수"""
    for i in range(4):
        _pasted(cconn, f"p-000{i + 1}", f"2026-11-2{i} 15:00:00")
    got = calls.patient_calls(cconn, "C-00001")
    assert got["total"] == 5
    assert len(got["calls"]) == calls.PATIENT_CALLS_MAX == 3
    assert [c["call_uid"] for c in got["calls"]] == ["c-0001", "p-0004", "p-0003"]
    assert got["calls"][1]["direction"] == "outbound" and got["calls"][1]["status"] is None


def test_card_has_complaint_flag(cconn):
    cconn.execute(
        "UPDATE ax_call_classifications SET labels = ? WHERE call_uid = 'c-0001'",
        (json.dumps(LABELS | {"complaint": 1}),),
    )
    card = calls.patient_calls(cconn, "C-00001")["calls"][0]
    assert card["complaint"] is True and card["purpose"] == "book" and card["summary"] == "요약"


def test_call_emergency_for_booking_calls(cconn):
    """예약표 "어떻게 잡혔나"의 응급 여부 — 활성 분류의 값, 분류 전이면 None, 판정 풀·없는 통화는 빠진다"""
    _pasted(cconn, "p-0001", "2026-11-20 15:00:00")
    cconn.execute(
        "UPDATE ax_call_classifications SET labels = ? WHERE call_uid = 'c-0004'",
        (json.dumps(LABELS | {"emergency": 1}),),
    )
    got = calls.call_emergency(cconn, ["c-0001", "c-0002", "c-0004", "p-0001", "c-9999"])
    assert got == {"c-0001": False, "c-0004": True, "p-0001": None}
    assert calls.call_emergency(cconn, []) == {}
