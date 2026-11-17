"""AI 비서 시나리오 채점·실행 (evaluation/assistant_score.py·assistant.py, AI 비서 설계 §7.1)"""

import json

from evaluation import assistant as ev
from evaluation.assistant_score import score_call, score_run, score_turn

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
