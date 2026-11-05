"""엇갈림 계산 (backend/disagreements.py, 앱 설계 §1, 분류기 설계 "엇갈림")."""

import json

from backend import disagreements

BASE = {
    "primary_purpose": "book",
    "counterpart": "patient",
    "urgency": "normal",
    "emergency": 0,
    "complaint": 0,
    "price_inquiry": 0,
    "wants_appt": 1,
    "appt_confirmed": 1,
    "has_todo": 0,
}


def _call(conn, fallback=0):
    return conn.execute(
        "INSERT INTO ax_llm_calls (task, model_alias, model_actual, fallback_used, ok, created_at)"
        " VALUES ('t', 'a', 'm', ?, 1, '2026-11-05 10:00:00')",
        (fallback,),
    ).lastrowid


def _put(conn, uid, a=None, b=None, todo=(), a_status="ok", b_status="ok", fb=(0, 0, 0)):
    conn.execute(
        "INSERT INTO ax_call_classifications (call_uid, model_alias, model_actual, prompt_version,"
        " status, labels, summary, todo, llm_call_id, summary_model_actual, summary_llm_call_id)"
        " VALUES (?, 'classify', 'm', 'v1', ?, ?, '요약', ?, ?, 'm', ?)",
        (
            uid,
            a_status,
            json.dumps(BASE | (a or {})),
            json.dumps(list(todo)),
            _call(conn, fb[0]),
            _call(conn, fb[1]),
        ),
    )
    conn.execute(
        "INSERT INTO ax_model_judgments (call_uid, model_alias, model_actual, prompt_version,"
        " status, labels, llm_call_id) VALUES (?, 'judge', 'm', 'v1', ?, ?, ?)",
        (uid, b_status, json.dumps(BASE | (b or {})), _call(conn, fb[2])),
    )


def _rows(conn):
    return [
        dict(r, items=json.loads(r["items"]))
        for r in conn.execute("SELECT * FROM ax_call_disagreements ORDER BY call_uid")
    ]


def test_identical_no_row(conn):
    _put(conn, "c-1")
    assert disagreements.compute(conn, "v1", "v1") == 0 and _rows(conn) == []


def test_one_item_differs_row(conn):
    _put(conn, "c-1", b={"complaint": 1})
    assert disagreements.compute(conn, "v1", "v1") == 1
    (r,) = _rows(conn)
    assert r["items"] == ["complaint"] and r["is_core"] == 0
    assert (r["fallback"], r["todo_mismatch"]) == (0, 0)


def test_core_item(conn):
    _put(conn, "c-1", a={"urgency": "urgent"})
    disagreements.compute(conn, "v1", "v1")
    (r,) = _rows(conn)
    assert r["items"] == ["urgency"] and r["is_core"] == 1


def test_urgency_normal_low_no_row(conn):
    """급함은 urgent 여부만 비교(평가 설계 §2.5) — normal ↔ low는 엇갈림이 아니다"""
    _put(conn, "c-1", b={"urgency": "low"})
    assert disagreements.compute(conn, "v1", "v1") == 0


def test_fallback_marked(conn):
    _put(conn, "c-1", fb=(0, 0, 1))  # 판정 호출 폴백
    _put(conn, "c-2", fb=(0, 1, 0))  # 요약 호출 폴백
    assert disagreements.compute(conn, "v1", "v1") == 2
    for r in _rows(conn):
        assert r["items"] == [] and r["fallback"] == 1 and r["is_core"] == 0


def test_todo_mismatch(conn):
    _put(conn, "c-1", todo=["직원이 원장님께 전달함"])
    _put(conn, "c-2", a={"has_todo": 1}, b={"has_todo": 1}, todo=["직원이 회신함"])
    assert disagreements.compute(conn, "v1", "v1") == 1
    (r,) = _rows(conn)
    assert r["call_uid"] == "c-1" and r["todo_mismatch"] == 1 and r["items"] == []


def test_only_both_ok(conn):
    _put(conn, "c-1", b={"complaint": 1}, b_status="failed")
    _put(conn, "c-2", a={"complaint": 1}, a_status="failed")
    assert disagreements.compute(conn, "v1", "v1") == 0


def test_recompute_replaces_pair(conn):
    _put(conn, "c-1", b={"complaint": 1})
    disagreements.compute(conn, "v1", "v1")
    conn.execute("UPDATE ax_model_judgments SET labels = ?", (json.dumps(BASE),))
    assert disagreements.compute(conn, "v1", "v1") == 0 and _rows(conn) == []
