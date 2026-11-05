"""분류기·판정기 함수와 저장 (backend/classify.py, 분류기 설계 "분류기"·"판정기", 앱 설계 §1)."""

import json
from dataclasses import replace

from backend import classify as cl
from backend.harness.store import SqliteLlmLog
from shared import call_labels
from shared.llm import FakeGateway


def _gw(conn):
    return FakeGateway(SqliteLlmLog(conn), scope="runtime")


class Recording(FakeGateway):
    """넘어간 인자를 남기고, fail_alias 호출은 ok=False로 돌려준다"""

    def __init__(self, log, fail_alias=None):
        super().__init__(log, scope="runtime")
        self.calls, self.fail_alias = [], fail_alias

    def complete(self, alias, **kw):
        self.calls.append((alias, kw))
        r = super().complete(alias, **kw)
        if alias == self.fail_alias:
            return replace(r, ok=False, data=None, error="실패")
        return r


def test_classify_ok_saves_row(conn):
    c = cl.classify(_gw(conn), "직원: 네 치과입니다\n환자: 예약하려고요", "inbound", "v1")
    assert c.ok and set(c.labels) == set(call_labels.ITEMS)
    cl.save_classification(conn, "c-0001", "v1", c)
    (row,) = conn.execute(
        "SELECT status, labels, summary, todo, llm_call_id, summary_llm_call_id, model_alias,"
        " model_actual, summary_model_actual FROM ax_call_classifications"
    ).fetchall()
    assert row["status"] == "ok" and row["model_alias"] == "classify"
    labels = json.loads(row["labels"])
    assert set(labels) == set(call_labels.ITEMS)
    assert all(labels[f] in (0, 1) for f in call_labels.FLAGS)
    assert row["summary"] and isinstance(json.loads(row["todo"]), list)
    assert row["llm_call_id"] != row["summary_llm_call_id"]
    tasks = dict(conn.execute("SELECT id, task FROM ax_llm_calls").fetchall())
    assert tasks[row["llm_call_id"]] == "classify"
    assert tasks[row["summary_llm_call_id"]] == "summarize"

    # 같은 (통화, 버전)은 덮어쓴다
    cl.save_classification(conn, "c-0001", "v1", c)
    assert conn.execute("SELECT COUNT(*) FROM ax_call_classifications").fetchone()[0] == 1


def test_classify_one_call_fails_marks_failed(conn):
    gw = Recording(SqliteLlmLog(conn), fail_alias="summarize")
    c = cl.classify(gw, "직원: 네\n환자: 여보세요", "inbound", "v1")
    assert not c.ok and c.labels is None and c.summary is None
    cl.save_classification(conn, "c-0002", "v1", c)
    row = conn.execute("SELECT status, labels, summary FROM ax_call_classifications").fetchone()
    assert tuple(row) == ("failed", None, None)
    assert conn.execute("SELECT COUNT(*) FROM ax_llm_calls").fetchone()[0] == 2


def test_classify_input_only_transcript_direction(conn):
    gw = Recording(SqliteLlmLog(conn))
    cl.classify(gw, "직원: 네\n환자: 취소할게요", "outbound", "v1")
    from backend import prompts

    p = prompts.load("classify", "v1")
    expected = [
        {"role": "user", "content": cl.user_message("직원: 네\n환자: 취소할게요", "outbound")}
    ]
    assert [a for a, _ in gw.calls] == ["classify", "summarize"]
    for (_, kw), part in zip(gw.calls, ("labels", "summary"), strict=True):
        assert kw["messages"] == expected and kw["system"] == p[part]
        assert kw["prompt_version"] == "v1"
    assert cl.user_message("T", "inbound") == "방향: inbound\n\nT"


def test_judge_saves_row(conn):
    j = cl.judge(_gw(conn), "직원: 네\n환자: 아파요", "inbound", "v1")
    assert j.ok and set(j.labels) == set(call_labels.ITEMS)
    cl.save_judgment(conn, "c-0003", "v1", j)
    row = conn.execute(
        "SELECT status, model_alias, llm_call_id, labels FROM ax_model_judgments"
    ).fetchone()
    assert row["status"] == "ok" and row["model_alias"] == "judge"
    assert row["llm_call_id"] == j.call.log_id
    assert all(json.loads(row["labels"])[f] in (0, 1) for f in call_labels.FLAGS)


def test_judge_effort_from_prompt(conn):
    """판정기 버전 머리말의 effort가 호출로 가고, 추론 토큰 몫만큼 max_tokens를 늘린다"""
    gw = Recording(SqliteLlmLog(conn))
    cl.judge(gw, "직원: 네\n환자: 아파요", "inbound", "v1")
    cl.judge(gw, "직원: 네\n환자: 아파요", "inbound", "v2")
    (_, v1), (_, v2) = gw.calls
    assert "effort" not in v1 and v1["max_tokens"] == cl.MAX_TOKENS
    assert v2["effort"] == "low" and v2["max_tokens"] == cl.REASONING_MAX_TOKENS
    assert v2["system"] == v1["system"]


def test_normalize_summary():
    assert (
        cl.normalize_summary("- 예약 변경 요청함\n\n**목요일** 3시로 옮김")
        == "예약 변경 요청함 목요일 3시로 옮김"
    )
    assert cl.normalize_summary("# 요약\n1. 첫째임\n• 둘째임  ") == "요약 첫째임 둘째임"
