# ① Qwen 파인튜닝 비교 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Qwen3.5-2B를 silver 재료 구간으로 LoRA 학습(재료 절반·전부 두 번)해, 검증 구간 silver·dev 75·문체 dev 225에서 Haiku v2(분류기 라벨)·Solar v2(판정기)와 나란히 채점하고 교체 기준 3개의 통과 여부를 낸다.

**Architecture:** `evaluation/finetune/qwen/`에 다섯 모듈 — `data.py`(학습·평가 셋 만들기, 순수 함수), `score.py`(채점·교체 기준, 순수 함수), `infer.py`(MLX 추론), `train.py`(mlx-lm LoRA 실행), `baselines.py`(Haiku v2·Solar v2 결과 모으기) — 와 명령 `python -m evaluation.finetune.qwen <data|train|predict|baselines|score>`. MLX를 import하는 것은 `infer.py`·`train.py`뿐이라 CI(`make test`, finetune 그룹 없음)에서 `data`·`score` 테스트가 돈다. 결과(예측·기준선·리포트 jsonl/md)는 `evaluation/finetune/qwen/out/`에 커밋하고, 학습 데이터·어댑터는 재생성 가능하거나 무거워 커밋하지 않는다.

**Tech Stack:** Python 3.11, mlx-lm(LoRA), `mlx-community/Qwen3.5-2B-bf16`, sqlite3, 기존 `shared/llm.py` 게이트웨이, pytest.

**Spec:** `docs/superpowers/specs/2026-11-05-classifier-design.md` "Qwen 비교" · 재료 경위 `docs/치과 AX - AI 실험 기록.md` §8

## 정한 것 (이번 세션)

- 비교 대상 "지금 분류기 라벨 모델" = Haiku v2(`ax_call_classifications` `prompt_version='v2'`). 판정기 = Solar v2(`ax_model_judgments` `prompt_version='v2'`)
- Qwen 지시문은 **짧은 고정 지시**(9항목 이름·허용값만, 판정 기준은 학습으로 배움). 학습·추론 같은 것
- 체크포인트 고르기: 재료 구간 통화의 10%(call_uid 단위, 변형도 같은 쪽)를 mlx-lm `valid`로 떼어 손실만 본다. 검증 구간 silver는 학습 중 보지 않는다
- 바꿔 쓰기 벌 수 비교(실험 기록 §8 "원본만·+1벌…")는 하지 않는다 — 설계대로 절반 vs 전부만
- 실패(파싱 실패·폴백)는 **모든 모델에서 오답**으로 세고 건수를 함께 적는다(분모는 셋 전체)
- 검증 구간은 비가중 k/n(관문과 같은 방식, 평가 설계 §2.7). dev·문체 dev는 역확률 가중을 주 수치로, 비가중·카테고리 평균을 병기(가중 모집단 = 생성 통화 전체의 골드 목적 건수, `evaluation/classification.py` `evaluate`와 같음)

## Global Constraints

- 학습 재료는 silver **재료 구간**(`evaluation/splits.py` `silver_split` = `material`)의 원본 중 `original_wrong`이 아닌 것 + `aug.jsonl`에서 `train = true`이고 원본이 재료 구간인 변형뿐. 어림 936 + 2,419 = 3,355건(분할 함수로 센 값과 같음)
- 검증 구간 통화는 원본도 변형도 학습·valid에 쓰지 않는다. dev·test 녹취록, `dev_eval.jsonl`은 학습에 쓰지 않는다. test 분할은 어디서도 읽지 않는다(SQL은 필요한 split을 `IN (...)`으로 골라 읽는다)
- `data/app.sqlite`는 `file:...?mode=ro`로만 연다. `make classify`·`make judge` 금지. 문체 dev의 Haiku·Solar 결과는 `app.sqlite`가 아닌 `evaluation/finetune/qwen/out/`에 쓴다
- 급함은 3값 그대로 학습한다. 리포트는 urgent 여부 일치를 주로, 3값을 함께
- 교체 기준(설계, 학습 전 고정): ① 검증 구간 목적 Strict ≥ Haiku v2 − 0.02 ② 검증 구간 플래그 6개 평균 ≥ Haiku v2 − 0.02 ③ 문체 dev 3벌 중 가장 낮은 벌의 목적 Strict(가중) ≥ 같은 Qwen의 원본 dev 목적 Strict(가중) − 0.05. 셋 다 통과해야 교체
- 의존성은 `pyproject.toml`의 새 그룹 `finetune`에만(backend·dev 그룹에 넣지 않음). `default-groups`에 넣지 않는다. 명령은 `uv run --group finetune python -m evaluation.finetune.qwen …`
- 가중치·어댑터·학습 데이터 jsonl은 커밋하지 않는다(`.gitignore`)
- 과금 호출(문체 dev Haiku·Solar)은 사용자 승인 뒤. 본 학습은 사용자가 정한 시간에
- 고칠 파일은 `evaluation/finetune/`, `tests/evaluation/test_qwen_*.py`, `pyproject.toml`, `uv.lock`, `.gitignore`, 실험 기록 새 절, PR 때 기능 정의 §9 해당 줄. 그 밖은 묻는다
- 코드 컨벤션: 한국어 독스트링·근거 절, 값은 이름으로(`shared.call_labels`, `evaluation.metrics`), sqlite3 SQL 그대로. 커밋은 사용자 요청 때만(아래 커밋 단계는 요청을 받았을 때 그 단위로)

## Review Focus

1. Qwen 출력에 JSON 앞뒤 군말·`<think>` 태그·코드 울타리가 붙는다 → 첫 JSON 객체만 읽고, 못 읽거나 9항목·enum이 안 맞으면 실패(오답). Task 1 `test_parse_labels_*`
2. 모델이 플래그를 `true/false`로 낸다 → 0/1로 맞춰 채점(골드는 0/1 정수). Task 1 `test_parse_labels_bool_flags`
3. 녹취록이 길어 학습 샘플이 `max_seq_length`를 넘는다 → mlx-lm이 끝을 잘라 정답 JSON이 사라진 채 학습. Task 3에서 전 샘플 토큰 수를 재 넘는 건이 있으면 멈춘다
4. 학습 때 mlx-lm이 그리는 프롬프트와 추론 때 프롬프트가 다르다(Qwen3.5 생각 블록) → 점수가 이유 없이 낮아짐. Task 3 확인 단계
5. 추론이 중간에 끊겨 다시 돌린다 → 이미 낸 id는 건너뛰고 줄이 겹치지 않는다. Task 3 `test_predict_resumes`

---

### Task 1: 학습·평가 셋 (`data.py`)

**Files:**
- Create: `evaluation/finetune/qwen/data.py`, `evaluation/finetune/qwen/__main__.py`(명령 `data`만 이번에)
- Test: `tests/evaluation/test_qwen_data.py`
- Modify: `.gitignore` — `evaluation/finetune/qwen/data/`, `evaluation/finetune/qwen/adapters/`

**Interfaces:**
- Produces:
  - `HELDOUT_SHARE = 0.1`, `HALF_SHARE = 0.5`
  - `INSTRUCTION: str` — `shared.call_labels`의 `ITEMS`·`PURPOSES`·`COUNTERPARTS`·`URGENCIES`·`FLAGS`로 만든 짧은 지시(항목 이름, 허용값, "JSON 한 줄만, 플래그는 0/1"). 판정 기준 문장은 넣지 않는다
  - `target_json(labels: dict) -> str` — `ITEMS` 순서, 플래그 `int`, `json.dumps(ensure_ascii=False, separators=(",", ":"))`
  - `parse_labels(text: str) -> dict | None` — 첫 `{…}` 객체를 읽어 플래그를 0/1로 맞추고 `backend.classify.valid_labels`를 통과하면 dict, 아니면 None
  - `stratified_take(labels: list[dict], share: float, salt: str) -> set[str]` — `silver_split`과 같은 층 키, 층 안 `sha256(salt + call_uid)` 순 앞쪽 `round(n × share)`
  - `build_sets(silver: list[dict], aug: list[dict]) -> dict[str, list[dict]]` — 키 `full`·`half`·`heldout`. 행 `{"id", "call_uid", "direction", "transcript", "labels"}`(원본 id = call_uid, 변형 id = aug_id). `silver` 행은 `truth_call_labels` 9항목 + `split`·`call_uid`·`direction`·`transcript`
  - `eval_sets(silver: list[dict], dev: list[dict], style: list[dict]) -> dict[str, list[dict]]` — 키 `validation`·`dev`·`s1`·`s2`·`s3`. 행 `{"id", "call_uid", "direction", "transcript", "gold"}`. 문체 행의 `gold`는 원본 dev 골드
  - `mlx_row(row: dict) -> dict` — `{"messages": [system=INSTRUCTION, user=backend.classify.user_message(transcript, direction), assistant=target_json(labels)]}`
  - `load_rows(root: Path) -> tuple[list, list, list, list]` — `source.sqlite`·`truth.sqlite`를 `?mode=ro`로 붙여 silver·dev 행(split을 `IN ('silver','dev')`로 고름), `aug.jsonl`, `dev_eval.jsonl`의 `kept` 행
  - 명령 `data` → `evaluation/finetune/qwen/data/{full,half}/{train,valid}.jsonl`(valid = heldout, 두 쪽 같음), `data/eval.jsonl`(평가 행 + `set` 칸), 셋별 건수 출력

절차 결정: 재료 구간 원본(`original_wrong` 제외) → `heldout` = `stratified_take(재료, HELDOUT_SHARE, "heldout")` → 나머지 call_uid에서 `half` = `stratified_take(나머지, HALF_SHARE, "half")`. 셋에는 각 call_uid의 원본 + `train=true` 변형이 함께 들어간다. `original_wrong` 원본의 변형은 `train` 값과 무관하게 뺀다.

- [ ] **Step 1: 실패하는 테스트 작성** — 합성 silver 200건(`test_splits.py` `_labels` 방식 + 9항목·transcript·direction)과 합성 aug(원본당 변형 2개, 일부 `train=false`, 일부 원본 `original_wrong=true`)로:
  - `test_no_validation_calls`: `silver_split`이 `validation`인 call_uid가 `full`·`half`·`heldout` 어느 행에도 없다
  - `test_original_wrong_excluded`: `original_wrong` call_uid의 원본·변형이 어디에도 없다
  - `test_only_train_variants`: 셋의 변형 id ⊆ `train=true` aug_id
  - `test_grouped_by_call`: `heldout`의 call_uid ∩ (`full` ∪ `half`)의 call_uid = ∅, `half` call_uid ⊆ `full` call_uid
  - `test_shares`: `heldout` 원본 수 ≈ 재료 원본 × 0.1, `half` 원본 수 ≈ (재료 − heldout) × 0.5 (층별 반올림 오차 ±층 수)
  - `test_rejects_non_silver`: `split='dev'` 행이 섞이면 `build_sets`가 `ValueError`
  - `test_deterministic`: 두 번 부른 결과가 같다
  - `test_eval_sets`: `validation` = 검증 구간 원본 전부(`original_wrong` 포함 — 평가에서는 빼지 않음), `s1` 행의 `gold` == 원본 dev 골드
  - `test_target_json_order`: `list(json.loads(target_json(l)))` == `list(ITEMS)`, 플래그가 0/1 정수
  - `test_parse_labels_extra_text`: `'<think>\n</think>\n```json\n' + target_json(l) + '\n```\n끝'` → `l`
  - `test_parse_labels_bool_flags`: 플래그가 `true/false`인 JSON → 0/1 dict
  - `test_parse_labels_invalid`: 항목 하나 빠짐 / 목적 값이 목록 밖 / JSON 아님 → None
  - `test_mlx_row`: 메시지 셋, system == `INSTRUCTION`, assistant == `target_json(labels)`

- [ ] **Step 2: 실패 확인** — `uv run pytest tests/evaluation/test_qwen_data.py -v` → import 실패

- [ ] **Step 3: `data.py` 구현**(위 Interfaces). MLX를 import하지 않는다

- [ ] **Step 4: 통과 확인** — 같은 명령 → PASS

- [ ] **Step 5: 실제 데이터로 명령 실행** — `uv run python -m evaluation.finetune.qwen data` → `full` + `heldout` 행 합 = 3,355, `half` ≈ `full`의 절반, `validation` 405, `dev` 75, `s1`·`s2`·`s3` 각 75

- [ ] **Step 6: 커밋**(요청 시) — `eval: Qwen 학습·평가 셋`

### Task 2: 채점·교체 기준 (`score.py`)

**Files:**
- Create: `evaluation/finetune/qwen/score.py`
- Test: `tests/evaluation/test_qwen_score.py`

**Interfaces:**
- Consumes: Task 1 평가 행(`id`·`gold`), 예측 `dict[id, dict | None]`
- Produces:
  - `MARGIN_VALIDATION = 0.02`, `MARGIN_STYLE = 0.05`(분류기 설계 "교체 기준")
  - `score_set(pred: dict, rows: list[dict], population: Counter | None) -> dict` — 키 `n`, `failed`(pred에 없거나 None), `items`(항목별 맞은 수, 실패는 오답), `urgent`(urgent 여부 일치 수), `flags_mean`(플래그 6개 맞은 수 합 / 6n), `purpose`(`k`, `strict` = k/n, `weighted`·`macro`·`by_category` — population이 None이면 `weighted` 없음). 가중은 `evaluation.metrics.inverse_weights`·`weighted_accuracy`·`macro_accuracy`, 카테고리 = 골드 목적
  - `criteria(qwen: dict[str, dict], ref_validation: dict) -> dict` — `qwen`은 셋 이름 → `score_set` 결과. 반환 `{"c1": {"pass", "qwen", "ref"}, "c2": …, "c3": {"pass", "worst_style", "worst", "dev"}, "all": bool}`. c1 = `purpose.strict`, c2 = `flags_mean`(검증 구간), c3 = `s1..s3` 중 `purpose.weighted` 최저 vs `dev` `purpose.weighted`
  - `report(results: dict[str, dict[str, dict]], crit: dict[str, dict]) -> str` — 모델 × 셋 마크다운 표(목적 k/n·가중, urgent 여부, 급함 3값, 플래그 평균, 실패 수)와 Qwen 모델별 기준 통과표. 실험 기록에 그대로 붙인다

- [ ] **Step 1: 실패하는 테스트 작성**
  - `test_failure_counts_wrong`: 행 4개, 예측 3개 맞음 + 1개 None → `n == 4`, `failed == 1`, `purpose["k"] == 3`
  - `test_urgent_only`: 골드 `normal`, 예측 `low` → `items["urgency"] == 0`, `urgent == 1`
  - `test_flags_mean`: 플래그 하나만 틀린 1건 → `flags_mean == 5/6`
  - `test_weighted_matches_eval`: 같은 입력에서 `purpose["weighted"]`가 `evaluation.classification._score(...)["purpose"]["weighted"]`와 같다(실패 없는 경우)
  - `test_criteria_boundary`: Qwen 0.96 vs Haiku 0.98 → c1 통과(같으면 통과), 0.9599 → 실패
  - `test_criteria_style`: dev 0.95, 문체 0.93·0.90·0.94 → c3 통과·`worst_style == "s2"`; 0.8999 → 실패
  - `test_report_has_all_sets`: 출력에 `validation`·`dev`·`s1`·`s2`·`s3`와 모든 모델 이름이 있다

- [ ] **Step 2: 실패 확인** — `uv run pytest tests/evaluation/test_qwen_score.py -v` → import 실패
- [ ] **Step 3: `score.py` 구현**
- [ ] **Step 4: 통과 확인** — 같은 명령 → PASS
- [ ] **Step 5: 커밋**(요청 시) — `eval: Qwen 채점·교체 기준`

### Task 3: MLX 학습·추론과 100건 속도 측정 → 보고 후 멈춤

**Files:**
- Modify: `pyproject.toml`(그룹 `finetune = ["mlx-lm"]` — 설치되는 버전을 하한으로), `uv.lock`
- Create: `evaluation/finetune/qwen/infer.py`, `evaluation/finetune/qwen/train.py`, `evaluation/finetune/qwen/lora.yaml`
- Modify: `evaluation/finetune/qwen/__main__.py`(명령 `train`·`predict`), `data.py`(명령 `data`가 `data/speed/{train,valid}.jsonl`도 씀 — `full` train의 id 해시 순 앞 100건, valid는 heldout 앞 20건)
- Test: `tests/evaluation/test_qwen_data.py`에 `test_predict_resumes`

**Interfaces:**
- Consumes: Task 1 `INSTRUCTION`·`parse_labels`·`mlx_row`, 데이터 폴더
- Produces:
  - `lora.yaml` — `model: mlx-community/Qwen3.5-2B-bf16`, `fine_tune_type: lora`, `num_layers: -1`, `lora_parameters: {rank: 16, dropout: 0.05, scale: 20.0}`, `batch_size: 4`, `learning_rate: 1e-4`, `mask_prompt: true`, `grad_checkpoint: true`, `max_seq_length: 2048`, `steps_per_eval: 100`, `steps_per_report: 10`, `save_every: 100`, `seed: 0`. 에폭 2(iters는 `train.py`가 train 행 수 × 2 / batch로 계산)
  - `train.run(name: str, epochs: int = 2, iters: int | None = None) -> Path` — `name` ∈ `full`·`half`·`speed`, `mlx_lm.lora`를 `-c lora.yaml --data data/<name> --adapter-path adapters/<name>`로 subprocess 실행하고 출력(손실·it/s·Peak mem)을 `out/train_<name>.log`에 저장
  - `infer.render_prompt(tokenizer, transcript: str, direction: str) -> str` — `apply_chat_template([system, user], add_generation_prompt=True, enable_thinking=False, tokenize=False)`
  - `infer.predict(rows: list[dict], out_path: Path, generate: Callable[[dict], str]) -> None` — `out_path`에 이미 있는 id는 건너뛰고 행마다 `{"id", "text", "labels": parse_labels(text), "sec"}`를 한 줄씩 append·flush
  - `infer.mlx_generate(model_id: str, adapter: Path | None) -> Callable[[dict], str]` — `mlx_lm.load` + 탐욕 디코딩(temp 0), `max_tokens=256`
  - 명령 `predict --adapter <name|none> --sets validation,dev,s1,s2,s3 [--limit N]` → `out/pred_<name>.jsonl`(행에 `set` 칸)

- [ ] **Step 1: 실패하는 테스트 작성** — `test_predict_resumes`: 가짜 `generate`(호출 횟수 셈)로 행 3개 → 파일에 2줄 미리 있음 → 호출 1번, 파일 3줄, id 중복 없음. `infer.py`는 MLX를 함수 안에서만 import해 이 테스트가 finetune 그룹 없이 돈다
- [ ] **Step 2: 실패 확인** — `uv run pytest tests/evaluation/test_qwen_data.py::test_predict_resumes -v` → FAIL
- [ ] **Step 3: 그룹 추가** — `uv add --group finetune mlx-lm` → `uv run --group finetune python -c "import mlx_lm; print(mlx_lm.__version__)"` 성공, `uv sync`(기본 그룹)로 `make test`가 mlx 없이 돈다
- [ ] **Step 4: `infer.py`·`train.py`·`lora.yaml`·명령 구현**
- [ ] **Step 5: 통과 확인** — `uv run pytest tests/evaluation/test_qwen_data.py -v` → PASS
- [ ] **Step 6: 토큰 길이 확인(Review Focus 3)** — `full`·`half`·`heldout`·평가 셋 전 행의 `apply_chat_template` 토큰 수 최대·99%를 출력. 최대 > 2048이면 멈추고 보고(잘라 버리지 않는다)
- [ ] **Step 7: 템플릿 일치 확인(Review Focus 4)** — mlx-lm의 학습 데이터 로더(`mlx_lm.tuner.datasets.load_dataset`)가 만든 첫 샘플 토큰의 프롬프트 부분 == `render_prompt` 토큰, 나머지 == `target_json` + 끝 토큰. 다르면 `lora.yaml`에 `chat_template_args: {enable_thinking: false}`를 시도하고, 그래도 다르면 멈추고 보고
- [ ] **Step 8: 속도 측정(100건)** — `train speed`(100행, iters 25 = 1에폭) → `out/train_speed.log`에서 it/s·학습 토큰/s·Peak mem. 이어서 `predict --adapter speed --sets validation --limit 20` → 건당 초. 기본 모델 추론도 5건(`--adapter none`) — 가중치 내려받기 시간은 따로 적는다
- [ ] **Step 9: 사용자에게 보고하고 멈춤** — 측정값과 어림: 학습 `full`(≈3,020행 × 2에폭), `half`(≈1,510행 × 2에폭) 각 시간, 추론 705건 × 2모델 시간, 최대 메모리. 메모리가 크면 대안(4bit QLoRA `mlx-community/Qwen3.5-2B-4bit`, batch 2)도 함께. 본 학습 시각은 사용자가 정한다
- [ ] **Step 10: 커밋**(요청 시) — `eval: Qwen MLX 학습·추론`, `chore: finetune 의존성 그룹`

### Task 4: 기준선 — Haiku v2·Solar v2 (`baselines.py`)

**Files:**
- Create: `evaluation/finetune/qwen/baselines.py`
- Modify: `evaluation/finetune/qwen/__main__.py`(명령 `baselines`)

**Interfaces:**
- Consumes: Task 1 평가 행, `backend.classify.label_request`·`judge_request`·`MAX_TOKENS`·`valid_labels`, `shared.llm.Gateway`·`JsonlLog`·`load_config`
- Produces:
  - `from_app(app_db: Path, ids: set[str]) -> dict[str, dict[str, dict | None]]` — `file:{app_db}?mode=ro`로 열어 `{"haiku_v2": …, "solar_v2": …}`. `status='ok'`이고 `ax_llm_calls.fallback_used = 0`인 행만 라벨, 그 밖은 None(오답). 플래그 0/1
  - `run_style(rows: list[dict], out_path: Path) -> None` — 문체 225행에 별칭 `classify`(v2 라벨 요청, `max_tokens=MAX_TOKENS`)와 `judge`(v2 요청 그대로) 일반 호출, 동시 4, 결과 `out/baseline_style.jsonl`(`id`·`model`·`labels`·`fallback`·`cost`), 호출 기록 `out/llm_log.jsonl`. 이미 있는 (id, model)은 건너뜀
  - 명령 `baselines [--run-style]` → `out/baseline.jsonl`(검증 405 + dev 75는 `from_app`, 문체는 `baseline_style.jsonl`에서)

- [ ] **Step 1: `from_app` 구현·실행** — 검증·dev 480건 모두 값이 있는지, None 건수 출력(0이 기대값). 과금 없음
- [ ] **Step 2: `run_style` 구현 후 1건만 시험 호출하고 멈춤** — 1건 비용으로 225건 비용을 다시 어림해 사용자에게 보고(사전 어림: Haiku 라벨 $0.0004 × 225 ≈ $0.09, Solar v2 $0.0011 × 225 ≈ $0.24, 합 약 $0.33 — 실험 기록 §11 단가)
- [ ] **Step 3: 승인 뒤 225건 실행** — `uv run --env-file .env python -m evaluation.finetune.qwen baselines --run-style` → 실패·폴백은 같은 명령을 다시 돌려 다시 시도(남은 것은 오답으로 둠), 실제 비용 출력
- [ ] **Step 4: 커밋**(요청 시) — `eval: Qwen 비교 기준선(Haiku v2·Solar v2)`

### Task 5: 본 학습·추론·채점 (사용자가 정한 시각)

**Files:**
- Modify: `evaluation/finetune/qwen/__main__.py`(명령 `score`)
- Create(산출물): `out/pred_full.jsonl`, `out/pred_half.jsonl`, `out/train_{full,half}.log`, `out/report.md`

- [ ] **Step 1: 학습** — `train half` → `train full`. 로그에서 heldout 손실 최저 체크포인트를 고른다(`adapters/<name>/<iters>_adapters.safetensors`를 `adapters.safetensors`로). 고른 iters를 로그 끝에 적는다
- [ ] **Step 2: 추론** — `predict --adapter half`, `predict --adapter full`(셋 다섯 개, 705건씩)
- [ ] **Step 3: 채점** — `score` → 모델 4개(Qwen half·full, Haiku v2, Solar v2) × 셋 5개를 `score_set`(검증은 population=None, 나머지는 생성 통화 전체 목적 건수), Qwen 둘에 `criteria(…, Haiku v2 validation)`, `report` → `out/report.md`와 화면 출력
- [ ] **Step 4: 재료 충분성 판단 메모** — half vs full 차이(검증 목적·플래그·문체 최저). 판단 문장은 사용자와 정한다(설계: 거의 같으면 충분, full이 확실히 높으면 희귀 통화 바꿔 쓰기 보강)
- [ ] **Step 5: 커밋**(요청 시) — `eval: Qwen 2B 학습 결과`

### Task 6: 문서

**Files:**
- Modify: `docs/치과 AX - AI 실험 기록.md` 새 절 `## 13. ① Qwen3.5-2B 파인튜닝 비교`
- Modify(PR 때): `docs/치과 AX - 기능 정의.md` §9 "① Qwen 파인튜닝 비교" 줄

- [ ] **Step 1: 실험 기록 13** — 문제→원인→시도→결과→한계(메모리 [[ai-experiment-log]] 형식). 시도에 정한 것(짧은 지시, heldout 10%, LoRA 설정, 고른 체크포인트, 벌 수 비교는 하지 않음), 결과에 `report.md` 표·교체 기준 통과표·학습/추론 시간·메모리, 한계에 급함 normal/low 추첨·문체 dev 변형 3개가 독립 아님·기준선 프롬프트가 silver 오답을 보고 고친 것(검증 구간 Haiku 유리). §8 "몇 벌이 적당한지 … dev로 비교해 고른다" 문장은 사용자 확인 뒤 "하지 않음(13번)"으로 정리
- [ ] **Step 2: 기능 정의 §9 줄** — 결과에 맞게(교체 여부·다음 할 일). PR 직전에 사용자와 문장 확인
- [ ] **Step 3: 커밋**(요청 시) — `docs: Qwen 파인튜닝 비교 실험 기록`
