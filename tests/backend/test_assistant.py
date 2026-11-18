"""AI 비서 한 턴(backend/assistant.py, AI 비서 설계 §4.1·§6·§7.1) — 도구 단계는 Script 게이트웨이의 정해 둔 도구
호출, 답 단계는 정해 둔 구조화 답. emr_bridge 테스트의 손으로 만든 source를 그대로 쓴다."""

# ruff: noqa: F401, F811  (다른 테스트 모듈의 fixture를 가져와 쓴다)

import json
import sqlite3
from dataclasses import replace

import pytest

from backend import assistant, chat, emr_bridge
from shared.llm import BudgetExceeded, FakeGateway
from tests.backend.test_emr_bridge import (
    CLINIC,
    MISSED,
    NEW,
    WED,
    _approve,
    _call,
    _holidays,
    conn,
    src,
)
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
    cards = [{"type": "patient", "id": "C-00001"}]
    gw = Turns(conn, [[PATIENT, DONE]], [reply("잔액 777원", cards), reply("잔액 777원", cards)])
    out = turn(conn, gw, "김철수 잔액")
    assert out["status"] == "number_failed" and out["text"] == assistant.FAILED_TEXT
    assert out["cards"] == []  # 고정 문구 아래 대상 카드를 붙이지 않는다
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
    # 단계 줄 — 첫 메시지(사용자)는 비고, 답은 이번 턴 도구 호출 그대로
    assert got["messages"][0]["tool_calls"] == []
    assert got["messages"][1]["tool_calls"] == out["tool_calls"] and out["tool_calls"]
    assert conn.execute("SELECT count(*) FROM ax_llm_calls").fetchone()[0] == calls
    assert [c["id"] for c in assistant.conversations(conn)] == [out["conversation_id"]]
    assert assistant.conversation(conn, 999) is None
    cid = out["conversation_id"]
    assert got["deleted"] is False
    assert assistant.delete_conversation(conn, cid, "2026-12-01 09:00:00") is True
    assert assistant.delete_conversation(conn, cid, "2026-12-02 09:00:00") is True  # 멱등
    assert assistant.delete_conversation(conn, 999, "2026-12-01 09:00:00") is False
    assert assistant.conversations(conn) == []
    again = assistant.conversation(conn, cid)
    assert again["deleted"] is True and len(again["messages"]) == 2
    assert (
        conn.execute("SELECT deleted_at FROM ax_assistant_conversations").fetchone()[0]
        == "2026-12-01 09:00:00"
    )


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


def test_failed_draft_time_is_not_evidence(conn):
    draft = (
        "tool",
        "draft_appointment",
        {
            "chart_no": "C-00002",
            "date": "2026-11-25",  # 지난 날 — 휴진이 아닌 실패(휴진은 코드가 답한다)
            "start": "10:30",
            "staff": "원장",
            "procedure": "레진",
        },
    )
    bad = reply("11월 25일 10시 30분 초안을 만들었습니다, 승인 버튼을 눌러 주세요")
    gw = Turns(conn, [[draft, DONE]], [bad, bad])
    out = turn(conn, gw, "이영희 지난 수요일 오전 레진")
    assert conn.execute("SELECT count(*) FROM ax_emr_writes").fetchone()[0] == 0
    assert out["status"] == "number_failed" and out["text"] == assistant.FAILED_TEXT
    assert len([kw for _, kw in gw.calls if kw.get("schema")]) == 2


def test_reply_budget_is_budget_status(conn):
    class Broke(Turns):
        def complete(self, alias, **kw):
            if kw.get("schema") is not None:
                raise BudgetExceeded("하루 상한")
            return super().complete(alias, **kw)

    out = turn(conn, Broke(conn, [[PATIENT, DONE]], [reply()]), "김철수 환자")
    assert out["status"] == "budget" and out["text"] == assistant.FAILED_TEXT
    saved = _messages(conn, out["conversation_id"])[-1]
    assert saved["status"] == "budget"


def test_input_states_today_and_closed_days(conn):
    gw = Turns(conn, [[DONE]], [reply()])
    turn(conn, gw, "내일 예약 몇 건?")
    first = [kw for _, kw in gw.calls if kw.get("tools")][0]["messages"][0]["content"]
    assert "오늘: 2026-12-01(화)" in first and "휴진 요일: 목·일" in first


DRAFT = (
    "tool",
    "draft_appointment",
    {"chart_no": "C-00002", "date": WED, "start": "10:00", "staff": "원장", "procedure": "레진"},
)


def test_draft_card_attached_even_if_reply_omits_it(conn):
    gw = Turns(conn, [[DRAFT, DONE]], [reply("확인해 주세요")])
    out = turn(conn, gw, "이영희 수요일 10시 레진")
    assert [(c["type"], c["id"]) for c in out["cards"]] == [("draft", "1")]
    saved = json.loads(_messages(conn, out["conversation_id"])[-1]["cards"])
    assert [c["id"] for c in saved] == ["1"]


def test_draft_card_not_duplicated_when_reply_lists_it(conn):
    cards = [{"type": "draft", "id": "1"}, {"type": "patient", "id": "C-00002"}]
    gw = Turns(conn, [[DRAFT, DONE]], [reply("초안을 만들었습니다", cards)])
    out = turn(conn, gw, "이영희 수요일 10시 레진")
    assert [(c["type"], c["id"]) for c in out["cards"]] == [
        ("draft", "1"),
        ("patient", "C-00002"),
    ]


def test_draft_card_attached_on_reply_budget(conn):
    class Broke(Turns):
        def complete(self, alias, **kw):
            if kw.get("schema") is not None:
                raise BudgetExceeded("하루 상한")
            return super().complete(alias, **kw)

    out = turn(conn, Broke(conn, [[DRAFT, DONE]], [reply()]), "이영희 수요일 10시 레진")
    assert out["status"] == "budget" and out["text"] == assistant.FAILED_TEXT
    assert [(c["type"], c["id"]) for c in out["cards"]] == [("draft", "1")]
    assert out["cards"][0]["data"]["status"] == "draft"


def test_draft_claim_without_draft_retries_then_fixed_text(conn):
    gw = Turns(conn, [[DONE]], [reply("예약을 잡았습니다"), reply("초안을 만들었어요")])
    out = turn(conn, gw, "이영희 수요일 10시 레진 잡아줘")
    assert out["status"] == "draft_failed" and out["text"] == assistant.FAILED_TEXT
    assert len([kw for _, kw in gw.calls if kw.get("schema")]) == 2
    rule = conn.execute(
        "SELECT blocked FROM ax_rule_counts WHERE rule_id = ?", (assistant.DRAFT_CLAIM,)
    ).fetchone()[0]
    assert rule == 2


# (답 글, 이번 턴 초안 성공 여부, 막히는가) — 초안·예약을 만든 말은 초안이 없을 때만, 확정·승인은 언제나 막는다
CLAIM_TABLE = [
    ("예약했습니다", False, True),
    ("예약을 잡았어요", False, True),
    ("초안을 만들었습니다", False, True),
    ("예약 넣었어요", False, True),
    ("초안을 생성했습니다", False, True),
    ("예약 초안을 작성했습니다", False, True),
    ("예약을 잡아 드렸습니다", False, True),
    ("예약이 잡혔습니다", False, True),
    ("예약되었습니다", False, True),
    ("예약을 넣어 두었습니다", False, True),
    ("예약을 등록해 두었습니다", False, True),
    ("초안이 만들어졌습니다", False, True),
    ("예약을 확정했습니다", False, True),
    ("예약을 승인했습니다", False, True),
    ("예약이 확정되었습니다", False, True),
    ("예약이 완료되었습니다", False, True),
    ("초안을 만들었습니다", True, False),
    ("예약이 잡혔습니다", True, False),
    ("예약 초안을 작성했습니다", True, False),
    ("예약을 확정했습니다", True, True),
    ("예약을 승인했습니다", True, True),
    ("확정했습니다", True, True),
    ("승인해 드렸습니다", True, True),
    ("예약이 확정되었습니다", True, True),
    ("예약이 완료됐어요", True, True),
    ("김철수님은 예약을 원했습니다", False, False),
    ("예약 원함 통화입니다", False, False),
    ("초안을 만들까요?", False, False),
    ("예약이 잡혀 있어요", False, False),
    ("예약을 하셨습니다", False, False),
    ("초안을 만들지 않았습니다", False, False),
    ("12월 3일 10시 예약이 있습니다", False, False),
    ("예약되어 있습니다", False, False),
    ("승인하시면 예약이 확정됩니다", True, False),
    # 다른 사람이 한 일임을 밝힌 문장은 비서의 말이 아니다(통화 요약의 "확정함"을 옮긴 답이 막히던 것)
    ("박서영 — 통화에서 12월 3일 10시 진료로 확정했습니다", False, False),
    ("통화 중에 예약을 잡았습니다", False, False),
    ("환자가 방문 일정을 확정했습니다", False, False),
    ("김철수 님이 예약을 잡았습니다", False, False),
    ("직원이 예약을 넣어 두었습니다", True, False),
    # 문장마다 본다 — 한 문장이 통화 이야기여도 다른 문장의 비서 주장은 막는다
    ("통화에서 안내했습니다. 예약을 잡았습니다", False, True),
    ("통화에서 문의했습니다\n예약을 확정했습니다", True, True),
    # 답 지침 v2가 쓰게 하는 문장(prompts/assistant_reply/v2.md)
    ("원하시면 예약 초안을 만들어 드릴게요", False, False),
    ("아래 카드에서 승인 버튼을 누르면 확정됩니다", True, False),
]


@pytest.mark.parametrize(("text", "drafted", "blocked"), CLAIM_TABLE)
def test_draft_claim_rule_wording(text, drafted, blocked):
    got = assistant._check(reply(text), {3, 10, 12}, set(), drafted)
    assert got == (assistant.DRAFT_CLAIM if blocked else None)


def test_confirm_claim_blocked_even_with_draft(conn):
    gw = Turns(conn, [[DRAFT, DONE]], [reply("예약을 확정했습니다"), reply("초안을 만들었습니다")])
    out = turn(conn, gw, "이영희 수요일 10시 레진")
    assert out["status"] == "ok" and out["text"] == "초안을 만들었습니다"
    retry = [kw for _, kw in gw.calls if kw.get("schema")][1]["messages"][-1]["content"]
    assert "카드" in retry


def test_draft_claim_then_honest_reply_passes(conn):
    gw = Turns(conn, [[DONE]], [reply("예약했습니다"), reply("그 시간은 비어 있지 않아요")])
    out = turn(conn, gw, "이영희 수요일 10시 레진 잡아줘")
    assert out["status"] == "ok" and out["text"] == "그 시간은 비어 있지 않아요"


def test_concurrent_send_failure_closes_run(conn):
    class Racing(Turns):
        def complete(self, alias, **kw):
            # 같은 대화에 동시에 보낸 말이 다음 자리(seq 2)를 먼저 쓰고, 이 턴은 도중에 끊긴다
            assistant._save(conn, 1, 2, "user", "동시에 보낸 말")
            conn.commit()
            raise RuntimeError("끊김")

    with pytest.raises(RuntimeError):
        turn(conn, Racing(conn, [[DONE]], [reply()]), "김철수 환자")
    assert not conn.in_transaction
    run = conn.execute("SELECT * FROM ax_agent_runs").fetchone()
    assert run["status"] == "failed" and run["locked_by"] is None
    assert [r["text"] for r in _messages(conn, 1)] == ["김철수 환자", "동시에 보낸 말"]


def test_draft_card_marks_pool_source_call(conn):
    _call(conn, "c-1", "2026-11-26 15:00:00", NEW)
    _call(conn, "c-2", "2026-11-26 16:00:00", MISSED)
    path = conn.execute("SELECT file FROM pragma_database_list WHERE name = 'source'").fetchone()[
        0
    ]
    s = sqlite3.connect(path)
    s.execute("UPDATE ax_calls SET in_eval_pool = 1 WHERE call_uid = 'c-2'")
    s.commit()
    s.close()
    args = {**DRAFT[2], "source_call_uid": "c-1"}
    pooled = {**DRAFT[2], "chart_no": "C-00001", "start": "11:00", "source_call_uid": "c-2"}
    gw = Turns(conn, [[("tool", DRAFT[1], args), ("tool", DRAFT[1], pooled), DONE]], [reply()])
    out = turn(conn, gw, "두 사람 수요일 레진")
    got = {c["data"]["source_call_uid"]: c["data"]["source_call_pool"] for c in out["cards"]}
    assert got == {"c-1": 0, "c-2": 1}


def test_tool_log_marks_error_result_failed(conn):
    closed = ("tool", DRAFT[1], {**DRAFT[2], "date": "2026-12-03"})  # 목요일 휴진
    gw = Turns(conn, [[closed, DONE]], [reply("그날은 휴진이에요")])
    out = turn(conn, gw, "이영희 목요일 레진")
    assert [c["ok"] for c in out["tool_calls"]] == [False, True]


THU = "2026-12-03"  # 목요일 휴진
CLOSED_FREE = ("tool", "free_slots", {"date": THU, "procedure": "스케일링"})


def test_closed_day_reply_written_by_code(conn):
    """휴진일 하루 확인뿐인 턴 — 답 단계를 부르지 않고 코드가 정한 문장(다른 날을 권하지 않는다)"""
    gw = Turns(conn, [[CLOSED_FREE, DONE]], [])
    out = turn(conn, gw, "송미영 님 12월 3일 오후 스케일링 잡아 줘")
    assert out["status"] == "ok" and out["cards"] == []
    assert out["text"] == assistant.CLOSED_TEXT.format(m=12, d=3, why="휴진일")
    assert not [kw for _, kw in gw.calls if kw.get("schema")]  # 답 단계 호출 없음


def test_closed_day_draft_error_also_code_reply(conn):
    draft = (
        "tool",
        "draft_appointment",
        {**DRAFT[2], "date": THU, "start": None, "window": "오후"},
    )
    gw = Turns(conn, [[draft, DONE]], [])
    out = turn(conn, gw, "이영희 목요일 오후 레진")
    assert out["text"] == assistant.CLOSED_TEXT.format(m=12, d=3, why="휴진일")


def test_holiday_code_reply_names_holiday(conn):
    """공휴일도 휴진과 같은 길 — 문장만 공휴일이라고"""
    _holidays(conn, "2026-12-25")
    xmas = ("tool", "free_slots", {"date": "2026-12-25", "procedure": "스케일링"})  # 금요일 공휴일
    gw = Turns(conn, [[xmas, DONE]], [])
    out = turn(conn, gw, "송미영 님 성탄절에 스케일링 잡아 줘")
    assert out["text"] == assistant.CLOSED_TEXT.format(m=12, d=25, why="공휴일")
    assert "공휴일이라" in out["text"]


SAT = "2026-12-05"


@pytest.mark.parametrize(
    "when", [{"start": None, "window": "오후"}, {"start": "15:00"}], ids=["window", "start"]
)
def test_saturday_afternoon_code_reply(conn, when):
    """토요일 오후 초안 실패뿐인 턴 — 마감 시각은 영업시간에서, 답 단계 호출 없음"""
    free = ("tool", "free_slots", {"date": SAT, "procedure": "레진"})
    draft = ("tool", DRAFT[1], {**DRAFT[2], "date": SAT, **when})
    gw = Turns(conn, [[free, draft, DONE]], [])
    out = turn(conn, gw, "이영희 토요일 오후 레진")
    assert out["text"] == assistant.SAT_PM_TEXT.format(close="13:30")
    assert out["status"] == "ok" and out["cards"] == []
    assert not [kw for _, kw in gw.calls if kw.get("schema")]


def test_saturday_morning_failure_uses_reply(conn):
    """토요일이어도 오전 시각이 걸린 실패는 답 단계가 쓴다"""
    draft = ("tool", DRAFT[1], {**DRAFT[2], "date": SAT, "start": "12:55"})  # 격자 밖
    gw = Turns(conn, [[draft, DONE]], [reply("그 시각은 잡을 수 없습니다")])
    out = turn(conn, gw, "이영희 토요일 12시 55분 레진")
    assert out["text"] == "그 시각은 잡을 수 없습니다"


def test_closed_day_mixed_with_open_day_uses_reply(conn):
    """휴진일과 다른 날을 같이 봤으면 답 단계가 쓴다"""
    open_day = ("tool", "free_slots", {"date": WED, "procedure": "스케일링"})
    gw = Turns(conn, [[CLOSED_FREE, open_day, DONE]], [reply("수요일은 빈 시간이 있습니다")])
    out = turn(conn, gw, "목요일이랑 수요일 빈 시간")
    assert out["text"] == "수요일은 빈 시간이 있습니다"
