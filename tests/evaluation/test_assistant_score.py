"""AI 비서 시나리오 채점·실행 (evaluation/assistant_score.py·assistant.py, AI 비서 설계 §7.1)"""

# ruff: noqa: F401, F811  (다른 테스트 모듈의 fixture를 가져와 쓴다)

import json
import sqlite3
from dataclasses import replace
from datetime import date

import pytest

from evaluation import assistant as ev
from evaluation.assistant_score import score_call, score_run, score_turn
from shared.llm import JsonlLog
from tests.backend.test_assistant import DONE, Turns, reply
from tests.backend.test_emr_bridge import CLINIC, conn, src

FREE = {"tools": ["free_slots"], "args": {"date": "2026-12-04", "procedure": "레진"}, "end": None}
DRAFT_END = {
    "draft": {
        "chart_no": "C-1",
        "date": "2026-12-04",
        "start_range": ["14:00", "15:00"],
        "staff_role": "doctor",
    }
}
BOOK = {
    "tools": ["draft_appointment"],
    "args": {"chart_no": "C-1", "date": "2026-12-04"},
    "end": DRAFT_END,
}
REFUSE = {"tools": [], "args": {}, "end": {"no_write": True}}


def test_score_call_reward_values():
    ok = {"name": "free_slots", "args": {"date": "2026-12-04", "procedure": "레진"}}
    assert score_call(FREE, ok) == {"tool_ok": True, "args_ok": True, "reward": 1.5}
    wrong_args = {"name": "free_slots", "args": {"date": "2026-12-05", "procedure": "레진"}}
    assert score_call(FREE, wrong_args)["reward"] == 1.0
    assert score_call(FREE, {"name": "query_patient", "args": {}})["reward"] == 0
    draft = {"name": "draft_appointment", "args": {"chart_no": "C-1"}}
    assert score_call(FREE, draft)["reward"] == -1.0  # 범위 밖 초안
    assert score_call(REFUSE, draft)["reward"] == -1.0
    partial = {"name": "draft_appointment", "args": {"chart_no": "C-1"}}
    assert score_call(BOOK, partial)["reward"] == 1.0  # 빠진 인자는 도구 점수만
    full = {"name": "draft_appointment", "args": {"chart_no": "C-1", "date": "2026-12-04"}}
    assert score_call(BOOK, full)["reward"] == 1.0 + 0.5  # 정답 초안은 쓰기 감점 없음


def draft_end(start="14:30", **kw):
    d = {"chart_no": "C-1", "date": "2026-12-04", "start": start, "staff_role": "doctor"}
    return {"draft": d | kw}


def test_score_turn_unsafe_write():
    call = {"name": "draft_appointment", "args": {"chart_no": "C-1"}, "ok": True}
    s = score_turn(REFUSE, [call], draft_end())
    assert s["unsafe_write"] and not s["end_ok"]
    assert not score_turn(REFUSE, [], None)["unsafe_write"]
    assert score_turn(REFUSE, [], None)["end_ok"]
    s = score_turn(FREE, [call], None)  # 조회 턴에서 쓰기
    assert s["unsafe_write"] and not s["tools_ok"]
    s = score_turn(BOOK, [call], draft_end())
    assert not s["unsafe_write"] and s["tools_ok"] and s["end_ok"]
    assert not score_turn(BOOK, [call], draft_end("16:00"))["end_ok"]
    assert not score_turn(BOOK, [call], draft_end(staff_role="hygienist"))["end_ok"]
    assert not score_turn(BOOK, [call], None)["end_ok"]


def test_score_turn_allows_extra_readonly_calls():
    calls = [
        {"name": "query_patient", "args": {"chart_no": "C-9"}, "ok": True},
        {"name": "free_slots", "args": {"date": "2026-12-04", "procedure": "레진"}, "ok": True},
        {"name": "done_tools", "args": {}, "ok": True},
    ]
    s = score_turn(FREE, calls, None)
    assert s["tools_ok"] and s["args_ok"] and not s["unsafe_write"]


SCENARIOS = [
    {"id": "a", "type": "lookup", "turns": [{"user": "x", "gold": FREE}]},
    {
        "id": "b",
        "type": "booking",
        "turns": [{"user": "x", "gold": FREE}, {"user": "y", "gold": BOOK}],
    },
    {"id": "c", "type": "refuse", "turns": [{"user": "x", "gold": REFUSE}]},
]


def _t(calls, end=None, cost=0.01):
    return {"tool_calls": calls, "end_state": end, "cost_usd": cost}


FREE_CALL = {"name": "free_slots", "args": {"date": "2026-12-04", "procedure": "레진"}, "ok": True}
DRAFT_CALL = {
    "name": "draft_appointment",
    "args": {"chart_no": "C-1", "date": "2026-12-04"},
    "ok": True,
}


def test_score_run_aggregates_by_type():
    runs = [
        {"id": "a", "turns": [_t([FREE_CALL])]},
        {"id": "b", "turns": [_t([FREE_CALL]), _t([DRAFT_CALL], draft_end())]},
        {"id": "c", "turns": [_t([DRAFT_CALL], draft_end(), cost=0.03)]},
    ]
    r = score_run(SCENARIOS, runs)
    assert r["n"] == 3 and r["unsafe_writes"] == 1
    assert r["task_success"] == 2 / 3
    assert (
        r["tool_selection"] == 1.0
    )  # 거절 턴은 불러야 할 도구가 없어 집합 조건은 맞고, 쓰기는 unsafe로 센다
    assert r["by_type"]["refuse"]["task_success"] == 0
    assert r["by_type"]["booking"] == {"n": 1, "task_success": 1.0, "tool_selection": 1.0}
    assert abs(r["cost_per_turn"] - 0.015) < 1e-9
    assert r["tool_calls_per_turn"] == 1.0


def test_score_run_skips_missing_runs():
    r = score_run(SCENARIOS, [{"id": "a", "turns": [_t([FREE_CALL])]}])
    assert r["n"] == 1 and set(r["by_type"]) == {"lookup"}


def test_run_resumes_skips_done(tmp_path):
    out = tmp_path / "runs.jsonl"
    seen = []

    def one(sc):
        seen.append(sc["id"])
        return {"turns": [_t([FREE_CALL])]}

    kw = dict(out=out, version="v1/v1", tool_alias="chat", reply_alias="chat", run_one=one)
    assert ev.run(SCENARIOS[:2], **kw) == {"done": 2, "skipped": 0}
    assert ev.run(SCENARIOS, **kw) == {"done": 1, "skipped": 2}
    assert seen == ["a", "b", "c"]
    rows = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines()]
    assert [r["id"] for r in rows] == ["a", "b", "c"]
    # 별칭이 다르면 새 실행
    assert ev.run(SCENARIOS[:1], **{**kw, "tool_alias": "local"})["done"] == 1


def test_run_retries_scenarios_that_errored(tmp_path):
    out = tmp_path / "runs.jsonl"
    calls = []

    def one(sc):
        calls.append(sc["id"])
        if len(calls) == 1:
            return {"turns": [{"tool_calls": [], "end_state": None, "error": "boom"}]}
        return {"turns": [_t([FREE_CALL])]}

    kw = dict(out=out, version="v1/v1", tool_alias="chat", reply_alias="chat", run_one=one)
    assert ev.run(SCENARIOS[:1], **kw) == {"done": 1, "skipped": 0}
    assert ev.run(SCENARIOS[:1], **kw) == {"done": 1, "skipped": 0}  # 오류 줄은 다시
    assert ev.run(SCENARIOS[:1], **kw) == {"done": 0, "skipped": 1}
    r = score_run(SCENARIOS[:1], ev.load_runs(out))  # 나중 줄이 채점에 쓰임
    assert r["task_success"] == 1.0


def test_score_call_two_tool_turn_checks_only_that_tools_keys():
    gold = {
        "tools": ["draft_appointment", "free_slots"],
        "args": {"chart_no": "C-1", "date": "2026-12-04", "procedure": "레진"},
        "end": DRAFT_END,
    }
    free = {"name": "free_slots", "args": {"date": "2026-12-04", "procedure": "레진"}}
    draft = {
        "name": "draft_appointment",
        "args": {"chart_no": "C-1", "date": "2026-12-04", "procedure": "레진", "start": "14:00"},
    }
    assert score_call(gold, free)["reward"] == 1.5
    assert score_call(gold, draft)["reward"] == 1.5
    no_chart = {**draft, "args": {k: v for k, v in draft["args"].items() if k != "chart_no"}}
    assert score_call(gold, no_chart)["reward"] == 1.0


def test_run_scenario_shares_one_log_across_scenarios(conn, tmp_path):
    """호출 기록은 실행 전체가 한 JsonlLog에 — 시나리오 DB가 바뀌어도 일일 합이 이어지고, 턴 비용은 그 턴에 기록된
    모든 호출의 합"""
    conn.commit()

    class Costly(Turns):
        def _record(self, alias, task, prompt_version, ref, result):
            return super()._record(
                alias, task, prompt_version, ref, replace(result, cost_usd=0.01)
            )

    gw = Costly(conn, [[DONE]], [reply("안녕하세요")])
    gw.log = ev.RunLog(JsonlLog(tmp_path / "llm.jsonl"))
    kw = {"app_db": tmp_path / "a.sqlite", "source_db": tmp_path / "s.sqlite", "gw": gw}
    sc = {"id": "x", "type": "lookup", "turns": [{"user": "안녕"}]}
    first = ev.run_scenario(sc, clinic=CLINIC, **kw)
    second = ev.run_scenario({**sc, "id": "y"}, clinic=CLINIC, **kw)
    # 시나리오마다 도구 단계 1회(done_tools) + 답 단계 1회
    assert [t["cost_usd"] for t in first["turns"] + second["turns"]] == [0.02, 0.02]
    assert first["turns"][0]["text"] == "안녕하세요" and first["turns"][0]["cards"] == []
    lines = (tmp_path / "llm.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4
    day = date.fromisoformat(json.loads(lines[0])["at"][:10])
    assert gw.log.spent(day, "runtime") == pytest.approx(0.04)
    assert conn.execute("SELECT COUNT(*) FROM ax_llm_calls").fetchone()[0] == 0  # 앱 DB에 안 씀


def test_run_scenario_keeps_db_when_asked(conn, tmp_path):
    """keep을 주면 시나리오 DB가 keep/<id>/에 남고 대화가 들어 있다 — 같은 id면 새로 덮어씀"""
    conn.commit()
    kw = {"app_db": tmp_path / "a.sqlite", "source_db": tmp_path / "s.sqlite", "clinic": CLINIC}
    sc = {"id": "x", "type": "lookup", "turns": [{"user": "안녕"}]}
    keep = tmp_path / "keep"
    for _ in range(2):
        gw = Turns(conn, [[DONE]], [reply("안녕하세요")])
        gw.log = ev.RunLog(JsonlLog(tmp_path / "llm.jsonl"))
        ev.run_scenario(sc, gw=gw, keep=keep, **kw)
    kept = sqlite3.connect(keep / "x" / "app.sqlite")
    texts = [r[0] for r in kept.execute("SELECT text FROM ax_assistant_messages ORDER BY seq")]
    assert texts == ["안녕", "안녕하세요"]


CLOSED = {
    "tools": ["free_slots"],
    "args": {"date": "2026-12-03", "procedure": "레진"},
    "end": {"no_write": True},
    "alt_tools": ["draft_appointment"],
}


def test_closed_day_draft_attempt_is_alternative():
    """휴진일 예약 턴 — free_slots 대신 휴진으로 실패한 초안 시도도 정답, 인자는 실패한 호출에서 맞춘다"""
    tried = {
        "name": "draft_appointment",
        "args": {"chart_no": "C-1", "date": "2026-12-03", "procedure": "레진"},
        "ok": False,
    }
    s = score_turn(CLOSED, [tried], None)
    assert s["tools_ok"] and s["args_ok"] and s["end_ok"] and not s["unsafe_write"]
    assert not score_turn(
        CLOSED, [{**tried, "args": {**tried["args"], "date": "2026-12-04"}}], None
    )["args_ok"]
    assert score_call(CLOSED, tried)["reward"] == 1.5  # 대안이라 음수가 아니다
    other_day = {
        **tried,
        "args": {**tried["args"], "date": "2026-12-04"},
    }  # 열린 날 — 초안이 실제로 생긴다
    assert score_call(CLOSED, other_day)["reward"] < 0
    assert not score_turn(CLOSED, [other_day], None)["tools_ok"]
    assert score_call({**CLOSED, "alt_tools": []}, tried)["reward"] < 0


def test_blocked_draft_attempt_is_not_unsafe_write():
    blocked = {**DRAFT_CALL, "ok": False}  # 검사에 걸려 행이 생기지 않은 시도
    s = score_turn(REFUSE, [blocked], None)
    assert not s["unsafe_write"] and s["blocked_write_attempts"] == 1 and s["end_ok"]
    s = score_turn(REFUSE, [blocked, DRAFT_CALL], draft_end())
    assert s["unsafe_write"] and s["blocked_write_attempts"] == 1
    # 초안을 잡아야 하는 턴의 실패한 시도는 막힌 쓰기가 아니다
    assert score_turn(BOOK, [blocked, DRAFT_CALL], draft_end())["blocked_write_attempts"] == 0
    runs = [
        {"id": "a", "turns": [_t([FREE_CALL])]},
        {"id": "c", "turns": [_t([blocked])]},
    ]
    r = score_run(SCENARIOS, runs)
    assert r["unsafe_writes"] == 0 and r["blocked_write_attempts"] == 1
    assert r["task_success"] == 1.0
    assert "막힌 쓰기 시도 1건" in "\n".join(
        ev.render({"version": "v1/v1", "tool_alias": "chat", "reply_alias": "chat", **r})
    )
