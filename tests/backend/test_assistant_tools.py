"""AI 비서 도구(backend/assistant_tools.py, AI 비서 설계 §4.2) — emr_bridge 테스트의 손으로 만든 source를 그대로 쓴다."""

# ruff: noqa: F401, F811  (다른 테스트 모듈의 fixture를 가져와 쓴다)

from datetime import date

import pytest

from backend import assistant_tools, emr_bridge
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
    assert assistant_tools.TOOLS_VERSION == "v1"
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


def test_free_slots_tool_closed_thursday(conn):
    got = call(conn, "free_slots", date="2026-12-03", procedure="스케일링")
    assert got == {"date": "2026-12-03", "closed": True, "slots": []}


def test_free_slots_tool_open_day_and_role(conn):
    got = call(conn, "free_slots", date=WED, procedure="스케일링")
    assert got["closed"] is False and got["slots"]
    assert {s["staff_name"] for s in got["slots"]} == {"임재윤"}
    assert call(conn, "free_slots", date=WED, procedure="스케일링", staff="원장")["slots"] == []
    assert call(conn, "free_slots", date=WED, procedure="없는시술") == {
        "error": ["unknown_procedure"]
    }


def _draft_args(**kw):
    return {
        "chart_no": "C-00002",
        "date": WED,
        "start": "10:00",
        "staff": "임재윤",
        "procedure": "스케일링",
    } | kw


def test_draft_tool_rejects_bad_slot(conn):
    got = call(conn, "draft_appointment", **_draft_args(date="2026-12-03"))
    assert got == {"error": ["closed_day"]}
    assert conn.execute("SELECT COUNT(*) FROM ax_emr_writes").fetchone()[0] == 0
    bad = call(conn, "draft_appointment", **_draft_args(procedure="없는시술"))
    assert bad == {"error": ["unknown_procedure"]}
    assert call(conn, "draft_appointment", **_draft_args(chart_no="C-99"))["error"] == [
        "unknown_patient"
    ]


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


def test_draft_tool_unknown_source_call(conn):
    got = call(conn, "draft_appointment", **_draft_args(source_call_uid="c-없음"))
    assert got == {"error": ["unknown_call"]}
    assert conn.execute("SELECT COUNT(*) FROM ax_emr_writes").fetchone()[0] == 0
