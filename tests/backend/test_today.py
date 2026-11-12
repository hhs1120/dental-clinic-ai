"""오늘 화면 한 장 (backend/today.py, 기능 정의 §4 오늘 화면) — 숫자마다 그 화면과 같은 함수로 센다."""

import json
import sqlite3
from datetime import date, datetime, timedelta

import pytest

from backend import autoclose, calls, claims, db, missed, prep, recon, today
from backend import judge_queue as jq
from generator.clinic import master_values_rows
from tests.backend.call_db import LABELS, TODAY, classify_row, make_call_db
from tests.backend.test_claims import NOTE, add_claim, reviewed_for
from tests.backend.test_missed import Source
from tests.backend.test_prep import make_source

NOW = datetime(2026, 11, 10, 9, 0, 0)


@pytest.fixture
def tconn(tmp_path):
    """통화 화면 테스트 DB + 준비표가 읽는 master 값(예약은 없음)"""
    c = make_call_db(tmp_path)
    s = sqlite3.connect(tmp_path / "source.sqlite")
    s.executemany("INSERT INTO ax_master_values VALUES (?, ?)", master_values_rows())
    s.commit()
    s.close()
    yield c
    c.close()


@pytest.fixture
def pconn(tmp_path):
    """준비표 테스트 source(기준일 예약 101·102·103, 104는 취소)"""
    make_source(tmp_path / "source.sqlite").close()
    c = db.connect(tmp_path / "app.sqlite", tmp_path / "source.sqlite")
    db.init_schema(c)
    yield c
    c.close()


def _sheet(conn, day=TODAY):
    return today.sheet(conn, day, NOW)


def test_cards_match_prep_chips(pconn):
    rows = prep.prep_facts(pconn, TODAY, TODAY)
    s = _sheet(pconn)
    assert s["date"] == "2026-12-01"
    assert s["cards"]["appointments"] == {"total": 3, "treatment": 1, "hygiene": 1, "consult": 1}
    alert = sum("alert" in r["chips"] for r in rows)
    unconfirmed = sum("unconfirmed" in r["chips"] for r in rows)
    assert (alert, unconfirmed) == (2, 2)
    assert s["cards"]["alert_patients"] == s["people"]["alert_patients"] == alert
    assert s["people"]["unconfirmed"] == unconfirmed
    assert s["cards"]["new_patients"] == sum(r["patient"]["new_patient"] for r in rows)


def test_todo_counts_match_board(tconn):
    b = calls.today_board(tconn, TODAY)
    open_ = b["internal"] + b["contact"]
    s = _sheet(tconn)
    todo = s["cards"]["todo"]
    assert todo == {
        "items": len(open_),
        "calls": 2,
        "urgent_items": 2,
    }  # c-0001·c-0004 — 급함은 줄 수(앞 숫자와 같은 단위): 급한 c-0004의 할 일 2줄
    assert s["people"]["todo_left"] == {"items": len(open_), "calls": 2}


def test_urgent_calls_one_line_per_call(tconn):
    got = _sheet(tconn)["people"]["urgent_calls"]
    assert got == [
        {
            "call_uid": "c-0004",
            "name": "이민수",
            "text": "원장님께 여쭙기",
            "at": "2026-11-30 09:00:00",
            "emergency": False,
        }
    ]


def test_urgent_text_skips_done_item(tconn):
    cid = calls.call_detail(tconn, "c-0004")["classification"]["id"]
    calls.set_todo_done(tconn, cid, 0, part="데스크", at="2026-11-10 09:00:00", done=True)
    assert _sheet(tconn)["people"]["urgent_calls"][0]["text"] == "회신 전화"


def test_review_calls_included(tconn):
    tconn.execute("DELETE FROM ax_call_classifications WHERE call_uid = 'c-0001'")
    classify_row(tconn, "c-0001", todo=[])
    tconn.execute(
        "INSERT INTO ax_judgments (call_uid, labels, judged_by, judged_at)"
        " VALUES ('c-0001', ?, '데스크', '2026-11-10 09:00:00')",
        (json.dumps(LABELS | {"has_todo": 0, "emergency": 1}),),
    )
    assert _sheet(tconn)["people"]["review_calls"] == calls.review_calls(tconn, TODAY)
    assert len(calls.review_calls(tconn, TODAY)) == 1


def test_judge_pending_equals_next_call_left(tconn):
    assert _sheet(tconn)["people"]["judge_pending"] == 0
    tconn.executemany(
        "INSERT INTO ax_judge_state VALUES (?, ?, '2026-11-05 09:00:00')",
        [("c-0001", "pending"), ("c-0003", "skipped"), ("c-0004", "done"), ("c-0002", "pending")],
    )
    left = jq.next_call(tconn, "2026-11-10")["left"]
    assert left == 2  # 판정 풀 c-0002·끝난 c-0004 제외
    assert jq.pending_count(tconn) == left == _sheet(tconn)["people"]["judge_pending"]


def _log(conn, kind, question, status, at, is_eval=0, reason=None):
    conn.execute(
        "INSERT INTO ax_search_logs (kind, question, status, is_eval, created_at, refuse_reason)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (kind, question, status, is_eval, at, reason),
    )


def test_refused_recent_non_eval_only(tconn):
    _log(tconn, "chat", "유치는 언제 빠져?", "refused", "2026-11-09 10:00:00")
    _log(tconn, "search_summary", "주차 되나요", "refused", "2026-11-04 10:00:00")
    _log(tconn, "chat", "평가 질문", "refused", "2026-11-09 11:00:00", is_eval=1)
    _log(tconn, "chat", "오래된 질문", "refused", "2026-11-02 10:00:00")  # 8일 전
    _log(tconn, "chat", "답한 질문", "answered", "2026-11-09 12:00:00")
    # 비공개 정보라 거절한 질문은 문서가 없어서가 아니다 — 근거 없는 질문이 아니다
    _log(tconn, "chat", "직원 급여 얼마야", "refused", "2026-11-09 13:00:00", reason="restricted")
    got = _sheet(tconn)["people"]["refused"]
    assert [(r["kind"], r["question"], r["at"]) for r in got] == [
        ("chat", "유치는 언제 빠져?", "2026-11-09 10:00:00"),
        ("search_summary", "주차 되나요", "2026-11-04 10:00:00"),
    ]
    assert all(isinstance(r["id"], int) for r in got)


def _close(conn, uid, at, versions=("v2", "v2")):
    conn.execute(
        "INSERT INTO ax_auto_actions (kind, target, result, reason, created_at)"
        " VALUES ('call_autoclose', ?, ?, ?, ?)",
        (uid, json.dumps({"versions": list(versions)}), autoclose.REASON, at),
    )


def test_auto_recent_twenty_and_total(tconn):
    autoclose.set_policy(tconn, autoclose.KIND, True)
    for i in range(21):  # 새로 추가한 통화 21건을 한 시간 간격으로 닫는다
        uid, at = (
            f"p-{i + 1:04d}",
            (datetime(2026, 11, 1, 9) + timedelta(hours=i)).isoformat(sep=" "),
        )
        tconn.execute(
            "INSERT INTO ax_pasted_calls (call_uid, direction, started_at, transcript, added_by,"
            " added_at) VALUES (?, 'inbound', '2026-12-01 09:00:00', '내용', '데스크', ?)",
            (uid, at),
        )
        _close(tconn, uid, at)
    _close(tconn, "c-0002", "2026-11-09 09:00:00")  # 판정 풀 — 앱 통화가 아니다
    _close(tconn, "c-0003", "2026-11-09 09:00:00", versions=("v1", "v2"))  # 옛 버전 쌍
    tconn.commit()
    ai = _sheet(tconn)["ai"]
    assert ai["autoclose_on"] is True and ai["auto_total"] == 21
    assert len(ai["auto_recent"]) == today.AUTO_RECENT == 20
    first = ai["auto_recent"][0]
    assert first["call_uid"] == "p-0021" and first["reason"] == autoclose.REASON
    assert first["at"] == "2026-11-02 05:00:00"
    assert [r["call_uid"] for r in ai["auto_recent"]][-1] == "p-0002"
    assert isinstance(first["id"], int) and "name" in first


def test_auto_policy_off_hides_actions(tconn):
    _close(tconn, "c-0003", "2026-11-09 09:00:00")
    tconn.commit()
    ai = _sheet(tconn)["ai"]
    assert (ai["autoclose_on"], ai["auto_total"], ai["auto_recent"]) == (False, 0, [])
    autoclose.set_policy(tconn, autoclose.KIND, True)
    assert _sheet(tconn)["ai"]["auto_total"] == 1


def test_reverted_not_counted(tconn):
    autoclose.set_policy(tconn, autoclose.KIND, True)
    _close(tconn, "c-0003", "2026-11-09 09:00:00")
    tconn.execute("UPDATE ax_auto_actions SET reverted_at = '2026-11-10 09:00:00'")
    tconn.commit()
    assert _sheet(tconn)["ai"]["auto_total"] == 0


def test_failures_counted(tconn):
    tconn.execute("DELETE FROM ax_call_classifications WHERE call_uid IN ('c-0002', 'c-0003')")
    classify_row(tconn, "c-0003", status="failed")
    classify_row(tconn, "c-0002", status="failed")  # 판정 풀 — 세지 않는다
    classify_row(tconn, "c-0003", version="v1", status="failed")  # 활성 버전이 아님
    cid = calls.call_detail(tconn, "c-0001")["classification"]["id"]
    tconn.execute(
        "INSERT INTO ax_call_followups (call_uid, classification_id, status, reason, created_at)"
        " VALUES ('c-0001', ?, 'failed', '도구 한도', '2026-11-10 09:00:00')",
        (cid,),
    )
    tconn.commit()
    ai = _sheet(tconn)["ai"]
    assert (ai["classify_failed"], ai["followup_failed"]) == (1, 1)


def test_new_call_without_classification_is_failed(tconn):
    """새로 추가한 통화가 분류 중 예외로 분류 행 없이 남으면(하루 상한 등) 분류 실패로 센다 — 어디에도 안 뜨지 않게.
    분류 행 없는 생성 통화(버전 전환 직후 일괄 분류 전)는 세지 않는다"""
    tconn.execute(
        "INSERT INTO ax_pasted_calls (call_uid, direction, started_at, transcript, added_by, added_at)"
        " VALUES ('p-0001', 'inbound', '2026-12-01 09:00:00', '내용', '데스크', '2026-11-10 09:00:00')"
    )
    tconn.execute("DELETE FROM ax_call_classifications WHERE call_uid = 'c-0003'")
    tconn.commit()
    assert _sheet(tconn)["ai"]["classify_failed"] == 1


def _save_prep(conn, appt, suggestion, valid, day=TODAY):
    conn.execute(
        "INSERT INTO ax_prep_sheets (sheet_date, patient_id, appointment_id, facts, facts_hash,"
        " suggestion, suggestion_valid, prompt_version, model, created_at)"
        " VALUES (?, 1, ?, '{}', 'h', ?, ?, 'v1', 'm', '2026-11-10 03:00:00')",
        (day.isoformat(), appt, json.dumps(suggestion) if suggestion else None, valid),
    )


def test_prep_saved_counts(pconn):
    _save_prep(pconn, 101, {"action": "suggest", "text": "t", "used_facts": []}, 1)
    _save_prep(pconn, 102, {"action": "none", "text": "", "used_facts": []}, 1)
    _save_prep(pconn, 103, None, 0)
    _save_prep(pconn, 90, None, 0, day=date(2026, 11, 30))  # 다른 날
    pconn.commit()
    assert _sheet(pconn)["ai"]["prep"] == {"rows": 3, "suggest": 1, "failed": 1}


def test_empty_day(pconn):
    s = _sheet(pconn, date(2026, 12, 2))
    assert s["cards"]["appointments"] == {"total": 0, "treatment": 0, "hygiene": 0, "consult": 0}
    assert s["cards"]["new_patients"] == s["cards"]["alert_patients"] == 0
    assert s["people"]["urgent_calls"] == [] and s["ai"]["prep"]["rows"] == 0


def test_today_does_not_count_missed(pconn, monkeypatch):
    """미예약 환자 줄은 숫자 없는 고정 링크 — 오늘 화면은 미예약 목록을 계산하지 않는다(기능 정의 §4 오늘 화면)"""

    def boom(*a, **k):
        raise AssertionError("오늘 화면이 미예약 목록을 계산함")

    monkeypatch.setattr(missed, "facts", boom)
    assert "missed_top" not in _sheet(pconn)["people"]


# ── ⑤ 청구 점검 줄 (설계 2026-11-10 청구 점검 §1 "오늘 화면 줄") ─────────────────────


def test_today_claim_lines_match_screen(tmp_path):
    """재심 신청 기한 임박 = /claims `soon` 칩(상병 착오 제외), 청구 전 상병 경고 = 청구 전 점검 경고 줄 수"""
    s = Source(tmp_path / "source.sqlite")
    add_claim(s, 1, note=NOTE["documentation"][1], reviewed=reviewed_for(10))
    add_claim(s, 2, note=NOTE["diagnosis"][1], reviewed=reviewed_for(10))
    add_claim(s, 3, day="2026-11-12", status="pending", dx="D07")
    conn = s.app()
    people = _sheet(conn)["people"]
    assert people["appeal_soon"] == 1
    assert people["preclaim_warning"] == 1
    assert people["appeal_soon"] == claims.adjusted(conn, TODAY)["chips"]["soon"]["count"]
    assert people["preclaim_warning"] == claims.pre_claim(conn, TODAY)["counts"]["warning"]


def test_today_claim_lines_zero(pconn):
    people = _sheet(pconn)["people"]
    assert (people["appeal_soon"], people["preclaim_warning"], people["preclaim_ai"]) == (0, 0, 0)


def test_today_preclaim_ai(tmp_path):
    """청구 전 AI 줄 = 청구 전 점검 AI 칩 수"""
    from tests.backend.test_claims import STD_Q, save_check

    s = Source(tmp_path / "source.sqlite")
    cid = add_claim(s, 1, day="2026-11-12", status="pending", code="U0012")
    conn = s.app()
    save_check(conn, cid, "missing", [STD_Q])
    people = _sheet(conn)["people"]
    assert people["preclaim_ai"] == 1 == claims.pre_claim(conn, TODAY)["counts"]["ai_missing"]


def test_today_preclaim_ai_ignores_omission_only(tmp_path):
    """오늘 화면 문구는 '요건 근거 못 찾음' — 처치 누락만 있는 줄은 세지 않는다(청구 전 AI 칩은 센다)"""
    from tests.backend.test_claims import CHART_Q, save_check

    s = Source(tmp_path / "source.sqlite")
    cid = add_claim(s, 1, day="2026-11-12", status="pending", code="U0012")
    conn = s.app()
    save_check(conn, cid, "met", [CHART_Q], [{"procedure": "봉합", "quote": "수평 매복, 골삭제"}])
    assert _sheet(conn)["people"]["preclaim_ai"] == 0
    assert claims.pre_claim(conn, TODAY)["counts"]["ai"] == 1


def test_today_recon_line_matches_screen(tmp_path):
    """일계표 불일치 = 확정 안 된 불일치 건(backend/recon.py open_count) — 확정하면 줄어든다"""
    from tests.backend.test_recon_match import Books

    b = Books(tmp_path / "source.sqlite")
    b.pay("2026-11-03 10:00:00", 3_500, "card")
    b.sale("2026-11-03 10:00:00", 35_000, "R1")
    b.settle("2026-11-03")
    conn = b.app()
    people = _sheet(conn)["people"]
    assert people["recon_open"] == recon.open_count(conn, TODAY) == 1
    assert people["recon_month"] == "2026-11"  # 링크가 가는 달 — 미확정이 있는 가장 이른 달
    key = recon.ledger(conn, TODAY)["days"][0]["items"][0]["key"]
    recon.confirm(conn, key, "amount_typo", "데스크", "2026-12-01 09:00", TODAY)
    conn.commit()
    assert _sheet(conn)["people"]["recon_open"] == 0


def test_today_recon_zero(tmp_path):
    from tests.backend.test_recon_match import Books, clean_day

    b = Books(tmp_path / "source.sqlite")
    clean_day(b)
    b.cash_in(b.settle("2026-11-03"))
    people = _sheet(b.app())["people"]
    assert (people["recon_open"], people["recon_month"]) == (0, None)


def test_today_expense_line_matches_screen(tmp_path):
    """지출 증빙 확인 = 지난달 증빙 없음·과목 확인 필요이면서 확정 안 된 출금(backend/expense.py open_summary)"""
    from backend import expense
    from tests.backend.test_expense import Outflows

    o = Outflows(tmp_path / "source.sqlite")
    oid = o.card("2026-11-05 12:00:00", "솔밭식당", 50_000)  # 영수증 없음
    conn = o.app()
    people = _sheet(conn)["people"]
    assert people["expense_open"] == expense.open_summary(conn, TODAY)["count"] == 1
    assert people["expense_month"] == "2026-11"
    expense.confirm(
        conn,
        "fin_card_expenses",
        oid,
        [],
        "복리후생비",
        "데스크",
        "2026-12-01 09:00",
        date(2026, 12, 1),
    )
    conn.commit()
    people = _sheet(conn)["people"]
    assert (people["expense_open"], people["expense_month"]) == (0, None)
