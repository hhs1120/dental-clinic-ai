---
title: 치과 AX - 앱 설계
related:
  - "[[치과 AX - 기능 정의]]"
  - "[[치과 AX - 가상 데이터 생성 설계]]"
  - "[[치과 AX - 평가 설계]]"
created: 2026-10-20
updated: 2026-10-20
tags:
  - 치과AX
  - 앱
---

> **앱이 데이터를 어떻게 읽고 쓰나.** backend 코드가 읽는 문서 — 테이블·도구 시그니처·고정 쿼리·기술 선택. 무엇을 만드나(화면·API·완료 기준)는 [[치과 AX - 기능 정의]], 뭘 측정하나는 [[치과 AX - 평가 설계]].
> **앱이 읽는 입력**: `emr_*`(미니 EMR, 읽기 전용)와 생성기가 적재한 `ax_calls`·`ax_documents`. 컬럼은 `shared/schema.sql`, 의미는 [[치과 AX - 가상 데이터 생성 설계]] §2·§3. 앱은 생성 규칙(분포·추첨·정답)에 의존하지 않는다 — 그건 정답 힌트다.

## 1. 통화 분류·판정

`ax_calls` 정의는 [[치과 AX - 가상 데이터 생성 설계]] §3.

**분류기 입력 = `transcript` + `direction`만** (환자·번호·시각은 정답 힌트. 함수 시그니처 `classify(transcript, direction)`로 강제).

**`ax_call_classifications`**: `id, call_id, model_alias(A/B), model_actual, prompt_version, summary, todo json, labels json{purpose, counterpart, urgency, has_todo, wants_appt, appt_confirmed, price_inquiry, complaint, emergency}, status(ok/failed), error?, llm_call_id, created_at`
UNIQUE `(call_id, model_alias, prompt_version)`. **`prompt_version`은 (프롬프트 텍스트 × 모델 쌍)의 버전** — 모델만 바꿔도 올림(`v3-final`). 대시보드는 버전마다 `model_actual` 쌍 표기.

**`ax_call_disagreements`**: PK `(call_id, prompt_version)`, `fields json, is_core`(목적·급함·응급 불일치)

**`ax_judgments`**: `id, call_id, labels json, memo?, judged_by, judged_at(실제 시계), elapsed_sec, shown_model?`(판정 중 요약을 펼쳤으면 어느 모델)

**`ax_blind_votes`**: `id, call_id, classification_left_id, classification_right_id(FK → classifications), choice(left/right/tie/both_wrong), judged_by, voted_at`
좌우 배치·승률 집계 규칙은 [[치과 AX - 평가 설계]] §2.4.

## 2. LLM 로그

**`ax_llm_calls`**: `id, task, model_alias, model_actual, prompt_version, input_path?(메시지 덤프 파일), fallback_used, prompt_tokens, completion_tokens, latency_ms, cost_usd, success, error?, ref_type, ref_id, created_at`
`fallback_used` 건의 집계 제외는 [[치과 AX - 평가 설계]] §1. `ref_type/ref_id`는 다형 참조 — verify에서 타입별 조인으로 검사.

## 3. 검색

**색인**(`make index`): 청킹 300~500자, 겹침 50, 마크다운 헤더 우선. 문자는 건별이 아니라 **환자별 월 스레드 1청크**(`[환자: 김○○(C-00001) | 2026-07] 07-03 예약 확인 … 07-10 답장: …`). 색인 코드는 앱 쪽 — `make data` 10단계가 생성 직후 1회 호출하고, 붙여넣기 통화는 앱이 색인.

**`ax_chunks`**: `id text PK('doc:12:3' / 'call:481:0' / 'thread:C-00001:2026-07'), source_type(document/call/message_thread), source_id, chunk_index, chart_no?, event_date?, doc_type?(실컬럼 — 필터용), text, meta json`
`text`는 **출처 prefix 의무**: 문서 `제목 > 섹션`, 통화 `[환자: 김○○(C-00001) | 2026-07-03 | 수신]`(환자 없는 통화는 `[상대: 보험사 | 2026-07-03 | 수신]`), 스레드 `[환자: … | 2026-07]`.
- FTS5 `ax_chunks_fts(text)` — `unicode61` + 질의 측 `원본어절* OR 조사제거어절*`
- Chroma 컬렉션 `chunks`(id = `ax_chunks.id`, metadata flat 복사). 단일 조건 필터는 Chroma `where`, id 선필터는 후보 수백 이하일 때만

**`ax_search_logs`**: `id, query, tools_used json[{tool, args(Pydantic 정규화 후), ok}], retrieved_ids json, answer, recommended_queries json, is_eval, llm_call_ids json, created_at`

**도구 5개 — 시그니처 고정** (평가 설계 §3.2 `expected_args` 채점의 기준)

| 도구 | 인자 | 반환 |
|---|---|---|
| `search_docs` | `query`, `source_types?[]`, `date_from?/date_to?`, `patient_chart_no?`, `top_k?=5` | 청크 목록 |
| `query_patient` | `chart_no?` / `name?` (하나 필수) | `{matches[], summary?}` — 동명이인이면 summary 없이 후보만. summary: 기본정보(메모 포함)·마지막 내원·잔액·다음 예약·가족 리콜·그해 스케일링 급여 사용 여부(`emr_claims`) |
| `query_schedule` | `date_from, date_to`(단일 날짜는 같은 값), `status?, type?, has_unpaid_balance?, is_new_patient?`(창 안에서 처음 등록한 환자 — 생성 설계 §2.2) | 예약 목록 + `{count, by_status, by_type}` |
| `query_claims` | `date_from?/date_to?, chart_no?, procedure_code?, status?` | 청구 목록 + `{count, total_amount, by_status}`. 확정 월은 결과·사유 포함 |
| `refuse` | `reason`(`not_found` / `restricted` — 비공개 행정정보) | 없음. 호출 시 턴 종료, 앱이 고정 거절 문구 표시 |

- 날짜 전부 `YYYY-MM-DD`. 도구 description에 `TODAY=2026-12-01` 예시 하드코딩
- 호출 상한 6회/질문. 경계: 건수·목록 → `query_*`, 절차·규정·과거 대화 → `search_docs`, 근거가 없거나 비공개 정보면 `refuse`
- **검색은 버킷 분리**: `document` 상위 25 + `call/message_thread` 상위 25 따로 → 리랭커 → `manual_results` / `case_results`. `event_date ≤ TODAY` 강제

## 4. 준비표·리포트

**`ax_prep_sheets`**: `id, sheet_date, patient_id, appointment_id, facts json, facts_hash, suggestion json?{text, used_facts[{key,value}]}, suggestion_valid?, llm_call_id?, generated_at`. UNIQUE `(sheet_date, patient_id, appointment_id)`
**`ax_reports`**(슬롯 ⑥): `id, kind(daily/weekly/monthly), period_start, period_end, metrics json, body_by_model json, pdf_path?, generated_at`

## 5. EOB (슬롯 ②, 조건부)

**`ax_eob_documents`**: `id, kind(monthly/per_patient), form_variant(A/B/C), pdf_path, page_count, degraded_type?, status`
**`ax_eob_lines`**: `id, eob_doc_id, page, line_no, chart_no, service_date, procedure_code, tooth_no, patient_id?, claim_amount, approved_amount, paid_amount, adjust_code?, extracted_by, llm_call_id`
**`ax_eob_reconciliations`**: `id, claim_id?, eob_line_id?, result(confirmed/amount_mismatch/denied/unmatched_claim/unmatched_eob), diff_amount?, reviewed, review_decision?(ok/fix_ledger/appeal), reviewed_at?`

## 6. 설정·작업·인덱스·고정 쿼리

**`ax_settings(key, value)`**: `active_prompt_version`, `active_models`(A·B의 model_actual), `today_override`, `daily_usd_limit`, `judge_upto`(판정 큐 개방 범위. 기본 = dev 크기(large 150), test 판정 시 풀 전체(large 300))
**`ax_jobs(id, kind, status, step, progress, result_json, error, created_at, updated_at)`**: 30초 넘는 작업(배치 분류·준비표 재생성)

| 테이블 | 인덱스 |
|---|---|
| `ax_llm_calls` | `(task, created_at)`, `(ref_type, ref_id)` |
| `ax_chunks` | `(source_type, source_id)` |

**판정 큐 "다음 건"**
```sql
SELECT c.* FROM ax_calls c
LEFT JOIN ax_call_disagreements d ON d.call_id = c.id AND d.prompt_version = :active_version
WHERE c.judge_status IN ('pending', 'skipped')
  AND (c.in_eval_pool = 0 OR c.eval_sort_order <= :judge_upto)
  AND (SELECT COUNT(*) FROM ax_call_classifications k
       WHERE k.call_id = c.id AND k.prompt_version = :active_version AND k.status = 'ok') = 2
ORDER BY (c.judge_status = 'skipped') ASC, COALESCE(d.is_core, 0) DESC,
         (c.source != 'generated') DESC, c.eval_sort_order ASC
LIMIT 1;
```
**블라인드 큐**: 현재 `active_version`의 분류 ID 쌍에 대한 `ax_blind_votes`가 `NOT EXISTS`이고 `judge_status='done'`인 건(판정 전에 요약을 보는 앵커링 방지).

## 7. 기술 선택

| 항목 | 선택 |
|---|---|
| DB | SQLite 1파일 + **Chroma PersistentClient**. `VectorStore` 인터페이스 뒤, 두 번째 구현 안 함 |
| FTS | `unicode61` + 조사 제거 OR prefix. 복합어는 적재 시 공백 정규화 |
| 리랭커·임베딩 | **비교 후 확정**: `text-embedding-3-small` vs `bge-m3`, 로컬 `bge-reranker-v2-m3` vs Cohere. dev 질문 10개로 비교 |
| LLM | 게이트웨이 모듈 1개(`shared/`, 생성기 7·9단계도 경유), 내부 LiteLLM 라이브러리. 프로바이더 조합 확정 + 구조화 출력·도구 호출 테스트 |
| 테스트 fixture | **`make fixture`**: small 데이터 + fake 게이트웨이로 분류 300×2 적재 + 해시 기반 결정적 fake 임베딩으로 Chroma까지 → `tests/fixtures/` 커밋. 테스트는 복사본 사용. CI 키 없이 판정 큐·검색이 동작 |
| 사람 데이터 보존 | `export-human/import-human`: 판정은 `hash(transcript)`로만 복원 — 미스 시 버리고 "복원 실패 N건" 리포트(전사가 바뀐 건에 `call_uid`로 붙이면 다른 대화에 라벨이 붙음). 투표는 `(hash, left_model, left_version, right_model, right_version, choice)` |
| 시간 | DB naive KST, API `+09:00`. `ax_*` 활동은 실제 시계 |
| 실행 | `make index / fixture / save-demo / reset-demo` |

## 8. 미결

- [ ] 리랭커·임베딩 모델
- [ ] 프로바이더 조합 A/B — 생성기 7단계(통화 전사) 전에 필요
- [ ] 홈 화면·네비 (와이어프레임에서)
