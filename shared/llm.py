"""LLM 게이트웨이 — 생성기·앱의 모든 LLM 호출이 지나는 모듈 하나 (앱 설계 §7). Anthropic 공식 SDK를 직접 쓴다.
설정은 config/llm.yaml: 별칭 → 모델 순서, 모델별 단가, 일일 비용 상한.
- 폴백: 일시 오류(연결·408·409·429·5xx — SDK 재시도를 다 쓴 뒤)나 거절이면 다음 모델로. 그 밖의 오류(400 등),
  잘린 응답(stop_reason이 end_turn 아님), JSON 파싱 실패는 다른 모델로 가도 같아 거기서 실패
- 호출 1건 = 기록 1행(시도한 모델 전부의 토큰·비용 합, 예외가 나도 남김). 생성기는 JsonlLog, 앱은 착수 때 ax_llm_calls
- 그날 쓴 돈이 범위(generator/runtime)의 상한 이상이면 호출하지 않고 BudgetExceeded. 확인과 호출 사이 시차로
  동시 호출 수 × 1건 비용만큼 넘을 수 있다(허용)
- FakeGateway는 같은 인터페이스로 입력 해시에서 결정적 응답 — 테스트·CI·fixture용(과금 없음)
- Batch(생성기 API 채우기): 별칭의 첫 모델만, 서버 측 폴백 없음(Batch에서 거부), 비용은 단가 × BATCH_PRICE_SHARE.
  결과 1건 = 기록 1행. 처리 안 된 건(오류·취소·만료)은 기록하지 않고 None
의존성: anthropic은 지금 generator 그룹 — 앱 착수 때 backend 그룹에도 추가
"""

import hashlib
import json
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import anthropic
import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "llm.yaml"
SERVER_FALLBACK_BETA = "server-side-fallback-2026-07-01"
DEFAULT_MAX_TOKENS = 16000
CACHE_READ_SHARE = 0.1  # 단가에 cache_read가 없으면 input 단가의 이 배수
CACHE_WRITE_SHARE = 1.25  # 캐시 쓰기 = input 단가의 이 배수
TRANSIENT_STATUS = (408, 409, 429)  # + 5xx — SDK 재시도 기준과 같음
ATTEMPT_ITERATIONS = ("message", "fallback_message")  # usage.iterations 중 모델 시도
BATCH_PRICE_SHARE = 0.5  # Batch 단가 = 일반 단가의 이 배수


def load_config(path: Path = CONFIG_PATH) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


class BudgetExceeded(RuntimeError):
    """그날 쓴 돈이 일일 상한 이상 — 호출하지 않았다"""


@dataclass(frozen=True)
class LLMResult:
    ok: bool
    text: str
    data: object  # schema를 줬으면 파싱한 JSON, 아니면 None
    model: str  # 실제로 응답한 모델 (실패면 마지막으로 시도한 모델)
    fallback_used: bool  # 별칭의 첫 모델이 아닌 모델(서버 측 폴백 포함)이 응답했거나 시도함
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: int
    error: str | None = None  # 실패 이유, 성공이면 앞 시도의 폴백 원인


class JsonlLog:
    """호출 기록 — 한 줄에 한 호출. 일일 상한용 (날짜, 범위)별 합은 처음 한 번 파일에서 읽고 이후 write로 더한다"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._spent = None

    def _load(self) -> dict:
        totals = defaultdict(float)
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    r = json.loads(line)
                    totals[(r["at"][:10], r.get("scope"))] += r.get("cost_usd", 0.0)
        return totals

    def write(self, record: dict) -> None:
        with self._lock:
            if self._spent is None:
                self._spent = self._load()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._spent[(record["at"][:10], record.get("scope"))] += record.get("cost_usd", 0.0)

    def spent(self, day: date, scope: str) -> float:
        with self._lock:
            if self._spent is None:
                self._spent = self._load()
            return self._spent[(day.isoformat(), scope)]


def _output_config(schema: dict | None, effort: str | None) -> dict | None:
    out = {}
    if schema is not None:
        # 구조화 출력이 지원하지 않는 제약(minimum 등)은 설명으로 옮기고 additionalProperties: false를 붙인다
        out["format"] = {"type": "json_schema", "schema": anthropic.transform_schema(schema)}
    if effort is not None:
        out["effort"] = effort
    return out or None


class _Base:
    def __init__(self, log, scope: str, clock=datetime.now):
        self.log, self.scope, self.clock = log, scope, clock

    def _record(self, alias, task, prompt_version, ref, result: LLMResult) -> None:
        ref_type, ref_id = ref if ref else (None, None)
        self.log.write(
            {
                "at": self.clock().isoformat(timespec="seconds"),
                "scope": self.scope,
                "task": task,
                "alias": alias,
                "model": result.model,
                "prompt_version": prompt_version,
                "fallback_used": result.fallback_used,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "cost_usd": round(result.cost_usd, 6),
                "latency_ms": result.latency_ms,
                "ok": result.ok,
                "error": result.error,
                "ref_type": ref_type,
                "ref_id": ref_id,
            }
        )


class Gateway(_Base):
    def __init__(self, config: dict, log, scope: str, client=None, clock=datetime.now):
        super().__init__(log, scope, clock)
        self.config = config
        self.client = client if client is not None else anthropic.Anthropic()

    def _price(self, model: str, requested: str) -> dict:
        return (
            self.config["models"].get(model) or self.config["models"][requested]
        )  # 설정에 없는 모델은 요청 모델 단가

    def _usage(self, requested: str, usage) -> tuple[int, int, float, bool]:
        """(입력 토큰, 출력 토큰, 비용, 서버 측 폴백 여부). 서버 측 폴백이면 top-level usage는 마지막 시도만이라
        usage.iterations의 시도별 값을 그 모델 단가로 합산 — 거절 크레딧 재가격은 반영하지 않아 비용은 보수적(크게)"""
        attempts = [
            it
            for it in (getattr(usage, "iterations", None) or [])
            if it.type in ATTEMPT_ITERATIONS
        ]
        parts = [(it.model, it) for it in attempts] or [(requested, usage)]
        tokens_in = tokens_out = 0
        cost = 0.0
        for model, u in parts:
            price = self._price(model, requested)
            cache_read = price.get("cache_read", price["input"] * CACHE_READ_SHARE)
            tokens_in += u.input_tokens
            tokens_out += u.output_tokens
            cost += (
                u.input_tokens * price["input"]
                + u.output_tokens * price["output"]
                + (u.cache_read_input_tokens or 0) * cache_read
                + (u.cache_creation_input_tokens or 0) * price["input"] * CACHE_WRITE_SHARE
            ) / 1e6
        return tokens_in, tokens_out, cost, any(it.type == "fallback_message" for it in attempts)

    def _call(self, model: str, system, messages, max_tokens, output_config):
        params = {"model": model, "max_tokens": max_tokens, "messages": messages}
        if system is not None:
            params["system"] = system
        if output_config is not None:
            params["output_config"] = output_config
        if self.config["models"][model].get("server_fallback"):
            return self.client.beta.messages.create(
                betas=[SERVER_FALLBACK_BETA], fallbacks="default", **params
            )
        return self.client.messages.create(**params)

    def complete(
        self,
        alias: str,
        *,
        task: str,
        messages: list,
        system=None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        schema: dict | None = None,
        effort: str | None = None,
        prompt_version: str | None = None,
        ref: tuple | None = None,
    ) -> LLMResult:
        chain = self.config["aliases"][alias]
        self._check_budget()
        output_config = _output_config(schema, effort)
        start = time.monotonic()
        state = {
            "in": 0,
            "out": 0,
            "cost": 0.0,
            "errors": [],
            "model": chain[0],
            "server_fallback": False,
        }

        def finish(ok: bool, text: str = "", data=None) -> LLMResult:
            errors = "; ".join(state["errors"]) or None
            return LLMResult(
                ok,
                text,
                data,
                state["model"],
                state["model"] != chain[0] or state["server_fallback"],
                state["in"],
                state["out"],
                state["cost"],
                int((time.monotonic() - start) * 1000),
                errors,
            )

        result = None
        try:
            for model in chain:
                state["model"] = model
                try:
                    response = self._call(model, system, messages, max_tokens, output_config)
                except anthropic.APIConnectionError as e:  # 타임아웃 포함
                    state["errors"].append(f"{model}: {type(e).__name__}")
                    continue
                except anthropic.APIStatusError as e:
                    state["errors"].append(f"{model}: {type(e).__name__} {e.status_code}")
                    if e.status_code in TRANSIENT_STATUS or e.status_code >= 500:
                        continue
                    break
                tokens_in, tokens_out, cost, server_fallback = self._usage(model, response.usage)
                state.update(
                    {
                        "in": state["in"] + tokens_in,
                        "out": state["out"] + tokens_out,
                        "cost": state["cost"] + cost,
                        "model": response.model,
                        "server_fallback": state["server_fallback"] or server_fallback,
                    }
                )
                if response.stop_reason == "refusal":
                    state["errors"].append(f"{model}: refusal")
                    continue
                if response.stop_reason != "end_turn":
                    state["errors"].append(f"{model}: stop_reason {response.stop_reason}")
                    break
                text = "".join(b.text for b in response.content if b.type == "text")
                if schema is None:
                    result = finish(True, text)
                    break
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    state["errors"].append(f"{model}: JSON 파싱 실패")
                    break
                result = finish(True, text, data)
                break
            if result is None:
                result = finish(False)
        except (
            Exception
        ) as e:  # 예상 밖 오류도 기록은 남기고 다시 던진다 (이미 낸 비용이 상한에서 빠지지 않게)
            state["errors"].append(f"{state['model']}: {type(e).__name__}")
            self._record(alias, task, prompt_version, ref, finish(False))
            raise
        self._record(alias, task, prompt_version, ref, result)
        return result

    def _check_budget(self) -> None:
        limit = self.config["daily_usd_limit"][self.scope]
        if self.log.spent(self.clock().date(), self.scope) >= limit:
            raise BudgetExceeded(f"{self.scope} 일일 상한 ${limit} 도달")

    def submit_batch(
        self,
        alias: str,
        requests: list[dict],
        *,
        task: str,
        prompt_version: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> str:
        """requests: [{custom_id, system, messages, schema}] → 배치 id. 하루 상한은 보지 않는다 — 건수가 정해진 배치는 사람이
        보고 제출한다(기능 정의 §7). 비용은 결과에서 기록"""
        model = self.config["aliases"][alias][0]
        batch = []
        for r in requests:
            params = {"model": model, "max_tokens": max_tokens, "messages": r["messages"]}
            if r.get("system") is not None:
                params["system"] = r["system"]
            output_config = _output_config(r.get("schema"), None)
            if output_config is not None:
                params["output_config"] = output_config
            batch.append({"custom_id": r["custom_id"], "params": params})
        return self.client.messages.batches.create(requests=batch).id

    def batch_status(self, batch_id: str) -> str:
        return self.client.messages.batches.retrieve(batch_id).processing_status

    def batch_results(
        self,
        batch_id: str,
        alias: str,
        *,
        task: str,
        prompt_version: str | None = None,
        schema: dict | None = None,
    ) -> dict:
        """{custom_id: LLMResult | None}. 거절·잘림·JSON 파싱 실패는 ok=False, 처리 안 된 건은 None"""
        model = self.config["aliases"][alias][0]
        out = {}
        for entry in self.client.messages.batches.results(batch_id):
            if entry.result.type != "succeeded":
                out[entry.custom_id] = None
                continue
            msg = entry.result.message
            tokens_in, tokens_out, cost, _ = self._usage(model, msg.usage)

            def fail(why, msg=msg, tokens_in=tokens_in, tokens_out=tokens_out, cost=cost):
                return LLMResult(
                    False,
                    "",
                    None,
                    msg.model,
                    False,
                    tokens_in,
                    tokens_out,
                    cost * BATCH_PRICE_SHARE,
                    0,
                    f"{msg.model}: {why}",
                )

            text = "".join(b.text for b in msg.content if b.type == "text")
            if msg.stop_reason != "end_turn":
                result = fail(
                    "refusal" if msg.stop_reason == "refusal" else f"stop_reason {msg.stop_reason}"
                )
            elif schema is None:
                result = LLMResult(
                    True,
                    text,
                    None,
                    msg.model,
                    False,
                    tokens_in,
                    tokens_out,
                    cost * BATCH_PRICE_SHARE,
                    0,
                )
            else:
                try:
                    result = LLMResult(
                        True,
                        text,
                        json.loads(text),
                        msg.model,
                        False,
                        tokens_in,
                        tokens_out,
                        cost * BATCH_PRICE_SHARE,
                        0,
                    )
                except json.JSONDecodeError:
                    result = fail("JSON 파싱 실패")
            self._record(alias, task, prompt_version, ("batch", entry.custom_id), result)
            out[entry.custom_id] = result
        return out


def _fake_value(schema: dict, seed: str, key: str, root: dict):
    """JSON 스키마에 맞는 결정적 값 — object·array·string·integer·number·boolean·null, enum·const, 타입 목록,
    $ref(루트 $defs), anyOf·oneOf(해시로 하나), allOf(첫 항목)"""
    h = int(hashlib.sha256(f"{seed}:{key}".encode()).hexdigest(), 16)
    if "$ref" in schema:
        node = root
        for part in schema["$ref"].lstrip("#/").split("/"):
            node = node[part]
        return _fake_value(node, seed, key, root)
    for combo in ("anyOf", "oneOf"):
        if combo in schema:
            return _fake_value(schema[combo][h % len(schema[combo])], seed, key, root)
    if "allOf" in schema:
        return _fake_value(schema["allOf"][0], seed, key, root)
    if "const" in schema:
        return schema["const"]
    if "enum" in schema:
        return schema["enum"][h % len(schema["enum"])]
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), "null")
    if kind == "object":
        return {
            k: _fake_value(v, seed, f"{key}.{k}", root)
            for k, v in schema.get("properties", {}).items()
        }
    if kind == "array":
        return [
            _fake_value(schema.get("items", {}), seed, f"{key}[{i}]", root)
            for i in range(max(1, schema.get("minItems", 1)))
        ]
    if kind == "integer":
        return schema.get("minimum", 0) + h % 10
    if kind == "number":
        return float(schema.get("minimum", 0) + h % 10)
    if kind == "boolean":
        return bool(h % 2)
    if kind == "null":
        return None
    return f"가짜 {key.split('.')[-1] or '응답'} {h % 100000:05d}"


class FakeGateway(_Base):
    """Gateway와 같은 complete() — 입력(별칭·system·messages·schema) 해시에서 결정적 응답, 과금 없음"""

    def complete(
        self,
        alias: str,
        *,
        task: str,
        messages: list,
        system=None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        schema: dict | None = None,
        effort: str | None = None,
        prompt_version: str | None = None,
        ref: tuple | None = None,
    ) -> LLMResult:
        seed = hashlib.sha256(
            json.dumps(
                [alias, system, messages, schema], ensure_ascii=False, sort_keys=True, default=str
            ).encode()
        ).hexdigest()
        data = _fake_value(schema, seed, "", schema) if schema is not None else None
        text = (
            json.dumps(data, ensure_ascii=False)
            if schema is not None
            else f"가짜 응답 {seed[:12]}"
        )
        result = LLMResult(True, text, data, "fake", False, 0, 0, 0.0, 0)
        self._record(alias, task, prompt_version, ref, result)
        return result
