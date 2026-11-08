"""통화·판정·블라인드·정확도 API (backend/api.py, 통화·② 판정 화면 설계 "API")."""

from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient

from backend import api
from backend.harness.store import SqliteLlmLog
from shared.clinic import Clinic
from shared.llm import FakeGateway
from tests.backend.call_db import LABELS, make_blind_db, make_call_db

CLINIC = Clinic(today=date(2026, 12, 1), range_start=date(2026, 1, 1), eval_pool_size=4)


def _client(tmp_path):
    api.app.dependency_overrides[api.get_db_paths] = lambda: (
        tmp_path / "app.sqlite",
        tmp_path / "source.sqlite",
    )
    api.app.dependency_overrides[api.get_clinic] = lambda: CLINIC
    api.app.dependency_overrides[api.get_now] = lambda: datetime(2026, 11, 5, 14, 30, 0)
    api.app.dependency_overrides[api.get_gateway_factory] = lambda: (
        lambda conn: FakeGateway(SqliteLlmLog(conn), scope="runtime")
    )
    return TestClient(api.app)


@pytest.fixture
def client(tmp_path):
    make_call_db(tmp_path).close()
    yield _client(tmp_path)
    api.app.dependency_overrides.clear()


@pytest.fixture
def bclient(tmp_path):
    make_blind_db(tmp_path).close()
    yield _client(tmp_path)
    api.app.dependency_overrides.clear()


def test_labels(client):
    r = client.get("/labels").json()
    assert r["purpose"]["book"] == "예약 잡기" and r["flag"]["has_todo"] == "할 일 있음"


def test_today_and_detail(client):
    b = client.get("/calls/today").json()
    assert {i["call_uid"] for i in b["internal"]} == {"c-0001", "c-0004"}
    assert client.get("/calls/c-0001").json()["call"]["name"] == "김철수"
    assert client.get("/calls/c-0002").status_code == 404


def test_list_calls_filter(client):
    r = client.get("/calls", params={"q": "C-00001"}).json()
    assert r["days"][0]["calls"][0]["call_uid"] == "c-0001"
    assert client.get("/calls", params={"filter": "nope"}).status_code == 422


def test_add_call_and_reject_empty(client):
    r = client.post(
        "/calls",
        json={"direction": "inbound", "transcript": "직원: 네\n환자: 예약", "part": "데스크"},
    )
    assert r.status_code == 200 and r.json() == {"call_uid": "p-0001"}
    d = client.get("/calls/p-0001").json()
    assert d["call"]["at"] == "2026-12-01 14:30:00" and d["classification"]["status"] == "ok"
    bad = client.post("/calls", json={"direction": "inbound", "transcript": " ", "part": "데스크"})
    assert bad.status_code == 422
    assert client.post("/calls/p-0001/classify").status_code == 200
    assert client.post("/calls/c-9999/classify").status_code == 404


def test_todo_done_and_missing(client):
    item = client.get("/calls/today").json()["internal"][0]
    url = f"/calls/{item['call_uid']}/todos/{item['item']}/done"
    assert client.post(url, json={"part": "데스크", "done": True}).status_code == 200
    assert client.get("/calls/today").json()["done"][0]["done_by"] == "데스크"
    missing = client.post(
        f"/calls/{item['call_uid']}/todos/9/done", json={"part": "데스크", "done": True}
    )
    assert missing.status_code == 404


def test_judge_flow(client):
    assert client.get("/judge/next").json() is None
    client.post(
        "/calls", json={"direction": "inbound", "transcript": "직원: 네", "part": "데스크"}
    )
    import sqlite3

    # 새 통화가 표본·엇갈림이 아닐 수 있어 직접 큐에 넣는다
    paths = api.app.dependency_overrides[api.get_db_paths]()
    c = sqlite3.connect(paths[0])
    c.execute("INSERT OR IGNORE INTO ax_judge_state VALUES ('c-0001', 'pending', 'x')")
    c.commit()
    c.close()
    n = client.get("/judge/next").json()
    assert "labels" not in n
    out = client.post(
        "/judgments", json={"call_uid": n["call_uid"], "labels": LABELS, "part": "데스크"}
    ).json()
    assert set(out) == {"classifier", "judge"}
    assert client.post("/calls/c-0003/skip").status_code == 200


def test_blind_flow(bclient):
    p = bclient.get("/blind/next").json()
    assert p["call_uid"] == "b-1"
    out = bclient.post(
        "/blind-votes",
        json={
            "left_id": p["left"]["classification_id"],
            "right_id": p["right"]["classification_id"],
            "choice": "tie",
            "part": "데스크",
        },
    ).json()
    assert sorted(out.values()) == ["v1", "v2"]
    bad = bclient.post(
        "/blind-votes", json={"left_id": 1, "right_id": 2, "choice": "x", "part": "d"}
    )
    assert bad.status_code == 422


def test_accuracy(bclient):
    r = bclient.get("/metrics/accuracy").json()
    assert set(r) == {"agreement", "blind", "human_share", "versions"}
    assert r["versions"]["blind"] == ["v1", "v2"]


def test_judgment_rejects_pool_call_and_bad_labels(client):
    assert (
        client.post(
            "/judgments", json={"call_uid": "c-0002", "labels": LABELS, "part": "데스크"}
        ).status_code
        == 404
    )
    import sqlite3

    paths = api.app.dependency_overrides[api.get_db_paths]()
    c = sqlite3.connect(paths[0])
    c.execute("INSERT INTO ax_judge_state VALUES ('c-0001', 'pending', 'x')")
    c.commit()
    c.close()
    bad = client.post(
        "/judgments",
        json={"call_uid": "c-0001", "labels": {"urgency": "soon"}, "part": "데스크"},
    )
    assert bad.status_code == 422


def test_add_call_gateway_exception_still_returns_uid(client):
    class Boom:
        def complete(self, alias, **kw):
            raise RuntimeError("상한")

    api.app.dependency_overrides[api.get_gateway_factory] = lambda: lambda conn: Boom()
    r = client.post(
        "/calls", json={"direction": "inbound", "transcript": "직원: 네", "part": "데스크"}
    )
    assert r.status_code == 200 and r.json() == {"call_uid": "p-0001"}
    assert client.get("/calls/p-0001").json()["classification"]["status"] is None


def test_blind_vote_not_offered_422(bclient):
    r = bclient.post(
        "/blind-votes", json={"left_id": 99999, "right_id": 99998, "choice": "tie", "part": "d"}
    )
    assert r.status_code == 422


def test_list_calls_limit(client):
    """화면이 더 불러온 만큼 한 번에 다시 받는다(체크·새 통화 뒤 쪽 유지) — 1~500"""
    r = client.get("/calls", params={"limit": 1}).json()
    assert sum(len(d["calls"]) for d in r["days"]) == 1 and r["next"]
    assert client.get("/calls", params={"limit": 0}).status_code == 422
    assert client.get("/calls", params={"limit": 501}).status_code == 422


def _close(tmp_path, uid):
    """자동 종결 행을 바로 넣는다(조건 계산은 test_autoclose가 본다)"""
    from backend import autoclose, db

    c = db.connect(tmp_path / "app.sqlite", tmp_path / "source.sqlite")
    c.execute(
        "INSERT INTO ax_auto_actions (kind, target, result, reason, created_at)"
        " VALUES (?, ?, '{}', ?, '2026-11-05 09:00:00')",
        (autoclose.KIND, uid, autoclose.REASON),
    )
    c.commit()
    aid = c.execute("SELECT max(id) FROM ax_auto_actions").fetchone()[0]
    c.close()
    return aid


def test_board_hides_closed(client, tmp_path):
    _close(tmp_path, "c-0001")
    b = client.get("/calls/today").json()
    uids = {i["call_uid"] for col in ("internal", "contact", "done") for i in b[col]}
    assert "c-0001" not in uids and "c-0004" in uids


def test_list_and_detail_show_closed(client, tmp_path):
    _close(tmp_path, "c-0001")
    cards = [c for d in client.get("/calls").json()["days"] for c in d["calls"]]
    closed = {c["call_uid"]: c["closed"] for c in cards}
    assert closed["c-0001"] is True and closed["c-0004"] is False
    d = client.get("/calls/c-0001").json()
    assert d["autoclose"]["reason"] and d["autoclose"]["at"] == "2026-11-05 09:00:00"
    assert client.get("/calls/c-0004").json()["autoclose"] is None


def test_revert_returns_to_board(client, tmp_path):
    aid = _close(tmp_path, "c-0001")
    r = client.post(f"/auto-actions/{aid}/revert", json={"part": "데스크"})
    assert r.status_code == 200
    b = client.get("/calls/today").json()
    assert "c-0001" in {i["call_uid"] for i in b["internal"]}
    assert client.get("/calls/c-0001").json()["autoclose"] is None
    assert client.post(f"/auto-actions/{aid}/revert", json={"part": "데스크"}).status_code == 409
    assert client.post("/auto-actions/999/revert", json={"part": "데스크"}).status_code == 404


def test_detail_and_revert_pasted(client, tmp_path):
    uid = client.post(
        "/calls",
        json={"direction": "inbound", "transcript": "직원: 네\n환자: 확인이요", "part": "데스크"},
    ).json()["call_uid"]
    aid = _close(tmp_path, uid)
    assert client.get(f"/calls/{uid}").json()["autoclose"]["id"] == aid
    assert client.post(f"/auto-actions/{aid}/revert", json={"part": "데스크"}).status_code == 200
    assert client.get(f"/calls/{uid}").json()["autoclose"] is None
