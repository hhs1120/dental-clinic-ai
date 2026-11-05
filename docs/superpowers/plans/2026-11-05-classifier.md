# ① 분류기·판정기 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 전체 생성 통화를 분류기(라벨 Solar Mini + 요약·할 일 Haiku)와 판정기(Haiku)로 분류·판정하고, 엇갈림을 계산하고, `eval.py --split dev`로 골드 정확도와 교차검증 지표를 낸다.

> Task 2 결과로 자리를 바꿨다 — 분류기 라벨 Haiku, 판정기 Solar Mini 4(`solar-mini4`). 이후 판정기 v2(머리말 `effort`), 급함 urgent 여부 비교, `--shard`, `init_schema` 맞춤은 앱 설계 §1·§7.2·평가 설계 §2.5가 원본

**Architecture:** 게이트웨이(`shared/llm.py`)에 Upstage 경로와 호출 기록 id(`log_id`) 반환을 더한다. 분류·판정은 `backend/classify.py`의 함수(`gateway, transcript, direction, version → 결과`)와 저장 함수로 두고, 일괄 실행 명령 `backend/classify_batch.py`가 Haiku는 Batch, Solar는 일반 호출로 돌려 `app.sqlite`에 쓴다. 엇갈림은 `backend/disagreements.py`가 계산한다. silver 재료·검증 분할은 `evaluation/splits.py`에 둔다(이번에는 함수와 상수만, Qwen 학습은 별도 계획).

**Tech Stack:** Python 3.11, sqlite3, anthropic SDK, openai SDK(Upstage OpenAI 호환 API), pytest.

**Spec:** `docs/superpowers/specs/2026-11-05-classifier-design.md`

**범위 메모:** 설계안의 "Qwen 비교" 절은 이 계획 다음의 별도 계획으로 한다(분류기 기준선과 분할 함수가 먼저 있어야 한다). 이 계획은 분할 함수(Task 7)까지 만든다. `make fixture`는 Task 0 결정대로 화면 브랜치로 넘긴다.

## Global Constraints

- backend는 `ground_truth`·`generator`·`evaluation`을 import하지 않는다. 주석·독스트링에도 그 낱말을 쓰지 않는다(`tests/test_no_ground_truth_import.py`). 정답은 `data/`·`app.sqlite`에 넣지 않는다
- 분류기·판정기 입력은 `transcript`와 `direction`뿐이다. 함수 시그니처로 강제한다(앱 설계 §1)
- `emr_*`·`fin_*`·`ax_calls`는 읽기만 한다. 앱 DB 연결은 `backend.db.connect(app, source)`(source는 `?mode=ro`로 붙음)
- test 분할·test 녹취록은 프롬프트 수정·평가 실행에 쓰지 않는다. `eval.py`는 `--split dev`만 돌린다(`--split test`는 사람만)
- 모델 배치(설계안 "모델 배치"): 분류기 라벨 = 별칭 `classify` = `[solar-mini]`, 분류기 요약·할 일 = `summarize` = `[claude-haiku-5-5, claude-sonnet-5-5]`, 판정기 = `judge` = `[claude-haiku-5-5, claude-sonnet-5-5]`. Task 2 결과로 전환 기준에 걸리면 `classify`와 `judge`의 모델만 맞바꾼다
- 라벨 9항목: `primary_purpose`, `counterpart`, `urgency`, `emergency`, `complaint`, `price_inquiry`, `wants_appt`, `appt_confirmed`, `has_todo`. enum 원본은 `generator/master.py`(`CALL_PURPOSE_SHARE` 키, `CALL_COUNTERPARTS`, `URGENCIES`, `CALL_FLAGS`)
- `has_todo` 기준 문장(두 프롬프트·평가 설계 §2.3에 같게): "직원이 원장님께 전달하겠다고만 약속해도 할 일이 남은 것으로 본다(has_todo = true)"
- 요약 형식(평가 설계 §2.4): 마크다운·글머리 금지, 명사형 종결(~함/~음), 공백 포함 80~110자 단일 문단. 코드 후처리로 줄바꿈·연속 공백·글머리 기호를 정규화한다
- 엇갈림 행을 만드는 조건은 셋 중 하나다 — (가) 9항목 중 하나라도 다름, (나) 어느 쪽이든 폴백(라벨이 같아도 — 폴백 건은 자동 종결하지 않는다), (다) 분류기 할 일 목록이 비어 있지 않은데 분류기 `has_todo = false`(라벨이 같아도). 칸: `items` = 다른 항목 목록(없으면 `[]`), `is_core` = 목적·급함·응급 중 하나라도 다름, `fallback`·`todo_mismatch` = (나)·(다) 해당 여부
- silver 재료·검증: 재료 70 : 검증 30, 층 키 `(희귀 목적 묶음, emergency, complaint)`(희귀 = complaint·emergency·unknown, 나머지는 `other`), 층 안 `sha256(call_uid)` 순 정렬 후 앞쪽 `round(n × 0.7)`이 재료
- 비용 기록·일일 상한은 게이트웨이가 한다(런타임 상한 $15). Batch 제출은 상한을 보지 않는다
- 커밋 전 `make fmt` → `make lint` → `make test`. `git add`는 파일 이름으로. 커밋 끝에 아래 두 줄
  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01G62cGwhYixc5LpBLhj8iGp
  ```

## Review Focus

1. 분류기 두 호출 중 하나만 실패(Solar 오류, Haiku 거절·JSON 파싱 실패)해도 행은 `failed`로 남고, 재실행이 그 통화만 다시 돌려야 한다 → Task 5 `test_classify_one_call_fails_marks_failed`, Task 6 `test_rerun_skips_ok_retries_failed`
2. Batch 결과에 빠진 건(처리 안 됨 = `None`)이 있어도 나머지는 저장되고 빠진 건은 다음 실행 대상으로 남아야 한다 → Task 6 `test_batch_missing_entry_left_for_next_run`
3. 판정기·요약이 서버 측 폴백·별칭 폴백으로 Sonnet이 답한 건은 엇갈림에 `fallback = 1`로 표시되어 자동 종결 대상이 되지 않아야 한다 → Task 6(엇갈림) `test_fallback_marked`
4. 요약이 줄바꿈·글머리·마크다운을 섞어 와도 저장 전에 한 문단으로 정규화되어야 한다(블라인드 비교 문체 누출 방지) → Task 5 `test_normalize_summary`
5. Upstage가 연결 오류·429·5xx를 내면 게이트웨이가 Anthropic과 같은 규칙으로 기록하고 실패를 돌려줘야 한다(예외가 새지 않게) → Task 1 `test_upstage_transient_error_recorded`

---

### Task 0: 범위 조정 기록 (fixture를 화면 브랜치로)

설계안은 `make fixture`를 이번 범위에 넣었지만, fixture의 쓰임(판정 큐·검색을 CI에서 키 없이 돌림)은 화면 브랜치에서 생기고, 이번 테스트는 작은 합성 DB로 충분하다. 설계안 "fixture" 절을 "화면 브랜치로 미룸"으로 고친다.

**Files:**
- Modify: `docs/superpowers/specs/2026-11-05-classifier-design.md` ("이번에 만든다" 목록과 "fixture" 절)

- [ ] **Step 1:** "이번에 만든다"에서 `make fixture` 줄을 지우고 "이번에 안 만든다"에 "`make fixture` — 쓰임이 화면(판정 큐·검색)이라 그 브랜치로"를 넣는다. "fixture" 절 본문을 같은 한 줄로 바꾼다
- [ ] **Step 2:** 커밋 `docs: 분류기 설계 — fixture를 화면 브랜치로`

### Task 1: 게이트웨이 — Upstage 경로와 호출 기록 id

**Files:**
- Modify: `shared/llm.py`, `backend/harness/store.py`(`SqliteLlmLog.write`가 id 반환)
- Test: `tests/shared/test_llm.py`, `tests/backend/test_harness_store.py`

**Interfaces:**
- Produces:
  - `LLMResult.log_id: int | None = None` — 그 호출의 기록 행 id(`SqliteLlmLog`면 `ax_llm_calls.id`, `JsonlLog`면 `None`)
  - `SqliteLlmLog.write(record) -> int`(lastrowid), `JsonlLog.write(record) -> None`
  - `_Base._record(...) -> LLMResult` — 기록하고 `log_id`를 채운 결과를 돌려준다. `complete`·`batch_results`·`FakeGateway.complete`는 이 반환값을 돌려준다
  - 설정 `models.<id>.provider: "anthropic" | "upstage"`(없으면 anthropic). `Gateway(config, log, scope, client=None, upstage_client=None, clock=...)`
  - Upstage 호출: `upstage_client.chat.completions.create(model, messages=[{"role":"system",...}, *messages], max_tokens, response_format={"type":"json_schema","json_schema":{"name":"labels","schema":schema,"strict":True}})`. 토큰은 `usage.prompt_tokens`·`completion_tokens`, 성공은 `finish_reason == "stop"`. 클라이언트는 필요할 때 `openai.OpenAI(api_key=os.environ["UPSTAGE_API_KEY"], base_url="https://api.upstage.ai/v1")`로 만든다 — `openai` import는 그 안에서 한다(generator 그룹에 openai가 없다)
  - Upstage 모델은 `submit_batch`에서 `ValueError("upstage: batch 미지원")`

- [ ] **Step 1: 실패하는 테스트 작성** — 가짜 Upstage 클라이언트(`chat.completions.create`를 흉내 내는 객체, `openai.types.chat.ChatCompletion`으로 응답)로:
  - `test_upstage_complete_parses_json`: 스키마를 준 호출이 `ok=True`, `data`가 파싱된 dict, `model`이 응답 모델, 비용 = `prompt_tokens × input + completion_tokens × output`(백만 단위)
  - `test_upstage_transient_error_recorded`: `openai.APIConnectionError`·`openai.APIStatusError(429)`면 `ok=False`, 기록 1행, 예외가 새지 않음. 별칭에 다음 모델이 있으면 넘어간다
  - `test_upstage_length_cut_fails`: `finish_reason == "length"`면 `ok=False`, `error`에 `finish_reason length`
  - `test_log_id_from_sqlite_log`: `SqliteLlmLog`를 쓴 `complete`·`FakeGateway.complete`의 결과 `log_id`가 `ax_llm_calls`의 그 행 id
  - `test_upstage_batch_rejected`: `submit_batch`가 Upstage 별칭이면 `ValueError`
- [ ] **Step 2:** `uv run pytest tests/shared/test_llm.py tests/backend/test_harness_store.py -q` → 새 테스트 FAIL
- [ ] **Step 3:** 구현. 폴백 규칙은 Anthropic 경로와 같다 — 일시 오류(연결·408·409·429·5xx)는 다음 모델, 그 밖의 오류·잘림·JSON 파싱 실패는 거기서 실패
- [ ] **Step 4:** 같은 명령 → PASS, `make test` 전체 PASS
- [ ] **Step 5:** 커밋 `shared: 게이트웨이에 Upstage 경로와 호출 기록 id`

### Task 2: Solar 맛보기 (Upstage 키 필요 — 전환 기준 판정)

설계안 "모델 배치" 착수 전 확인. 사람이 `.env`에 `UPSTAGE_API_KEY`를 넣은 뒤에 한다. 키가 없으면 여기서 멈추고 사람에게 요청한다.

**Files:**
- Create: `evaluation/finetune/spike_labels.py`(버리는 실험이지만 기록으로 남긴다 — `evaluation/finetune/aug/augment.py`와 같은 방식)
- Modify: `config/llm.yaml`(Solar 모델·단가 — 이 Task에서 먼저 넣는다), `.env.example`(`UPSTAGE_API_KEY=`), `docs/치과 AX - AI 실험 기록.md`(9절), `docs/superpowers/specs/2026-11-05-classifier-design.md`(결과 한 줄)

**Interfaces:**
- Consumes: Task 1 `Gateway`(Upstage 경로)
- Produces: `spike_labels.py run <model> <split>` / `score <model> <split>` — 프롬프트는 `evaluation/finetune/aug/classify_v1.md` 원문, 스키마는 같은 파일의 `SCHEMA`(`augment.py`), 대상은 `split IN ('dev','silver')`만 SQL로

- [ ] **Step 1:** Upstage 문서로 Solar Mini 4의 API 모델 id·단가를 확인해 `config/llm.yaml` `models`에 `{ input, output, provider: upstage }`로 넣는다(문서 단가가 콘솔과 다르면 콘솔 값)
- [ ] **Step 2:** `spike_labels.py` 작성 — 결과 jsonl·호출 기록은 `evaluation/finetune/spike/`. 점수는 항목별 k/n, 목적 Strict, 플래그 6개 평균
- [ ] **Step 3:** 실행 `uv run --env-file .env python evaluation/finetune/spike_labels.py run solar-mini dev` 와 `... silver`(약 $0.4). Haiku 비교값은 실험 기록 7의 v1 수치(silver 목적 1328/1350, 플래그는 `evaluation/finetune/aug/v1_claude-haiku-5-5_silver.jsonl`로 다시 센다)
- [ ] **Step 4: 판정** — silver 목적 Strict 또는 플래그 평균이 Haiku보다 2%p 넘게 낮으면 **전환**(이후 Task의 `classify`·`judge` 별칭 모델을 맞바꾼다). 결과와 판정을 설계안 "모델 배치"에 한 줄, 실험 기록 9절(문제→시도→결과→판단→한계·비용)에 적는다
- [ ] **Step 5:** 커밋 `eval: Solar 맛보기와 분류기 배치 판정`(spike 결과 파일 포함)

### Task 3: 라벨 상수·스키마·앱 테이블

**Files:**
- Create: `shared/call_labels.py`
- Modify: `shared/app_schema.sql`
- Test: `tests/generator/test_call_labels_sync.py`(generator를 읽는 비교 테스트는 tests/generator에), `tests/backend/test_db.py`

**Interfaces:**
- Produces (`shared/call_labels.py`):
  - `PURPOSES: tuple[str, ...]`(14개, `CALL_PURPOSE_SHARE` 키 순), `COUNTERPARTS`, `URGENCIES`, `FLAGS`(6개), `ITEMS`(9개 = `("primary_purpose","counterpart","urgency") + FLAGS`), `CORE_ITEMS = ("primary_purpose","urgency","emergency")`
  - `LABEL_SCHEMA: dict` — 9항목 required, `additionalProperties: false`, 플래그는 boolean
  - `SUMMARY_SCHEMA: dict` — `{summary: string, todo: array[string]}` required
- Produces (스키마): `ax_call_classifications`에 `summary_model_actual TEXT`, `summary_llm_call_id INTEGER REFERENCES ax_llm_calls(id)`. 새 표
  ```sql
  CREATE TABLE ax_call_disagreements (
      call_uid TEXT NOT NULL, a_version TEXT NOT NULL, b_version TEXT NOT NULL,
      is_core INTEGER NOT NULL, fallback INTEGER NOT NULL, todo_mismatch INTEGER NOT NULL,
      items TEXT NOT NULL,  -- 다른 항목 이름 JSON 목록
      PRIMARY KEY (call_uid, a_version, b_version)
  );
  ```

- [ ] **Step 1: 실패하는 테스트** — `test_label_enums_match_master`: `call_labels`의 네 enum이 `generator.master`의 원본과 같은 순서·값. `test_label_schema_items`: `LABEL_SCHEMA["required"] == list(ITEMS)`. `test_app_schema_has_disagreements`: 새 DB에 `ax_call_disagreements`와 두 칸이 있다
- [ ] **Step 2:** `uv run pytest tests/generator/test_call_labels_sync.py tests/backend/test_db.py -q` → FAIL
- [ ] **Step 3:** 구현
- [ ] **Step 4:** PASS, `make test` PASS
- [ ] **Step 5:** 커밋 `shared: 통화 라벨 상수·스키마와 엇갈림 표`

### Task 4: 프롬프트·설정·평가의 프롬프트 경로

**Files:**
- Create: `prompts/classify/v1/labels.md`, `prompts/classify/v1/summary.md`, `prompts/judge/v1.md`, `backend/prompts.py`
- Modify: `config/llm.yaml`(별칭 `classify`·`summarize`·`judge`), `evaluation/eval.py`(few-shot 머리말 경로), `tests/evaluation/test_eval_cli.py`
- Test: `tests/backend/test_prompts.py`

**Interfaces:**
- Produces: `backend.prompts.load(task: str, version: str) -> dict[str, str]` — `classify`면 `{"labels": ..., "summary": ...}`(폴더), 그 밖은 `{"system": ...}`(파일). 머리말(`---` YAML)은 떼고 본문만. 없는 버전은 `FileNotFoundError`
- `eval.py`는 classify few-shot을 `prompts/classify/<v>/labels.md` 머리말에서 읽는다

- [ ] **Step 1:** 프롬프트 작성
  - `labels.md`: `evaluation/finetune/aug/classify_v1.md`에 Global Constraints의 `has_todo` 기준 문장을 `has_todo` 줄에 더한 것
  - `summary.md`: 요약(Global Constraints 형식)과 할 일("누가 무엇을" 문장 목록, 없으면 빈 목록)만 쓰게 하는 지시. 라벨은 묻지 않는다
  - `judge/v1.md`: 분류기 문장을 옮기지 않고 평가 설계 §2.3 플래그 판정 기준과 가상 데이터 생성 설계 §4.1 라벨 정의에서 새로 쓴다. 주 목적 = 처음 꺼낸 용건, `has_todo` 기준 문장은 같게
- [ ] **Step 2: 실패하는 테스트** — `test_load_classify_folder`(두 키, 머리말 제거), `test_load_judge_file`, `test_missing_version_raises`. `test_eval_cli`의 few-shot 테스트를 폴더 경로로 고친다
- [ ] **Step 3:** FAIL 확인 → 구현 → PASS, `make test` PASS
- [ ] **Step 4:** 커밋 `backend: 분류·판정 프롬프트 v1과 불러오기`

### Task 5: 분류·판정 함수와 저장

**Files:**
- Create: `backend/classify.py`
- Test: `tests/backend/test_classify.py`

**Interfaces:**
- Consumes: Task 1 `LLMResult.log_id`, Task 3 `LABEL_SCHEMA`·`SUMMARY_SCHEMA`, Task 4 `prompts.load`
- Produces:
  - `@dataclass Classification: ok: bool, labels: dict | None, summary: str | None, todo: list[str] | None, label: LLMResult, summary_call: LLMResult`
  - `@dataclass Judgment: ok: bool, labels: dict | None, call: LLMResult`
  - `user_message(transcript: str, direction: str) -> str` — `f"방향: {direction}\n\n{transcript}"`
  - `classify(gw, transcript: str, direction: str, version: str) -> Classification` — 라벨(`classify` 별칭, `task="classify"`)과 요약(`summarize` 별칭, `task="summarize"`) 두 호출, `prompt_version=version`
  - `judge(gw, transcript: str, direction: str, version: str) -> Judgment` — `judge` 별칭, `task="judge"`
  - `normalize_summary(text: str) -> str` — 줄바꿈·연속 공백을 공백 하나로, 줄머리 글머리 기호(`-`,`*`,`•`,숫자.)와 `#`·`**` 제거, 앞뒤 공백 제거
  - `save_classification(conn, call_uid: str, version: str, c: Classification) -> None` — `ax_call_classifications` UPSERT(같은 `(call_uid, model_alias='classify', version)`이면 교체). `labels`·`todo`는 JSON, 실패면 `status='failed'`와 빈 칸. `llm_call_id = c.label.log_id`, `summary_llm_call_id = c.summary_call.log_id`, `model_actual = c.label.model`, `summary_model_actual = c.summary_call.model`
  - `save_judgment(conn, call_uid: str, version: str, j: Judgment) -> None` — `ax_model_judgments` UPSERT, `model_alias='judge'`

- [ ] **Step 1: 실패하는 테스트**(`FakeGateway` + `SqliteLlmLog`, 임시 app DB)
  - `test_classify_ok_saves_row`: 행 1개 `ok`, `labels` 9키, `llm_call_id`·`summary_llm_call_id`가 서로 다른 `ax_llm_calls` 행
  - `test_classify_one_call_fails_marks_failed`: 요약 호출만 `ok=False`를 돌려주는 가짜 게이트웨이 → `status='failed'`, 기록 2행
  - `test_classify_input_only_transcript_direction`: 게이트웨이에 넘어간 messages가 `user_message(...)` 하나뿐이고 system은 프롬프트 본문
  - `test_judge_saves_row`
  - `test_normalize_summary`: `"- 예약 변경 요청함\n\n**목요일** 3시로 옮김"` → `"예약 변경 요청함 목요일 3시로 옮김"`
- [ ] **Step 2:** `uv run pytest tests/backend/test_classify.py -q` → FAIL
- [ ] **Step 3:** 구현
- [ ] **Step 4:** PASS, `make test` PASS(`test_no_ground_truth_import` 포함)
- [ ] **Step 5:** 커밋 `backend: 분류기·판정기 함수와 저장`

### Task 6: 엇갈림 계산과 일괄 실행 명령

**Files:**
- Create: `backend/disagreements.py`, `backend/classify_batch.py`
- Modify: `Makefile`(`classify`·`judge`·`disagreements` 타깃)
- Test: `tests/backend/test_disagreements.py`, `tests/backend/test_classify_batch.py`

**Interfaces:**
- Consumes: Task 5 전부
- Produces:
  - `compute(conn, a_version: str, b_version: str) -> int` — 두 버전 모두 `ok`인 통화마다 Global Constraints 엇갈림 규칙으로 `ax_call_disagreements`를 다시 쓴다(그 버전 쌍의 행을 지우고 넣음). 폴백은 `ax_llm_calls.fallback_used`(라벨 호출·판정 호출)로 본다. 반환은 엇갈림 행 수
  - `run(conn, gw, task: Literal["classify","judge"], version: str, *, use_batch: bool, limit: int | None = None, poll_seconds: int = 30) -> dict` — 대상은 `source.ax_calls`의 생성 통화(`source = 'generated'`) 중 그 버전 행이 `ok`가 아닌 것(없거나 `failed`), `call_uid` 순. 반환 `{"done": n, "failed": n, "missing": n}`
    - `use_batch=False`: 통화마다 `classify`/`judge` → 저장
    - `use_batch=True`: Haiku 호출(분류기의 요약, 판정기 전체)은 `gw.submit_batch` → `batch_status`가 `ended`일 때까지 `poll_seconds`마다 → `batch_results`(값이 `None`인 건은 `missing`으로 세고 저장하지 않음). 분류기의 Solar 라벨은 통화마다 일반 호출, 요약 결과와 합쳐 저장
  - 명령: `uv run --env-file .env python -m backend.classify_batch {classify|judge} --version v1 [--limit N] [--no-batch]`, `... disagreements --a v1 --b v1`. `make classify V=v1`, `make judge V=v1`, `make disagreements A=v1 B=v1`

- [ ] **Step 1: 실패하는 테스트**
  - 엇갈림: `test_identical_no_row`, `test_one_item_differs_row`(`items == ["complaint"]`, `is_core == 0`), `test_core_item`(urgency 다름 → `is_core == 1`), `test_fallback_marked`(라벨이 같고 판정 호출 `fallback_used=1` → 행 1개, `items == []`, `fallback == 1`, `is_core == 0`), `test_todo_mismatch`(라벨 같음, 분류기 todo 1개, 분류기 `has_todo=false` → `todo_mismatch == 1`), `test_only_both_ok`(한쪽 `failed`면 행 없음)
  - 일괄: `test_run_no_batch_classifies_all`(합성 source 3통화), `test_rerun_skips_ok_retries_failed`, `test_batch_missing_entry_left_for_next_run`(가짜 게이트웨이의 `batch_results`가 한 건 `None` → 그 통화는 행 없음, 다음 `run`의 대상)
- [ ] **Step 2:** FAIL 확인
- [ ] **Step 3:** 구현
- [ ] **Step 4:** PASS, `make test` PASS
- [ ] **Step 5:** 커밋 `backend: 엇갈림 계산과 분류·판정 일괄 실행`

### Task 7: silver 재료·검증 분할

**Files:**
- Create: `evaluation/splits.py`
- Modify: `evaluation/metrics.py`(`SILVER_MATERIAL_SHARE = 0.7`). 희귀 목적 묶음은 `generator.master.EVAL_RARE_PURPOSES`를 읽는다(새 상수를 두지 않는다)
- Test: `tests/evaluation/test_splits.py`

**Interfaces:**
- Produces: `silver_split(labels: list[dict]) -> dict[str, str]` — 입력은 `truth_call_labels`의 silver 행(`call_uid`, `primary_purpose`, `emergency`, `complaint`), 출력 `{call_uid: "material" | "validation"}`. 규칙은 Global Constraints. 자동 개선과 Qwen이 같이 쓴다

- [ ] **Step 1: 실패하는 테스트** — `test_split_deterministic`(같은 입력 두 번 같음), `test_split_ratio_per_stratum`(층마다 재료 = `round(n × 0.7)`), `test_split_independent_of_input_order`
- [ ] **Step 2~4:** FAIL → 구현 → PASS
- [ ] **Step 5:** 실데이터로 한 번 세어 본다(재료 원본 936·검증 405 근처, 설계안 어림과 맞는지) — 숫자는 커밋 메시지에
- [ ] **Step 6:** 커밋 `eval: silver 재료·검증 분할(70:30)`

### Task 8: 실제 실행과 dev 채점 (키 필요)

**Files:**
- Modify: `docs/치과 AX - AI 실험 기록.md`(10절)

- [ ] **Step 1:** `make classify V=v1` → `make judge V=v1` → `make disagreements A=v1 B=v1`. 예상 비용 합계 약 $1(설계안 "실행"). 실패가 남으면 같은 명령을 다시 돌려 0이 될 때까지
- [ ] **Step 2:** `uv run python -m evaluation.eval --split dev` → 리포트(`reports/eval_dev_*`, 커밋 안 함)에서 분류기·판정기 항목별 정확도, 목적 Strict(가중·단순), 교차검증(일치 오류율·오류 포착률·향상 배수)을 옮긴다
- [ ] **Step 3:** 실험 기록 10절: 문제→시도→결과→한계. 한계에 급함 normal/low 추첨, `has_todo` 골드 불일치(dev 2건), v1 라벨 프롬프트가 silver 오답을 보고 고친 것(dev 점수의 silver 부분이 좋게 보일 수 있음)
- [ ] **Step 4:** 커밋 `docs: 분류기·판정기 v1 dev 결과`

### Task 9: 문서 반영

**Files:**
- Modify: `docs/치과 AX - 앱 설계.md`(§1 표 두 줄, §7 "모델 (확정)" 줄, §7.1 게이트웨이, §7.2 classify 폴더), `docs/치과 AX - 평가 설계.md`(§2.3 `has_todo` 문장, §2.7 분할 상수 이름), `docs/치과 AX - 기능 정의.md`(§9: "모델 B 착수 시" 정리, "자동 개선 착수 시" 항목에서 분할 상수 고정 완료분 빼기, 후순위 두 줄), `docs/치과 AX - AI 실험 기록.md`(8절 급함 문장)

- [ ] **Step 1:** 설계안 "문서 변경" 목록 그대로 고친다. 값은 코드 이름으로 가리키고 문서에 숫자를 새로 쓰지 않는다(분할 비율은 `evaluation/metrics.py` `SILVER_MATERIAL_SHARE`)
- [ ] **Step 2:** `make lint`(마크다운 안 파이썬 블록 포함) PASS
- [ ] **Step 3:** 커밋 `docs: 분류기 배치·has_todo 기준·silver 분할 반영`
