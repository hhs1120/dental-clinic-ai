"""LLM 게이트웨이 (shared/llm.py, 앱 설계 §7) — 실제 API 대신 가짜 SDK 클라이언트를 넣어 동작을 본다.
응답·오류는 SDK의 실제 타입(Message·BetaMessage·OverloadedError 등)으로 만든다 — 단순화한 객체는 일반 경로에
usage.iterations가 없다는 것, 529가 InternalServerError가 아니라는 것 같은 차이를 가린다."""

import importlib
import json
import sys
import threading
import time
import types
from datetime import datetime

import anthropic
import pytest
from anthropic.types import Message
from anthropic.types.beta import BetaMessage

from shared import llm

http = importlib.import_module(
    type(anthropic.DefaultHttpxClient()).__mro__[1].__module__.split(".")[0]
)  # SDK가 쓰는 HTTP 라이브러리(이름에 기대지 않게)
CONFIG = {
    "aliases": {"gen": ["model-a", "model-b"], "solo": ["model-b"]},
    "models": {
        "model-a": {"input": 2.0, "output": 10.0, "cache_read": 0.2, "server_fallback": True},
        "model-b": {"input": 1.0, "output": 5.0},
    },
    "daily_usd_limit": {"generator": 1.0, "runtime": 0.5},
}
NOW = datetime(2026, 10, 27, 10, 0, 0)
MSGS = [{"role": "user", "content": "안녕"}]
NO_CACHE = {"cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}


def _usage(input_tokens=1000, output_tokens=200, cache_read=0):
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": cache_read,
    }


def _message(text="안녕하세요", model="model-b", stop="end_turn", **usage):
    """일반 경로(messages.create) 응답 — usage에 iterations 필드가 없다"""
    return Message.model_validate(
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": text}],
            "stop_reason": stop,
            "stop_sequence": None,
            "usage": _usage(**usage),
        }
    )


def _beta(text="안녕하세요", model="model-a", stop="end_turn", iterations=None, **usage):
    """서버 측 폴백 경로(beta.messages.create) 응답"""
    u = _usage(**usage) | ({"iterations": iterations} if iterations is not None else {})
    return BetaMessage.model_validate(
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": text}],
            "stop_reason": stop,
            "stop_sequence": None,
            "usage": u,
        }
    )


def _status_error(cls, status: int):
    request = http.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("오류", response=http.Response(status, request=request), body=None)


class _Messages:
    def __init__(self, outcomes, calls, path):
        self.outcomes, self.calls, self.path = outcomes, calls, path

    def create(self, **kwargs):
        self.calls.append((self.path, kwargs))
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


class StubClient:
    """messages.create(일반)와 beta.messages.create(서버 측 폴백) — 결과는 순서대로 하나의 목록에서 꺼낸다"""

    def __init__(self, *outcomes):
        self.calls, outcomes = [], list(outcomes)
        self.messages = _Messages(outcomes, self.calls, "plain")
        self.beta = type("Beta", (), {"messages": _Messages(outcomes, self.calls, "beta")})()


def _gateway(tmp_path, client, scope="generator"):
    return llm.Gateway(
        CONFIG,
        llm.JsonlLog(tmp_path / "calls.jsonl"),
        scope=scope,
        client=client,
        clock=lambda: NOW,
    )


def _log(tmp_path):
    return [
        json.loads(line)
        for line in (tmp_path / "calls.jsonl").read_text(encoding="utf-8").splitlines()
    ]


def test_alias_calls_first_model_with_server_fallback_and_logs_one_row(tmp_path):
    client = StubClient(_beta())
    result = _gateway(tmp_path, client).complete(
        "gen", task="gen_transcript", prompt_version="v1", messages=MSGS, ref=("call", 3)
    )
    assert (
        result.ok
        and result.text == "안녕하세요"
        and result.model == "model-a"
        and not result.fallback_used
    )
    assert result.cost_usd == pytest.approx(1000 * 2.0 / 1e6 + 200 * 10.0 / 1e6)
    path, kwargs = client.calls[0]
    assert (
        path == "beta"
        and kwargs["fallbacks"] == "default"
        and kwargs["betas"] == [llm.SERVER_FALLBACK_BETA]
    )
    [row] = _log(tmp_path)
    assert row["task"] == "gen_transcript" and row["alias"] == "gen" and row["model"] == "model-a"
    assert (
        row["prompt_version"] == "v1"
        and row["ref_type"] == "call"
        and row["ref_id"] == 3
        and row["ok"]
    )
    assert row["scope"] == "generator" and row["at"] == NOW.isoformat(timespec="seconds")


def test_plain_model_uses_plain_path_without_iterations(tmp_path):
    """서버 측 폴백을 안 켠 모델 — 일반 경로 응답(usage.iterations 없음)도 성공으로 기록"""
    client = StubClient(_message())
    result = _gateway(tmp_path, client).complete("solo", task="t", messages=MSGS)
    assert result.ok and not result.fallback_used and result.error is None
    path, kwargs = client.calls[0]
    assert path == "plain" and "fallbacks" not in kwargs and "betas" not in kwargs
    assert len(_log(tmp_path)) == 1


@pytest.mark.parametrize(
    "error",
    [
        _status_error(anthropic.OverloadedError, 529),
        _status_error(anthropic.InternalServerError, 500),
        _status_error(anthropic.RateLimitError, 429),
        anthropic.APIConnectionError(
            request=http.Request("POST", "https://api.anthropic.com/v1/messages")
        ),
    ],
    ids=["529", "500", "429", "connection"],
)
def test_transient_error_falls_back_and_keeps_cause(tmp_path, error):
    client = StubClient(error, _message(model="model-b"))
    result = _gateway(tmp_path, client).complete("gen", task="t", messages=MSGS)
    assert result.ok and result.model == "model-b" and result.fallback_used
    assert result.error and result.error.startswith(
        "model-a: "
    )  # 폴백 원인이 성공 결과에도 남는다
    assert [kw["model"] for _, kw in client.calls] == ["model-a", "model-b"]
    assert _log(tmp_path)[0]["fallback_used"] and _log(tmp_path)[0]["error"] == result.error


def test_bad_request_does_not_fall_back(tmp_path):
    client = StubClient(_status_error(anthropic.BadRequestError, 400))
    result = _gateway(tmp_path, client).complete("gen", task="t", messages=MSGS)
    assert (
        not result.ok and len(client.calls) == 1 and "400" in result.error
    )  # 요청이 틀렸으면 다른 모델도 같다


def test_refusal_falls_back_and_all_fail_is_not_ok(tmp_path):
    client = StubClient(_beta(stop="refusal", text=""), _message(stop="refusal", text=""))
    result = _gateway(tmp_path, client).complete("gen", task="t", messages=MSGS)
    assert not result.ok and "refusal" in result.error
    # 거절된 시도의 토큰도 비용에 들어간다
    assert result.cost_usd == pytest.approx(
        1000 * 2.0 / 1e6 + 200 * 10.0 / 1e6 + 1000 * 1.0 / 1e6 + 200 * 5.0 / 1e6
    )
    assert not _log(tmp_path)[0]["ok"]


def test_truncated_response_is_failure_without_fallback(tmp_path):
    client = StubClient(_beta(stop="max_tokens", text="잘린"))
    result = _gateway(tmp_path, client).complete("gen", task="t", messages=MSGS)
    assert not result.ok and "max_tokens" in result.error and len(client.calls) == 1
    assert result.cost_usd > 0 and _log(tmp_path)[0]["cost_usd"] > 0


def test_invalid_json_with_schema_is_logged_failure(tmp_path):
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}},
        "required": ["a"],
        "additionalProperties": False,
    }
    client = StubClient(_beta(text="{잘린 json"))
    result = _gateway(tmp_path, client).complete("gen", task="t", messages=MSGS, schema=schema)
    assert not result.ok and "JSON" in result.error and not _log(tmp_path)[0]["ok"]


def test_unexpected_error_is_logged_then_raised(tmp_path):
    client = StubClient(ValueError("SDK 내부 오류"))
    with pytest.raises(ValueError):
        _gateway(tmp_path, client).complete("gen", task="t", messages=MSGS)
    [row] = _log(tmp_path)
    assert not row["ok"] and "ValueError" in row["error"]


def test_server_side_fallback_sums_iterations(tmp_path):
    """서버 측 폴백: top-level usage는 마지막 시도만 — iterations의 시도별 토큰을 그 모델 단가로 합산"""
    iterations = [
        {
            "type": "message",
            "model": "model-a",
            "input_tokens": 1000,
            "output_tokens": 300,
            **NO_CACHE,
        },
        {
            "type": "fallback_message",
            "model": "model-b",
            "input_tokens": 100,
            "output_tokens": 50,
            **NO_CACHE,
        },
    ]
    client = StubClient(
        _beta(model="model-b", iterations=iterations, input_tokens=100, output_tokens=50)
    )
    result = _gateway(tmp_path, client).complete("gen", task="t", messages=MSGS)
    assert result.ok and result.fallback_used and result.model == "model-b"
    assert (result.input_tokens, result.output_tokens) == (1100, 350)
    assert result.cost_usd == pytest.approx((1000 * 2.0 + 300 * 10.0 + 100 * 1.0 + 50 * 5.0) / 1e6)


def test_cache_read_tokens_priced_separately(tmp_path):
    client = StubClient(_message(input_tokens=100, cache_read=1000))
    result = _gateway(tmp_path, client).complete("solo", task="t", messages=MSGS)
    assert result.cost_usd == pytest.approx(
        100 * 1.0 / 1e6 + 1000 * 0.1 / 1e6 + 200 * 5.0 / 1e6
    )  # cache_read 없으면 input × 0.1


def test_schema_is_transformed_and_json_parsed(tmp_path):
    """구조화 출력이 지원하지 않는 제약(minimum)은 SDK transform_schema로 설명으로 옮겨 보낸다"""
    schema = {
        "type": "object",
        "properties": {"n": {"type": "integer", "minimum": 1}},
        "required": ["n"],
    }
    client = StubClient(_beta(text='{"n": 3}'))
    result = _gateway(tmp_path, client).complete(
        "gen", task="t", messages=MSGS, schema=schema, effort="medium"
    )
    assert result.data == {"n": 3}
    sent = client.calls[0][1]["output_config"]
    assert sent["effort"] == "medium" and sent["format"]["type"] == "json_schema"
    assert sent["format"]["schema"] == anthropic.transform_schema(schema)
    assert "minimum" not in sent["format"]["schema"]["properties"]["n"]


def test_budget_exceeded_stops_before_call(tmp_path):
    log = llm.JsonlLog(tmp_path / "calls.jsonl")
    log.write({"at": NOW.isoformat(timespec="seconds"), "scope": "generator", "cost_usd": 0.6})
    log.write({"at": NOW.isoformat(timespec="seconds"), "scope": "generator", "cost_usd": 0.5})
    log.write(
        {"at": "2026-10-26T10:00:00", "scope": "runtime", "cost_usd": 9.0}
    )  # 다른 날·다른 범위는 세지 않음
    assert log.spent(NOW.date(), "generator") == pytest.approx(1.1)
    assert llm.JsonlLog(tmp_path / "calls.jsonl").spent(NOW.date(), "generator") == pytest.approx(
        1.1
    )  # 파일에서 다시 읽음
    client = StubClient(_beta())
    with pytest.raises(llm.BudgetExceeded):
        llm.Gateway(CONFIG, log, scope="generator", client=client, clock=lambda: NOW).complete(
            "gen", task="t", messages=MSGS
        )
    assert client.calls == []
    # 런타임 범위는 그날 쓴 돈이 없어 통과
    assert (
        llm.Gateway(CONFIG, log, scope="runtime", client=client, clock=lambda: NOW)
        .complete("gen", task="t", messages=MSGS)
        .ok
    )


def test_unknown_alias_is_error(tmp_path):
    with pytest.raises(KeyError):
        _gateway(tmp_path, StubClient()).complete("nope", task="t", messages=MSGS)


def _conforms(value, schema: dict, root: dict) -> bool:
    """테스트용 최소 스키마 검사 — FakeGateway가 다루는 문법과 같은 범위"""
    if "$ref" in schema:
        node = root
        for part in schema["$ref"].lstrip("#/").split("/"):
            node = node[part]
        return _conforms(value, node, root)
    if "anyOf" in schema:
        return any(_conforms(value, s, root) for s in schema["anyOf"])
    if "const" in schema:
        return value == schema["const"]
    if "enum" in schema:
        return value in schema["enum"]
    kinds = schema["type"] if isinstance(schema.get("type"), list) else [schema.get("type")]
    checks = {
        "object": lambda v: (
            isinstance(v, dict)
            and set(schema.get("required", [])) <= set(v)
            and all(
                _conforms(v[k], s, root) for k, s in schema.get("properties", {}).items() if k in v
            )
        ),
        "array": lambda v: (
            isinstance(v, list) and all(_conforms(x, schema.get("items", {}), root) for x in v)
        ),
        "string": lambda v: isinstance(v, str),
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "number": lambda v: isinstance(v, (int, float)),
        "boolean": lambda v: isinstance(v, bool),
        "null": lambda v: v is None,
    }
    return any(checks[k](value) for k in kinds)


SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["scenario", "turns", "urgent", "kind", "speaker", "note", "version", "nested"],
    "properties": {
        "scenario": {"type": "string"},
        "turns": {"type": "array", "items": {"type": "integer"}, "minItems": 2},
        "urgent": {"type": "boolean"},
        "kind": {"type": "string", "enum": ["a", "b", "c"]},
        "speaker": {"anyOf": [{"const": "환자"}, {"const": "직원"}]},
        "note": {"type": ["string", "null"]},
        "version": {"const": 1},
        "nested": {"$ref": "#/$defs/line"},
    },
    "$defs": {
        "line": {
            "type": "object",
            "additionalProperties": False,
            "required": ["who"],
            "properties": {"who": {"type": "string"}},
        }
    },
}


def test_fake_gateway_is_deterministic_and_follows_schema(tmp_path):
    fake = llm.FakeGateway(
        llm.JsonlLog(tmp_path / "calls.jsonl"), scope="generator", clock=lambda: NOW
    )
    one = fake.complete("gen", task="t", messages=MSGS, schema=SCHEMA)
    assert one == fake.complete("gen", task="t", messages=MSGS, schema=SCHEMA)
    assert (
        one.ok and one.model == "fake" and one.cost_usd == 0 and json.loads(one.text) == one.data
    )
    assert _conforms(one.data, SCHEMA, SCHEMA) and len(one.data["turns"]) == 2
    other = fake.complete(
        "gen", task="t", messages=[{"role": "user", "content": "다른 입력"}], schema=SCHEMA
    )
    assert _conforms(other.data, SCHEMA, SCHEMA) and other.data["scenario"] != one.data["scenario"]
    plain = fake.complete("gen", task="t", messages=MSGS)
    assert plain.text and plain.data is None
    assert len(_log(tmp_path)) == 4


def test_default_config_models_have_prices():
    cfg = llm.load_config()
    assert all(m in cfg["models"] for chain in cfg["aliases"].values() for m in chain)


# ── Batch (생성기 API 채우기 — 앱 설계 §7) ─────────────────────

from anthropic.types.messages import MessageBatch, MessageBatchIndividualResponse  # noqa: E402

TEXT_SCHEMA = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}


def _batch(status="ended"):
    return MessageBatch.model_validate(
        {
            "id": "batch_1",
            "type": "message_batch",
            "processing_status": status,
            "created_at": "2026-10-27T10:00:00Z",
            "expires_at": "2026-10-28T10:00:00Z",
            "archived_at": None,
            "cancel_initiated_at": None,
            "ended_at": None,
            "results_url": None,
            "request_counts": {
                "processing": 0,
                "succeeded": 2,
                "errored": 0,
                "canceled": 0,
                "expired": 0,
            },
        }
    )


def _entry(custom_id, result):
    return MessageBatchIndividualResponse.model_validate(
        {"custom_id": custom_id, "result": result}
    )


class _Batches:
    def __init__(self, results):
        self.results_, self.created = results, None

    def create(self, requests):
        self.created = requests
        return _batch("in_progress")

    def retrieve(self, batch_id):
        return _batch()

    def results(self, batch_id):
        return iter(self.results_)


class BatchClient:
    def __init__(self, results=()):
        self.messages = type("M", (), {"batches": _Batches(list(results))})()


def test_batch_submit_uses_first_model_without_server_fallback(tmp_path):
    client = BatchClient()
    gw = _gateway(tmp_path, client)
    batch_id = gw.submit_batch(
        "gen",
        [{"custom_id": "k1", "system": "지시", "messages": MSGS, "schema": TEXT_SCHEMA}],
        task="toy",
        prompt_version="toy/v1",
    )
    assert batch_id == "batch_1" and gw.batch_status(batch_id) == "ended"
    (req,) = client.messages.batches.created
    assert req["custom_id"] == "k1"
    params = req["params"]
    assert (
        params["model"] == "model-a" and params["system"] == "지시" and params["messages"] == MSGS
    )
    assert "fallbacks" not in params and params["output_config"]["format"]["type"] == "json_schema"


def test_batch_submit_keeps_per_request_effort_and_max_tokens(tmp_path):
    """요청에 effort·max_tokens가 있으면 그것으로 — 단건 호출과 같은 버전이 같은 설정으로 돈다(앱 설계 §7.2)"""
    client = BatchClient()
    gw = _gateway(tmp_path, client)
    gw.submit_batch(
        "gen",
        [
            {"custom_id": "k1", "messages": MSGS, "effort": "low", "max_tokens": 16000},
            {"custom_id": "k2", "messages": MSGS},
        ],
        task="toy",
        max_tokens=3000,
    )
    r1, r2 = (r["params"] for r in client.messages.batches.created)
    assert r1["max_tokens"] == 16000 and r1["output_config"] == {"effort": "low"}
    assert r2["max_tokens"] == 3000 and "output_config" not in r2


def test_batch_results_priced_at_batch_share_and_logged(tmp_path):
    ok = _message(
        text='{"text": "사과"}', model="model-a", input_tokens=1000, output_tokens=200
    ).model_dump()
    refused = _message(
        text="", model="model-a", stop="refusal", input_tokens=1000, output_tokens=0
    ).model_dump()
    bad_json = _message(text="사과", model="model-a").model_dump()
    client = BatchClient(
        [
            _entry("k1", {"type": "succeeded", "message": ok}),
            _entry("k2", {"type": "succeeded", "message": refused}),
            _entry("k3", {"type": "succeeded", "message": bad_json}),
            _entry("k4", {"type": "expired"}),
            _entry(
                "k5",
                {
                    "type": "errored",
                    "error": {"type": "error", "error": {"type": "api_error", "message": "x"}},
                },
            ),
        ]
    )
    gw = _gateway(tmp_path, client)
    out = gw.batch_results(
        "batch_1", "gen", task="toy", prompt_version="toy/v1", schema=TEXT_SCHEMA
    )
    assert out["k1"].ok and out["k1"].data == {"text": "사과"}
    assert out["k1"].cost_usd == pytest.approx(
        (1000 * 2.0 + 200 * 10.0) / 1e6 * llm.BATCH_PRICE_SHARE
    )
    assert not out["k2"].ok and "refusal" in out["k2"].error
    assert not out["k3"].ok and "JSON" in out["k3"].error
    assert out["k4"] is None and out["k5"] is None  # 처리 안 됨 — 시도로 세지 않는다
    log = _log(tmp_path)
    assert [r["ref_id"] for r in log] == ["k1", "k2", "k3"] and all(
        r["task"] == "toy" for r in log
    )


def test_batch_submit_ignores_daily_limit(tmp_path):
    """배치는 하루 상한 밖 — 건수가 정해져 있어 사람이 보고 제출한다(기능 정의 §7). 즉시 호출만 상한에 걸린다"""
    gw = _gateway(tmp_path, BatchClient())
    gw.log.write({"at": NOW.isoformat(), "scope": "generator", "cost_usd": 1.0})
    assert gw.submit_batch("gen", [], task="toy")


TOOLS = [{"name": "query_patient", "description": "환자 조회", "input_schema": {"type": "object"}}]


def _tool_message(model="model-b", **usage):
    """모델이 도구를 부른 응답 — stop_reason tool_use, 글과 도구 호출이 함께 온다"""
    message = _message(model=model, **usage).model_dump()
    message["stop_reason"] = "tool_use"
    message["content"] = [
        {"type": "text", "text": "조회합니다"},
        {"type": "tool_use", "id": "t1", "name": "query_patient", "input": {"chart_no": "C-1"}},
    ]
    return Message.model_validate(message)


def test_tool_use_stop_is_success_and_returns_calls_and_content(tmp_path):
    client = StubClient(_tool_message())
    result = _gateway(tmp_path, client).complete("solo", task="t", messages=MSGS, tools=TOOLS)
    assert result.ok and result.stop_reason == "tool_use" and result.text == "조회합니다"
    assert result.tool_calls == (
        {"id": "t1", "name": "query_patient", "input": {"chart_no": "C-1"}},
    )
    assert result.content == [
        {"type": "text", "text": "조회합니다"},
        {"type": "tool_use", "id": "t1", "name": "query_patient", "input": {"chart_no": "C-1"}},
    ]
    assert client.calls[0][1]["tools"] == TOOLS


def test_no_tools_argument_is_not_sent(tmp_path):
    client = StubClient(_message())
    result = _gateway(tmp_path, client).complete("solo", task="t", messages=MSGS)
    assert "tools" not in client.calls[0][1]
    assert result.stop_reason == "end_turn" and result.tool_calls == ()


def test_tool_use_without_tools_is_still_failure(tmp_path):
    """도구를 주지 않았는데 tool_use로 끝나면 비정상 — 기존 호출자는 성공으로 받으면 안 된다"""
    result = _gateway(tmp_path, StubClient(_tool_message())).complete(
        "solo", task="t", messages=MSGS
    )
    assert not result.ok and "stop_reason tool_use" in result.error


def test_fake_gateway_accepts_tools_and_ends_turn(tmp_path):
    fake = llm.FakeGateway(llm.JsonlLog(tmp_path / "calls.jsonl"), scope="runtime")
    result = fake.complete("chat", task="t", messages=MSGS, tools=TOOLS)
    assert result.ok and result.stop_reason == "end_turn" and result.tool_calls == ()


def test_tool_use_with_schema_skips_json_parse(tmp_path):
    client = StubClient(_tool_message())
    result = _gateway(tmp_path, client).complete(
        "solo", task="t", messages=MSGS, tools=TOOLS, schema={"type": "object"}
    )
    assert result.ok and result.data is None and result.tool_calls


def test_tools_are_sent_to_fallback_model(tmp_path):
    client = StubClient(_status_error(anthropic.InternalServerError, 500), _tool_message())
    result = _gateway(tmp_path, client).complete("gen", task="t", messages=MSGS, tools=TOOLS)
    assert result.ok and result.fallback_used and client.calls[1][1]["tools"] == TOOLS


# ── Upstage (Solar, OpenAI 호환 API — 분류기 설계 "게이트웨이") ─────────────────────

import openai  # noqa: E402
from openai.types.chat import ChatCompletion  # noqa: E402

UP_CONFIG = {
    "aliases": {"up": ["solar"], "up-then-b": ["solar", "model-b"]},
    "models": {
        "solar": {"input": 0.15, "output": 0.6, "provider": "upstage"},
        "model-b": {"input": 1.0, "output": 5.0},
    },
    "daily_usd_limit": {"generator": 1.0, "runtime": 0.5},
}
LABELS_SCHEMA = {
    "type": "object",
    "properties": {"label": {"type": "string"}},
    "required": ["label"],
    "additionalProperties": False,
}


def _chat(text='{"label": "예약"}', model="solar-mini-250422", finish="stop", pt=1000, ct=100):
    return ChatCompletion.model_validate(
        {
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "created": 0,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": finish,
                    "message": {"role": "assistant", "content": text},
                }
            ],
            "usage": {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct},
        }
    )


class UpstageStub:
    """chat.completions.create — 결과는 순서대로 꺼낸다"""

    def __init__(self, *outcomes):
        self.calls, outcomes = [], list(outcomes)
        self.chat = type("Chat", (), {"completions": _Messages(outcomes, self.calls, "upstage")})()


def _up_gateway(tmp_path, upstage, client=None):
    return llm.Gateway(
        UP_CONFIG,
        llm.JsonlLog(tmp_path / "calls.jsonl"),
        scope="runtime",
        client=client or StubClient(),
        upstage_client=upstage,
        clock=lambda: NOW,
    )


def test_upstage_complete_parses_json(tmp_path):
    up = UpstageStub(_chat())
    gw = _up_gateway(tmp_path, up)
    r = gw.complete("up", task="classify", system="지시", messages=MSGS, schema=LABELS_SCHEMA)
    assert r.ok and r.data == {"label": "예약"} and r.model == "solar-mini-250422"
    assert not r.fallback_used and (r.input_tokens, r.output_tokens) == (1000, 100)
    assert r.cost_usd == pytest.approx((1000 * 0.15 + 100 * 0.6) / 1e6)
    ((_, kwargs),) = up.calls
    assert kwargs["model"] == "solar"
    assert kwargs["messages"] == [{"role": "system", "content": "지시"}, *MSGS]
    fmt = kwargs["response_format"]
    assert fmt["type"] == "json_schema" and fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["schema"] == LABELS_SCHEMA
    (row,) = _log(tmp_path)
    assert row["ok"] and row["model"] == "solar-mini-250422"


@pytest.mark.parametrize(
    "error",
    [
        openai.APIConnectionError(request=http.Request("POST", "https://api.upstage.ai/v1")),
        openai.APIStatusError(
            "오류",
            response=http.Response(429, request=http.Request("POST", "https://api.upstage.ai")),
            body=None,
        ),
    ],
)
def test_upstage_transient_error_recorded(tmp_path, error):
    gw = _up_gateway(tmp_path, UpstageStub(error))
    r = gw.complete("up", task="classify", messages=MSGS, schema=LABELS_SCHEMA)
    assert not r.ok and "solar" in r.error
    assert len(_log(tmp_path)) == 1

    # 별칭에 다음 모델이 있으면 넘어간다
    client = StubClient(_message(text='{"label": "x"}'))
    gw = _up_gateway(tmp_path, UpstageStub(error), client)
    r = gw.complete("up-then-b", task="classify", messages=MSGS, schema=LABELS_SCHEMA)
    assert r.ok and r.fallback_used and r.model == "model-b"


def test_upstage_length_cut_fails(tmp_path):
    gw = _up_gateway(tmp_path, UpstageStub(_chat(finish="length")))
    r = gw.complete("up-then-b", task="classify", messages=MSGS, schema=LABELS_SCHEMA)
    assert not r.ok and "finish_reason length" in r.error and r.model == "solar-mini-250422"


def test_upstage_batch_rejected(tmp_path):
    gw = _up_gateway(tmp_path, UpstageStub())
    with pytest.raises(ValueError):
        gw.submit_batch("up", [{"custom_id": "k", "messages": MSGS}], task="classify")


def test_log_id_is_none_for_jsonl_log(tmp_path):
    gw = _up_gateway(tmp_path, UpstageStub(_chat()))
    assert gw.complete("up", task="t", messages=MSGS, schema=LABELS_SCHEMA).log_id is None


def test_upstage_effort_sent_as_reasoning_effort(tmp_path):
    up = UpstageStub(_chat(), _chat())
    gw = _up_gateway(tmp_path, up)
    gw.complete("up", task="t", messages=MSGS, schema=LABELS_SCHEMA, effort="low")
    gw.complete("up", task="t", messages=MSGS, schema=LABELS_SCHEMA)
    (_, with_effort), (_, without) = up.calls
    assert with_effort["reasoning_effort"] == "low" and "reasoning_effort" not in without


def test_batch_with_tools_round_trip(tmp_path):
    """라운드 배치 — 요청에 tools가 들어가고, tool_use 응답은 단건 complete와 같은 모양(tool_calls·content)으로 성공"""
    tools = [{"name": "query_patient", "description": "환자", "input_schema": {"type": "object"}}]
    msg = Message.model_validate(
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "model-a",
            "content": [
                {"type": "text", "text": "조회합니다"},
                {
                    "type": "tool_use",
                    "id": "tu1",
                    "name": "query_patient",
                    "input": {"chart_no": "C-1"},
                },
            ],
            "stop_reason": "tool_use",
            "stop_sequence": None,
            "usage": _usage(input_tokens=100, output_tokens=20),
        }
    ).model_dump()
    client = BatchClient([_entry("r1", {"type": "succeeded", "message": msg})])
    gw = _gateway(tmp_path, client)
    gw.submit_batch("gen", [{"custom_id": "r1", "messages": MSGS, "tools": tools}], task="toy")
    (req,) = client.messages.batches.created
    assert req["params"]["tools"] == tools
    out = gw.batch_results("batch_1", "gen", task="toy")["r1"]
    assert out.ok and out.stop_reason == "tool_use" and out.text == "조회합니다"
    assert out.tool_calls == (
        {"id": "tu1", "name": "query_patient", "input": {"chart_no": "C-1"}},
    )
    assert out.content[-1] == {
        "type": "tool_use",
        "id": "tu1",
        "name": "query_patient",
        "input": {"chart_no": "C-1"},
    }


# ── 로컬 MLX(Qwen 판정기 설계 "게이트웨이 로컬 갈래") ──────────────────────────
MLX_CONFIG = {
    "aliases": {"q": ["qwen"]},
    "models": {
        "qwen": {"provider": "mlx", "base": "base-id", "adapter": "ad", "input": 0, "output": 0}
    },
    "daily_usd_limit": {"generator": 1.0, "runtime": 0.5},
}


def _mlx_gateway(tmp_path, local_generate=None):
    return llm.Gateway(
        MLX_CONFIG,
        llm.JsonlLog(tmp_path / "calls.jsonl"),
        scope="runtime",
        client=StubClient(),
        local_generate=local_generate,
        clock=lambda: NOW,
    )


def test_mlx_ok_records_row(tmp_path):
    seen = []

    def gen(name, cfg, messages):
        seen.append((name, cfg["base"], messages))
        return llm.LocalOutput('잡담 {"label": "예약"} 끝', 50, 9, "qwen@abcd")

    r = _mlx_gateway(tmp_path, gen).complete(
        "q", task="judge", system="지시", messages=MSGS, schema=LABELS_SCHEMA, max_tokens=3000
    )
    assert r.ok and r.data == {"label": "예약"} and r.model == "qwen@abcd"
    assert (r.input_tokens, r.output_tokens, r.cost_usd, r.fallback_used) == (50, 9, 0, False)
    assert seen == [("qwen", "base-id", [{"role": "system", "content": "지시"}, *MSGS])]
    (row,) = _log(tmp_path)
    assert row["ok"] and row["model"] == "qwen@abcd" and row["cost_usd"] == 0


def test_mlx_no_json_fails(tmp_path):
    gen = lambda name, cfg, messages: llm.LocalOutput("모름", 5, 1, "qwen@abcd")  # noqa: E731
    r = _mlx_gateway(tmp_path, gen).complete(
        "q", task="judge", messages=MSGS, schema=LABELS_SCHEMA
    )
    assert not r.ok and "JSON 파싱 실패" in r.error
    assert len(_log(tmp_path)) == 1


def test_mlx_missing_module(tmp_path, monkeypatch):
    """--group finetune 없이 띄우면 예외 대신 실패 — 앱이 멈추지 않는다"""
    monkeypatch.setattr(llm, "_LOCAL_MODELS", {})
    monkeypatch.setitem(sys.modules, "mlx_lm", None)
    r = _mlx_gateway(tmp_path).complete("q", task="judge", messages=MSGS, schema=LABELS_SCHEMA)
    assert not r.ok and "--group finetune" in r.error
    assert len(_log(tmp_path)) == 1


class FakeTokenizer:
    def apply_chat_template(self, messages, **kw):
        assert kw == {"add_generation_prompt": True, "enable_thinking": False, "tokenize": False}
        return "|".join(m["content"] for m in messages)

    def encode(self, text):
        return list(text)


def _fake_mlx(monkeypatch, loads, overlap):
    busy = []

    def load(base, adapter_path=None):
        loads.append((base, adapter_path))
        return "model", FakeTokenizer()

    def generate(model, tokenizer, prompt, max_tokens):
        if busy:
            overlap.append(prompt)
        busy.append(1)
        time.sleep(0.01)
        busy.pop()
        assert max_tokens == llm.LOCAL_MAX_TOKENS
        return '{"label": "예약"}'

    monkeypatch.setitem(sys.modules, "mlx_lm", types.SimpleNamespace(load=load, generate=generate))
    monkeypatch.setattr(llm, "_LOCAL_MODELS", {})


def test_mlx_missing_adapter(tmp_path, monkeypatch):
    _fake_mlx(monkeypatch, [], [])
    monkeypatch.setattr(llm, "ROOT", tmp_path)
    r = _mlx_gateway(tmp_path).complete("q", task="judge", messages=MSGS, schema=LABELS_SCHEMA)
    assert not r.ok and str(tmp_path / "ad") in r.error


def test_mlx_loads_once_and_serializes(tmp_path, monkeypatch):
    loads, overlap = [], []
    _fake_mlx(monkeypatch, loads, overlap)
    monkeypatch.setattr(llm, "ROOT", tmp_path)
    (tmp_path / "ad").mkdir()
    (tmp_path / "ad" / "adapters.safetensors").write_bytes(b"w")
    results = []

    def run():
        gw = _mlx_gateway(tmp_path)  # API처럼 요청마다 새 게이트웨이
        for _ in range(3):
            results.append(gw.complete("q", task="judge", messages=MSGS, schema=LABELS_SCHEMA))

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert loads == [("base-id", str(tmp_path / "ad"))] and overlap == []
    fp = llm.adapter_fingerprint(tmp_path / "ad")
    assert all(r.ok and r.model == f"qwen@{fp}" for r in results) and len(results) == 6


def test_adapter_fingerprint_matches_infer(tmp_path):
    from evaluation.finetune.qwen.infer import adapter_fingerprint

    (tmp_path / "adapters.safetensors").write_bytes(b"w")
    assert llm.adapter_fingerprint(tmp_path) == adapter_fingerprint(tmp_path)


def test_local_max_tokens_matches_infer():
    from evaluation.finetune.qwen import infer

    assert llm.LOCAL_MAX_TOKENS == infer.MAX_TOKENS


def test_mlx_load_error_is_failure(tmp_path, monkeypatch):
    """적재 중 예상 밖 오류(오프라인 hub·메모리 부족)도 실패 결과 — make judge·다시 분류가 멈추지 않게"""

    def load(base, adapter_path=None):
        raise RuntimeError("hub offline")

    monkeypatch.setitem(sys.modules, "mlx_lm", types.SimpleNamespace(load=load, generate=None))
    monkeypatch.setattr(llm, "_LOCAL_MODELS", {})
    monkeypatch.setattr(llm, "ROOT", tmp_path)
    (tmp_path / "ad").mkdir()
    (tmp_path / "ad" / "adapters.safetensors").write_bytes(b"w")
    r = _mlx_gateway(tmp_path).complete("q", task="judge", messages=MSGS, schema=LABELS_SCHEMA)
    assert not r.ok and "로컬 실행 실패" in r.error and "hub offline" in r.error
    assert llm._LOCAL_MODELS == {}  # 다음 호출이 다시 적재한다


def test_mlx_cache_key_includes_name(tmp_path, monkeypatch):
    """같은 base·adapter의 설정 항목이 둘이어도 응답 모델 이름은 각자"""
    _fake_mlx(monkeypatch, [], [])
    monkeypatch.setattr(llm, "ROOT", tmp_path)
    (tmp_path / "ad").mkdir()
    (tmp_path / "ad" / "adapters.safetensors").write_bytes(b"w")
    cfg = {**MLX_CONFIG, "aliases": {"q": ["qwen"], "q2": ["qwen2"]},
           "models": {**MLX_CONFIG["models"], "qwen2": MLX_CONFIG["models"]["qwen"]}}  # fmt: skip
    gw = llm.Gateway(cfg, llm.JsonlLog(tmp_path / "calls.jsonl"), scope="runtime",
                     client=StubClient(), clock=lambda: NOW)  # fmt: skip
    a = gw.complete("q", task="judge", messages=MSGS, schema=LABELS_SCHEMA)
    b = gw.complete("q2", task="judge", messages=MSGS, schema=LABELS_SCHEMA)
    assert a.model.startswith("qwen@") and b.model.startswith("qwen2@")


# ── 로컬 보기 강제 (제약 디코딩 — 자유 생성이 스키마 밖일 때만) ─────────────────

ENUM_SCHEMA = {
    "type": "object",
    "properties": {
        "counterpart": {"type": "string", "enum": ["patient", "guardian"]},
        "emergency": {"type": "boolean"},
    },
    "required": ["counterpart", "emergency"],
    "additionalProperties": False,
}


def test_fits_schema_accepts_enum_and_zero_one_flags():
    assert llm.fits_schema({"counterpart": "patient", "emergency": 0}, ENUM_SCHEMA)
    assert llm.fits_schema({"counterpart": "patient", "emergency": True}, ENUM_SCHEMA)
    assert not llm.fits_schema({"counterpart": "student", "emergency": 0}, ENUM_SCHEMA)
    assert not llm.fits_schema({"counterpart": "patient"}, ENUM_SCHEMA)
    assert not llm.fits_schema({"counterpart": "patient", "emergency": 2}, ENUM_SCHEMA)


def _mlx_with_constrain(tmp_path, text):
    seen = []

    def gen(name, cfg, messages):
        return llm.LocalOutput(text, 50, 9, "qwen@abcd")

    def constrain(name, cfg, messages, schema):
        seen.append(schema)
        return llm.LocalOutput('{"counterpart":"patient","emergency":0}', 50, 0, "qwen@abcd")

    gw = llm.Gateway(
        MLX_CONFIG,
        llm.JsonlLog(tmp_path / "calls.jsonl"),
        scope="runtime",
        client=StubClient(),
        local_generate=gen,
        local_constrain=constrain,
        clock=lambda: NOW,
    )
    return gw, seen


def test_out_of_schema_output_is_redone_with_constraint(tmp_path):
    gw, seen = _mlx_with_constrain(tmp_path, '{"counterpart":"student","emergency":0}')
    r = gw.complete("q", task="judge", system="지시", messages=MSGS, schema=ENUM_SCHEMA)
    assert r.ok and r.data == {"counterpart": "patient", "emergency": 0} and seen == [ENUM_SCHEMA]


def test_valid_output_is_not_redone(tmp_path):
    gw, seen = _mlx_with_constrain(tmp_path, '{"counterpart":"guardian","emergency":1}')
    r = gw.complete("q", task="judge", system="지시", messages=MSGS, schema=ENUM_SCHEMA)
    assert r.data == {"counterpart": "guardian", "emergency": 1} and seen == []


def test_unparsable_output_is_redone_with_constraint(tmp_path):
    gw, seen = _mlx_with_constrain(tmp_path, "글만 냄")
    assert gw.complete("q", task="judge", messages=MSGS, schema=ENUM_SCHEMA).ok and seen


# ── OpenAI (Chat Completions·Batch — 어려운 통화 녹취, 어려운 통화 설계 §3.5) ─────────────

OA_CONFIG = {
    "aliases": {"oa": ["gpt-x"]},
    "models": {"gpt-x": {"input": 2.0, "output": 10.0, "provider": "openai"}},
    "daily_usd_limit": {"generator": 1.0, "runtime": 0.5},
}


def _oa_gateway(tmp_path, oa):
    return llm.Gateway(
        OA_CONFIG,
        llm.JsonlLog(tmp_path / "calls.jsonl"),
        scope="generator",
        client=StubClient(),
        openai_client=oa,
        clock=lambda: NOW,
    )


def test_openai_complete_uses_completion_tokens(tmp_path):
    oa = UpstageStub(_chat(model="gpt-x-2026"))
    r = _oa_gateway(tmp_path, oa).complete(
        "oa", task="hard_transcript", messages=MSGS, schema=LABELS_SCHEMA, max_tokens=900
    )
    assert r.ok and r.data == {"label": "예약"}
    assert r.cost_usd == pytest.approx((1000 * 2.0 + 100 * 10.0) / 1e6)
    ((_, kwargs),) = oa.calls
    assert kwargs["max_completion_tokens"] == 900 and "max_tokens" not in kwargs
    assert kwargs["response_format"]["json_schema"]["strict"] is True


class _OaFiles:
    def __init__(self, contents):
        self.contents, self.created = contents, []

    def create(self, file, purpose):
        self.created.append((file, purpose))
        return types.SimpleNamespace(id="file-in")

    def content(self, file_id):
        return types.SimpleNamespace(text=self.contents[file_id])


class _OaBatches:
    def __init__(self, status, out, err):
        self.status, self.out, self.err, self.created = status, out, err, []

    def create(self, **kwargs):
        self.created.append(kwargs)
        return types.SimpleNamespace(id="batch_1")

    def retrieve(self, batch_id):
        return types.SimpleNamespace(
            status=self.status,
            output_file_id=self.out,
            error_file_id=self.err,
            errors=getattr(self, "errors", None),
        )


def _oa_line(key, text, finish="stop"):
    body = _chat(text=text, model="gpt-x-2026", finish=finish).model_dump()
    return json.dumps({"custom_id": key, "response": {"status_code": 200, "body": body}})


def test_openai_batch_submit_and_partial_results(tmp_path):
    """제출은 JSONL 파일 → /v1/chat/completions 배치, 결과는 성공·잘림 실패·만료(None)로 나뉘고 비용은 배치 단가"""
    out = "\n".join([_oa_line("k1", '{"label": "예약"}'), _oa_line("k2", "{", "length")])
    err = json.dumps({"custom_id": "k3", "response": None, "error": {"code": "batch_expired"}})
    files = _OaFiles({"file-out": out, "file-err": err})
    oa = types.SimpleNamespace(
        files=files, batches=_OaBatches("completed", "file-out", "file-err")
    )
    gw = _oa_gateway(tmp_path, oa)
    reqs = [
        {"custom_id": k, "messages": MSGS, "schema": LABELS_SCHEMA} for k in ("k1", "k2", "k3")
    ]
    assert gw.submit_batch("oa", reqs, task="hard_transcript") == "batch_1"
    (created,) = oa.batches.created
    assert created["endpoint"] == "/v1/chat/completions" and created["input_file_id"] == "file-in"
    (((_, payload), purpose),) = files.created
    lines = [json.loads(x) for x in payload.decode().splitlines()]
    assert purpose == "batch" and [x["custom_id"] for x in lines] == ["k1", "k2", "k3"]
    assert lines[0]["url"] == "/v1/chat/completions" and lines[0]["body"]["model"] == "gpt-x"
    assert gw.batch_status("batch_1", "oa") == "ended"
    r = gw.batch_results("batch_1", "oa", task="hard_transcript", schema=LABELS_SCHEMA)
    assert r["k1"].ok and r["k1"].data == {"label": "예약"}
    assert r["k1"].cost_usd == pytest.approx((1000 * 2.0 + 100 * 10.0) / 1e6 * 0.5)
    assert not r["k2"].ok and r["k3"] is None


def test_openai_batch_status_in_progress(tmp_path):
    oa = types.SimpleNamespace(files=None, batches=_OaBatches("in_progress", None, None))
    assert _oa_gateway(tmp_path, oa).batch_status("batch_1", "oa") == "in_progress"


def test_openai_batch_failed_shows_reason(tmp_path, capsys):
    """입력 검증 실패 등으로 배치가 failed면 결과 파일이 없다 — 사유(batch.errors)를 보여 주고 빈 결과(다시 돌면 재제출)"""
    batches = _OaBatches("failed", None, None)
    batches.errors = types.SimpleNamespace(
        data=[types.SimpleNamespace(code="invalid_json", message="line 3")]
    )
    oa = types.SimpleNamespace(files=_OaFiles({}), batches=batches)
    r = _oa_gateway(tmp_path, oa).batch_results("batch_1", "oa", task="hard_transcript")
    assert r == {} and "invalid_json" in capsys.readouterr().out
