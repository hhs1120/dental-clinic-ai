"""자동 개선 이력 API (기능 정의 §4 ② "자동 개선", 자동 개선 설계 §6)."""

import json

import pytest
from fastapi.testclient import TestClient

from backend import db
from backend.api import app, get_db_paths, get_improve_report


@pytest.fixture
def client(source_paths, tmp_path):
    report = tmp_path / "improve_runs.jsonl"
    app.dependency_overrides[get_db_paths] = lambda: source_paths
    app.dependency_overrides[get_improve_report] = lambda: report
    conn = db.connect(*source_paths)
    db.init_schema(conn)
    conn.close()
    yield TestClient(app), report, source_paths
    app.dependency_overrides.clear()


def _row(cand, adopted=False, at="2026-11-17T10:00:00"):
    return {"at": at, "task": "classify", "item": "has_todo", "base": "v2", "candidate": cand,
            "attempt": 1, "error_types": [{"name": "전달 약속 놓침", "n": 3}],
            "changed_rule": "- has_todo: 규칙", "gate": {"passed": adopted, "failed": [], "items": {}},
            "failed": [] if adopted else ["target_up"], "adopted": adopted, "cost_usd": 0.9}  # fmt: skip


def test_improve_runs_empty(client):
    c, _, _ = client
    got = c.get("/improve/runs").json()
    assert got["runs"] == [] and got["pending"] is None and got["active"]


def test_improve_runs_reads_report_newest_first(client):
    c, report, _ = client
    rows = [_row("v2-imp1", at="2026-11-17T10:00:00"), _row("v2-imp2", at="2026-11-17T11:00:00")]
    report.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    got = c.get("/improve/runs").json()
    assert [r["candidate"] for r in got["runs"]] == ["v2-imp2", "v2-imp1"]
    assert got["runs"][0]["failed"] == ["target_up"]


def test_improve_runs_has_pending(client):
    c, _, paths = client
    conn = db.connect(*paths)
    with conn:
        db.set_setting(conn, "pending_prompt_version", "v2-imp1")
    conn.close()
    assert c.get("/improve/runs").json()["pending"] == "v2-imp1"
