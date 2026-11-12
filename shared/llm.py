"""LLM 게이트웨이 — 생성기·앱의 모든 LLM 호출이 지나는 모듈 하나 (앱 설계 §7). 프로바이더마다 공식 SDK를 직접 쓴다 —
Anthropic, OpenAI(Chat Completions — 어려운 통화 녹취, 어려운 통화 설계 §3.5), Upstage(Solar, OpenAI 호환 API라 openai SDK), 로컬 MLX(파인튜닝 판정기 — Qwen 판정기 설계 "게이트웨이 로컬 갈래").
모델의 프로바이더는 설정의 provider(없으면 anthropic).
설정은 config/llm.yaml: 별칭 → 모델 순서, 모델별 단가, 일일 비용 상한.
- 폴백: 일시 오류(연결·408·409·429·5xx — SDK 재시도를 다 쓴 뒤)나 거절이면 다음 모델로. 그 밖의 오류(400 등),
  잘린 응답(stop_reason이 end_turn 아님, Upstage는 finish_reason이 stop 아님), JSON 파싱 실패는 다른 모델로 가도
  같아 거기서 실패
- 호출 1건 = 기록 1행(시도한 모델 전부의 토큰·비용 합, 예외가 나도 남김). 생성기는 JsonlLog, 앱은 ax_llm_calls(backend/harness/store.py SqliteLlmLog)
- 그날 쓴 돈이 범위(generator/runtime)의 상한 이상이면 호출하지 않고 BudgetExceeded. 확인과 호출 사이 시차로
  동시 호출 수 × 1건 비용만큼 넘을 수 있다(허용)
- FakeGateway는 같은 인터페이스로 입력 해시에서 결정적 응답 — 테스트·CI·fixture용(과금 없음)
- 결과의 log_id = 그 호출의 기록 행 id(SqliteLlmLog면 ax_llm_calls.id, JsonlLog면 None)
- Batch(생성기 API 채우기·분류 일괄): Anthropic·OpenAI(Chat Completions 배치 — 상태·결과는 별칭의 프로바이더로), 별칭의 첫 모델만, 서버 측 폴백 없음(Batch에서 거부), 비용은 단가 × BATCH_PRICE_SHARE.
  결과 1건 = 기록 1행. 처리 안 된 건(오류·취소·만료)은 기록하지 않고 None
- 로컬 MLX: 모델·어댑터는 처음 부를 때 한 번 올려 프로세스 안에서 다시 쓰고, 적재·생성은 잠금 하나로 한 번에 하나씩.
  출력 상한 LOCAL_MAX_TOKENS(요청 max_tokens는 안 씀), 비용 0, 구조화 출력이 없어 첫 JSON 객체를 data로. 모델·어댑터가
  없으면 예외 대신 실패 결과. 응답 모델은 "<설정 이름>@<어댑터 지문>"
의존성: anthropic은 generator·backend 그룹, openai는 backend 그룹(OpenAI·Upstage 모델을 처음 부를 때 import),
mlx-lm은 finetune 그룹(로컬 모델을 처음 부를 때 import — uv run --group finetune)
"""

import hashlib
import json
import os
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, replace
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
UPSTAGE_BASE_URL = "https://api.upstage.ai/v1"
OPENAI_BATCH_ENDPOINT = "/v1/chat/completions"
OPENAI_BATCH_DONE = ("completed", "failed", "expired", "cancelled")
LOCAL_MAX_TOKENS = 256  # 로컬 판정기 출력 상한 — 정답 JSON은 100토큰 안팎(evaluation/finetune/qwen/infer.py와 같음)
_LOCAL_MODELS: dict = {}  # (설정 이름, base, adapter) → (model, tokenizer, 응답 모델 이름) — 게이트웨이는 요청마다 새로 만들어진다
_LOCAL_LOCK = threading.Lock()  # MLX 적재·생성은 한 번에 하나(API 스레드가 동시에 부를 수 있다)


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
    stop_reason: str | None = None
    tool_calls: tuple = ()  # 도구 호출 {id, name, input}
    content: list | None = None  # 다음 턴에 그대로 넘길 assistant 블록(글·도구 호출)
    log_id: int | None = None  # 기록 행 id (기록 저장소가 돌려준 값)


@dataclass(frozen=True)
class LocalOutput:
    text: str
    input_tokens: int
    output_tokens: int
    model: str  # 응답 모델 이름 — "<설정 이름>@<어댑터 지문>"


def adapter_fingerprint(adapter_dir: Path) -> str:
    """어댑터 가중치 sha256 앞 16자 — evaluation/finetune/qwen/infer.py adapter_fingerprint와 같은 값"""
    return hashlib.sha256((adapter_dir / "adapters.safetensors").read_bytes()).hexdigest()[:16]


def first_json(text: str):
    """출력에서 읽히는 첫 JSON 객체 — 없으면 None"""
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            d, _ = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            continue
        if isinstance(d, dict):
            return d
    return None


def _local_model(name: str, cfg: dict) -> tuple:
    """(모델, 토크나이저, 응답 이름) — 처음 한 번 적재해 둔다. _LOCAL_LOCK 안에서 부른다"""
    import mlx_lm

    adapter = ROOT / cfg["adapter"] if cfg.get("adapter") else None
    key = (name, cfg["base"], str(adapter))
    if key not in _LOCAL_MODELS:
        if adapter is not None and not (adapter / "adapters.safetensors").exists():
            raise FileNotFoundError(f"어댑터 없음 {adapter}")
        model, tokenizer = mlx_lm.load(cfg["base"], adapter_path=str(adapter) if adapter else None)
        label = f"{name}@{adapter_fingerprint(adapter)}" if adapter else name
        _LOCAL_MODELS[key] = (model, tokenizer, label)
    return _LOCAL_MODELS[key]


def _chat_prompt(tokenizer, messages: list[dict]) -> str:
    return tokenizer.apply_chat_template(
        messages, add_generation_prompt=True, enable_thinking=False, tokenize=False
    )


def _mlx_generate(name: str, cfg: dict, messages: list[dict]) -> LocalOutput:
    """학습·실험 추론(infer.py)과 같은 채팅 템플릿(생각 끔)·탐욕 디코딩. 오류는 부른 쪽(_local)이 실패 결과로"""
    import mlx_lm

    with _LOCAL_LOCK:
        model, tokenizer, label = _local_model(name, cfg)
        prompt = _chat_prompt(tokenizer, messages)
        text = mlx_lm.generate(model, tokenizer, prompt=prompt, max_tokens=LOCAL_MAX_TOKENS)
    return LocalOutput(text, len(tokenizer.encode(prompt)), len(tokenizer.encode(text)), label)


def _logprob(model, tokenizer, context: str, continuation: str) -> float:
    """context 뒤에 continuation이 올 로그 확률(토큰 합)"""
    import mlx.core as mx

    ctx = tokenizer.encode(context)
    full = tokenizer.encode(context + continuation)
    start = min(len(ctx), len(full) - 1)  # 경계 토큰이 합쳐져도 이어 붙인 부분은 센다
    logits = model(mx.array([full]))[0]
    lp = logits - mx.logsumexp(logits, axis=-1, keepdims=True)
    return sum(lp[j - 1, full[j]].item() for j in range(start, len(full)))


def _mlx_constrain(name: str, cfg: dict, messages: list[dict], schema: dict) -> LocalOutput:
    """보기 강제(제약 디코딩) — 스키마의 칸 순서대로 JSON을 채우며 칸마다 허용 값(enum 보기·불은 0/1) 중 모델 확률이
    가장 높은 것을 고른다. 보기 밖 값이 나올 수 없다. 자유 생성이 스키마에 맞지 않을 때만 부른다(_local)"""
    with _LOCAL_LOCK:
        model, tokenizer, label = _local_model(name, cfg)
        prompt = _chat_prompt(tokenizer, messages)
        text = "{"
        for i, (key, prop) in enumerate(schema["properties"].items()):
            sep = "," if i else ""
            if "enum" in prop:
                text += f'{sep}"{key}":"'
                choices = [f'{v}"' for v in prop["enum"]]
            else:
                text += f'{sep}"{key}":'
                choices = ["0", "1"]
            text += max(choices, key=lambda c: _logprob(model, tokenizer, prompt + text, c))
        text += "}"
    return LocalOutput(text, len(tokenizer.encode(prompt)), 0, label)


def fits_schema(data, schema: dict) -> bool:
    """평평한 객체 스키마에 맞나 — 필수 칸, 남는 칸, enum 보기, 불(0/1도 인정), 문자열·정수 타입"""
    if not isinstance(data, dict):
        return False
    props = schema.get("properties", {})
    if not set(schema.get("required", [])) <= set(data):
        return False
    if schema.get("additionalProperties") is False and set(data) - set(props):
        return False
    for key, value in data.items():
        prop = props.get(key, {})
        if "enum" in prop and value not in prop["enum"]:
            return False
        kind = prop.get("type")
        if kind == "boolean" and not (type(value) in (bool, int) and value in (0, 1)):
            return False
        if kind == "string" and not isinstance(value, str):
            return False
        if kind == "integer" and type(value) is not int:
            return False
    return True


def _constrainable(schema: dict) -> bool:
    """칸이 모두 보기(enum)나 불이면 제약 디코딩으로 채울 수 있다"""
    props = schema.get("properties", {})
    return bool(props) and all("enum" in p or p.get("type") == "boolean" for p in props.values())


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


def _blocks(content) -> list[dict]:
    """응답의 글·도구 호출 블록을 JSON으로 저장할 수 있는 dict로"""
    out = []
    for b in content:
        if b.type == "text":
            out.append({"type": "text", "text": b.text})
        elif b.type == "tool_use":
            out.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
    return out


def _output_config(schema: dict | None, effort: str | None) -> dict | None:
    out = {}
    if schema is not None:
        # 구조화 출력이 지원하지 않는 제약(minimum 등)은 설명으로 옮기고 additionalProperties: false를 붙인다
        out["format"] = {"type": "json_schema", "schema": anthropic.transform_schema(schema)}
    if effort is not None:
        out["effort"] = effort
    return out or None


def _chat_params(provider: str, model: str, system, messages, max_tokens, schema, effort) -> dict:
    """Chat Completions 요청 본문 — OpenAI는 추론 모델이라 출력 상한이 max_completion_tokens"""
    head = [{"role": "system", "content": system}] if system is not None else []
    key = "max_completion_tokens" if provider == "openai" else "max_tokens"
    params = {"model": model, key: max_tokens, "messages": head + list(messages)}
    if schema is not None:
        params["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "labels", "schema": schema, "strict": True},
        }
    if effort is not None:
        params["reasoning_effort"] = effort
    return params


class _Base:
    def __init__(self, log, scope: str, clock=datetime.now):
        self.log, self.scope, self.clock = log, scope, clock

    def _record(self, alias, task, prompt_version, ref, result: LLMResult) -> LLMResult:
        """기록하고 log_id를 채운 결과를 돌려준다"""
        ref_type, ref_id = ref if ref else (None, None)
        log_id = self.log.write(
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
        return replace(result, log_id=log_id)


class Gateway(_Base):
    def __init__(
        self,
        config: dict,
        log,
        scope: str,
        client=None,
        *,
        upstage_client=None,
        openai_client=None,
        local_generate=None,
        local_constrain=None,
        clock=datetime.now,
    ):
        super().__init__(log, scope, clock)
        self.config = config
        self.client = client if client is not None else anthropic.Anthropic()
        self.upstage_client = upstage_client  # 없으면 Upstage 모델을 처음 부를 때 만든다
        self.openai_client = openai_client  # 없으면 OpenAI 모델을 처음 부를 때 만든다
        self.local_generate = local_generate or _mlx_generate  # 테스트는 가짜를 넣는다
        self.local_constrain = local_constrain or _mlx_constrain

    def _provider(self, model: str) -> str:
        return self.config["models"][model].get("provider", "anthropic")

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

    def _call(self, model: str, system, messages, max_tokens, output_config, tools=None):
        params = {"model": model, "max_tokens": max_tokens, "messages": messages}
        if system is not None:
            params["system"] = system
        if output_config is not None:
            params["output_config"] = output_config
        if tools:
            params["tools"] = tools
        if self.config["models"][model].get("server_fallback"):
            return self.client.beta.messages.create(
                betas=[SERVER_FALLBACK_BETA], fallbacks="default", **params
            )
        return self.client.messages.create(**params)

    def _openai(self):
        import openai

        if self.openai_client is None:
            self.openai_client = openai.OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        return self.openai_client

    def _upstage(self, model: str, system, messages, max_tokens, schema, effort, state) -> tuple:
        """Upstage·OpenAI 한 시도(같은 Chat Completions 모양) — 토큰·비용·오류는 state에 더하고
        ("next" | "stop" | "ok", (text, data))를 돌려준다. 폴백 규칙은 Anthropic과 같다(일시 오류면 next, 그 밖의 오류·
        잘림·JSON 파싱 실패면 stop). effort는 reasoning_effort로 보낸다(없으면 추론 꺼짐 — 추론 토큰도 max_tokens 안)"""
        import openai

        if self._provider(model) == "openai":
            client = self._openai()
        else:
            if self.upstage_client is None:
                self.upstage_client = openai.OpenAI(
                    api_key=os.environ["UPSTAGE_API_KEY"], base_url=UPSTAGE_BASE_URL
                )
            client = self.upstage_client
        params = _chat_params(
            self._provider(model), model, system, messages, max_tokens, schema, effort
        )
        try:
            response = client.chat.completions.create(**params)
        except openai.APIConnectionError as e:  # 타임아웃 포함
            state["errors"].append(f"{model}: {type(e).__name__}")
            return "next", None
        except openai.APIStatusError as e:
            state["errors"].append(f"{model}: {type(e).__name__} {e.status_code}")
            transient = e.status_code in TRANSIENT_STATUS or e.status_code >= 500
            return ("next" if transient else "stop"), None
        price = self._price(response.model, model)
        u = response.usage
        state.update(
            {
                "in": state["in"] + u.prompt_tokens,
                "out": state["out"] + u.completion_tokens,
                "cost": state["cost"]
                + (u.prompt_tokens * price["input"] + u.completion_tokens * price["output"]) / 1e6,
                "model": response.model,
            }
        )
        choice = response.choices[0]
        if choice.finish_reason != "stop":
            state["errors"].append(f"{model}: finish_reason {choice.finish_reason}")
            return "stop", None
        text = choice.message.content or ""
        if schema is None:
            return "ok", (text, None)
        try:
            return "ok", (text, json.loads(text))
        except json.JSONDecodeError:
            state["errors"].append(f"{model}: JSON 파싱 실패")
            return "stop", None

    def _local(self, model: str, system, messages, schema, state) -> tuple:
        """로컬 MLX 한 시도 — _upstage와 같은 ("stop" | "ok", (text, data)). 폴백 대상 오류는 없다"""
        head = [{"role": "system", "content": system}] if system is not None else []
        try:
            out = self.local_generate(model, self.config["models"][model], head + list(messages))
        except ImportError:
            state["errors"].append(f"{model}: mlx_lm 없음 — uv run --group finetune으로 실행")
            return "stop", None
        except FileNotFoundError as e:
            state["errors"].append(f"{model}: {e}")
            return "stop", None
        except (
            Exception
        ) as e:  # 적재·생성 중 예상 밖 오류(오프라인 hub·메모리 부족) — 앱·일괄 실행이 멈추지 않게
            state["errors"].append(f"{model}: 로컬 실행 실패 {type(e).__name__}: {e}")
            return "stop", None
        state.update(
            {
                "in": state["in"] + out.input_tokens,
                "out": state["out"] + out.output_tokens,
                "model": out.model,
            }
        )
        if schema is None:
            return "ok", (out.text, None)
        data = first_json(out.text)
        if not fits_schema(data, schema) and _constrainable(schema):
            # 보기 밖 값·형식 오류 — 같은 입력이면 같은 답이라 다시 생성하지 않고 보기를 강제한다
            try:
                out = self.local_constrain(
                    model, self.config["models"][model], head + list(messages), schema
                )
            except Exception as e:  # noqa: BLE001 — 강제도 실패하면 원래 결과로 판단
                state["errors"].append(f"{model}: 보기 강제 실패 {type(e).__name__}: {e}")
            else:
                state["in"] += out.input_tokens
                data = first_json(out.text)
        if data is None:
            state["errors"].append(f"{model}: JSON 파싱 실패")
            return "stop", None
        return "ok", (out.text, data)

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
        tools: list | None = None,
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
            "tried": chain[
                0
            ],  # 지금 시도하는 별칭 모델 — 응답 모델명은 날짜 붙은 id일 수 있어 따로 둔다
            "server_fallback": False,
        }

        def finish(ok: bool, text: str = "", data=None, response=None) -> LLMResult:
            errors = "; ".join(state["errors"]) or None
            return LLMResult(
                ok,
                text,
                data,
                state["model"],
                state["tried"] != chain[0] or state["server_fallback"],
                state["in"],
                state["out"],
                state["cost"],
                int((time.monotonic() - start) * 1000),
                errors,
                response.stop_reason if response else None,
                tuple(
                    {"id": b.id, "name": b.name, "input": b.input}
                    for b in (response.content if response else [])
                    if b.type == "tool_use"
                ),
                _blocks(response.content) if response else None,
            )

        result = None
        try:
            for model in chain:
                state["model"] = state["tried"] = model
                if self._provider(model) in ("upstage", "openai", "mlx"):
                    outcome, got = (
                        self._upstage(model, system, messages, max_tokens, schema, effort, state)
                        if self._provider(model) != "mlx"
                        else self._local(model, system, messages, schema, state)
                    )
                    if outcome == "next":
                        continue
                    if outcome == "ok":
                        result = finish(True, *got)
                    break
                try:
                    response = self._call(
                        model, system, messages, max_tokens, output_config, tools
                    )
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
                done = ("end_turn", "tool_use") if tools else ("end_turn",)
                if response.stop_reason not in done:
                    state["errors"].append(f"{model}: stop_reason {response.stop_reason}")
                    break
                text = "".join(b.text for b in response.content if b.type == "text")
                if schema is None or response.stop_reason == "tool_use":
                    result = finish(True, text, response=response)
                    break
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    state["errors"].append(f"{model}: JSON 파싱 실패")
                    break
                result = finish(True, text, data, response)
                break
            if result is None:
                result = finish(False)
        except (
            Exception
        ) as e:  # 예상 밖 오류도 기록은 남기고 다시 던진다 (이미 낸 비용이 상한에서 빠지지 않게)
            state["errors"].append(f"{state['model']}: {type(e).__name__}")
            self._record(alias, task, prompt_version, ref, finish(False))
            raise
        return self._record(alias, task, prompt_version, ref, result)

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
        """requests: [{custom_id, system, messages, schema, effort?, max_tokens?, tools?}] → 배치 id. 요청의 effort·max_tokens가
        있으면 그것으로(단건 호출과 같은 버전은 같은 설정 — 앱 설계 §7.2), max_tokens 인자는 없을 때의 기본값. 하루 상한은 보지 않는다 — 건수가 정해진 배치는 사람이
        보고 제출한다(기능 정의 §7). 비용은 결과에서 기록"""
        model = self.config["aliases"][alias][0]
        if self._provider(model) == "openai":
            return self._openai_submit(model, requests, max_tokens)
        if self._provider(model) != "anthropic":
            raise ValueError(f"{self._provider(model)}: batch 미지원")
        batch = []
        for r in requests:
            params = {
                "model": model,
                "max_tokens": r.get("max_tokens", max_tokens),
                "messages": r["messages"],
            }
            if r.get("system") is not None:
                params["system"] = r["system"]
            output_config = _output_config(r.get("schema"), r.get("effort"))
            if output_config is not None:
                params["output_config"] = output_config
            if r.get("tools"):  # 라운드 배치(에이전트) — 단건 complete와 같은 도구 목록
                params["tools"] = r["tools"]
            batch.append({"custom_id": r["custom_id"], "params": params})
        return self.client.messages.batches.create(requests=batch).id

    def _openai_submit(self, model: str, requests: list[dict], max_tokens: int) -> str:
        """OpenAI Batch — 요청마다 Chat Completions 본문 한 줄(JSONL)을 올리고 배치를 만든다(도구 호출은 없다)"""
        lines = [
            json.dumps(
                {
                    "custom_id": r["custom_id"],
                    "method": "POST",
                    "url": OPENAI_BATCH_ENDPOINT,
                    "body": _chat_params(
                        "openai",
                        model,
                        r.get("system"),
                        r["messages"],
                        r.get("max_tokens", max_tokens),
                        r.get("schema"),
                        r.get("effort"),
                    ),
                },
                ensure_ascii=False,
            )
            for r in requests
        ]
        client = self._openai()
        upload = client.files.create(
            file=("batch.jsonl", "\n".join(lines).encode()), purpose="batch"
        )
        return client.batches.create(
            input_file_id=upload.id, endpoint=OPENAI_BATCH_ENDPOINT, completion_window="24h"
        ).id

    def batch_status(self, batch_id: str, alias: str | None = None) -> str:
        """끝났으면 "ended"(Anthropic과 같은 말) — OpenAI(별칭의 프로바이더)는 completed·failed·expired·cancelled"""
        if alias is not None and self._provider(self.config["aliases"][alias][0]) == "openai":
            status = self._openai().batches.retrieve(batch_id).status
            return "ended" if status in OPENAI_BATCH_DONE else status
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
        if self._provider(model) == "openai":
            return self._openai_results(batch_id, model, alias, task, prompt_version, schema)
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
            if msg.stop_reason == "tool_use":  # 도구 호출 — 단건 complete와 같은 모양으로 돌려준다
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
                    stop_reason=msg.stop_reason,
                    tool_calls=tuple(
                        {"id": b.id, "name": b.name, "input": b.input}
                        for b in msg.content
                        if b.type == "tool_use"
                    ),
                    content=_blocks(msg.content),
                )
            elif msg.stop_reason != "end_turn":
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
                    stop_reason=msg.stop_reason,
                    content=_blocks(msg.content),
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
            out[entry.custom_id] = self._record(
                alias, task, prompt_version, ("batch", entry.custom_id), result
            )
        return out

    def _openai_results(self, batch_id, model, alias, task, prompt_version, schema) -> dict:
        """OpenAI 배치 결과 — 출력 파일의 200 줄만 결과, 오류 파일·200 아닌 줄(만료·취소·서버 오류)은 None"""
        client = self._openai()
        batch = client.batches.retrieve(batch_id)
        if batch.status == "failed":  # 입력 검증 실패 등 — 결과 파일이 없다
            print(f"OpenAI 배치 {batch_id} 실패: {batch.errors}")
        price = self._price(model, model)
        out = {}
        for file_id in (batch.output_file_id, batch.error_file_id):
            if not file_id:
                continue
            for line in client.files.content(file_id).text.splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                response = row.get("response") or {}
                if response.get("status_code") != 200:
                    out[row["custom_id"]] = None
                    continue
                result = _openai_batch_result(model, price, response["body"], schema)
                out[row["custom_id"]] = self._record(
                    alias, task, prompt_version, ("batch", row["custom_id"]), result
                )
        return out


def _openai_batch_result(model: str, price: dict, body: dict, schema) -> "LLMResult":
    """OpenAI 배치 성공 줄의 Chat Completions 본문 → LLMResult(배치 단가)"""
    u = body.get("usage") or {}
    tokens_in, tokens_out = u.get("prompt_tokens", 0), u.get("completion_tokens", 0)
    cost = (tokens_in * price["input"] + tokens_out * price["output"]) / 1e6 * BATCH_PRICE_SHARE
    actual = body.get("model", model)
    choice = body["choices"][0]
    message = choice.get("message") or {}

    def result(ok, text="", data=None, error=None):
        return LLMResult(ok, text, data, actual, False, tokens_in, tokens_out, cost, 0, error)

    if message.get("refusal"):
        return result(False, error=f"{actual}: refusal")
    if choice.get("finish_reason") != "stop":
        return result(False, error=f"{actual}: finish_reason {choice.get('finish_reason')}")
    text = message.get("content") or ""
    if schema is None:
        return result(True, text)
    try:
        return result(True, text, json.loads(text))
    except json.JSONDecodeError:
        return result(False, error=f"{actual}: JSON 파싱 실패")


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
        tools: list | None = None,
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
        result = LLMResult(
            True,
            text,
            data,
            "fake",
            False,
            0,
            0,
            0.0,
            0,
            stop_reason="end_turn",
            content=[{"type": "text", "text": text}],
        )
        return self._record(alias, task, prompt_version, ref, result)
