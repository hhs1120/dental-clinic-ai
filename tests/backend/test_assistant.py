"""AI 비서 한 턴(backend/assistant.py, AI 비서 설계 §4.1·§6·§7.1) — 도구 단계는 Script 게이트웨이의 정해 둔 도구
호출, 답 단계는 정해 둔 구조화 답. emr_bridge 테스트의 손으로 만든 source를 그대로 쓴다."""

# ruff: noqa: F401, F811  (다른 테스트 모듈의 fixture를 가져와 쓴다)

import json
from dataclasses import replace

import pytest

from backend import assistant, chat, emr_bridge
from shared.llm import FakeGateway
from tests.backend.test_emr_bridge import CLINIC, WED, _approve, conn, src
from tests.backend.test_followup import Script

PATIENT = ("tool", "query_patient", {"chart_no": "C-00001"})
DONE = ("tool", "done_tools", {})


def reply(text="확인했습니다", cards=(), cited=()):
    return {"text": text, "cards": list(cards), "cited_ids": list(cited)}


class Turns(Script):
    """Script + 답 단계(schema가 있는 호출)는 정해 둔 답을 차례로. 받은 (별칭, 인자)를 모두 남긴다"""

    def __init__(self, conn, runs, replies):
        super().__init__(conn, *runs)
        self.replies, self.calls = list(replies), []

    def complete(self, alias, **kw):
        self.calls.append((alias, kw))
        if kw.get("schema") is None:
            return super().complete(alias, **kw)
        r = FakeGateway.complete(self, alias, **kw)
        data = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return replace(r, data=data, text=json.dumps(data, ensure_ascii=False))


def turn(conn, gw, text, conversation_id=None, **kw):
    return assistant.turn(
        conn, gw, None, CLINIC, conversation_id=conversation_id, text=text, part="데스크", **kw
    )


def _messages(conn, cid):
    return conn.execute(
        "SELECT * FROM ax_assistant_messages WHERE conversation_id = ? ORDER BY seq", (cid,)
    ).fetchall()


def test_turn_tool_then_reply_saves_messages(conn):
    gw = Turns(conn, [[PATIENT, DONE]], [reply("김철수님 정보입니다")])
    events = []
    out = turn(conn, gw, "김철수 환자 정보 알려줘", on_event=events.append)
    assert out["status"] == "ok" and out["text"] == "김철수님 정보입니다"
    rows = _messages(conn, out["conversation_id"])
    assert [r["role"] for r in rows] == ["user", "assistant"] and out["seq"] == 2
    calls = json.loads(rows[1]["tool_calls"])
    assert [c["name"] for c in calls] == ["query_patient", "done_tools"]
    assert calls[0]["args"]["chart_no"] == "C-00001" and calls[0]["ok"]
    assert "김철수" in calls[0]["result_summary"] and rows[1]["run_id"] == out["run_id"]
    conv = conn.execute("SELECT * FROM ax_assistant_conversations").fetchone()
    assert conv["title"] == "김철수 환자 정보 알려줘" and conv["part"] == "데스크"
    run = conn.execute("SELECT * FROM ax_agent_runs WHERE id = ?", (out["run_id"],)).fetchone()
    assert run["agent"] == assistant.AGENT and run["target"] == f"{conv['id']}:1"
    assert events[0] == {"type": "step", "tool": "query_patient", "args": calls[0]["args"]}
    assert events[-1]["type"] == "reply" and events[-1]["text"] == out["text"]


def test_followup_turn_gets_previous_card_ids(conn):
    gw = Turns(
        conn,
        [[PATIENT, DONE], [DONE]],
        [reply("김철수님입니다", [{"type": "patient", "id": "C-00001"}])],
    )
    first = turn(conn, gw, "김철수 환자 찾아줘")
    assert [c["id"] for c in first["cards"]] == ["C-00001"]
    turn(conn, gw, "그 사람 다음 예약은?", first["conversation_id"])
    tool_calls = [kw for _, kw in gw.calls if kw.get("tools")]
    second = tool_calls[-1]["messages"][0]["content"]
    assert "그 사람 다음 예약은?" in second and "C-00001" in second and "patient" in second
    assert "오늘: 2026-12-01" in second


def test_reply_uses_reply_alias(conn, monkeypatch):
    monkeypatch.setattr(assistant, "REPLY_ALIAS", "other")
    gw = Turns(conn, [[PATIENT, DONE]], [reply()])
    turn(conn, gw, "김철수 환자")
    aliases = [a for a, _ in gw.calls]
    assert aliases == [assistant.TOOL_ALIAS] * 2 + ["other"]
    assert gw.calls[-1][1]["task"] == "assistant_reply"


def test_card_outside_turn_dropped(conn):
    cards = [{"type": "patient", "id": "C-00001"}, {"type": "patient", "id": "C-00002"}]
    gw = Turns(conn, [[PATIENT, DONE]], [reply("김철수님", cards)])
    out = turn(conn, gw, "김철수 환자")
    assert [(c["type"], c["id"]) for c in out["cards"]] == [("patient", "C-00001")]
    assert out["cards"][0]["data"]["name"] == "김철수"


def test_number_mismatch_retry_then_fixed_text(conn):
    gw = Turns(conn, [[PATIENT, DONE]], [reply("잔액 777원"), reply("잔액 777원")])
    out = turn(conn, gw, "김철수 잔액")
    assert out["status"] == "number_failed" and out["text"] == assistant.FAILED_TEXT
    replies = [kw for _, kw in gw.calls if kw.get("schema")]
    assert len(replies) == 2 and len(replies[1]["messages"]) > len(replies[0]["messages"])
    saved = _messages(conn, out["conversation_id"])[-1]
    assert saved["status"] == "number_failed" and saved["text"] == assistant.FAILED_TEXT


def test_number_mismatch_then_fixed_passes(conn):
    gw = Turns(conn, [[PATIENT, DONE]], [reply("잔액 777원"), reply("잔액 확인했습니다")])
    out = turn(conn, gw, "김철수 잔액")
    assert out["status"] == "ok" and out["text"] == "잔액 확인했습니다"


def test_ask_records_calls_chat_after_tool_phase(conn, monkeypatch):
    conn.execute(
        "INSERT INTO ax_chunks (id, source_type, text, chart_no, event_date, doc_type)"
        " VALUES ('call:c-9:0', 'call', ?, NULL, '2026-11-20', NULL)",
        ("[환자: 김철수(C-00001) | 2026-11-20 | 수신]\n주차 문의",),
    )
    conn.commit()
    seen = []

    def fake_chat(c, gw, searcher, clinic, question, **kw):
        running = c.execute(
            "SELECT count(*) FROM ax_agent_runs WHERE status = 'running'"
        ).fetchone()[0]
        seen.append((question, running))
        return {
            "status": "answered",
            "answer_text": "진료 당일 2시간 무료",
            "cited_ids": ["call:c-9:0"],
            "retrieved_ids": ["call:c-9:0"],
        }

    monkeypatch.setattr(chat, "chat", fake_chat)
    ask = ("tool", "ask_records", {"question": "주차 몇 시간 무료?"})
    gw = Turns(
        conn,
        [[ask, DONE]],
        [
            reply(
                "진료 당일 2시간 무료입니다",
                [{"type": "source", "id": "call:c-9:0"}],
                ["call:c-9:0"],
            )
        ],
    )
    out = turn(conn, gw, "주차 몇 시간 무료야?")
    assert seen == [("주차 몇 시간 무료?", 0)]  # 도구 단계가 끝난 뒤
    reply_input = [kw for _, kw in gw.calls if kw.get("schema")][0]["messages"]
    assert "진료 당일 2시간 무료" in json.dumps(reply_input, ensure_ascii=False)
    assert out["status"] == "ok"
    assert [(c["type"], c["id"]) for c in out["cards"]] == [("source", "call:c-9:0")]
    assert out["cards"][0]["data"]["source_type"] == "call"


def test_cancel_request_makes_no_draft(conn):
    gw = Turns(conn, [[DONE]], [reply("예약 취소는 EMR에서 직접 해 주세요")])
    out = turn(conn, gw, "김철수 예약 취소해줘")
    assert out["status"] == "ok"
    assert conn.execute("SELECT count(*) FROM ax_emr_writes").fetchone()[0] == 0


def test_conversation_reopen_no_model_call(conn):
    draft = (
        "tool",
        "draft_appointment",
        {
            "chart_no": "C-00002",
            "date": WED,
            "start": "10:00",
            "staff": "원장",
            "procedure": "레진",
        },
    )
    gw = Turns(
        conn,
        [[draft, DONE]],
        [reply("초안을 만들었습니다. 승인 버튼을 눌러 주세요", [{"type": "draft", "id": "1"}])],
    )
    out = turn(conn, gw, "이영희 수요일 10시 레진 잡아줘")
    assert out["cards"][0]["data"]["status"] == "draft"
    calls = conn.execute("SELECT count(*) FROM ax_llm_calls").fetchone()[0]
    _approve(conn, 1)
    conn.commit()
    got = assistant.conversation(conn, out["conversation_id"])
    assert [m["role"] for m in got["messages"]] == ["user", "assistant"]
    card = got["messages"][1]["cards"][0]
    assert card["type"] == "draft" and card["data"]["status"] == "approved"
    assert conn.execute("SELECT count(*) FROM ax_llm_calls").fetchone()[0] == calls
    assert [c["id"] for c in assistant.conversations(conn)] == [out["conversation_id"]]
    assert assistant.conversation(conn, 999) is None


def test_turn_exception_closes_run_and_saves_failed_message(conn):
    class Boom(Turns):
        def complete(self, alias, **kw):
            raise RuntimeError("끊김")

    with pytest.raises(RuntimeError):
        turn(conn, Boom(conn, [[DONE]], [reply()]), "김철수 환자")
    run = conn.execute("SELECT * FROM ax_agent_runs").fetchone()
    assert run["status"] == "failed" and run["locked_by"] is None
    rows = _messages(conn, 1)
    assert [r["role"] for r in rows] == ["user", "assistant"]
    assert rows[1]["status"] == "failed" and rows[1]["text"] == assistant.FAILED_TEXT
