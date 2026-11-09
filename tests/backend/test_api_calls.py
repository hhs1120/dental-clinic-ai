"""통화·판정·블라인드·정확도 API (backend/api.py, 통화·② 판정 화면 설계 "API")."""

import json
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


def _close(tmp_path, uid, *, versions=("v2", "v2"), policy=True):
    """자동 종결 행을 바로 넣는다(조건 계산은 test_autoclose가 본다). versions = 닫을 때의 활성 버전 쌍"""
    from backend import autoclose, db

    c = db.connect(tmp_path / "app.sqlite", tmp_path / "source.sqlite")
    c.execute(
        "INSERT INTO ax_auto_actions (kind, target, result, reason, created_at)"
        " VALUES (?, ?, ?, ?, '2026-11-05 09:00:00')",
        (autoclose.KIND, uid, json.dumps({"versions": list(versions)}), autoclose.REASON),
    )
    autoclose.set_policy(c, autoclose.KIND, policy)
    aid = c.execute("SELECT max(id) FROM ax_auto_actions").fetchone()[0]
    c.close()
    return aid


def _closed_cards(client) -> dict:
    return {
        c["call_uid"]: c["closed"] for d in client.get("/calls").json()["days"] for c in d["calls"]
    }


def test_list_and_detail_show_closed(client, tmp_path):
    _close(tmp_path, "c-0001")
    closed = _closed_cards(client)
    assert closed["c-0001"] is True and closed["c-0004"] is False
    d = client.get("/calls/c-0001").json()
    assert d["autoclose"]["reason"] and d["autoclose"]["at"] == "2026-11-05 09:00:00"
    assert client.get("/calls/c-0004").json()["autoclose"] is None


def test_stale_close_is_not_shown(client, tmp_path):
    """정책을 껐거나 활성 버전이 바뀐 뒤의 종결은 종결로 보지 않는다 — 할 일을 숨기지 않게"""
    _close(tmp_path, "c-0001", policy=False)
    assert _closed_cards(client)["c-0001"] is False
    assert "c-0001" in {i["call_uid"] for i in client.get("/calls/today").json()["internal"]}
    assert client.get("/calls/c-0001").json()["autoclose"] is None


def test_close_from_other_versions_is_not_shown(client, tmp_path):
    _close(tmp_path, "c-0001", versions=("v1", "v2"))
    assert _closed_cards(client)["c-0001"] is False


def test_board_hides_closed(client, tmp_path):
    _close(tmp_path, "c-0001")
    b = client.get("/calls/today").json()
    uids = {i["call_uid"] for col in ("internal", "contact", "done") for i in b[col]}
    assert "c-0001" not in uids and "c-0004" in uids


def test_revert_clears_badge(client, tmp_path):
    """되돌리면 딱지·상세 줄이 빠진다(다시 닫히지 않는 것은 test_autoclose.test_human_revert_sticks)"""
    aid = _close(tmp_path, "c-0001")
    assert client.post(f"/auto-actions/{aid}/revert", json={"part": "데스크"}).status_code == 200
    assert _closed_cards(client)["c-0001"] is False
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


def _plan(tmp_path, uid, version="v2", status="done"):
    """처리안 행과 그 실행의 도구 단계를 바로 넣는다(에이전트는 test_followup이 본다)"""
    from backend import db

    c = db.connect(tmp_path / "app.sqlite", tmp_path / "source.sqlite")
    cid = c.execute(
        "SELECT id FROM ax_call_classifications WHERE call_uid = ? AND prompt_version = ?",
        (uid, version),
    ).fetchone()[0]
    c.execute(
        "INSERT INTO ax_agent_runs (agent, target, status, prompt_version, started_at)"
        " VALUES ('call_followup', ?, 'done', 'v1', '2026-11-05 09:00:00')",
        (uid,),
    )
    run = c.execute("SELECT max(id) FROM ax_agent_runs").fetchone()[0]
    c.execute(
        "INSERT INTO ax_agent_steps (run_id, seq, kind, tool_use_id, tool, args, result, hook_status, created_at)"
        " VALUES (?, 1, 'tool', 't1', 'query_patient', '{\"chart_no\": \"C-00001\"}', '{}', 'pass', '2026-11-05 09:00:01')",
        (run,),
    )
    c.execute(
        "INSERT INTO ax_call_followups (call_uid, classification_id, action, summary, facts, run_id, status,"
        " created_at) VALUES (?, ?, 'doctor_reply', '원장님 확인 뒤 회신', '[\"다음 예약 없음\"]', ?, ?,"
        " '2026-11-05 09:00:02')",
        (uid, cid, run, status),
    )
    c.commit()
    c.close()


def test_board_and_detail_show_followup(client, tmp_path):
    _plan(tmp_path, "c-0001")
    b = client.get("/calls/today").json()
    item = next(i for i in b["internal"] if i["call_uid"] == "c-0001")
    assert item["followup"] == {
        "action": "doctor_reply",
        "summary": "원장님 확인 뒤 회신",
        "status": "done",
    }
    assert next(i for i in b["internal"] if i["call_uid"] == "c-0004")["followup"] is None
    d = client.get("/calls/c-0001").json()["followup"]
    assert d["facts"] == ["다음 예약 없음"] and d["trace"] == [
        {"tool": "query_patient", "args": {"chart_no": "C-00001"}}
    ]


def test_followup_is_per_classification(client, tmp_path):
    from backend import db
    from tests.backend.call_db import classify_row

    c = db.connect(tmp_path / "app.sqlite", tmp_path / "source.sqlite")
    classify_row(c, "c-0001", version="v1")
    c.close()
    _plan(tmp_path, "c-0001", version="v1")
    assert client.get("/calls/c-0001").json()["followup"] is None


def test_labels_has_action(client):
    assert client.get("/labels").json()["action"]["scaling_timing"] == "스케일링 시기 안내"
