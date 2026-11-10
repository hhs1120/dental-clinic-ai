"""⑨ 미예약 환자 API (backend/api.py GET /missed·POST /missed/{patient_id}/contacts, 설계
docs/superpowers/specs/2026-11-10-missed-patients-design.md §4·§6)."""

import pytest

from backend import api, db, missed
from tests.backend.test_api_calls import _client
from tests.backend.test_missed import Source


@pytest.fixture
def client(tmp_path):
    """미예약 환자 둘 — 1(최근 방문 + 진료계획)·2(오래전 방문, 스케일링 칩만)"""
    s = Source(tmp_path / "source.sqlite")
    for pid in (1, 2):
        s.patient(pid)
    s.appt(1, "2026-10-01")
    s.plan(1, "2026-10-01 10:30:00", [("U0009", "46")])
    s.appt(2, "2025-06-01")
    s.app().close()
    yield _client(tmp_path)
    api.app.dependency_overrides.clear()


def test_get_missed_shape(client):
    r = client.get("/missed").json()
    assert set(r) == {"date", "chip_counts", "rows", "rules"}
    assert r["date"] == "2026-12-01"
    assert set(r["chip_counts"]) == set(missed.CHIPS)
    assert [x["patient_id"] for x in r["rows"]] == [1, 2]
    row = r["rows"][0]
    assert {
        "patient_id",
        "chart_no",
        "name",
        "age",
        "phone",
        "last_visit",
        "balance",
        "chips",
        "score",
        "rank",
        "contact",
    } <= set(row)
    assert r["rules"] and {"name", "value", "reasons"} == set(r["rules"][0])


def test_post_contact_then_hidden(client, tmp_path):
    body = {"result": "booked", "channel": "call", "part": "데스크"}
    assert client.post("/missed/1/contacts", json=body).json() == {"ok": True}
    assert [x["patient_id"] for x in client.get("/missed").json()["rows"]] == [2]
    body = {"result": "later", "channel": "text", "part": "상담실"}
    assert client.post("/missed/2/contacts", json=body).json() == {"ok": True}
    (row,) = client.get("/missed").json()["rows"]
    assert row["contact"] == {
        "result": "later",
        "channel": "text",
        "by": "상담실",
        "at": "2026-11-05 14:30:00",
    }
    c = db.connect(tmp_path / "app.sqlite", tmp_path / "source.sqlite")
    assert c.execute("SELECT COUNT(*) FROM ax_missed_contacts").fetchone()[0] == 2
    c.close()


def test_post_contact_bad_400(client):
    for pid, body in (
        (1, {"result": "maybe", "channel": "call", "part": "데스크"}),
        (1, {"result": "booked", "channel": "fax", "part": "데스크"}),
        (99, {"result": "booked", "channel": "call", "part": "데스크"}),
    ):
        assert client.post(f"/missed/{pid}/contacts", json=body).status_code == 400
