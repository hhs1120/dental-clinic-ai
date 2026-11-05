"""통화 화면 테스트 DB — 생성 통화 몇 건과 활성 v2 분류(test_calls·test_judge_queue·test_blind 등이 같이 쓴다)."""

import json
import sqlite3
from datetime import date

from backend import db
from shared.clinic import Clinic
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


def make_call_db(tmp_path):
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
    return c


BLIND_CLINIC = Clinic(today=date(2026, 12, 1), range_start=date(2026, 1, 1), eval_pool_size=4)
V2_TODO = [{"kind": "contact", "text": "회신 전화", "who": "환자", "after": -1}]


def make_blind_db(tmp_path):
    """판정 풀 4건(정렬 1~4, dev = 1·2) — 블라인드는 b-1(dev)·b-3(test)·b-2(dev, v2 없음)"""
    source = tmp_path / "source.sqlite"
    s = sqlite3.connect(source)
    s.executescript(SOURCE_SCHEMA.read_text(encoding="utf-8"))
    for uid, order, blind_ in (("b-1", 1, 1), ("b-2", 2, 1), ("b-3", 3, 1), ("b-4", 4, 0)):
        s.execute(
            "INSERT INTO ax_calls (call_uid, source, direction, started_at, duration_sec,"
            " has_transcript, transcript, in_eval_pool, eval_sort_order, in_blind_pool)"
            " VALUES (?, 'generated', 'inbound', '2026-11-28 10:00:00', 60, 1, ?, 1, ?, ?)",
            (uid, f"직원: 네\n환자: {uid} 문의", order, blind_),
        )
    s.commit()
    s.close()
    c = db.connect(tmp_path / "app.sqlite", source)
    db.init_schema(c)
    db.set_setting(c, "blind_versions", ["v1", "v2"])
    for uid in ("b-1", "b-2", "b-3", "b-4"):
        classify_row(c, uid, version="v1", todo=["원장님께 전달"], summary=f"{uid} v1 요약")
    for uid in ("b-1", "b-3", "b-4"):
        classify_row(c, uid, version="v2", todo=V2_TODO, summary=f"{uid} v2 요약")
    return c
