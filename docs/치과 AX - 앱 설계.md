---
title: 치과 AX - 앱 설계
related:
  - "[[치과 AX - 기능 정의]]"
  - "[[치과 AX - 가상 데이터 생성 설계]]"
  - "[[치과 AX - 평가 설계]]"
created: 2026-10-20
updated: 2026-10-22
tags:
  - 치과AX
  - 앱
---

> **앱이 데이터를 어떻게 읽고 쓰나.** backend 코드가 읽는 문서 — 테이블·도구 시그니처·고정 쿼리·기술 선택. 앱 `ax_*` 테이블은 목적과 핵심 제약만 적는다 — DDL(컬럼)은 앱 착수 시 `shared/schema.sql`에 쓰고 그쪽이 원본. 무엇을 만드나(화면·API·완료 기준)는 [[치과 AX - 기능 정의]], 뭘 측정하나는 [[치과 AX - 평가 설계]].
> **앱이 읽는 입력**: `emr_*`(미니 EMR, 읽기 전용)와 생성기가 적재한 `ax_calls`·`ax_documents`. 컬럼은 `shared/schema.sql`, 의미는 [[치과 AX - 가상 데이터 생성 설계]] §2·§3. 앱은 생성 규칙(분포·추첨·정답)에 의존하지 않는다 — 그건 정답 힌트다.

## 1. 통화 분류·판정

`ax_calls` 정의는 [[치과 AX - 가상 데이터 생성 설계]] §3.

**분류기 입력 = `transcript` + `direction`만** (환자·번호·시각은 정답 힌트. 함수 시그니처 `classify(transcript, direction)`로 강제).

| 테이블 | 목적 · 핵심 제약 |
|---|---|
| `ax_call_classifications` | 모델별 요약·할 일·분류 labels(9항목)·`status(ok/failed)`. UNIQUE `(call_id, model_alias, prompt_version)`. **`prompt_version`은 (프롬프트 텍스트 × 모델 쌍)의 버전** — 모델만 바꿔도 올림(`v3-final`). 대시보드는 버전마다 `model_actual` 쌍 표기 |
| `ax_call_disagreements` | A·B 분류 불일치(9항목 중 하나라도 다를 때만 1행). PK `(call_id, prompt_version)`, `is_core`(목적·급함·응급 불일치) |
| `ax_judgments` | 사람 판정. `judged_at`은 실제 시계 |
| `ax_blind_votes` | 블라인드 투표. 좌·우 분류 ID(FK → classifications) + `choice(left/right/tie/both_wrong)`. 좌우 배치·승률·큐 규칙은 [[치과 AX - 평가 설계]] §2.4 |

## 2. LLM 로그

**`ax_llm_calls`**: 게이트웨이 호출 1건 = 1행(`task`·모델·`prompt_version`·`fallback_used`·토큰·지연·비용·성공, `ref_type/ref_id` 다형 참조 — verify에서 타입별 조인으로 검사, 입력 메시지 덤프 파일 경로 — verify의 분류기 입력 누출 검사 대상). `fallback_used` 건의 집계 제외는 [[치과 AX - 평가 설계]] §1.

## 3. 검색

**색인**(`make index`): 청킹 300~500자, 겹침 50, 마크다운 헤더 우선. 문자는 건별이 아니라 **환자별 월 스레드 1청크**(`[환자: 김○○(C-00001) | 2026-07] 07-03 예약 확인 … 07-10 답장: …`). 색인 코드는 앱 쪽 — `make data` 10단계가 생성 직후 1회 호출하고, 붙여넣기 통화는 앱이 색인.

**`ax_chunks`**: id는 출처에서 결정적으로(`'doc:12:3'` / `'call:481:0'` / `'thread:C-00001:2026-07'`), `source_type(document/call/message_thread)`, 필터용 실컬럼(`chart_no`·`event_date`·`doc_type`).
`text`는 **출처 prefix 의무**: 문서 `제목 > 섹션`, 통화 `[환자: 김○○(C-00001) | 2026-07-03 | 수신]`(환자 없는 통화는 `[상대: 보험사 | 2026-07-03 | 수신]`), 스레드 `[환자: … | 2026-07]`.
- FTS5 `ax_chunks_fts(text)` — `unicode61` + 질의 측 `원본어절* OR 조사제거어절*`. 복합어는 적재 시 공백 정규화
- 벡터 `ax_chunk_vectors(chunk_id = ax_chunks.id, embedding)` — sqlite-vec. 필터는 `ax_chunks` 실컬럼으로 먼저 거르고 남은 행만 `vec_distance_cosine` 전수 계산(정확 검색 — 근사 인덱스 없음, top-k가 필터에 깎이지 않음). 다중 조건 필터도 SQL 그대로

**`ax_search_logs`**: 검색·챗 1회 = 1행. 도구 호출(`args`는 Pydantic 정규화 후 기록)·`retrieved_ids`·답변·`cited_ids`·`is_eval` — 평가 설계 §3 채점의 입력. 답변은 `{text, cited_ids[]}` 구조화 출력으로 강제(모델이 근거 청크를 스스로 명시 — ④ `used_facts`와 같은 패턴), 화면의 출처 = `cited_ids`. 데이터형(`query_*`)만 쓴 답변은 `cited_ids`가 비어도 됨. 검색 화면의 AI 요약도 같은 출력.

**도구 5개 — 시그니처 고정** (평가 설계 §3.2 `expected_args` 채점의 기준)

| 도구 | 인자 | 반환 |
|---|---|---|
| `search_docs` | `query`, `source_types?[]`, `date_from?/date_to?`, `patient_chart_no?`, `top_k?=5` | 청크 목록 |
| `query_patient` | `chart_no?` / `name?` (하나 필수) | `{matches[], summary?}` — 동명이인이면 summary 없이 후보만. summary: 기본정보(메모 포함)·마지막 내원·잔액·다음 예약·가족 리콜·그해 스케일링 급여 사용 여부(`emr_claims`) |
| `query_schedule` | `date_from, date_to`(단일 날짜는 같은 값), `status?, type?, has_unpaid_balance?, is_new_patient?`(`has_unpaid_balance` = 그 예약일 개원 전 잔액 > 0 — 정의 원본. `is_new_patient` = 창 안에서 처음 등록한 환자 — 생성 설계 §2.2) | 예약 목록 + `{count, by_status, by_type}` |
| `query_claims` | `date_from?/date_to?, chart_no?, procedure_code?, status?` | 청구 목록 + `{count, total_amount, by_status}`. 확정 월은 결과·사유 포함 |
| `refuse` | `reason`(`not_found` / `restricted` — 비공개 행정정보) | 없음. 호출 시 턴 종료, 앱이 고정 거절 문구 표시 |

- 날짜 전부 `YYYY-MM-DD`. 도구 description에 TODAY 값(§6) 예시를 넣음
- 호출 상한 6회/질문. 경계: 건수·목록 → `query_*`, 절차·규정·과거 대화 → `search_docs`, 근거가 없거나 비공개 정보면 `refuse`
- **검색은 버킷 분리**: `document` 상위 25 + `call/message_thread` 상위 25 따로 → 리랭커 → `manual_results` / `case_results`. `event_date ≤ TODAY` 강제

## 4. 준비표·리포트

**`ax_prep_sheets`**: 날짜·환자·예약별 준비표 행(`id` PK). `facts json` + `facts_hash`(같으면 LLM 생략) + `suggestion{text, used_facts[{key,value}]}` + `suggestion_valid` + 사람 채점 `human_rating(useful/ok/wrong)`·`rated_by`("다시 만들기"로 제안이 바뀌면 둘 다 초기화). UNIQUE `(sheet_date, patient_id, appointment_id)`
리포트 테이블(슬롯 ⑥)은 슬롯 착수 시 추가.

## 5. 심평원 청구 점검·삭감 대응 (슬롯 ⑤, 조건부)

테이블(코드명 `claim_review`)은 슬롯 착수 시 추가 — 받은 전자 심사결과 레코드는 `emr_`(병원 청구 프로그램이 받는 데이터), 점검 결과·조정 건별 검색·판단은 `ax_`. ⑤-3 조정 사유 검색은 §3 검색 구조를 재활용한다. 단계는 [[치과 AX - 기능 정의]] §5 ⑤.

## 6. 설정·작업·고정 쿼리

**`ax_settings(key, value)`**: `active_prompt_version`, `active_models`(A·B의 model_actual), `daily_usd_limit`, `judge_upto`(판정 큐 개방 범위 — 기본 = 판정 풀의 dev 건수, test 판정 시 풀 전체. dev 건수 = 판정 풀의 ⌈풀 크기/2⌉ = `eval_sort_order` 앞부분(평가 설계 §2.1의 계약이라 앱은 생성 규칙을 몰라도 됨). 분할 규칙은 [[치과 AX - 평가 설계]] §2.1)
**기준일 TODAY** (원본은 여기): `config` `today` 고정. 앱에서 바꾸는 기능은 두지 않는다 — 데이터가 그날 개원 전 스냅샷이라 앱에서만 옮기면 예약 상태·기록·청구 구간이 어긋난다. 바꾸려면 config를 고치고 재생성
**`ax_jobs`**: 30초 넘는 작업(배치 분류·준비표 재생성)의 상태·진행률. 프론트는 폴링

**판정 큐 "다음 건"**: 대상 = `judge_status` pending/skipped · 판정 풀이면 `eval_sort_order ≤ judge_upto` · 활성 버전 분류 2행 모두 `ok`. 순서 = 건너뛴 건 뒤로 → 핵심 엇갈림(`is_core`) 먼저 → 붙여넣기 통화 먼저 → `eval_sort_order`.
**블라인드 큐**: [[치과 AX - 평가 설계]] §2.4.

## 7. 기술 선택

| 항목 | 선택 |
|---|---|
| 스택 | **Next.js + TypeScript** / **FastAPI** / **SQLite**(FTS5 + **sqlite-vec** 벡터) |
| DB | SQLite 1파일(`app.sqlite`에 FTS5·벡터까지). 벡터 접근은 `VectorStore` 인터페이스 뒤, 두 번째 구현 안 함 |
| 벡터 저장소 결정 근거 | **규모**: 청크 수천~1만(문서 ~100 + 통화 `config` `calls_with_transcript` + 월 스레드 + 차트 서술 — 차트는 `source_type` 추가와 함께 [[치과 AX - 기능 정의]] §9 이후) — 질의마다 전수 계산해도 ms 단위라 근사 인덱스(ANN)가 필요 없다. **한 파일**: `ax_chunks`·FTS5·벡터가 같은 DB라 하이브리드 검색과 메타데이터 필터가 SQL 조인 하나, 두 저장소 동기화(삭제·재색인 불일치)가 없다. **fixture**: `make fixture`·`save-demo`가 파일 하나 복사. Chroma는 이 규모에서 얻는 것(ANN·서버 모드)이 없고 저장소만 둘로 늘린다 |
| Chroma로 옮길 때 | 쉽다 — 벡터는 `VectorStore`(`upsert(ids, vectors, metadata)`·`query(vector, filter, k)`) 뒤에만 있고, 청킹·임베딩·`ax_chunks`·FTS5·리랭커는 그대로. 구현 하나 추가 + 필터용 메타데이터를 Chroma에 복사하고 다중 조건 필터는 Chroma `where`(`$and`)로 번역("SQL 조인 하나" 이점은 사라짐) + `make index` 재실행(임베딩은 다시 계산하거나 `ax_chunk_vectors`에서 복사). 옮길 조건: 청크가 수십만 이상으로 늘어 전수 계산이 느려질 때 |
| FTS | SQLite FTS5 — 토크나이저·질의 방식은 §3 |
| 리랭커·임베딩 | **비교 후 확정**: `text-embedding-3-small` vs `bge-m3`, 로컬 `bge-reranker-v2-m3` vs Cohere. dev 질문 10개로 비교 |
| LLM (원본은 여기) | 게이트웨이 모듈 1개(`shared/`, 생성기 7·9단계도 경유), 내부 LiteLLM 라이브러리(서버 아님). 이름(A/B)으로 호출, 폴백, 호출 로그(`ax_llm_calls`), 일일 비용 상한(금액은 [[치과 AX - 기능 정의]] §7). 프로바이더 조합 확정 + 구조화 출력·도구 호출 테스트 |
| 프롬프트 | `prompts/<task>/<version>.md` — 파일명이 곧 `prompt_version`. task: `classify`(분류·요약), `chat`(챗 시스템), `prep`(준비표 제안), `gen_transcript`·`gen_document`(생성기). 버전 파일은 고치지 않고 새 버전으로 추가(비교 재현성) — 모델만 바꿔도 같은 텍스트로 새 버전(§1 `prompt_version` 정의 — A·B 쌍이 있는 classify). 쓰는 버전: classify는 `ax_settings.active_prompt_version`, chat·prep은 코드에 고정, 생성기는 생성기 코드에 고정(LLM 캐시 키의 `prompt_template_hash` = 그 파일 해시) |
| 테스트 fixture | **`make fixture`**: small 데이터 + fake 게이트웨이로 판정 풀 전체 × 2(A/B) 분류 적재 + 해시 기반 결정적 fake 임베딩으로 벡터 테이블까지 → `tests/fixtures/` 커밋. 테스트는 복사본 사용. CI 키 없이 판정 큐·검색이 동작 |
| 사람 데이터 보존 | `export-human/import-human`: 판정은 `hash(transcript)`로만 복원 — 미스 시 버리고 "복원 실패 N건" 리포트(전사가 바뀐 건에 `call_uid`로 붙이면 다른 대화에 라벨이 붙음). 투표는 `(hash, left_model, left_version, right_model, right_version, choice)` |
| 시간 | DB naive KST, API `+09:00`. `ax_*` 활동은 실제 시계. 기준일은 §6 |
| 실행 | `make index / fixture / save-demo / reset-demo / set KEY=… VALUE=…`(설정 변경, 허용 키는 [[치과 AX - 기능 정의]] §7) |

## 8. 미결

- [ ] 리랭커·임베딩 모델
- [ ] 프로바이더 조합 A/B — 생성기 7단계(통화 전사) 전에 필요
- [ ] 홈 화면·네비 (와이어프레임에서)
- [ ] `prep_facts` fact 키 목록 (④ 착수 시 — 후검증·회귀 테스트의 기준)
