# ① 어려운 통화 평가셋 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `hc-` 통화 250건(5종류 × 50)을 GPT로 만든다. 생성 라벨과 Opus 독립 라벨, 사람 검토로 정답을 붙이고 `hard` 분할에서 규칙·n-gram·분류기를 나란히 채점한다.

**Architecture:** 생성기 맨 끝에 `hard_calls` 단계를 더한다. 이 단계는 기존 데이터를 읽기만 해서 슬롯을 고르고 `ax_calls`에 `hc-` 행을 붙인다. 녹취는 기존 LLM 작업 흐름(`llm_jobs`)으로 OpenAI Batch가 채운다. 최종 라벨은 `evaluation/hard_labels.py`가 `ground_truth/hard_call_labels.json`에 쓰고, 빌드가 적재해 생성 라벨을 덮어쓴다.

**Tech Stack:** Python 3 · sqlite3 · openai SDK(Upstage 갈래와 같음) · anthropic SDK · scikit-learn(기존 n-gram) · pytest

**Spec:** `docs/superpowers/specs/2026-11-11-hard-calls-design.md`

## Global Constraints

- 기존 `c-` 통화, `emr_*`·`fin_*` 행, 기존 라벨·분할, 기존 LLM 캐시는 하나도 바뀌지 않는다. 확인 방법: `make data`가 `transcript`·`document`·`chart` 작업을 0건 낸다.
- 새 단계는 `build.STAGES` 맨 끝(`master_values` 뒤)에 두고 `stage_rng(seed, "hard_calls")`를 쓴다.
- `call_uid`는 `hc-0001`부터다. 기존 행 id를 다시 매기지 않는다.
- 상수(종류, 건수, 허용 목적, 최소 건수, 날짜 창 60일, 표본 30)는 `generator/master.py`나 `evaluation/metrics.py`에만 둔다. 값을 직접 쓰지 않는다(CLAUDE.md).
- verify 검사는 생성 코드보다 먼저 쓴다(TDD).
- `hard`는 다음에 들어가지 않는다: silver 재료·검증, dev·test, 판정·블라인드 풀, `prompt_gate`, `tune`, 자동 개선, few-shot, Qwen 재료, 골드 점검.
- 생성 프롬프트에는 예시 문구가 없다. 샘플 확인과 사람 승인 뒤에는 고정한다. 그 뒤 재생성은 검증기 실패 때만 한다.
- 과금 명령은 실행 전에 건수와 금액을 보여 주고 사람 승인을 받는다. `--split test`는 쓰지 않는다.
- 주석·독스트링은 한국어로, 근거 문서 절을 가리킨다. `git add`는 파일 이름으로 한다. 커밋 메시지는 `<영역>: <무엇>`이다.

## Review Focus

1. `hc-` 행이 기존 평가 경로로 샌다(silver 재료, dev 채점, 프롬프트 관문 모수). 이러면 `hard`를 빼고 잰 기존 숫자가 소리 없이 바뀐다. → Task 1 테스트
2. 최종 라벨을 만든 뒤 `make data`를 다시 돌리면 생성 라벨로 되돌아간다. → Task 5 테스트
3. 같은 예약 이벤트에 `c-`와 `hc-` 통화가 함께 붙는다. → Task 2 verify 검사
4. OpenAI 배치에서 일부가 만료·오류로 빠지면, 빠진 건은 저장하지 않고 다음 채우기의 대상으로 남아야 한다. → Task 3 테스트
5. verify의 기존 통화 검사가 `hc-`를 `c-` 규칙으로 검사해 거짓 위반을 낸다. 반대로 `hc-`를 아예 검사하지 않는 경우도 문제다. → Task 1·2 테스트

---

### Task 1: `hard` 분할과 정답 표, 기존 경로 분리

**Files:**
- Modify: `ground_truth/schema.sql`. `truth_call_labels.split` CHECK에 `'hard'`를 넣고, 새 표 `truth_hard_calls`를 만든다.
- Modify: `generator/verify/calls.py`. `_calls`는 `c-`만 읽는다. `check_eval_pool`은 풀 밖 `c-`만 silver로 본다.
- Modify: `generator/verify/*.py` 중 `ax_calls`를 직접 읽는 곳(27곳, `grep -n ax_calls generator/verify/*.py`). 곳마다 `c-`로 좁힐지, 둘 다 볼지(예약 이벤트 1건 규칙, 통화 없음 전제)를 정하고 주석으로 남긴다.
- Test: `tests/generator/test_hard_calls.py`(새), `tests/evaluation/test_call_baselines.py`(있으면 거기에)

**Interfaces:**
- Produces: `truth_hard_calls(call_uid TEXT PRIMARY KEY, hard_type TEXT NOT NULL, half TEXT, label_source TEXT NOT NULL CHECK (label_source IN ('generated','agreed','human')))`
  - `half`는 `urgency_inverted`에서만 쓴다: `'calm_urgent'` | `'pain_not_urgent'`. 다른 종류는 NULL이다.
- Produces: `generator.verify.calls.HARD_PREFIX = "hc-"`

- [ ] **Step 1: 실패하는 테스트 작성**

```python
def test_hard_rows_stay_out_of_dev_scoring():
    # call_baselines.evaluate가 split='hard' 통화를 dev ids와 재료에 넣지 않는다
    calls = _toy_calls(silver=40, dev=5, hard=5)  # 테스트 파일 안의 도우미
    r = call_baselines.evaluate(calls, "dev", {"v": {u: calls[u]["gold"] for u in calls}})
    assert not any(u.startswith("hc-") for u in r["ids"])


def test_eval_pool_check_ignores_hard(tmp_db):
    # 풀 밖 hc- 행(split hard)이 있어도 check_eval_pool 위반 0
    ...
    assert check_eval_pool(conn, ctx) == []
```

- [ ] **Step 2:** `uv run pytest tests/generator/test_hard_calls.py -k "hard_rows or eval_pool" -v`
  - Expected: `check_eval_pool` 테스트가 FAIL한다(`hc-`가 silver가 아니라 위반). baselines 테스트는 PASS할 수 있다. 이미 `== 'silver'`로 거르기 때문이고, 고정용 테스트다.
- [ ] **Step 3:** 스키마, `_calls`, `check_eval_pool`, 그 밖의 `ax_calls` 직접 조회 범위를 고친다.
- [ ] **Step 4:** `uv run pytest tests/generator/test_hard_calls.py -v` 뒤 `make test`
  - Expected: 모두 PASS다. 아직 `hc-` 행이 없으므로 기존 테스트도 그대로 통과한다.
- [ ] **Step 5:** 커밋 `generator: hard 분할·truth_hard_calls 표, verify 통화 검사를 c-로 좁힘`

### Task 2: 슬롯 단계 `hard_calls` (녹취 없이 행·생성 라벨)

**Files:**
- Modify: `generator/master.py`. 상수를 더한다.
- Create: `generator/hard_calls.py`. 슬롯 고르기와 적재를 맡는다.
- Create: `generator/verify/hard_calls.py`. 검사를 등록한다(`generator/verify/__init__.py`).
- Modify: `generator/build.py`. `STAGES` 끝에 `("hard_calls", hard_calls.stage_slots)`를 더한다.
- Test: `tests/generator/test_hard_calls.py`

**Interfaces:**
- Produces (master)
  - `HARD_CALL_TYPES = ("indirect", "reversal", "urgency_inverted", "misleading_noise", "outbound_real")`
  - `HARD_CALLS_PER_TYPE = 50`
  - `HARD_URGENCY_HALVES = ("calm_urgent", "pain_not_urgent")`, 절반마다 25
  - `HARD_TYPE_PURPOSES: dict[str, tuple[str, ...]]`: 종류별 허용 목적. `outbound_real`은 `("confirm", "recall_q", "billing_q", "treatment_q")`, 모두 outbound다.
  - `HARD_RARE_MIN: int`: 희귀 목적이 허용되는 종류에서 종류별 최소 건수다.
  - `HARD_WINDOW_DAYS = 60`
  - `HARD_URGENT_SIGNS`: 정의 신호(붓기·출혈·파절·심한 통증)마다 담담한 표현의 사실 문구다. `HARD_NOT_URGENT_CUES`: "지금은 괜찮다"류 사실 문구다.
- Produces: `hard_calls.stage_slots(run, rng, fake) -> None`
  - `ax_calls`에 `hc-` 행을 넣는다(transcript NULL, has_transcript 0).
  - `truth_call_labels`에 `split='hard'` 생성 라벨을 넣는다.
  - `truth_hard_calls`에 `label_source='generated'`로 넣는다.
  - `run.state["hard_slots"]`에 슬롯 목록을 둔다. Task 4가 쓴다.
- Produces: `hard_calls.excluded_patients(conn) -> set[int]`
  - 장부 실수 환자(`truth_ledger_errors`), cancels 장면 환자, 생성기에서 "그 뒤 통화 없음"으로 고른 환자를 모은다. 해당 조건은 `grep -n "NOT EXISTS (SELECT 1 FROM ax_calls" generator/*.py`로 전수 확인한다.
  - `ground_truth/rag_qa.json` 질문·정답에 나오는 환자도 넣는다. 정답 `answer_doc_ids`의 차트 번호와 질문 속 차트 번호·이름으로 찾는다.
- Produces: `hard_calls.free_events(conn, purpose, start, end) -> list[dict]`
  - `c-`가 붙지 않은 예약 이벤트다. 표는 spec §3.3과 같다.

- [ ] **Step 1: verify 검사 먼저** (`generator/verify/hard_calls.py`, 독스트링이 규칙 설명)
  - `check_hard_counts`: 종류마다 50건, `urgency_inverted`는 절반마다 25건
  - `check_hard_uids`: `hc-0001`… 연속, 기존 `c-`와 겹치지 않음
  - `check_hard_window`: 통화가 TODAY 전 `HARD_WINDOW_DAYS`일 안이고 영업시간 안
  - `check_hard_purposes`: 종류별 허용 목적, `outbound_real`은 outbound
  - `check_hard_booking_events`: 예약류가 비어 있던 이벤트에 붙고 시각이 이벤트와 같음. `c-`와 `hc-`를 합쳐 이벤트당 1건
  - `check_hard_emergency_appt`: emergency 플래그가 켜진 `hc-`는 통화일 당일(통화 뒤) 또는 다음 진료일에 같은 환자 예약이 있음
  - `check_hard_excluded`: 제외 환자 없음
  - `check_hard_not_pooled`: `in_eval_pool`·`in_blind_pool` 0
  - `check_hard_urgency_labels`: `calm_urgent`는 urgent+emergency 1, `pain_not_urgent`는 normal+emergency 0
  - 각 검사에 위반 행을 만든 작은 DB로 실패하는 테스트를 하나씩 쓴다.
- [ ] **Step 2:** `uv run pytest tests/generator/test_hard_calls.py -v`
  - Expected: 검사 테스트는 PASS다(위반 DB에서 위반을 냄). `stage_slots` 테스트는 FAIL이다(정의 없음).
- [ ] **Step 3: `stage_slots` 테스트**
  - `test_stage_slots_counts_and_labels`: 가짜 생성(`generate(..., fake_llm=True)`) 뒤 위 verify 검사가 모두 `[]`
  - `test_existing_rows_unchanged`: `hard_calls`를 뺀 STAGES로 만든 DB와 비교해 `c-` 행, `emr_*`, `fin_*` 전 행이 같음
- [ ] **Step 4: `stage_slots` 구현**
  - 종류 안 목적 할당: `calls.quota(shares, n)`에 `CALL_PURPOSE_SHARE`의 허용 목적 부분을 넣고, 희귀 최소를 먼저 뺀다.
  - 예약류 후보가 모자라면 그 목적은 있는 만큼만 할당하고, 남는 몫은 같은 종류의 비예약 목적에 다시 나눈다.
  - 날짜는 `calls.call_day_weight`로 고른다. 사실(`used_fact`)은 기존 `calls._used_fact` 모양을 따른다.
  - 응급 슬롯은 "통화 뒤 그날·다음 진료일 예약"이 있는 환자·시각에서만 고르고, 그 예약 id를 `used_fact`에 넣는다.
- [ ] **Step 5:** `uv run pytest tests/generator/test_hard_calls.py -v` → PASS, `make test` → PASS
- [ ] **Step 6:** 커밋 `generator: 어려운 통화 슬롯 단계 hard_calls — 5종류 × 50, 기존 예약 이벤트에 붙이기`

### Task 3: 게이트웨이 OpenAI 갈래(즉시·Batch)와 작업별 별칭

**Files:**
- Modify: `shared/llm.py`
  - `_openai`: 한 시도다. `_upstage`와 같은 모양이고 `OPENAI_API_KEY`를 쓴다.
  - `submit_batch`·`batch_status`·`batch_results`: provider가 openai면 OpenAI Batch API를 쓴다(JSONL 업로드 → `/v1/chat/completions` 배치).
  - 상태는 `"ended"`로 맞춰 돌려준다. 실패·만료 건은 None이다.
- Modify: `config/llm.yaml`
  - `gen_hard: [gpt-6.1-sol]`, `hard_label: [claude-opus-5-5]`
  - 모델 `gpt-6.1-sol: { input: 2.00, output: 10.00, cache_read: 0.10, provider: openai }`
  - 기존 `gold_audit` 줄 주석의 "Solar"를 Qwen으로 고친다.
- Modify: `generator/llm_jobs.py`
  - `Task`에 `alias: str = ALIAS`를 더한다. 작업 줄에 `alias`를 쓰고 `fill_api`가 그 별칭으로 제출한다.
  - `fill_api(..., items: list[str] | None = None, use_batch: bool = True)`: `use_batch=False`면 건마다 `gateway.complete`로 즉시 채워 같은 결과 파일을 쓴다.
  - CLI에 `--items a,b,c`, `--no-batch`를 더한다.
- Modify: `Makefile` `llm-fill-api`: `ITEMS=`, `NO_BATCH=1`을 넘긴다.
- Test: `tests/shared/test_llm.py`, `tests/generator/test_llm_jobs.py`

**Interfaces:**
- Produces: `Gateway(..., openai_client=None)`. 테스트는 가짜 클라이언트를 넣는다.
- Produces: 작업 줄 `{"alias": "gen_hard", ...}`. 별칭이 없는 옛 작업 줄은 `"gen"`으로 본다.

- [ ] **Step 1: 실패하는 테스트**
  - `test_openai_complete_json`: 가짜 openai 클라이언트가 JSON 문자열을 내면 `LLMResult.ok`이고 data가 파싱되며, 비용이 단가대로 나온다.
  - `test_openai_batch_partial`: 가짜 배치 결과 3건 중 1건이 expired면 그 건은 `None`이고 나머지 2건은 ok다. 비용은 `BATCH_PRICE_SHARE`를 곱한 값이다.
  - `test_fill_api_uses_task_alias`: `gen_hard` 작업 줄이면 `submit_batch`에 `gen_hard`가 넘어간다.
  - `test_fill_api_items_no_batch`: `items=[...]`, `use_batch=False`면 그 건만 `complete`로 채운다.
- [ ] **Step 2:** `uv run pytest tests/shared/test_llm.py tests/generator/test_llm_jobs.py -k "openai or alias or items" -v` → FAIL
- [ ] **Step 3:** 구현한다. 착수 때 OpenAI 문서에서 `gpt-6.1-sol` Chat Completions의 `response_format: json_schema`와 Batch 지원을 확인한다(web-fetch).
  - 지원하지 않으면 `{"type": "json_object"}`로 보내고 기존 `problems()` 검증이 형식을 거른다(spec §3.5 설계자 결정).
- [ ] **Step 4:** 같은 테스트 → PASS, `make test` → PASS
- [ ] **Step 5:** 커밋 `chore: LLM 게이트웨이 OpenAI 갈래(즉시·Batch), 작업별 별칭·건 지정 채우기`

### Task 4: 녹취 작업 `hard_transcript`

**Files:**
- Create: `prompts/gen_hard_transcript.md`
  - 입력 형식과 대화 규칙(화자, 절대 날짜, 길이, `must_mention`, `avoid_words`)은 `gen_transcript.md`를 따른다.
  - 종류별 규칙을 둔다(`hard_type`, `half`). 예시 대화 문장은 넣지 않는다.
- Modify: `generator/hard_calls.py`
  - `TASK = Task(name="hard_transcript", prompt="gen_hard_transcript", schema=transcripts.SCHEMA, store=("transcript",), alias="gen_hard")`
  - `payload(slot) -> dict`, `checks(slot, names) -> dict`, `stage_transcripts(run, rng, fake) -> None`
- Modify: `generator/build.py`. `hard_calls`를 슬롯 단계 뒤 `("hard_transcripts", hard_calls.stage_transcripts)`로 나눈다.
- Test: `tests/generator/test_hard_calls.py`

**Interfaces:**
- Consumes: `run.state["hard_slots"]`(Task 2), `transcripts.SCHEMA`·`transcripts.duration_sec`·`transcripts.korean_date`, `llm_jobs.problems`
- Produces: `checks()` 키는 기존 `transcripts.checks`와 같은 형식이다. 기존 검증기가 읽을 수 있어야 한다. 종류별 규칙:
  - `reversal`: 다시 예약 제안 금지(`REBOOK_OFFER`)는 최종값이 `cancel`이고 `wants_appt`가 0일 때 **마지막 직원 줄들**에만 적용한다. reschedule의 원래·새 날짜는 최종 결과일 때만 `must_mention`에 넣는다.
  - `urgency_inverted`: `calm_urgent`는 신호 사실 문구가 `must_mention`이다. `avoid_patterns`로 센 통증 낱말(`아파|통증|응급|급해`)과 경계 표현(`것 같기도|모르겠는데`)을 막는다. `pain_not_urgent`는 통증 낱말 하나와 "지금은 괜찮다"류 문구가 `must_mention`이다.
  - inbound `document_req`: `avoid_patterns`에 `문자.{0,6}(드릴|보내)`를 넣는다(spec §3.3).
  - 응급 슬롯: 기존 예약 날짜·시각이 `must_mention`이다. 직원이 그 예약으로 내원을 안내한다.
- Produces: `sample_items(conn) -> list[str]`, CLI `python -m generator.hard_calls sample-items`(쉼표로 이어 출력)
  - 종류마다 4건(`urgency_inverted`는 절반마다 2건)을 uid 해시 순으로 고른다.
  - 테스트 `test_sample_items_per_type`: 20건이고 종류별 개수가 맞다.

- [ ] **Step 1: 실패하는 테스트**
  - `test_checks_reversal_cancel`: 최종 cancel 슬롯의 checks가 전체가 아닌 마지막 직원 줄에만 `REBOOK_OFFER`를 건다. 그래서 중간에 "옮길까요"가 있는 녹취는 통과하고, 끝에 "다시 잡아 드릴까요"가 있으면 실패한다.
  - `test_checks_calm_urgent_blocks_pain_words`: "아파요"가 있는 녹취는 `problems()`가 위반을 낸다.
  - `test_checks_doc_req_no_text_promise`: "준비되면 문자 드릴게요"는 위반이다.
  - `test_fake_build_fills_hard_transcripts`: 가짜 생성 뒤 `hc-` 250건이 `has_transcript=1`이다.
- [ ] **Step 2:** `uv run pytest tests/generator/test_hard_calls.py -k "checks or fills" -v` → FAIL
- [ ] **Step 3:** 프롬프트, `payload`, `checks`, `stage_transcripts`를 구현한다. 들여온 녹취는 `ax_calls.transcript`, `has_transcript=1`, `duration_sec`에 넣는다.
- [ ] **Step 4:** 같은 테스트 → PASS, `make test` → PASS
- [ ] **Step 5:** 커밋 `generator: 어려운 통화 녹취 작업 — 종류별 검증 규칙, 예시 문구 없는 프롬프트`

### Task 5: 최종 라벨 적재

**Files:**
- Modify: `generator/build.py`
  - `generate(..., hard_labels_path: Path | None = None)`: 파일이 있으면 `hc-`의 `truth_call_labels` 행을 최종 라벨로 덮어쓰고 `truth_hard_calls.label_source`를 바꾼다.
  - `TRUTH_JSON`에 `truth_hard_calls`(`hard_calls.json`)를 더한다.
- Modify: `generator/__main__.py`. `make data`가 `ground_truth/hard_call_labels.json`을 넘긴다. `rag_qa`와 같은 방식이다.
- Test: `tests/generator/test_hard_calls.py`

**Interfaces:**
- Produces: `hard_call_labels.json` 행 `{call_uid, labels: {primary_purpose, accepted_purposes, counterpart, urgency, emergency, complaint, price_inquiry, wants_appt, appt_confirmed, has_todo}, label_source: "agreed"|"human"}`
  - 파일에 없는 `hc-`는 생성 라벨 그대로다.
  - 파일의 `call_uid`가 DB에 없으면 빌드가 실패한다. 오래된 파일이라는 뜻이다.

- [ ] **Step 1:** `test_final_labels_survive_rebuild`: 최종 라벨 파일로 두 번 생성해도 `hc-0001` 라벨이 파일 값이다. `test_stale_final_labels_fail`: 없는 uid면 `ValueError`다. → FAIL 확인
- [ ] **Step 2:** 구현 → PASS, `make test` → PASS
- [ ] **Step 3:** 커밋 `generator: 어려운 통화 최종 라벨 적재 — 재생성해도 유지`

### Task 6: 정답 라벨 도구 `evaluation/hard_labels.py`

**Files:**
- Create: `evaluation/hard_labels.py`, `prompts/hard_label/v1.md`
  - 라벨 정의는 `prompts/classify/v2/labels.md`를 읽어 그대로 넣는다. 복사하지 않는다.
  - 출력 스키마는 분류기 라벨 항목과 같다.
- Create: 기록 `evaluation/hard_labels.jsonl`, 호출 로그 `evaluation/hard_labels_llm.jsonl`. `app.sqlite`에 쓰지 않는다.
- Modify: `evaluation/metrics.py`. `HARD_AGREED_SAMPLE = 30`
- Modify: `Makefile`. `hard-label`(과금, 이어 돎, Batch), `hard-review`(사람)
- Test: `tests/evaluation/test_hard_labels.py`

**Interfaces:**
- `label(conn, gateway, path) -> int`: 기록에 없는 `hc-`를 Opus Batch로 라벨한다. 입력은 녹취와 방향뿐이다. 처리 안 된 건은 다음 실행 대상으로 남긴다.
- `compare(conn, records) -> dict[str, list[str]]`: `{call_uid: 엇갈린 항목}`. 비교는 `shared.call_labels.same_value`, 대상 항목은 `ITEMS`다.
- `review(conn, path, read=input, write=print) -> None`: `gold_audit.audit`와 같은 방식이다. 대상은 엇갈린 통화와 확정 통화 표본(`HARD_AGREED_SAMPLE`, sha256 순)이다.
  - 두 라벨을 가·나로 출처를 가려 보여 준다. 섞는 순서는 uid 해시로 정한다.
  - 선택지는 가 / 나 / 둘 다 아님이다. 둘 다 아니면 enum 목록에서 직접 고른다.
  - 통화마다 저장한다.
- `finalize(conn, path, out) -> dict`: 리뷰가 끝난 뒤 `ground_truth/hard_call_labels.json`을 쓴다. 일치율(항목·종류별), 사람이 고른 쪽 분포, 표본 결과를 돌려주고 출력한다.

- [ ] **Step 1: 실패하는 테스트** (가짜 게이트웨이와 작은 DB)
  - `test_label_input_has_no_answer`: 요청 user 메시지에 생성 라벨, `hard_type`, 시나리오 문자열이 없다.
  - `test_compare_urgency_only_urgent`: normal과 low는 같은 값으로 본다.
  - `test_review_blinds_source`: 출력에 "생성", "Opus" 낱말이 없다.
  - `test_finalize_requires_review_done`: 남은 검토가 있으면 `SystemExit`다.
- [ ] **Step 2:** `uv run pytest tests/evaluation/test_hard_labels.py -v` → FAIL
- [ ] **Step 3:** 구현 → PASS, `make test` → PASS
- [ ] **Step 4:** 커밋 `eval: 어려운 통화 정답 라벨 — Opus 독립 라벨·가린 사람 검토·최종 JSON`

### Task 7: `hard` 채점

**Files:**
- Modify: `evaluation/classification.py` `_calls`. `truth_hard_calls`를 LEFT JOIN해 `hard_type`·`half`를 더한다.
- Modify: `evaluation/call_baselines.py` `evaluate`
  - `split == "hard"`면 ids는 `split == 'hard'` 통화다. 재료는 지금과 같은 silver 재료다.
  - 결과에 `by_type: {hard_type: 같은 모양}`을 더하고, `urgency_inverted`는 `by_half`도 더한다.
- Modify: `evaluation/eval.py`. `hard` 묶음과 `_render_classification`에 표를 더한다. 전체 줄, 종류별 줄, 절반 줄, "고르게 나눈 셋" 주석이 들어간다.
- Test: `tests/evaluation/test_call_baselines.py`, `tests/evaluation/test_eval.py`

- [ ] **Step 1:** `test_hard_split_scores_only_hard`, `test_hard_by_type_sums_to_total` → FAIL 확인
- [ ] **Step 2:** 구현 → PASS, `make test` → PASS
- [ ] **Step 3:** 커밋 `eval: hard 분할 채점 — 규칙·n-gram·분류기, 종류별 줄`

### Task 8: 실행 (사람 승인 지점마다 멈춤)

이 Task는 코드가 아니라 실행 순서다. 과금 단계 앞에서는 건수와 금액을 보여 주고 멈춘다.

- [ ] **Step 1:** `make data`
  - Expected: `hard_transcript 250건` 작업만 나온다. `transcript`·`document`·`chart` 등 기존 작업은 0건이다. 0건이 아니면 멈추고 원인을 찾는다(Global Constraints).
- [ ] **Step 2: 샘플 (과금 50센트 안팎, 승인)**
  - `uv run python -m generator.hard_calls sample-items`로 종류당 4건, `urgency_inverted`는 절반당 2건, 모두 20건을 고른다.
  - `make llm-fill-api TASK=hard_transcript ITEMS=<목록> NO_BATCH=1` → `make llm-import`
- [ ] **Step 3: 샘플 확인**
  - 블라인드 판독: Opus 서브에이전트에게 녹취 20건과 `labels.md`만 주고 라벨을 받는다. 생성 라벨과 비교한다.
  - 사람에게 녹취 20건, 생성 라벨, 판독 결과를 보여 준다.
  - 문제가 있으면 프롬프트와 검증기를 고치고 Step 2로 돌아간다. 승인되면 프롬프트를 고정한다.
- [ ] **Step 4: 전체 녹취 (과금 약 $4, 승인)**
  - `make llm-fill-api TASK=hard_transcript` → `make llm-import` → `make data`를 빈 건이 0이 될 때까지 돈다.
  - 그다음 `make verify`. Expected: 위반 0이다.
- [ ] **Step 5: Opus 라벨 (과금 약 $3, 승인)**
  - `make hard-label` → `make hard-review`(사람) → finalize → `make data` → `make verify`
- [ ] **Step 6: 분류 (과금 1달러 미만, 승인)**
  - `make classify V=<활성 버전>`. 대상은 `hc-` 250건뿐이다.
- [ ] **Step 7: 검색 (과금 몇 센트, 승인)**
  - `make index EMBEDDING=voyage-4`와 `EMBEDDING=text-embedding-3-small` → ③ 검색 평가. Hit@5가 바뀌면 그대로 적는다.
- [ ] **Step 8: 평가 보고**
  - `uv run python -m evaluation.eval`로 dev와 hard를 낸다. test는 돌리지 않는다.

### Task 9: 문서

**Files:**
- Modify: `docs/치과 AX - 가상 데이터 생성 설계.md`. §5.1 단계 순서(`hard_calls`·`hard_transcripts`), 생성 규칙, verify 표, 알려진 흠(응급 통화와 그날 진료 기록)
- Modify: `docs/치과 AX - 평가 설계.md`. `hard` 분할과 채점, §6 정답 저장소 `truth_hard_calls`, 정답 라벨 절차
- Modify: `docs/치과 AX - 앱 설계.md` §7. OpenAI 갈래와 배치
- Modify: `docs/치과 AX - 기능 정의.md` §9. 항목을 지운다.
- Modify: `docs/치과 AX - AI 실험 기록.md`. 새 절 §25: 문제 → 원인 → 시도 → 결과 → 한계
- Modify: `docs/치과 AX - 개발자 소개.md`. 숫자
- Modify: `CLAUDE.md` 명령 목록. `make hard-label`(과금, 이어 돎), `make hard-review`

- [ ] **Step 1:** 문서를 고친 뒤 `uv run pytest tests/generator/test_doc_sync.py -v` → PASS
- [ ] **Step 2:** 커밋 `docs: 어려운 통화 평가셋 — 생성 규칙·hard 분할·실험 기록`
