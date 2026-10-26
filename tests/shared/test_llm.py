"""LLM 게이트웨이 (shared/llm.py, 앱 설계 §7) — 실제 API 대신 가짜 SDK 클라이언트를 넣어 동작을 본다.
응답·오류는 SDK의 실제 타입(Message·BetaMessage·OverloadedError 등)으로 만든다 — 단순화한 객체는 일반 경로에
usage.iterations가 없다는 것, 529가 InternalServerError가 아니라는 것 같은 차이를 가린다."""
import importlib
import json
from datetime import datetime

import anthropic
import pytest
from anthropic.types import Message
from anthropic.types.beta import BetaMessage

from shared import llm

http = importlib.import_module(type(anthropic.DefaultHttpxClient()).__mro__[1].__module__.split(".")[0])  # SDK가 쓰는 HTTP 라이브러리(이름에 기대지 않게)
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
    return {"input_tokens": input_tokens, "output_tokens": output_tokens, "cache_read_input_tokens": cache_read}


def _message(text="안녕하세요", model="model-b", stop="end_turn", **usage):
    """일반 경로(messages.create) 응답 — usage에 iterations 필드가 없다"""
    return Message.model_validate({"id": "msg_1", "type": "message", "role": "assistant", "model": model,
                                   "content": [{"type": "text", "text": text}], "stop_reason": stop,
                                   "stop_sequence": None, "usage": _usage(**usage)})


def _beta(text="안녕하세요", model="model-a", stop="end_turn", iterations=None, **usage):
    """서버 측 폴백 경로(beta.messages.create) 응답"""
    u = _usage(**usage) | ({"iterations": iterations} if iterations is not None else {})
    return BetaMessage.model_validate({"id": "msg_1", "type": "message", "role": "assistant", "model": model,
                                       "content": [{"type": "text", "text": text}], "stop_reason": stop,
                                       "stop_sequence": None, "usage": u})


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
    return llm.Gateway(CONFIG, llm.JsonlLog(tmp_path / "calls.jsonl"), scope=scope, client=client, clock=lambda: NOW)


def _log(tmp_path):
    return [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text(encoding="utf-8").splitlines()]


def test_alias_calls_first_model_with_server_fallback_and_logs_one_row(tmp_path):
    client = StubClient(_beta())
    result = _gateway(tmp_path, client).complete("gen", task="gen_transcript", prompt_version="v1", messages=MSGS,
                                                 ref=("call", 3))
    assert result.ok and result.text == "안녕하세요" and result.model == "model-a" and not result.fallback_used
    assert result.cost_usd == pytest.approx(1000 * 2.0 / 1e6 + 200 * 10.0 / 1e6)
    path, kwargs = client.calls[0]
    assert path == "beta" and kwargs["fallbacks"] == "default" and kwargs["betas"] == [llm.SERVER_FALLBACK_BETA]
    [row] = _log(tmp_path)
    assert row["task"] == "gen_transcript" and row["alias"] == "gen" and row["model"] == "model-a"
    assert row["prompt_version"] == "v1" and row["ref_type"] == "call" and row["ref_id"] == 3 and row["ok"]
    assert row["scope"] == "generator" and row["at"] == NOW.isoformat(timespec="seconds")


def test_plain_model_uses_plain_path_without_iterations(tmp_path):
    """서버 측 폴백을 안 켠 모델 — 일반 경로 응답(usage.iterations 없음)도 성공으로 기록"""
    client = StubClient(_message())
    result = _gateway(tmp_path, client).complete("solo", task="t", messages=MSGS)
    assert result.ok and not result.fallback_used and result.error is None
    path, kwargs = client.calls[0]
    assert path == "plain" and "fallbacks" not in kwargs and "betas" not in kwargs
    assert len(_log(tmp_path)) == 1


@pytest.mark.parametrize("error", [
    _status_error(anthropic.OverloadedError, 529), _status_error(anthropic.InternalServerError, 500),
    _status_error(anthropic.RateLimitError, 429),
    anthropic.APIConnectionError(request=http.Request("POST", "https://api.anthropic.com/v1/messages")),
], ids=["529", "500", "429", "connection"])
def test_transient_error_falls_back_and_keeps_cause(tmp_path, error):
    client = StubClient(error, _message(model="model-b"))
    result = _gateway(tmp_path, client).complete("gen", task="t", messages=MSGS)
    assert result.ok and result.model == "model-b" and result.fallback_used
    assert result.error and result.error.startswith("model-a: ")  # 폴백 원인이 성공 결과에도 남는다
    assert [kw["model"] for _, kw in client.calls] == ["model-a", "model-b"]
    assert _log(tmp_path)[0]["fallback_used"] and _log(tmp_path)[0]["error"] == result.error


def test_bad_request_does_not_fall_back(tmp_path):
    client = StubClient(_status_error(anthropic.BadRequestError, 400))
    result = _gateway(tmp_path, client).complete("gen", task="t", messages=MSGS)
    assert not result.ok and len(client.calls) == 1 and "400" in result.error  # 요청이 틀렸으면 다른 모델도 같다


def test_refusal_falls_back_and_all_fail_is_not_ok(tmp_path):
    client = StubClient(_beta(stop="refusal", text=""), _message(stop="refusal", text=""))
    result = _gateway(tmp_path, client).complete("gen", task="t", messages=MSGS)
    assert not result.ok and "refusal" in result.error
    # 거절된 시도의 토큰도 비용에 들어간다
    assert result.cost_usd == pytest.approx(1000 * 2.0 / 1e6 + 200 * 10.0 / 1e6 + 1000 * 1.0 / 1e6 + 200 * 5.0 / 1e6)
    assert not _log(tmp_path)[0]["ok"]


def test_truncated_response_is_failure_without_fallback(tmp_path):
    client = StubClient(_beta(stop="max_tokens", text="잘린"))
    result = _gateway(tmp_path, client).complete("gen", task="t", messages=MSGS)
    assert not result.ok and "max_tokens" in result.error and len(client.calls) == 1
    assert result.cost_usd > 0 and _log(tmp_path)[0]["cost_usd"] > 0


def test_invalid_json_with_schema_is_logged_failure(tmp_path):
    schema = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"], "additionalProperties": False}
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
        {"type": "message", "model": "model-a", "input_tokens": 1000, "output_tokens": 300, **NO_CACHE},
        {"type": "fallback_message", "model": "model-b", "input_tokens": 100, "output_tokens": 50, **NO_CACHE},
    ]
    client = StubClient(_beta(model="model-b", iterations=iterations, input_tokens=100, output_tokens=50))
    result = _gateway(tmp_path, client).complete("gen", task="t", messages=MSGS)
    assert result.ok and result.fallback_used and result.model == "model-b"
    assert (result.input_tokens, result.output_tokens) == (1100, 350)
    assert result.cost_usd == pytest.approx((1000 * 2.0 + 300 * 10.0 + 100 * 1.0 + 50 * 5.0) / 1e6)


def test_cache_read_tokens_priced_separately(tmp_path):
    client = StubClient(_message(input_tokens=100, cache_read=1000))
    result = _gateway(tmp_path, client).complete("solo", task="t", messages=MSGS)
    assert result.cost_usd == pytest.approx(100 * 1.0 / 1e6 + 1000 * 0.1 / 1e6 + 200 * 5.0 / 1e6)  # cache_read 없으면 input × 0.1


def test_schema_is_transformed_and_json_parsed(tmp_path):
    """구조화 출력이 지원하지 않는 제약(minimum)은 SDK transform_schema로 설명으로 옮겨 보낸다"""
    schema = {"type": "object", "properties": {"n": {"type": "integer", "minimum": 1}}, "required": ["n"]}
    client = StubClient(_beta(text='{"n": 3}'))
    result = _gateway(tmp_path, client).complete("gen", task="t", messages=MSGS, schema=schema, effort="medium")
    assert result.data == {"n": 3}
    sent = client.calls[0][1]["output_config"]
    assert sent["effort"] == "medium" and sent["format"]["type"] == "json_schema"
    assert sent["format"]["schema"] == anthropic.transform_schema(schema)
    assert "minimum" not in sent["format"]["schema"]["properties"]["n"]


def test_budget_exceeded_stops_before_call(tmp_path):
    log = llm.JsonlLog(tmp_path / "calls.jsonl")
    log.write({"at": NOW.isoformat(timespec="seconds"), "scope": "generator", "cost_usd": 0.6})
    log.write({"at": NOW.isoformat(timespec="seconds"), "scope": "generator", "cost_usd": 0.5})
    log.write({"at": "2026-10-26T10:00:00", "scope": "runtime", "cost_usd": 9.0})  # 다른 날·다른 범위는 세지 않음
    assert log.spent(NOW.date(), "generator") == pytest.approx(1.1)
    assert llm.JsonlLog(tmp_path / "calls.jsonl").spent(NOW.date(), "generator") == pytest.approx(1.1)  # 파일에서 다시 읽음
    client = StubClient(_beta())
    with pytest.raises(llm.BudgetExceeded):
        llm.Gateway(CONFIG, log, scope="generator", client=client, clock=lambda: NOW).complete("gen", task="t", messages=MSGS)
    assert client.calls == []
    # 런타임 범위는 그날 쓴 돈이 없어 통과
    assert llm.Gateway(CONFIG, log, scope="runtime", client=client, clock=lambda: NOW).complete(
        "gen", task="t", messages=MSGS).ok


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
    checks = {"object": lambda v: isinstance(v, dict) and set(schema.get("required", [])) <= set(v)
              and all(_conforms(v[k], s, root) for k, s in schema.get("properties", {}).items() if k in v),
              "array": lambda v: isinstance(v, list) and all(_conforms(x, schema.get("items", {}), root) for x in v),
              "string": lambda v: isinstance(v, str), "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
              "number": lambda v: isinstance(v, (int, float)), "boolean": lambda v: isinstance(v, bool),
              "null": lambda v: v is None}
    return any(checks[k](value) for k in kinds)


SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["scenario", "turns", "urgent", "kind", "speaker", "note", "version", "nested"],
    "properties": {
        "scenario": {"type": "string"}, "turns": {"type": "array", "items": {"type": "integer"}, "minItems": 2},
        "urgent": {"type": "boolean"}, "kind": {"type": "string", "enum": ["a", "b", "c"]},
        "speaker": {"anyOf": [{"const": "환자"}, {"const": "직원"}]}, "note": {"type": ["string", "null"]},
        "version": {"const": 1}, "nested": {"$ref": "#/$defs/line"},
    },
    "$defs": {"line": {"type": "object", "additionalProperties": False, "required": ["who"],
                       "properties": {"who": {"type": "string"}}}},
}


def test_fake_gateway_is_deterministic_and_follows_schema(tmp_path):
    fake = llm.FakeGateway(llm.JsonlLog(tmp_path / "calls.jsonl"), scope="generator", clock=lambda: NOW)
    one = fake.complete("gen", task="t", messages=MSGS, schema=SCHEMA)
    assert one == fake.complete("gen", task="t", messages=MSGS, schema=SCHEMA)
    assert one.ok and one.model == "fake" and one.cost_usd == 0 and json.loads(one.text) == one.data
    assert _conforms(one.data, SCHEMA, SCHEMA) and len(one.data["turns"]) == 2
    other = fake.complete("gen", task="t", messages=[{"role": "user", "content": "다른 입력"}], schema=SCHEMA)
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
    return MessageBatch.model_validate({
        "id": "batch_1", "type": "message_batch", "processing_status": status, "created_at": "2026-10-27T10:00:00Z",
        "expires_at": "2026-10-28T10:00:00Z", "archived_at": None, "cancel_initiated_at": None, "ended_at": None,
        "results_url": None,
        "request_counts": {"processing": 0, "succeeded": 2, "errored": 0, "canceled": 0, "expired": 0}})


def _entry(custom_id, result):
    return MessageBatchIndividualResponse.model_validate({"custom_id": custom_id, "result": result})


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
    batch_id = gw.submit_batch("gen", [{"custom_id": "k1", "system": "지시", "messages": MSGS, "schema": TEXT_SCHEMA}],
                               task="toy", prompt_version="toy/v1")
    assert batch_id == "batch_1" and gw.batch_status(batch_id) == "ended"
    (req,) = client.messages.batches.created
    assert req["custom_id"] == "k1"
    params = req["params"]
    assert params["model"] == "model-a" and params["system"] == "지시" and params["messages"] == MSGS
    assert "fallbacks" not in params and params["output_config"]["format"]["type"] == "json_schema"


def test_batch_results_priced_at_batch_share_and_logged(tmp_path):
    ok = _message(text='{"text": "사과"}', model="model-a", input_tokens=1000, output_tokens=200).model_dump()
    refused = _message(text="", model="model-a", stop="refusal", input_tokens=1000, output_tokens=0).model_dump()
    bad_json = _message(text="사과", model="model-a").model_dump()
    client = BatchClient([
        _entry("k1", {"type": "succeeded", "message": ok}),
        _entry("k2", {"type": "succeeded", "message": refused}),
        _entry("k3", {"type": "succeeded", "message": bad_json}),
        _entry("k4", {"type": "expired"}),
        _entry("k5", {"type": "errored", "error": {"type": "error", "error": {"type": "api_error", "message": "x"}}}),
    ])
    gw = _gateway(tmp_path, client)
    out = gw.batch_results("batch_1", "gen", task="toy", prompt_version="toy/v1", schema=TEXT_SCHEMA)
    assert out["k1"].ok and out["k1"].data == {"text": "사과"}
    assert out["k1"].cost_usd == pytest.approx((1000 * 2.0 + 200 * 10.0) / 1e6 * llm.BATCH_PRICE_SHARE)
    assert not out["k2"].ok and "refusal" in out["k2"].error
    assert not out["k3"].ok and "JSON" in out["k3"].error
    assert out["k4"] is None and out["k5"] is None  # 처리 안 됨 — 시도로 세지 않는다
    log = _log(tmp_path)
    assert [r["ref_id"] for r in log] == ["k1", "k2", "k3"] and all(r["task"] == "toy" for r in log)


def test_batch_submit_checks_budget(tmp_path):
    gw = _gateway(tmp_path, BatchClient())
    gw.log.write({"at": NOW.isoformat(), "scope": "generator", "cost_usd": 1.0})
    with pytest.raises(llm.BudgetExceeded):
        gw.submit_batch("gen", [], task="toy")
