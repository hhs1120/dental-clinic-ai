"""⑥ 경영 리포트 API (backend/api.py — GET /report). 설계 docs/superpowers/specs/2026-11-13-report-design.md §5."""

from datetime import date

import pytest
from fastapi.testclient import TestClient

from backend import api
from shared.clinic import Clinic
from tests.backend.test_report import make_source

CLINIC = Clinic(today=date(2026, 12, 1), range_start=date(2026, 1, 1), eval_pool_size=4)


@pytest.fixture
def client(tmp_path):
    make_source(tmp_path / "source.sqlite")
    (tmp_path / "app.sqlite").touch()
    api.app.dependency_overrides[api.get_db_paths] = lambda: (
        tmp_path / "app.sqlite",
        tmp_path / "source.sqlite",
    )
    api.app.dependency_overrides[api.get_clinic] = lambda: CLINIC
    yield TestClient(api.app)
    api.app.dependency_overrides.clear()


def test_api_report_ok(client):
    r = client.get("/report?scope=monthly&period=2026-10")
    assert r.status_code == 200
    body = r.json()
    assert body["period"] == "2026-10" and body["metrics"]["sales"] == 610_000
    assert client.get("/report").json()["period"] == "2026-11"  # 기본 월간·마지막 기간


def test_api_report_bad_period(client):
    assert client.get("/report?scope=monthly&period=1999-01").status_code == 400
    assert client.get("/report?scope=yearly").status_code == 400
