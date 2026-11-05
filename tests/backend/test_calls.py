"""통화 화면 조회·쓰기 (backend/calls.py, 통화·② 판정 화면 설계 "통화 화면")."""

import json
import sqlite3
from datetime import date

import pytest

from backend import calls, db
from tests.backend.conftest import SOURCE_SCHEMA

TODAY = date(2026, 12, 1)
LABELS = {
    "primary_purpose": "book",
    "counterpart": "patient",
    "urgency": "normal",
    "emergency": 0,
    "complaint": 0,
    "price_inquiry": 0,
    "wants_appt": 1,
    "appt_confirmed": 0,
    "has_todo": 1,
}
TODO = [
    {"kind": "internal", "text": "원장님께 여쭙기", "who": "", "after": -1},
    {"kind": "contact", "text": "회신 전화", "who": "김철수", "after": 0},
]


def _call(s, uid, at, *, patient=None, name=None, pool=0, transcript="직원: 네\n환자: 예약"):
    s.execute(
        "INSERT INTO ax_calls (call_uid, source, patient_id, caller_phone, caller_name, direction,"
        " started_at, duration_sec, has_transcript, transcript, in_eval_pool)"
        " VALUES (?, 'generated', ?, '010-1111-2222', ?, 'inbound', ?, 60, 1, ?, ?)",
        (uid, patient, name, at, transcript, pool),
    )


def classify_row(conn, uid, *, version="v2", labels=None, todo=None, status="ok", summary="요약"):
    conn.execute(
        "INSERT INTO ax_llm_calls (task, model_alias, model_actual, ok, created_at)"
        " VALUES ('classify', 'classify', 'm', 1, '2026-11-05 09:00:00')"
    )
    log = conn.execute("SELECT max(id) FROM ax_llm_calls").fetchone()[0]
    conn.execute(
        "INSERT INTO ax_call_classifications (call_uid, model_alias, model_actual, prompt_version,"
        " status, labels, summary, todo, llm_call_id, summary_model_actual, summary_llm_call_id)"
        " VALUES (?, 'classify', 'm', ?, ?, ?, ?, ?, ?, 'm', ?)",
        (
            uid,
            version,
            status,
            json.dumps(labels or LABELS) if status == "ok" else None,
            summary if status == "ok" else None,
            json.dumps(TODO if todo is None else todo) if status == "ok" else None,
            log,
            log,
        ),
    )
    conn.commit()
    return conn.execute(
        "SELECT id FROM ax_call_classifications WHERE call_uid = ? AND prompt_version = ?",
        (uid, version),
    ).fetchone()[0]


def judge_row(conn, uid, labels, version="v2"):
    conn.execute(
        "INSERT INTO ax_llm_calls (task, model_alias, model_actual, ok, created_at)"
        " VALUES ('judge', 'judge', 'solar', 1, '2026-11-05 09:00:00')"
    )
    log = conn.execute("SELECT max(id) FROM ax_llm_calls").fetchone()[0]
    conn.execute(
        "INSERT INTO ax_model_judgments (call_uid, model_alias, model_actual, prompt_version,"
        " status, labels, llm_call_id) VALUES (?, 'judge', 'solar', ?, 'ok', ?, ?)",
        (uid, version, json.dumps(labels), log),
    )
    conn.commit()


@pytest.fixture
def cconn(tmp_path):
    """생성 통화: c-0001 7일 안(환자 김철수), c-0002 7일 안·판정 풀, c-0003 7일 밖, c-0004 7일 안(이름만)"""
    source = tmp_path / "source.sqlite"
    s = sqlite3.connect(source)
    s.executescript(SOURCE_SCHEMA.read_text(encoding="utf-8"))
    s.execute(
        "INSERT INTO emr_patients (id, chart_no, name, birth_date, sex, phone, registered_at)"
        " VALUES (1, 'C-00001', '김철수', '1980-01-01', 'M', '010-1111-2222', '2024-01-01')"
    )
    _call(s, "c-0001", "2026-11-28 10:00:00", patient=1)
    _call(s, "c-0002", "2026-11-29 10:00:00", patient=1, pool=1)
    _call(s, "c-0003", "2026-11-20 10:00:00", name="박영희")
    _call(s, "c-0004", "2026-11-30 09:00:00", name="이민수")
    s.commit()
    s.close()
    c = db.connect(tmp_path / "app.sqlite", source)
    db.init_schema(c)
    db.set_setting(c, "active_prompt_version", "v2")
    db.set_setting(c, "prompt_versions", {"judge": "v2"})
    for uid in ("c-0001", "c-0002", "c-0003"):
        classify_row(c, uid)
    classify_row(c, "c-0004", labels=LABELS | {"urgency": "urgent"})
    c.commit()
    yield c
    c.close()


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
