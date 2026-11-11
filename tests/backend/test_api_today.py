"""오늘 화면·확인함 API (backend/api.py GET /today·POST /calls/{call_uid}/check, 기능 정의 §4 오늘 화면)."""

import json
import sqlite3

import pytest

from backend import api, autoclose, db
from generator.clinic import master_values_rows
from tests.backend.call_db import LABELS, classify_row, make_call_db
from tests.backend.test_api_calls import _client


def _app(tmp_path):
    return db.connect(tmp_path / "app.sqlite", tmp_path / "source.sqlite")


@pytest.fixture
def client(tmp_path):
    """통화 화면 테스트 DB + master 값. c-0001은 할 일 목록이 빈 분류로 바꿔 둔다(오늘 할 일에 안 뜸)"""
    c = make_call_db(tmp_path)
    c.execute("DELETE FROM ax_call_classifications WHERE call_uid = 'c-0001'")
    classify_row(c, "c-0001", todo=[])
    c.close()
    s = sqlite3.connect(tmp_path / "source.sqlite")
    s.executemany("INSERT INTO ax_master_values VALUES (?, ?)", master_values_rows())
    s.commit()
    s.close()
    yield _client(tmp_path)
    api.app.dependency_overrides.clear()


def _judge(tmp_path, uid, **on):
    c = _app(tmp_path)
    c.execute(
        "INSERT INTO ax_judgments (call_uid, labels, judged_by, judged_at)"
        " VALUES (?, ?, '데스크', '2026-11-05 09:00:00')",
        (uid, json.dumps(LABELS | {"has_todo": 0} | on)),
    )
    c.commit()
    c.close()


def test_get_today_shape(client):
    r = client.get("/today").json()
    assert r["date"] == "2026-12-01"
    assert set(r) == {"date", "cards", "people", "ai"}
    assert set(r["cards"]) == {"appointments", "new_patients", "alert_patients", "todo"}
    assert set(r["people"]) == {
        "urgent_calls",
        "review_calls",
        "unconfirmed",
        "alert_patients",
        "todo_left",
        "judge_pending",
        "refused",
        "appeal_soon",
        "preclaim_warning",
        "recon_open",
    }
    assert set(r["ai"]) == {
        "autoclose_on",
        "auto_total",
        "auto_recent",
        "prep",
        "classify_failed",
        "followup_failed",
    }


def test_check_removes_review(client, tmp_path):
    _judge(tmp_path, "c-0001", emergency=1)
    assert [x["call_uid"] for x in client.get("/today").json()["people"]["review_calls"]] == [
        "c-0001"
    ]
    assert client.get("/calls/c-0001").json()["review"]["flags"] == ["emergency"]
    assert client.post("/calls/c-0001/check", json={"part": "데스크"}).json() == {"ok": True}
    assert client.get("/today").json()["people"]["review_calls"] == []
    assert client.get("/calls/c-0001").json()["review"] is None
    c = _app(tmp_path)
    row = c.execute("SELECT checked_by, checked_at FROM ax_call_checks").fetchone()
    c.close()
    assert tuple(row) == ("데스크", "2026-11-05 14:30:00")


def test_check_not_review_404(client):
    r = client.post("/calls/c-0004/check", json={"part": "데스크"})
    assert r.status_code == 404 and r.json()["detail"] == "확인할 통화가 아닙니다"
    assert client.post("/calls/c-0002/check", json={"part": "데스크"}).status_code == 404


def test_revert_then_review(client, tmp_path):
    c = _app(tmp_path)
    c.execute(
        "INSERT INTO ax_auto_actions (kind, target, result, reason, created_at)"
        " VALUES (?, 'c-0001', ?, ?, '2026-11-05 09:00:00')",
        (autoclose.KIND, json.dumps({"versions": ["v2", "v2"]}), autoclose.REASON),
    )
    autoclose.set_policy(c, autoclose.KIND, True)
    aid = c.execute("SELECT max(id) FROM ax_auto_actions").fetchone()[0]
    c.close()
    before = client.get("/today").json()
    assert before["ai"]["auto_total"] == 1 and before["people"]["review_calls"] == []
    assert client.post(f"/auto-actions/{aid}/revert", json={"part": "데스크"}).status_code == 200
    after = client.get("/today").json()
    assert after["ai"]["auto_total"] == 0 and after["ai"]["auto_recent"] == []
    assert [(x["call_uid"], x["ai"]) for x in after["people"]["review_calls"]] == [
        ("c-0001", "closed")
    ]
