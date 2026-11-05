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


def test_v2_summary_uses_kind_schema(conn):
    """분류기 v2는 할 일 종류 스키마, 라벨 프롬프트는 v1과 같다(통화·② 판정 화면 설계 '분류기 v2')"""
    gw = Recording(SqliteLlmLog(conn))
    c = cl.classify(gw, "직원: 네\n환자: 예약", "inbound", "v2")
    cl.classify(gw, "직원: 네\n환자: 예약", "inbound", "v1")
    schemas = [kw["schema"] for a, kw in gw.calls if a == "summarize"]
    assert schemas == [call_labels.SUMMARY_SCHEMA_KINDS, call_labels.SUMMARY_SCHEMA]
    assert all(set(t) == {"kind", "text", "who", "after"} for t in c.todo)
    from backend import prompts

    assert prompts.load("classify", "v2")["labels"] == prompts.load("classify", "v1")["labels"]


def test_judge_effort_from_prompt(conn):
    """판정기 버전 머리말의 effort가 호출로 가고, 추론 토큰 몫만큼 max_tokens를 늘린다"""
    gw = Recording(SqliteLlmLog(conn))
    cl.judge(gw, "직원: 네\n환자: 아파요", "inbound", "v1")
    cl.judge(gw, "직원: 네\n환자: 아파요", "inbound", "v2")
    (_, v1), (_, v2) = gw.calls
    assert "effort" not in v1 and v1["max_tokens"] == cl.MAX_TOKENS
    assert v2["effort"] == "low" and v2["max_tokens"] == cl.REASONING_MAX_TOKENS
    assert v2["system"] == v1["system"]


class BadLabels(FakeGateway):
    """JSON은 맞지만 라벨이 틀린 응답 — 항목 빠짐(classify)·목록 밖 값(judge)"""

    def complete(self, alias, **kw):
        r = super().complete(alias, **kw)
        if alias == "classify":
            return replace(r, data={k: v for k, v in r.data.items() if k != "urgency"})
        if alias == "judge":
            return replace(r, data=r.data | {"primary_purpose": "lunch"})
        return r


def test_invalid_labels_are_failed(conn):
    """9항목이 다 있고 값이 목록 안일 때만 ok — 아니면 failed로 남아 다음 실행이 다시 한다"""
    gw = BadLabels(SqliteLlmLog(conn), scope="runtime")
    assert not cl.classify(gw, "직원: 네\n환자: 예약", "inbound", "v1").ok
    assert not cl.judge(gw, "직원: 네\n환자: 예약", "inbound", "v1").ok


def test_valid_labels():
    good = dict.fromkeys(call_labels.FLAGS, False) | {
        "primary_purpose": "book",
        "counterpart": "patient",
        "urgency": "low",
    }
    assert cl.valid_labels(good)
    assert not cl.valid_labels(good | {"extra": 1})
    assert not cl.valid_labels(good | {"urgency": "soon"})
    assert not cl.valid_labels(good | {"has_todo": "yes"})
    assert not cl.valid_labels(good | {"has_todo": 1.0})
    assert cl.valid_labels(good | {"has_todo": 1, "complaint": 0})
    assert not cl.valid_labels(None)


def test_normalize_summary():
    assert (
        cl.normalize_summary("- 예약 변경 요청함\n\n**목요일** 3시로 옮김")
        == "예약 변경 요청함 목요일 3시로 옮김"
    )
    assert cl.normalize_summary("# 요약\n1. 첫째임\n• 둘째임  ") == "요약 첫째임 둘째임"
