"""AI 비서 API (backend/api.py /assistant/*, AI 비서 설계 §6) — 손으로 만든 source + 대본 게이트웨이."""

# ruff: noqa: F401, F811  (다른 테스트 모듈의 fixture를 가져와 쓴다)

import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from backend import api, chat
from tests.backend.test_api import _events
from tests.backend.test_assistant import DONE, PATIENT, Turns, reply
from tests.backend.test_emr_bridge import (
    CLINIC,
    HYGIENIST,
    MISSED,
    NEW,
    SCALING,
    WED,
    _draft,
    conn,
    src,
)

NOW = datetime(2026, 12, 1, 9, 5, 0)


@pytest.fixture
def client(conn, tmp_path):
    api.app.dependency_overrides[api.get_db_paths] = lambda: (
        tmp_path / "a.sqlite",
        tmp_path / "s.sqlite",
    )
    api.app.dependency_overrides[api.get_clinic] = lambda: CLINIC
    api.app.dependency_overrides[api.get_now] = lambda: NOW
    api.app.dependency_overrides[api.get_assistant_models] = lambda: None  # 색인 없음
    yield TestClient(api.app)
    api.app.dependency_overrides.clear()


def _script(*runs, replies=None):
    api.app.dependency_overrides[api.get_chat_gateway] = lambda: (
        lambda c: Turns(c, list(runs), replies or [reply("김철수님 정보입니다")])
    )


def _new_draft(conn, at=f"{WED} 10:00"):
    d = _draft(conn, at, code=SCALING, staff=HYGIENIST, patient=MISSED)
    conn.commit()
    return d["id"]


def test_turn_sse_events(client):
    _script([PATIENT, DONE])
    r = client.post(
        "/assistant/turn", json={"conversation_id": None, "text": "김철수 환자", "part": "데스크"}
    )
    events = _events(r.text)
    kinds = [k for k, _ in events]
    assert kinds[0] == "step" and kinds[-1] == "reply" and "error" not in kinds
    assert events[0][1]["tool"] == "query_patient"
    assert events[-1][1]["text"] == "김철수님 정보입니다" and events[-1][1]["conversation_id"] == 1


def test_turn_validates_and_404s(client):
    _script([DONE])
    assert client.post("/assistant/turn", json={"text": "  ", "part": "데스크"}).status_code == 422
    big = {"text": "가" * 1001, "part": "데스크"}
    assert client.post("/assistant/turn", json=big).status_code == 422
    ghost = {"conversation_id": 99, "text": "안녕", "part": "데스크"}
    assert client.post("/assistant/turn", json=ghost).status_code == 404


def test_turn_works_without_index_for_ask_records(client, monkeypatch):
    def no_chat(*a, **k):
        raise AssertionError("색인이 없으면 ③을 부르지 않는다")

    monkeypatch.setattr(chat, "chat", no_chat)
    ask = ("tool", "ask_records", {"question": "주차 몇 시간 무료?"})
    _script([ask, DONE], replies=[reply("색인이 없어 확인하지 못했습니다")])
    r = client.post("/assistant/turn", json={"text": "주차 알려줘", "part": "데스크"})
    events = _events(r.text)
    assert events[-1][0] == "reply" and events[-1][1]["status"] == "ok"


def test_conversations_list_and_get(client):
    _script([PATIENT, DONE])
    client.post("/assistant/turn", json={"text": "김철수 환자", "part": "데스크"})
    lst = client.get("/assistant/conversations").json()
    assert [c["title"] for c in lst] == ["김철수 환자"] and lst[0]["part"] == "데스크"
    assert client.get("/assistant/conversations", params={"before": lst[0]["id"]}).json() == []
    got = client.get(f"/assistant/conversations/{lst[0]['id']}").json()
    assert [m["role"] for m in got["messages"]] == ["user", "assistant"]
    assert {"seq", "text", "cards"} <= set(got["messages"][1])
    assert client.get("/assistant/conversations/99").status_code == 404


def test_delete_hides_from_list_keeps_read_only_and_blocks_turn(client):
    _script([PATIENT, DONE])
    client.post("/assistant/turn", json={"text": "김철수 환자", "part": "데스크"})
    cid = client.get("/assistant/conversations").json()[0]["id"]
    assert client.get(f"/assistant/conversations/{cid}").json()["deleted"] is False
    url = f"/assistant/conversations/{cid}/delete"
    assert client.post(url, json={"part": "데스크"}).json() == {"ok": True}
    assert client.post(url, json={"part": "데스크"}).status_code == 200  # 멱등
    assert client.get("/assistant/conversations").json() == []
    got = client.get(f"/assistant/conversations/{cid}").json()
    assert got["deleted"] is True and len(got["messages"]) == 2
    r = client.post(
        "/assistant/turn", json={"text": "이어서", "part": "데스크", "conversation_id": cid}
    )
    assert r.status_code == 409
    assert (
        client.post("/assistant/conversations/99/delete", json={"part": "데스크"}).status_code
        == 404
    )


def test_approve_ok_then_409(client, conn):
    did = _new_draft(conn)
    r = client.post(f"/assistant/drafts/{did}/approve", json={"part": "데스크"})
    assert r.status_code == 200 and r.json()["ok"] is True
    card = r.json()["card"]
    assert card["type"] == "draft" and card["id"] == str(did)
    assert card["data"]["status"] == "approved" and card["data"]["decided_by"] == "데스크"
    assert card["data"]["decided_at"] == "2026-12-01 09:05:00"
    assert (
        client.post(f"/assistant/drafts/{did}/approve", json={"part": "데스크"}).status_code == 409
    )
    assert client.post("/assistant/drafts/99/approve", json={"part": "데스크"}).status_code == 404


def test_approve_rejected_reasons(client, conn):
    first = _new_draft(conn)
    second = _new_draft(conn)  # 같은 자리 — 첫 승인 뒤 재검사에서 걸린다
    assert client.post(f"/assistant/drafts/{first}/approve", json={"part": "데스크"}).json()["ok"]
    r = client.post(f"/assistant/drafts/{second}/approve", json={"part": "데스크"}).json()
    assert r["ok"] is False and "staff_busy" in r["reasons"]
    assert r["card"]["data"]["status"] == "rejected"


def test_cancel_409_when_approved(client, conn):
    a, b = _new_draft(conn), _new_draft(conn, f"{WED} 11:00")
    client.post(f"/assistant/drafts/{a}/approve", json={"part": "데스크"})
    assert client.post(f"/assistant/drafts/{a}/cancel", json={"part": "데스크"}).status_code == 409
    r = client.post(f"/assistant/drafts/{b}/cancel", json={"part": "데스크"})
    assert r.status_code == 200 and r.json()["card"]["data"]["status"] == "cancelled"
    assert client.post(f"/assistant/drafts/{b}/cancel", json={"part": "데스크"}).status_code == 409


def test_deleted_conversation_drafts_are_read_only(client, conn):
    _script([PATIENT, DONE])
    client.post("/assistant/turn", json={"text": "김철수 환자", "part": "데스크"})
    a, b = _new_draft(conn), _new_draft(conn, f"{WED} 11:00")  # 둘 다 대화 1의 초안
    client.post("/assistant/conversations/1/delete", json={"part": "데스크"})
    for did, action in ((a, "approve"), (b, "cancel")):
        r = client.post(f"/assistant/drafts/{did}/{action}", json={"part": "데스크"})
        assert r.status_code == 409 and r.json()["detail"] == "삭제된 대화의 초안"
    rows = conn.execute("SELECT DISTINCT status FROM ax_emr_writes").fetchall()
    assert [tuple(r) for r in rows] == [("draft",)]
