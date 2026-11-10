"""④ 준비표 API (backend/api.py — GET /prep, POST /jobs/prep-regenerate, GET /jobs/{id})."""

import time
from datetime import date

import pytest
from fastapi.testclient import TestClient

from backend import api
from shared.clinic import Clinic
from tests.backend.test_prep import NONE, SUGGEST, FakeGw, make_source, res

CLINIC = Clinic(today=date(2026, 12, 1), range_start=date(2026, 1, 1), eval_pool_size=4)


@pytest.fixture
def client(tmp_path):
    make_source(tmp_path / "source.sqlite").close()
    (tmp_path / "app.sqlite").touch()
    api.app.dependency_overrides[api.get_db_paths] = lambda: (
        tmp_path / "app.sqlite",
        tmp_path / "source.sqlite",
    )
    api.app.dependency_overrides[api.get_clinic] = lambda: CLINIC
    gw = FakeGw(*[res(SUGGEST), res(NONE), res(NONE)] * 3)
    api.app.dependency_overrides[api.get_gateway_factory] = lambda: lambda conn: gw
    yield TestClient(api.app)
    api.app.dependency_overrides.clear()


def _wait(client, job_id):
    for _ in range(100):
        job = client.get(f"/jobs/{job_id}").json()
        if job["status"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError("작업이 끝나지 않음")


def test_get_prep_rows_and_days(client):
    body = client.get("/prep").json()
    assert body["date"] == "2026-12-01" and body["generated_at"] is None
    assert [r["appointment_id"] for r in body["rows"]] == [101, 102, 103]
    assert body["rows"][0]["generated"] is False and body["rows"][0]["suggestion"] is None
    assert body["rows"][0]["chips"][0] == "appointment"
    days = {d["date"]: d["count"] for d in body["days"]}
    assert list(days)[0] == "2026-11-24" and len(days) == 8
    assert days["2026-12-01"] == 3 and days["2026-11-24"] == 0


def test_get_prep_out_of_range_400(client):
    assert client.get("/prep?date=2026-11-24").status_code == 200
    assert client.get("/prep?date=2026-11-23").status_code == 400
    assert client.get("/prep?date=2026-12-02").status_code == 400
    assert client.get("/prep?date=oops").status_code == 422


def test_job_done_after_build(client):
    job = _wait(client, client.post("/jobs/prep-regenerate?date=2026-12-01").json()["job_id"])
    assert job["status"] == "done" and job["done"] == job["total"] == 3
    body = client.get("/prep").json()
    first = body["rows"][0]
    assert first["generated"] is True and first["suggestion"]["action"] == "suggest"
    assert first["suggestion_valid"] is True and first["stale"] is False
    assert first["changed"] == [] and first["made_at"] == body["generated_at"] is not None


def test_stale_row_lists_changed_facts(client, tmp_path):
    """저장 때 사실과 지금 사실을 제안 입력(llm_facts) 키로 비교해 바뀐 것만 — 화면의 "제안 만든 뒤 바뀜" """
    import json

    from backend import db

    _wait(client, client.post("/jobs/prep-regenerate?date=2026-12-01").json()["job_id"])
    conn = db.connect(tmp_path / "app.sqlite", tmp_path / "source.sqlite")
    facts = json.loads(
        conn.execute("SELECT facts FROM ax_prep_sheets WHERE appointment_id = 101").fetchone()[0]
    )
    old = facts | {"balance": 50000, "alerts": facts["alerts"][1:]}
    with conn:  # 제안을 만들 때는 미수가 50000원, 항응고제 주의사항이 없었던 것으로
        conn.execute(
            "UPDATE ax_prep_sheets SET facts = ?, facts_hash = 'old' WHERE appointment_id = 101",
            (json.dumps(old),),
        )
    conn.close()
    row = client.get("/prep").json()["rows"][0]
    assert row["stale"] is True
    assert row["changed"] == [
        {"key": "balance", "before": "50000", "after": "30000"},
        {"key": "alert.ANTICOAG", "before": None, "after": facts_alert(facts)},
    ]


def facts_alert(facts):
    a = facts["alerts"][0]
    return f"{a['label']} — {a['action']}"


def test_regenerate_returns_running_job(client, tmp_path):
    from backend import db

    conn = db.connect(tmp_path / "app.sqlite", tmp_path / "source.sqlite")
    db.init_schema(conn)
    with conn:
        conn.execute(
            "INSERT INTO ax_jobs (id, kind, arg, status, started_at)"
            " VALUES (7, 'prep', '2026-12-01', 'running', datetime('now', 'localtime'))"
        )
    conn.close()
    assert client.post("/jobs/prep-regenerate?date=2026-12-01").json()["job_id"] == 7
    other = client.post("/jobs/prep-regenerate?date=2026-11-30").json()["job_id"]
    assert other != 7
    _wait(client, other)  # 작업 스레드가 테스트 뒤에 tmp DB에 쓰지 않게 끝을 기다린다


def test_regenerate_ignores_stale_running_job(client, tmp_path):
    """서버가 죽어 running으로 남은 옛 작업은 막지 않는다"""
    from backend import db

    conn = db.connect(tmp_path / "app.sqlite", tmp_path / "source.sqlite")
    db.init_schema(conn)
    with conn:
        conn.execute(
            "INSERT INTO ax_jobs (id, kind, arg, status, started_at)"
            " VALUES (7, 'prep', '2026-12-01', 'running', '2026-01-01T00:00:00')"
        )
    conn.close()
    assert client.post("/jobs/prep-regenerate?date=2026-12-01").json()["job_id"] != 7


def test_regenerate_out_of_range_400(client):
    assert client.post("/jobs/prep-regenerate?date=2026-11-01").status_code == 400


def test_unknown_job_404(client):
    assert client.get("/jobs/999").status_code == 404


def test_job_failure_is_recorded(client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("터짐")

    monkeypatch.setattr(api.prep, "build", boom)
    job = _wait(client, client.post("/jobs/prep-regenerate?date=2026-12-01").json()["job_id"])
    assert job["status"] == "failed" and "터짐" in job["error"] and job["finished_at"]


def test_job_connect_failure_is_recorded(client, monkeypatch):
    real = api.db.connect
    calls = []

    def flaky(*a, **k):
        calls.append(1)
        if len(calls) == 2:  # 요청 연결 다음, 작업 스레드의 첫 연결
            raise RuntimeError("연결 실패")
        return real(*a, **k)

    monkeypatch.setattr(api.db, "connect", flaky)
    job = _wait(client, client.post("/jobs/prep-regenerate?date=2026-12-01").json()["job_id"])
    assert job["status"] == "failed" and "연결 실패" in job["error"]
