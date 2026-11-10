"""④ 준비표 채점(평가 설계 §4) — evaluation/prep.py score. 관련 여부는 앱이 저장한 related가 아니라 master로 다시 센다."""

import json
import sqlite3
from datetime import date

import pytest

from evaluation import prep as prep_eval
from tests.backend.test_prep import make_source

DAY = date(2026, 12, 1)


def row(appt, procedure_code, alerts=(), chips=("appointment",), **facts):
    return {
        "appointment_id": appt,
        "procedure_code": procedure_code,
        "alerts": [{"code": c, "label": c, "related": rel, "action": "x"} for c, rel in alerts],
        "balance": facts.get("balance", 0),
        "scaling_unused": facts.get("scaling_unused", False),
        "family_recall": facts.get("family_recall", []),
        "open_plan": facts.get("open_plan", []),
        "chips": list(chips),
    }


def suggest(*keys):
    return {
        "action": "suggest",
        "text": "x",
        "used_facts": [{"key": k, "value": "v"} for k in keys],
    }


NONE = {"action": "none", "text": "", "used_facts": []}


@pytest.fixture
def conn(tmp_path):
    make_source(tmp_path / "source.sqlite").close()
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("ATTACH DATABASE ? AS source", (str(tmp_path / "source.sqlite"),))
    c.execute(
        "CREATE TABLE ax_prep_sheets (sheet_date, patient_id, appointment_id, facts, facts_hash,"
        " suggestion, suggestion_valid, prompt_version, model, created_at)"
    )
    return c


def put(conn, r, s, valid=True, version="v1"):
    conn.execute(
        "INSERT INTO ax_prep_sheets VALUES ('2026-12-01', 1, ?, ?, 'h', ?, ?, ?, 'm', 'now')",
        (r["appointment_id"], json.dumps(r), s and json.dumps(s), int(valid), version),
    )


def test_four_metrics(conn):
    # 101 발치 + 항응고제(관련) → 반영함 / 102 스케일링 + 구역반사(관련) → 빠뜨림 + 무관 HTN 언급
    put(
        conn,
        row(101, "U0011", [("ANTICOAG", True)], balance=30000),
        suggest("alert.ANTICOAG", "balance"),
    )
    put(conn, row(102, "U0001", [("GAG", True), ("HTN", False)]), suggest("alert.HTN"))
    put(conn, row(103, "U0019"), NONE)  # 칩이 다 꺼진 행 — none
    s = prep_eval.score(conn, [DAY])
    assert s["valid"] == {"k": 3, "n": 3}
    assert s["reflect"] == {"k": 1, "n": 2}
    assert s["over_mention"] == {"k": 1, "n": 3}
    assert s["over_suggest"] == {"k": 0, "n": 1}
    assert s["not_generated"] == 0 and s["by_prompt_version"] == {"v1": 3}


def test_null_suggestion_fails_valid_and_reflect(conn):
    put(conn, row(101, "U0011", [("ANTICOAG", True)]), None, valid=False)
    s = prep_eval.score(conn, [DAY])
    assert s["valid"] == {"k": 0, "n": 1} and s["reflect"] == {"k": 0, "n": 1}
    assert s["not_generated"] == 2  # 102·103은 만들지 않음


def test_relatedness_from_master_not_stored(conn):
    # 앱이 related를 틀리게 저장해도(항응고제를 무관으로) 채점은 master 기준 — 발치와 관련
    put(conn, row(101, "U0011", [("ANTICOAG", False)]), NONE)
    s = prep_eval.score(conn, [DAY])
    assert s["reflect"] == {"k": 0, "n": 1}
    assert s["over_suggest"] == {"k": 0, "n": 0}  # 주의 칩이 켜진 행이라 과잉 제안 모수 밖


def test_suggest_with_all_chips_off_is_over(conn):
    put(conn, row(103, "U0019", chips=("appointment", "unconfirmed")), suggest("procedure"))
    assert prep_eval.score(conn, [DAY])["over_suggest"] == {"k": 1, "n": 1}
