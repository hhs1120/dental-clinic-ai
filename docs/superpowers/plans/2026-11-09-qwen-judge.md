# Qwen 판정기 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** ① 판정기를 Solar(`judge v2`)에서 로컬 Qwen3.5-2B full(`judge v3`)로 바꾼다. 호출은 게이트웨이의 새 `mlx` 갈래를 지나고, 평가는 학습한 통화를 빼고 두 판정기를 같은 모수에서 나란히 낸다.

**Architecture:** `shared/llm.py`에 Upstage와 같은 모양의 `provider: mlx` 갈래를 둔다. 모델·어댑터는 모듈 수준 캐시와 잠금을 쓴다. `config/llm.yaml`의 `judge` 별칭이 Qwen을 가리키므로 판정을 부르는 backend 코드(`classify_batch`·`calls.py`)는 그대로다. 평가(`evaluation/`)는 판정기 프롬프트 머리말 `trained_on`을 읽어 학습 통화를 뺀다.

**Tech Stack:** Python 3 · sqlite3 · mlx-lm 0.32.2(`finetune` 그룹) · pytest · ruff

**Spec:** `docs/superpowers/specs/2026-11-09-qwen-judge-design.md`

## Global Constraints

- backend는 `evaluation/`·`generator/`·`ground_truth/`를 import하지 않는다(`tests/test_no_ground_truth_import.py`). 테스트(`tests/`)는 둘 다 import해도 된다
- `mlx_lm`·`mlx`는 함수 안에서만 import한다. CI(리눅스)에는 `finetune` 그룹이 없다
- `pyproject.toml`·`uv.lock`·CLAUDE.md는 바꾸지 않는다. Qwen 판정이 일어나는 Makefile 명령(`judge`, `dev`)만 `uv run --group finetune`
- 되도록 건드리지 않는 파일: `shared/app_schema.sql`, `backend/harness/`, `backend/api.py`, `frontend/`. `shared/llm.py`는 사람이 허락했고, 더하기만 한다
- `app.sqlite`에 쓰는 실행(Task 7의 3·4단계)은 돌리기 전에 사람에게 묻는다. `make data`·재생성은 하지 않는다
- 주석·독스트링은 한국어로 쓰고 근거 문서 절을 가리킨다. 값은 이름으로 읽는다
- 커밋 전 `make fmt` → `make lint` → `make test`. `git add`는 파일 이름으로. 커밋 메시지는 `<영역>: <무엇>` + 본문 글머리, 끝에 `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`
- 커밋은 사람이 요청할 때만 한다. 아래 "Commit" 단계는 사람이 허락했을 때 실행한다

## Review Focus

1. 같은 프로세스에서 두 스레드가 동시에 판정을 부른다(API의 새 통화 판정 두 건) → 모델은 한 번만 올라가고, 생성은 차례로 돈다(Task 2 `test_mlx_loads_once_and_serializes`)
2. `--group finetune` 없이 앱을 띄운다(`mlx_lm` import 실패) → 예외 없이 `ok=False`가 되고, 오류에 `--group finetune`이 나오고, 기록 1행이 남는다(Task 2 `test_mlx_missing_module`)
3. 어댑터 링크가 끊겼다(새 클론·다른 PC) → `ok=False`, 오류에 어댑터 경로, 판정은 `failed`(Task 2 `test_mlx_missing_adapter`)
4. 모델이 JSON 앞뒤에 글을 붙이거나, 객체를 둘 내거나, 플래그를 `true`로 낸다 → 실험 채점(`parse_labels`)과 같은 라벨(Task 1 `test_first_labels_matches_parse_labels`)
5. 목록 밖 값(예: 상대 `student`) → 판정 `failed`. 일괄 실행에서 다시 돌아도 같은 실패라 멈추지 않는다(Task 1 `test_judgment_invalid_enum_failed`)

---

### Task 1: 판정 출력에서 라벨 찾기

**Files:**
- Modify: `backend/classify.py` (`judgment_from`, 새 `first_labels`)
- Test: `tests/backend/test_classify.py`

**Interfaces:**
- Produces: `first_labels(text: str) -> dict | None`. 규칙은 `evaluation/finetune/qwen/data.py` `parse_labels`와 같다. `{`마다 `json.JSONDecoder().raw_decode`로 읽어 보고, 불 플래그를 0/1로 바꾼 뒤 `valid_labels`를 통과하는 첫 객체를 돌려준다
- Produces: `judgment_from(call)`이 `call.ok`인데 `call.data`가 라벨이 아니면 `first_labels(call.text)`를 쓴다. 찾으면 그 라벨로 ok, 못 찾으면 failed

- [ ] **Step 1: 실패하는 테스트**

```python
def test_first_labels_matches_parse_labels():
    from evaluation.finetune.qwen.data import parse_labels

    lines = (ROOT / "evaluation/finetune/qwen/out/pred_full.jsonl").read_text().splitlines()
    texts = [json.loads(x)["text"] for x in lines]
    extra = ['앞말 {"x": 1} ' + texts[0], texts[0].replace(":0", ":false"), "JSON 없음"]
    for t in texts + extra:
        assert cl.first_labels(t) == parse_labels(t)


def test_judgment_falls_back_to_text():
    good = '{"x":1} ' + VALID_JSON  # VALID_JSON = 9항목 라벨 한 줄(파일에 이미 있는 고정값을 쓴다)
    j = cl.judgment_from(_result(ok=True, text=good, data={"x": 1}))
    assert j.ok and j.labels == json.loads(VALID_JSON)


def test_judgment_invalid_enum_failed():
    bad = VALID_JSON.replace('"patient"', '"student"')
    j = cl.judgment_from(_result(ok=True, text=bad, data=json.loads(bad)))
    assert not j.ok and j.labels is None
```

(`_result`·`VALID_JSON`가 없으면 이 파일의 기존 `LLMResult` 만드는 방식을 따라 만든다)

- [ ] **Step 2: 실패 확인** — `uv run pytest tests/backend/test_classify.py -k "first_labels or falls_back or invalid_enum" -v` → `first_labels` 없음으로 FAIL
- [ ] **Step 3: 구현** — 위 Interfaces대로. 독스트링에 "규칙은 실험 채점 `parse_labels`와 같다(Qwen 판정기 설계 '판정기 v3')"
- [ ] **Step 4: 통과 확인** — 같은 명령 → PASS. `uv run pytest tests/backend/test_classify.py` 전체 PASS
- [ ] **Step 5: Commit** — `git add backend/classify.py tests/backend/test_classify.py` · `backend: 판정 출력에서 라벨을 실험 채점과 같은 규칙으로 찾기`

### Task 2: 게이트웨이 `mlx` 갈래

**Files:**
- Modify: `shared/llm.py` (모듈 독스트링, `Gateway.__init__`, `complete`, 새 `_local`·`_mlx_generate`·`first_json`·상수)
- Test: `tests/shared/test_llm.py`

**Interfaces:**
- Produces: `LOCAL_MAX_TOKENS = 256` — 출력 상한. 요청의 `max_tokens`는 쓰지 않는다
- Produces: `@dataclass(frozen=True) class LocalOutput: text: str; input_tokens: int; output_tokens: int; model: str`. `model`은 `"<설정 모델 이름>@<어댑터 지문 16자>"`이고, 어댑터가 없으면 이름만
- Produces: `Gateway(..., local_generate=None)` 키워드 전용. `local_generate(name: str, cfg: dict, messages: list[dict]) -> LocalOutput`이고, 기본값은 `_mlx_generate`
- Produces: `adapter_fingerprint(adapter_dir: Path) -> str` — `adapters.safetensors` sha256 앞 16자. 값은 `evaluation/finetune/qwen/infer.py`의 같은 이름 함수와 같다
- Produces: `first_json(text: str) -> object | None` — `{`마다 `raw_decode`로 읽어 첫 dict를 돌려준다
- `_mlx_generate`가 하는 일
  - 처음 부를 때 `cfg["base"]`와 `ROOT / cfg["adapter"]`로 `mlx_lm.load`를 한다. 결과는 모듈 수준 `_LOCAL_MODELS: dict`에 둔다. 적재와 생성은 모듈 수준 `_LOCAL_LOCK = threading.Lock()` 안에서 한다
  - `apply_chat_template(..., add_generation_prompt=True, enable_thinking=False, tokenize=False)` 다음 `mlx_lm.generate(..., max_tokens=LOCAL_MAX_TOKENS)`
  - 토큰 수는 `tokenizer.encode`로 센다
- `complete`에서 `provider == "mlx"`인 모델은 `_local(model, system, messages, schema, state)`를 탄다. 돌려주는 값은 `_upstage`와 같은 `("ok"|"stop", (text, data))`. 비용은 0이다. 기록은 기존 `_record` 1행
- 실패 문구
  - `ImportError`: `"{model}: mlx_lm 없음 — uv run --group finetune으로 실행"`
  - 어댑터 파일 없음: `"{model}: 어댑터 없음 {경로}"`
  - JSON 없음: `"{model}: JSON 파싱 실패"`
  - 셋 다 `"stop"`이고 예외를 던지지 않는다

- [ ] **Step 1: 실패하는 테스트** (`UP_CONFIG` 옆에 `MLX_CONFIG = {"aliases": {"q": ["qwen"]}, "models": {"qwen": {"provider": "mlx", "base": "b", "adapter": "a", "input": 0, "output": 0}}, "daily_usd_limit": {...}}`)

```python
def test_mlx_ok_records_row(tmp_path):
    gen = lambda name, cfg, msgs: llm.LocalOutput('잡담 {"label": "예약"}', 50, 9, "qwen@abcd")
    gw = _gw(MLX_CONFIG, tmp_path, local_generate=gen)
    r = gw.complete("q", task="judge", messages=[{"role": "user", "content": "x"}],
                    system="지시", schema=LABELS_SCHEMA, max_tokens=3000)
    assert r.ok and r.data == {"label": "예약"} and r.model == "qwen@abcd"
    assert (r.input_tokens, r.output_tokens, r.cost_usd, r.fallback_used) == (50, 9, 0, False)
    assert _rows(tmp_path)[-1]["model"] == "qwen@abcd"

def test_mlx_passes_system_first(tmp_path): ...  # gen이 받은 msgs[0] == {"role": "system", "content": "지시"}

def test_mlx_no_json_fails(tmp_path): ...  # text "모름" → not r.ok, "JSON 파싱 실패" in r.error, 기록 1행

def test_mlx_missing_module(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "mlx_lm", None)  # import → ImportError
    gw = _gw(MLX_CONFIG, tmp_path)  # 기본 _mlx_generate
    r = gw.complete("q", task="judge", messages=[...], schema=LABELS_SCHEMA)
    assert not r.ok and "--group finetune" in r.error and len(_rows(tmp_path)) == 1

def test_mlx_missing_adapter(tmp_path, monkeypatch): ...  # 가짜 mlx_lm 모듈, adapter 경로 없음 → not ok, 경로가 r.error에

def test_mlx_loads_once_and_serializes(tmp_path, monkeypatch):
    # 가짜 mlx_lm: load 호출 수를 세고, generate는 진입 중 다른 generate가 겹치면 실패 표시
    # 스레드 2개 × 3회 complete → load 1회, 겹침 없음

def test_adapter_fingerprint_matches_infer(tmp_path):
    from evaluation.finetune.qwen.infer import adapter_fingerprint as f
    (tmp_path / "adapters.safetensors").write_bytes(b"w")
    assert llm.adapter_fingerprint(tmp_path) == f(tmp_path)

def test_local_max_tokens_matches_infer():
    from evaluation.finetune.qwen import infer
    assert llm.LOCAL_MAX_TOKENS == infer.MAX_TOKENS
```

(테스트끼리 `_LOCAL_MODELS` 캐시가 섞이지 않게 `monkeypatch.setattr(llm, "_LOCAL_MODELS", {})`를 쓴다. `_gw`·`_rows`는 이 파일의 기존 도우미를 쓰고, 없으면 `JsonlLog(tmp_path / "log.jsonl")`로 만든다)

- [ ] **Step 2: 실패 확인** — `uv run pytest tests/shared/test_llm.py -k mlx -v` → FAIL
- [ ] **Step 3: 구현** — 위 Interfaces대로. 모듈 독스트링 프로바이더 줄에 "로컬 MLX(파인튜닝 판정기, `--group finetune`)"를 더하고, 의존성 줄에 "mlx-lm은 finetune 그룹(처음 부를 때 import)"을 더한다
- [ ] **Step 4: 통과 확인** — `uv run pytest tests/shared/test_llm.py -v` → 전부 PASS
- [ ] **Step 5: Commit** — `git add shared/llm.py tests/shared/test_llm.py` · `shared: 게이트웨이에 로컬 MLX 갈래`

### Task 3: 판정기 v3 프롬프트·설정·명령

**Files:**
- Create: `prompts/judge/v3.md`
- Modify: `config/llm.yaml` (`judge` 별칭, 모델 항목), `Makefile` (`judge`·`dev`), `backend/classify_batch.py` (독스트링 사용법 줄)
- Test: `tests/backend/test_prompts.py`

**Interfaces:**
- Produces: `prompts/judge/v3.md`
  - 머리말 `trained_on: silver_material`
  - 본문은 `data.INSTRUCTION`과 같다
- Produces: `config/llm.yaml`
  - 별칭 `judge: [qwen3.5-2b-judge]`. 주석에 "폴백 없음 — 옛 판정기 버전(v1·v2)은 다시 돌리지 않는다(Qwen 판정기 설계 '비교 규칙')"
  - 모델 항목 `qwen3.5-2b-judge: { provider: mlx, base: mlx-community/Qwen3.5-2B-bf16, adapter: evaluation/finetune/qwen/adapters/full, input: 0, output: 0 }`
  - `solar-mini4` 모델 항목은 그대로 둔다(옛 기록의 단가)
- Makefile
  - `judge`: `uv run --group finetune $(if …)`
  - `dev`의 uvicorn 줄: `uv run --group finetune …`
  - 위에 주석 한 줄: "Qwen 판정기는 finetune 그룹(mlx-lm)이 있어야 돈다 — Qwen 판정기 설계 '의존성'"

- [ ] **Step 1: 실패하는 테스트**

```python
def test_judge_v3_matches_training_instruction():
    from evaluation.finetune.qwen.data import INSTRUCTION

    assert prompts.load("judge", "v3")["system"] == INSTRUCTION
    assert prompts.effort("judge", "v3") is None


def test_judge_alias_is_local_qwen():
    cfg = llm.load_config()
    m = cfg["aliases"]["judge"]
    assert len(m) == 1 and cfg["models"][m[0]]["provider"] == "mlx"
    from evaluation.finetune.qwen import train

    assert ROOT / cfg["models"][m[0]]["adapter"] == train.ADAPTERS / "full"
    assert cfg["models"][m[0]]["base"] == train.config()["model"]
```

- [ ] **Step 2: 실패 확인** — `uv run pytest tests/backend/test_prompts.py -k "v3 or local_qwen" -v` → FAIL
- [ ] **Step 3: 구현** — 파일·설정·Makefile·독스트링
- [ ] **Step 4: 통과 확인** — 같은 명령 → PASS. 다음으로 `make test`를 돌려 기존 judge 테스트가 실제 설정의 별칭을 가정하는지 본다. 깨지면 그 테스트가 가짜 설정을 쓰게 고친다(값을 Qwen으로 바꾸지 않는다)
- [ ] **Step 5: Commit** — `git add prompts/judge/v3.md config/llm.yaml Makefile backend/classify_batch.py tests/backend/test_prompts.py` · `backend: 판정기 v3를 로컬 Qwen으로`

### Task 4: 평가 — 학습 통화 빼기와 판정기 나란히

**Files:**
- Modify: `evaluation/classification.py` (새 `trained_uids`, `evaluate_judge`·`crosscheck`에 `skip`), `evaluation/eval.py` (dev 교차검증·자동 종결을 판정기 버전마다), `evaluation/gold_audit.py` (`targets`가 학습 통화를 뺌), `evaluation/autoclose.py`(필요하면 `skip`)
- Test: `tests/evaluation/test_classification.py`, `tests/evaluation/test_eval_cli.py`, `tests/evaluation/test_gold_audit.py`

**Interfaces:**
- Produces: `trained_uids(conn, version: str, prompts_dir: Path = PROMPTS_DIR) -> set[str]`
  - `prompts/judge/<version>.md` 머리말의 `trained_on`을 읽는다. 머리말 정규식은 `backend.prompts.FRONT_MATTER`를 쓴다
  - 값이 없으면 빈 집합, `silver_material`이면 `silver_split`이 `material`로 나눈 call_uid 전부, 그 밖의 값이면 `EvalRefused`
  - silver 행 모으기는 `evaluation/autoclose.py` `_population`과 같은 방식이다. 같은 모양을 두 번 쓰게 되면 `classification.py`에 `silver_parts(calls) -> dict[str, str]`로 꺼내 둘이 같이 쓴다
- Produces: `judge_skip(conn, versions: list[str]) -> set[str]` — 판정기 버전들의 `trained_uids` 합집합
  - 판정기끼리 같은 모수로 비교하려면 모든 판정기 쪽에서 같은 집합을 뺀다
  - dev 리포트는 `ax_model_judgments`에 있는 판정기 버전 전부로 이 집합을 만든다
- `evaluate_judge(conn, split, versions=None, gold_drop=None, skip: set | None = None)` — `skip`은 `_gold_ids`에서 뺀다. dev에서만 뺀다(test 판정 풀은 학습하지 않았다)
- `crosscheck(conn, split, a, b, fewshot=None, skip: set | None = None)` — 기존 few-shot 빼기와 같은 자리에서 dev면 `skip`도 뺀다
- eval.py dev
  - `result["crosscheck"]`는 분류 버전 × 판정기 버전 전부다(지금은 분류 버전 × 활성 판정기)
  - `result["autoclose"]`는 지금처럼 활성 쌍이다. 새 `result["autoclose_by_judge"]`는 활성 분류 × 판정기 버전 전부의 목록이다
  - 렌더는 기존 `_render_crosscheck`가 목록을 그린다. 자동 종결 목록은 기존 `_render_autoclose`를 쌍마다 부른다
  - 리포트에 "판정기 학습 통화 제외 N통화" 한 줄을 넣는다
  - test 1회 가드와 `--section` 동작은 바꾸지 않는다
- `gold_audit.targets(conn, judge_version)` — `trained_uids(conn, judge_version)`를 대상에서 뺀다. `make gold-arbitrate` 출력에 뺀 통화 수를 한 줄로 낸다

- [ ] **Step 1: 실패하는 테스트** (`tests/evaluation/eval_db.py`의 기존 고정 DB에 판정기 두 버전을 넣는다. `prompts_dir`는 `tmp_path`에 v-x.md 두 개를 둔다. 하나는 `trained_on: silver_material`, 하나는 머리말 없음)

```python
def test_trained_uids_reads_front_matter(...):
    assert trained_uids(conn, "vq", d) == {재료 구간 call_uid들}  # silver_split으로 기대값 계산
    assert trained_uids(conn, "vs", d) == set()

def test_trained_uids_unknown_value_refused(...): ...  # trained_on: 아무거나 → EvalRefused

def test_crosscheck_same_population_for_both_judges(...):
    skip = judge_skip(conn, ["vs", "vq"])
    x, y = crosscheck(conn, "dev", "v1", "vs", skip=skip), crosscheck(conn, "dev", "v1", "vq", skip=skip)
    assert x["n"] == y["n"] and not (set(재료) & 모수)  # 모수에 학습 통화 없음

def test_evaluate_judge_skips_trained_on_dev_only(...): ...  # dev 모수에서 빠지고 test 모수는 그대로

def test_eval_dev_reports_each_judge(...):  # test_eval_cli 방식
    r = run(... split="dev")
    assert {(c["a"], c["b"]) for c in r["crosscheck"]} == {("v1", "vs"), ("v1", "vq")}
    assert [x["pair"] for x in r["autoclose_by_judge"]] == ["v1×vq", "v1×vs"]  # 버전 순서는 version_key

def test_gold_audit_targets_skip_trained(...): ...  # 학습 통화에서 판정기≠골드여도 대상 아님
```

- [ ] **Step 2: 실패 확인** — `uv run pytest tests/evaluation -k "trained or same_population or each_judge or skip_trained" -v` → FAIL
- [ ] **Step 3: 구현** — 위 Interfaces대로. `trained_uids` 독스트링에 "Qwen 판정기 설계 '학습한 통화 빼기', 평가 설계 §2.5"
- [ ] **Step 4: 통과 확인** — `uv run pytest tests/evaluation -v` → 전부 PASS
- [ ] **Step 5: Commit** — `git add evaluation/classification.py evaluation/eval.py evaluation/gold_audit.py evaluation/autoclose.py tests/evaluation/test_classification.py tests/evaluation/test_eval_cli.py tests/evaluation/test_gold_audit.py` · `eval: 판정기 학습 통화 빼기, 판정기 버전 나란히`

### Task 5: 판정 큐 정리

**Files:**
- Modify: `backend/judge_queue.py` (새 `prune`), `backend/classify_batch.py` (`disagreements` 명령)
- Test: `tests/backend/test_judge_queue.py`

**Interfaces:**
- Produces: `prune(conn, a: str, b: str) -> int`
  - `ax_judge_state`의 `pending` 가운데 `refresh`가 넣지 않을 통화를 지우고 지운 수를 돌려준다. `refresh`가 넣지 않는 통화는 `TARGETS_SQL` 행이 없거나, 엇갈림도 표본(`is_sample`)도 아닌 것이다
  - `skipped`·`done`과 `ax_judgments`는 건드리지 않는다
- `classify_batch disagreements`: 활성 쌍일 때 `refresh_if_active` 다음에 `prune`을 부르고, `판정 큐 정리 N건`을 출력한다

- [ ] **Step 1: 실패하는 테스트**

```python
def test_prune_drops_stale_pending_only(conn):
    # pending 셋: 엇갈림 있음 / 엇갈림 없음·표본 아님 / 대상 밖. skipped·done 각 하나(엇갈림 없음)
    assert judge_queue.prune(conn, "v1", "v1") == 2
    assert 상태 == {엇갈림 있음: "pending", skipped건: "skipped", done건: "done"}
```

- [ ] **Step 2: 실패 확인** — `uv run pytest tests/backend/test_judge_queue.py -k prune -v` → FAIL
- [ ] **Step 3: 구현** — `prune`, `disagreements` 명령에서 부르기. 독스트링에 "판정기를 바꾸면 옛 판정기 엇갈림으로 들어간 대기 건을 지운다(Qwen 판정기 설계 '실행 순서' 4)"
- [ ] **Step 4: 통과 확인** — `uv run pytest tests/backend -v` → PASS
- [ ] **Step 5: Commit** — `git add backend/judge_queue.py backend/classify_batch.py tests/backend/test_judge_queue.py` · `backend: 판정기를 바꾸면 판정 큐의 낡은 대기 건 정리`

### Task 6: 게이트웨이 확인 명령

**Files:**
- Modify: `evaluation/finetune/qwen/__main__.py` (새 `gateway-check [n]`, 독스트링 사용법 줄)
- Create: `evaluation/finetune/qwen/gateway_check.py`
- Test: `tests/evaluation/test_qwen_gateway_check.py`

**Interfaces:**
- Produces: `compare(rows: list[dict], ref: dict[str, str], infer_gen, gateway_judge) -> list[dict]`
  - `rows`: `data/eval.jsonl`의 validation·dev 행
  - `ref`: `pred_full.jsonl`의 id → text
  - `infer_gen(row) -> str`
  - `gateway_judge(row) -> (text, labels | None)`
  - 반환: 다른 건 목록 `{id, kind: "adapter"|"gateway", ...}`
    - `adapter`: (가) `infer_gen` 출력이 `ref`와 다름
    - `gateway`: (나) 게이트웨이 텍스트가 `infer_gen` 출력과 다르거나, 라벨이 `parse_labels(infer 출력)`과 다름
- 명령 `uv run --group finetune python -m evaluation.finetune.qwen gateway-check [n=30]`
  - validation 앞 n/2건, dev 앞 n/2건
  - `infer_gen`은 `infer.mlx_generate(train.config()["model"], train.ADAPTERS / "full")`
  - `gateway_judge`는 `backend.classify.judge(Gateway(load_config(), JsonlLog(<임시 폴더>), "runtime"), …, "v3")`
  - `app.sqlite`는 열지 않는다
  - 출력: 같은 건수, 다른 건 목록. 하나라도 다르면 종료 코드 1
  - `data/eval.jsonl`이 없으면 `uv run python -m evaluation.finetune.qwen data`를 먼저 돌리라고 알리고 멈춘다

- [ ] **Step 1: 실패하는 테스트** — 가짜 함수 셋으로 (가)만 다름, (나)만 다름, 모두 같음 세 경우의 반환을 단언
- [ ] **Step 2: 실패 확인** — `uv run pytest tests/evaluation/test_qwen_gateway_check.py -v` → FAIL
- [ ] **Step 3: 구현**
- [ ] **Step 4: 통과 확인** — 같은 명령 → PASS
- [ ] **Step 5: Commit** — `git add evaluation/finetune/qwen/gateway_check.py evaluation/finetune/qwen/__main__.py tests/evaluation/test_qwen_gateway_check.py` · `eval: Qwen 판정기 게이트웨이 확인 명령`

### Task 7: 실행과 확인 (사람에게 묻는 단계 포함)

- [ ] **Step 1:** `make fmt` → `make lint` → `make test` → 전부 통과
- [ ] **Step 2:** 필요하면 `uv run python -m evaluation.finetune.qwen data`, 이어서 `uv run --group finetune python -m evaluation.finetune.qwen gateway-check` → `다른 건 0`. (가)가 다르면 멈추고 사람에게 묻는다(지금 어댑터로 예측을 다시 낼지). (나)가 다르면 Task 1·2로 돌아가 원인을 찾는다
- [ ] **Step 3 (묻고 실행):** `make judge V=v3 NO_BATCH=1`
  - 약 55분. `--shard`는 둘까지
  - 끝나면 `ax_model_judgments`에서 v3 ok·failed 수를 확인한다. failed는 건수만 적는다
- [ ] **Step 4 (묻고 실행):**
  - `ax_settings.prompt_versions`를 `{"judge": "v3"}`로 바꾼다(`backend.db.set_setting`)
  - `make disagreements A=v2 B=v3` → 출력에 `판정 큐 정리 N건`
  - `make autoclose` → `auto_policy`가 꺼져 있어 "아무것도 하지 않음"
- [ ] **Step 5:** `uv run python -m evaluation.eval --split dev` 실행. 확인할 것:
  - 교차검증에 `v2×v2`·`v2×v3`가 같은 `n`으로 나오는지
  - 포착이 실험 기록 §15(Solar 30/62·Qwen 58/62)와 같은지. 다르면 이유를 찾는다
  - 자동 종결 쌍별 수치

### Task 8: 문서

**Files:**
- Modify: `docs/superpowers/specs/2026-11-05-classifier-design.md`, `docs/치과 AX - 기능 정의.md` §9, `docs/치과 AX - 앱 설계.md` §7·§7.1, `docs/치과 AX - 평가 설계.md` §2.5, `docs/치과 AX - AI 실험 기록.md` §15

- [ ] **Step 1: 분류기 설계 "모델 배치"**
  - 표는 분류기 라벨 Haiku, 요약·할 일 Haiku, 판정기 라벨 Qwen3.5-2B full(로컬, `judge v3`). 표 머리 "Qwen 비교 뒤" 칸은 결과에 맞게 고친다
  - "Qwen 자리" 줄은 판정기 자리로 정한 결과와 이유 한 줄로 바꾸고, 새 설계 문서를 가리킨다
  - "판정기는 처음부터 끝까지 고정한다"는 비교 규칙 줄로 바꾼다
  - 계열이 달라 교차검증을 낸다는 줄은 Haiku·Qwen으로 고친다
  - "Qwen 비교"의 "앱에서 Qwen을 돌리는 방법은 바꾸기로 한 뒤에 정한다"는 새 설계 문서를 가리키게 한다
- [ ] **Step 2: 기능 정의 §9**
  - "Qwen 자리 정하기"를 지운다
  - "Qwen 도입 시"에 "앱만 설치할 때 mlx-lm을 `backend` 그룹(Mac 표지)으로 옮긴다"를 더한다
  - 4B 줄에 설계 문서의 조건 줄과 "PEFT 어댑터라 비교는 Colab 예측을 `score.py`로, 바꾸면 bf16에 합쳐 `mlx_lm.convert`"를 더한다
  - "사람 개입 최소화" 순서에서 "Qwen 자리"를 뺀다
  - "세 계열 다수결"·"다른 오픈 모델 비교"의 "Qwen 자리를 정한 뒤"를 맞게 고친다
- [ ] **Step 3: 앱 설계 §7 "모델 (확정)"·§7.1**
  - 판정기는 Qwen3.5-2B full(로컬 MLX, `finetune` 그룹으로 실행)
  - 프로바이더는 Anthropic·Upstage·로컬 MLX
  - 문서 안의 다른 "판정기 Solar" 문장도 함께 고친다(실험 경위는 그대로)
- [ ] **Step 4: 평가 설계 §2.5**
  - 비교 규칙: 분류기 비교는 같은 판정기 안에서, 판정기 비교는 같은 분류기·같은 모수에서
  - 판정기가 학습한 통화(`trained_on` 머리말)는 dev 판정기 채점·교차검증·골드 점검 대상에서 모든 판정기 쪽으로 뺀다
- [ ] **Step 5: 실험 기록 §15** — Task 7 5단계의 앱 수치, v3 실패 건수, 다르면 그 이유. 문제→원인→시도→결과→한계 형식을 지킨다
- [ ] **Step 6:** 바뀐 문서를 훑어 날짜·경위·중복이 없는지 본다(메모리 "문서에는 규칙만"). `make test`(`test_doc_sync`)도 통과해야 한다
- [ ] **Step 7: Commit** — 바뀐 문서와 이 설계·계획(`docs/superpowers/specs/2026-11-09-qwen-judge-design.md`, `docs/superpowers/plans/2026-11-09-qwen-judge.md`)을 파일 이름으로 `git add` · `docs: 판정기를 Qwen으로 — 배치 표·비교 규칙·§9`
