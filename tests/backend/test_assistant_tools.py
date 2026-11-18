"""AI 비서 도구(backend/assistant_tools.py, AI 비서 설계 §4.2) — emr_bridge 테스트의 손으로 만든 source를 그대로 쓴다."""

# ruff: noqa: F401, F811  (다른 테스트 모듈의 fixture를 가져와 쓴다)

import sqlite3
from datetime import date

import pytest

from backend import assistant_tools, calls, claims, emr_bridge
from backend.harness.loop import ToolContext
from backend.harness.store import RunStore
from tests.backend.test_emr_bridge import (
    CLINIC,
    HYGIENIST,
    MISSED,
    NEW,
    RESIN,
    SCALING,
    WED,
    _approve,
    _call,
    _draft,
    _holidays,
    _src_add,
    conn,
    src,
)

NAMES = {
    "query_patient",
    "query_schedule",
    "query_claims",
    "query_charts",
    "find_calls",
    "unbooked_requests",
    "free_slots",
    "prep_rows",
    "claim_items",
    "recon_days",
    "missed_patients",
    "ask_records",
    "draft_appointment",
    "done_tools",
}


def call(conn, name, **args):
    store = RunStore(conn)
    with conn:
        n = conn.execute("SELECT count(*) FROM ax_agent_runs").fetchone()[0]
        run_id = store.create_run("assistant", f"1:{n}", "v1", "me")
    tool = {t.name: t for t in assistant_tools.assistant_tools(CLINIC, None, TURN)}[name]
    norm = tool.args.model_validate(args).model_dump(mode="json")
    return tool.fn(ToolContext(run_id, "assistant", "1:1", store), norm)


TURN = {"conversation_id": 7, "turn": 3}


def test_registry_names_fixed():
    tools = assistant_tools.assistant_tools(CLINIC, None, TURN)
    assert {t.name for t in tools} == NAMES and len(tools) == len(NAMES)
    assert assistant_tools.TOOLS_VERSION == "v5"
    assert {t.name for t in tools if t.writes} == {"draft_appointment"}
    assert [t.name for t in tools if t.ends_run] == ["done_tools"]


def test_unbooked_tool_matches_bridge(conn):
    _call(conn, "c-1", "2026-11-26 15:00:00", NEW)
    _call(conn, "c-2", "2026-11-27 12:00:00", None)
    got = call(conn, "unbooked_requests", date_from="2026-11-24", date_to="2026-11-30")
    want = emr_bridge.unbooked_requests(conn, date(2026, 11, 24), date(2026, 11, 30))
    assert got == {"count": 2, "patients": want}


def test_find_calls_filters(conn):
    _call(conn, "c-1", "2026-11-26 15:00:00", NEW)
    _call(conn, "c-2", "2026-11-27 12:00:00", MISSED, wants=0)
    base = {"date_from": "2026-11-24", "date_to": "2026-11-30"}
    assert call(conn, "find_calls", **base)["count"] == 2
    wants = call(conn, "find_calls", wants_appt=True, **base)
    assert [c["call_uid"] for c in wants["calls"]] == ["c-1"]
    assert wants["calls"][0]["wants_appt"] is True and wants["calls"][0]["chart_no"] == "C-00002"
    only = call(conn, "find_calls", chart_no="C-00001", **base)
    assert [c["call_uid"] for c in only["calls"]] == ["c-2"]


def test_find_calls_carries_todo_with_done(conn):
    """통화 화면 할 일을 그대로 — 연락(contact)·안에서(internal)와 끝낸 상태"""
    _call(conn, "c-1", "2026-11-26 15:00:00", NEW)
    base = {"date_from": "2026-11-24", "date_to": "2026-11-30"}
    todo = call(conn, "find_calls", **base)["calls"][0]["todo"]
    assert todo == [
        {"kind": "internal", "who": "", "text": "원장님께 여쭙기", "done": False},
        {"kind": "contact", "who": "김철수", "text": "회신 전화", "done": False},
    ]
    cid = conn.execute("SELECT id FROM ax_call_classifications WHERE call_uid = 'c-1'").fetchone()[
        0
    ]
    calls.set_todo_done(conn, cid, 1, part="데스크", at="2026-11-27 09:00:00", done=True)
    todo = call(conn, "find_calls", **base)["calls"][0]["todo"]
    assert [t["done"] for t in todo] == [False, True]


def test_free_slots_tool_closed_thursday(conn):
    got = call(conn, "free_slots", date="2026-12-03", procedure="스케일링")
    assert got == {"date": "2026-12-03", "closed": True, "slots": []}


def test_free_slots_tool_open_day_and_role(conn):
    """시각마다 한 줄(빈 체어 수), 담당 이름은 보이지 않는다 — 역할·이름으로 거른다"""
    got = call(conn, "free_slots", date=WED, procedure="스케일링")
    assert got["closed"] is False and got["slots"]
    assert got["slots"][0] == {"start": "09:30", "end": "10:00", "free_chairs": 5}
    starts = [s["start"] for s in got["slots"]]
    assert starts == sorted(set(starts))  # 같은 시각은 한 줄
    by_name = call(conn, "free_slots", date=WED, procedure="스케일링", staff="임재윤")
    assert by_name["slots"] == got["slots"]
    assert call(conn, "free_slots", date=WED, procedure="스케일링", staff="원장") == {
        "error": ["wrong_role"]  # 빈 시각 없음과 가른다
    }
    assert call(conn, "free_slots", date=WED, date_to="2026-12-01", procedure="스케일링") == {
        "error": ["bad_range"]
    }
    assert call(conn, "free_slots", date=WED, procedure="스케일링", staff="없는사람") == {
        "error": ["unknown_staff"]
    }
    assert call(conn, "free_slots", date=WED, procedure="없는시술") == {
        "error": ["unknown_procedure"]
    }


def test_free_slots_range_equals_days_joined(conn):
    """기간 조회 = 날마다 부른 결과를 날짜순으로 이은 것 — 휴진일(목)은 closed, 빈 수 0"""
    one = {"procedure": "스케일링", "staff": None}
    got = call(conn, "free_slots", date="2026-12-02", date_to="2026-12-04", **one)
    assert [d["date"] for d in got["days"]] == ["2026-12-02", "2026-12-03", "2026-12-04"]
    assert got["days"][1] == {"date": "2026-12-03", "closed": True, "free": 0}
    joined = [
        {"date": d, **s}
        for d in ("2026-12-02", "2026-12-04")
        for s in call(conn, "free_slots", date=d, **one)["slots"]
    ]
    assert got["count"] == len(joined) == sum(d["free"] for d in got["days"])
    assert got["slots"] == joined[: assistant_tools.LIST_CAP]
    assert got["slots"][0]["date"] == "2026-12-02"  # 가까운 순


def test_free_slots_range_capped(conn):
    """기간은 FREE_SPAN_DAYS일까지만 본다"""
    got = call(conn, "free_slots", date="2026-12-02", date_to="2027-06-30", procedure="스케일링")
    assert len(got["days"]) == assistant_tools.FREE_SPAN_DAYS
    assert got["date_to"] == "2027-01-01"


def _draft_args(**kw):
    return {
        "chart_no": "C-00002",
        "date": WED,
        "start": "10:00",
        "staff": "임재윤",
        "procedure": "스케일링",
    } | kw


def _drafted_at(conn, got):
    assert "error" not in got, got
    return conn.execute(
        "SELECT scheduled_at FROM ax_emr_writes WHERE id = ?", (got["id"],)
    ).fetchone()[0]


def test_draft_window_picks_earliest_free_in_window(conn):
    """시각 없이 시간대만 — 코드가 그 시간대 가장 이른 빈 시각을 고른다(모델이 고르지 않는다), 담당은 역할 중 자동"""
    _src_add(
        conn, (MISSED, HYGIENIST, f"{WED} 14:00:00", "hygiene", SCALING, 1)
    )  # 오후 첫 칸 막힘
    pm = call(conn, "draft_appointment", **_draft_args(start=None, window="오후", staff=None))
    assert _drafted_at(conn, pm) == f"{WED} 14:30:00"


def test_draft_without_time_takes_earliest_of_day(conn):
    got = call(conn, "draft_appointment", **_draft_args(start=None, staff="위생사"))
    assert _drafted_at(conn, got) == f"{WED} 09:30:00"


def test_draft_window_closed_day_and_no_slot(conn):
    got = call(
        conn, "draft_appointment", **_draft_args(date="2026-12-03", start=None, window="오전")
    )
    assert got == {"error": ["closed_day"]}
    sat = call(
        conn, "draft_appointment", **_draft_args(date="2026-12-05", start=None, window="오후")
    )
    assert sat == {"error": ["no_slot"]}  # 토요일은 점심 전에 마감 — 오후 빈 시각 없음
    assert conn.execute("SELECT COUNT(*) FROM ax_emr_writes").fetchone()[0] == 0


def test_free_slots_consult_has_no_chairs(conn):
    """상담은 체어를 쓰지 않아 빈 체어 수가 없다(0으로 보이면 자리 없음으로 읽힌다)"""
    got = call(conn, "free_slots", date=WED, procedure="상담")
    assert got["slots"] and all(set(s) == {"start", "end"} for s in got["slots"])


def test_draft_auto_reports_real_reason(conn):
    """시각 없이 부를 때 담당 역할이 시술과 다르거나 지난 날이면 no_slot 대신 그 이유, 이름 오타는 unknown_staff"""
    args = _draft_args(start=None)
    assert call(conn, "draft_appointment", **args | {"staff": "원장"}) == {"error": ["wrong_role"]}
    assert call(conn, "draft_appointment", **args | {"date": "2026-11-25"}) == {
        "error": ["before_today"]
    }
    assert call(conn, "draft_appointment", **args | {"staff": "없는사람"}) == {
        "error": ["unknown_staff"]
    }


def _add_hygienist(conn, sid):
    path = conn.execute("SELECT file FROM pragma_database_list WHERE name = 'source'").fetchone()[
        0
    ]
    s = sqlite3.connect(path)
    s.execute("INSERT INTO emr_staff (id, name, role) VALUES (?, '위생사둘', 'hygienist')", (sid,))
    s.commit()
    s.close()


def test_draft_auto_same_time_lower_staff_id_then_next(conn):
    """같은 시각이면 담당 id 순 — 앞 담당이 그 시각에 막혀 있으면 같은 시각의 다음 담당"""
    _add_hygienist(conn, 9)
    first = call(conn, "draft_appointment", **_draft_args(start=None, staff=None))
    assert _drafted_at(conn, first) == f"{WED} 09:30:00"
    assert conn.execute("SELECT staff_id FROM ax_emr_writes").fetchone()[0] == HYGIENIST
    conn.execute("DELETE FROM ax_emr_writes")
    _src_add(conn, (MISSED, HYGIENIST, f"{WED} 09:30:00", "hygiene", SCALING, 1))
    got = call(conn, "draft_appointment", **_draft_args(start=None, staff=None))
    assert _drafted_at(conn, got) == f"{WED} 09:30:00"
    assert conn.execute("SELECT staff_id FROM ax_emr_writes").fetchone()[0] == 9


def test_draft_auto_all_candidates_fail_returns_first_error(conn):
    """후보가 모두 검사에 걸리면(같은 환자 같은 날) 첫 실패를 돌려주고 행을 남기지 않는다"""
    _src_add(conn, (NEW, HYGIENIST, f"{WED} 16:00:00", "hygiene", SCALING, 1))
    got = call(conn, "draft_appointment", **_draft_args(start=None, staff=None))
    assert "patient_same_day" in got["error"]
    assert conn.execute("SELECT COUNT(*) FROM ax_emr_writes").fetchone()[0] == 0


def test_draft_tool_rejects_bad_slot(conn):
    got = call(conn, "draft_appointment", **_draft_args(date="2026-12-03"))
    assert got == {"error": ["closed_day"]}
    assert conn.execute("SELECT COUNT(*) FROM ax_emr_writes").fetchone()[0] == 0
    bad = call(conn, "draft_appointment", **_draft_args(procedure="없는시술"))
    assert bad == {"error": ["unknown_procedure"]}
    assert call(conn, "draft_appointment", **_draft_args(chart_no="C-99"))["error"] == [
        "unknown_patient"
    ]


def test_day_off_names_holiday_before_closed_weekday(conn):
    _holidays(conn, "2026-12-25", "2026-12-24")
    day = date.fromisoformat
    assert assistant_tools.day_off(conn, day("2026-12-25")) == "공휴일"  # 금요일 공휴일
    assert assistant_tools.day_off(conn, day("2026-12-24")) == "공휴일"  # 목요일 휴진이어도
    assert assistant_tools.day_off(conn, day("2026-12-03")) == "휴진일"  # 목요일
    assert assistant_tools.day_off(conn, day("2026-12-05")) is None  # 토요일은 진료


def test_draft_tool_records_conversation_turn(conn):
    _call(conn, "c-1", "2026-11-26 15:00:00", NEW)
    got = call(conn, "draft_appointment", **_draft_args(source_call_uid="c-1"))
    assert got["status"] == "draft" and got["staff_id"] == HYGIENIST
    row = conn.execute("SELECT * FROM ax_emr_writes").fetchone()
    assert (row["conversation_id"], row["turn"], row["source_call_uid"]) == (7, 3, "c-1")
    assert row["procedure_code"] == SCALING


def test_draft_tool_role_resolves_first_passing(conn):
    got = call(conn, "draft_appointment", **_draft_args(staff="위생사"))
    assert got["staff_id"] == HYGIENIST
    wrong = call(conn, "draft_appointment", **_draft_args(staff="원장", date="2026-12-04"))
    assert wrong == {"error": ["wrong_role"]}


def test_query_schedule_sees_approved_ax(conn):
    d = _draft(conn, f"{WED} 10:00", code=SCALING, staff=HYGIENIST, patient=MISSED)
    args = {"date_from": WED, "date_to": WED}
    assert call(conn, "query_schedule", **args)["count"] == 0
    _approve(conn, d["id"])
    assert call(conn, "query_schedule", **args)["count"] == 1


def test_ask_records_queues_only(conn):
    assert call(conn, "ask_records", question="주차 되나요?") == {"queued": "주차 되나요?"}


def test_missed_patients_limit(conn):
    got = call(conn, "missed_patients", limit=1)
    assert got["count"] >= 1 and len(got["patients"]) == 1
    with pytest.raises(ValueError):
        assistant_tools.MissedArgs(limit=11)


def test_prep_rows_includes_approved_ax(conn):
    d = _draft(conn, "2026-12-01 10:00", code=RESIN, patient=MISSED)
    _approve(conn, d["id"])
    got = call(conn, "prep_rows", date="2026-12-01")
    assert got["count"] == 1 and got["rows"][0]["chart_no"] == "C-00001"


@pytest.mark.parametrize("kind", ["appeal_soon", "adjusted", "pre_claim_warning"])
def test_claim_items_empty(conn, kind):
    assert call(conn, "claim_items", kind=kind) == {"kind": kind, "count": 0, "items": []}


def test_appeal_soon_matches_claims_soon_chip(conn, monkeypatch):
    """재심 기한 임박 = 청구 점검 soon 칩(오늘 화면 숫자와 같음) — 상병 착오(diagnosis)는 재청구 불가라 뺀다"""
    row = {
        "claim_id": 1,
        "chart_no": "C-00001",
        "name": "김철수",
        "procedure_name": "스케일링",
        "adjust_note": "메모",
        "unpaid": 1000,
        "deadline": "2026-12-08",
        "days_left": 7,
        "expired": False,
    }
    rows = [row | {"chips": ["soon"]}, row | {"claim_id": 2, "chips": ["diagnosis"]}]
    monkeypatch.setattr(claims, "adjusted", lambda conn, today: {"rows": rows})
    got = call(conn, "claim_items", kind="appeal_soon")
    assert got["count"] == 1 and [i["claim_id"] for i in got["items"]] == [1]


def test_draft_tool_accepts_pasted_source_call(conn):
    """새로 추가한 통화(ax_pasted_calls)도 find_calls가 돌려주는 통화 — 근거 통화로 받는다"""
    conn.execute(
        "INSERT INTO ax_pasted_calls (call_uid, direction, started_at, transcript, patient_id,"
        " added_by, added_at) VALUES ('p-0001', 'inbound', '2026-11-30 10:00:00', '환자: 예약', ?,"
        " '데스크', '2026-11-30 10:05:00')",
        (NEW,),
    )
    got = call(conn, "draft_appointment", **_draft_args(source_call_uid="p-0001"))
    assert "error" not in got and got["source_call_uid"] == "p-0001"
    from backend.assistant import card_detail

    detail = card_detail(conn, "draft", str(got["id"]))
    assert detail["source_call_uid"] == "p-0001" and detail["source_call_pool"] == 0
    assert card_detail(conn, "call", "p-0001")["call_uid"] == "p-0001"


def test_draft_tool_unknown_source_call(conn):
    got = call(conn, "draft_appointment", **_draft_args(source_call_uid="c-없음"))
    assert got == {"error": ["unknown_call"]}
    assert conn.execute("SELECT COUNT(*) FROM ax_emr_writes").fetchone()[0] == 0
