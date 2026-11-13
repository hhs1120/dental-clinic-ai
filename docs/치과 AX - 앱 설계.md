---
title: 치과 AX - 앱 설계
related:
  - "[[치과 AX - 기능 정의]]"
  - "[[치과 AX - 가상 데이터 생성 설계]]"
  - "[[치과 AX - 평가 설계]]"
created: 2026-10-20
tags:
  - 치과AX
  - 앱
---

> **앱이 데이터를 어떻게 읽고 쓰나.** backend·frontend 코드가 읽는 문서 — 테이블·도구 시그니처·고정 쿼리·기술 선택·화면. 앱 `ax_*` 테이블은 목적과 핵심 제약만 적는다 — DDL(컬럼)은 `shared/app_schema.sql`이 원본이다(§1). 무엇을 만드나(화면·API·완료 기준)는 [[치과 AX - 기능 정의]], 뭘 측정하나는 [[치과 AX - 평가 설계]].
> **앱이 읽는 입력**: `data/source.sqlite`(생성기 출력 — `emr_*`·`fin_*`·생성기가 적재한 `ax_calls`·`ax_documents`)를 읽기 전용으로 붙여서(SQLite ATTACH) 읽는다 — `emr_`·`fin_`을 쓰지 않는다는 절대 규칙이 연결 방식으로 지켜진다. 앱이 쓰는 것은 전부 `data/app.sqlite`. 컬럼은 `shared/source_schema.sql`, 의미는 [[치과 AX - 가상 데이터 생성 설계]] §2·§3. 앱은 생성 규칙(분포·추첨·정답)에 의존하지 않는다 — 그건 정답 힌트다.

## 1. 통화 분류·판정

`ax_calls` 정의는 [[치과 AX - 가상 데이터 생성 설계]] §3. 앱 테이블은 통화를 `call_uid`로 가리킨다(생성 통화 `c-0481`, 새로 추가한 통화 `p-0001`) — 붙인 DB로는 외래 키를 걸 수 없고 새로 추가한 통화는 앱 DB에 있다.

**분류기 입력 = `transcript` + `direction`만** (환자·번호·시각은 정답 힌트. 함수 시그니처 `classify(gw, transcript, direction, version)`로 강제 — 나머지 인자는 게이트웨이·프롬프트 버전).

eval.py가 읽는 칸(아래 분류·판정기 판정·투표, §2 LLM 로그)은 `shared/app_schema.sql`이 정한다 — 앱은 그 칸을 그대로 쓰고 나머지 칸만 착수 때 추가한다. 스키마는 앱이 DB를 열 때 맞춘다 — 빈 DB면 전체, 앞선 DB면 없는 테이블·인덱스·칸만 더하고 있던 행은 그대로 둔다(NOT NULL에 기본값 없는 칸은 더하지 못해 멈춘다, `backend/db.py` `init_schema`). RAG 평가는 앱이 `evaluation/rag.py`의 `search`·`chat`을 `backend/eval_hooks.py`의 `make_app(app_db, source_db)`로 내준다.

| 테이블 | 목적 · 핵심 제약 |
|---|---|
| `ax_call_classifications` | 분류기(모델 A)의 요약·할 일·분류 labels(9항목)·`status(ok/failed)`. 라벨 호출과 요약·할 일 호출이 따로라 호출마다 `model_actual`·`llm_call_id`(라벨)와 `summary_model_actual`·`summary_llm_call_id`(요약) — 둘 중 하나라도 실패면 `failed`. UNIQUE `(call_uid, model_alias, prompt_version)`. **`prompt_version`은 (프롬프트 텍스트 × 모델)의 버전** — 모델만 바꿔도 올림(`vN-final`). 대시보드는 버전마다 `model_actual` 표기 |
| `ax_judgments` | 사람 판정(운영 구간 엇갈림·표본). `judged_at`은 실제 시계. `note`(메모, 선택) |
| `ax_model_judgments` | 판정기(모델 B) 판정. 사람 판정과 섞지 않으려고 테이블을 나눈다. UNIQUE `(call_uid, model_alias, prompt_version)` |
| `ax_call_disagreements` | 엇갈림 `(call_uid, a_version, b_version)` 1행(급함은 urgent 여부만 비교 — [[치과 AX - 평가 설계]] §2.5), `is_core` = 목적·급함·응급 불일치. 어느 쪽이든 폴백이면 표시(`fallback`). 분류기 할 일 목록이 있는데 분류기 `has_todo` = 0이면 라벨이 같아도 1행(`todo_mismatch`). 계산은 `backend/disagreements.py` |
| `ax_judge_state` | 판정 상태(pending / skipped / done) — 생성 DB가 아니라 앱 DB에 둔다. 채우는 때는 §6 판정 큐 |
| `ax_pasted_calls` | 새로 추가한 통화(통화 화면 "새 통화 추가") — 앱이 만든 통화라 앱 DB. 목록·분류·판정에서는 생성 통화와 합쳐 보인다(`backend/calls.py`) |
| `ax_todo_checks` | "오늘 할 일" 할 일 체크 — 분류 행의 할 일 항목 번호 단위로 끝낸 파트·시각. 되돌리면 지운다 |
| `ax_call_checks` | 오늘 화면 확인할 통화의 "확인함" — 통화 단위로 확인한 파트·시각(실제 시계), 다시 확인하면 덮어쓴다. 다시 뜨는 조건은 [[치과 AX - 기능 정의]] §4 앱 구성(`backend/calls.py` `review_calls`) |
| (정확도 뷰) | 정확도 표의 집계 — `backend/metrics.py`. `agreement`: 분류 버전 × 항목 9개 행(모델 vs 판정기 `k/n`, 모수는 분류·판정기 모두 ok인 dev 판정 풀, 급함 3값). `blind_rates`: 버전 쌍마다 LLM 쌍 비교·사람 투표의 B 나음·A 나음·비슷·둘 다 틀림·좌우 선택과 같은 쌍에서 둘의 일치(모수는 dev 블라인드 통화). `human_share`: 판정 큐 대상 중 사람이 볼 몫(엇갈림 또는 표본) `k/n`. 테이블이 아니라 고정 쿼리 |
| `ax_call_followups` | 후속 에이전트 처리안 — 분류 행 단위 1행(분류 버전이 바뀌면 새 처리안, 에이전트 프롬프트만 바꾸면 `make followups REDO=1`이 오늘 할 일 통화의 옛 버전 처리안을 지우고 다시 낸다): 처리 종류(`shared/followup.py` `ACTIONS`)·요지·근거·실행 id·`done`/`failed`와 이유. 할 일 체크(`ax_todo_checks`)와 따로 — 처리안이 체크를 대신하지 않는다. 쓰는 곳은 `backend/followup.py` |
| `ax_model_blind_votes` | LLM 쌍 비교 판정 — 좌·우 분류 ID·쌍 비교 판정기(`blind_judge`) 버전·두 번 판정(좌우 바꿈)과 합친 `choice`·두 호출의 LLM 호출 id(폴백 제외용). 규칙은 [[치과 AX - 평가 설계]] §2.4 |
| `ax_blind_votes` | 사람 블라인드 투표(대상은 [[치과 AX - 평가 설계]] §2.4). 좌·우 분류 ID(FK → classifications) + `choice(left/right/tie/both_wrong)`. 좌우 배치·승률·큐 규칙은 [[치과 AX - 평가 설계]] §2.4 |

## 2. LLM 로그

**`ax_llm_calls`**: 게이트웨이 호출 1건 = 1행. 칸은 `shared/app_schema.sql`, 쓰는 곳은 `backend/harness/store.py` `SqliteLlmLog`. `ref_type/ref_id`는 다형 참조이고 타입별 조인 검사는 [[치과 AX - 가상 데이터 생성 설계]] §5.3 '참조'다. 입력 덤프 칸은 두지 않는다 — 에이전트 실행의 입력·모델 응답·도구 결과는 `ax_agent_steps`에 남고 `ref_type='agent_run'`(`ref_id` = `ax_agent_runs.id`)이 그 실행을 가리킨다. `ref_type`은 `agent_run`, `claim`(⑤ 일반 호출 — `ref_id` = 청구 id), `batch`(배치 결과 — `ref_id`는 배치 안 요청의 `custom_id`, 하루 상한 밖은 [[치과 AX - 기능 정의]] §7)를 쓰고 그 밖의 일반 호출(분류·판정 — 결과 행의 `llm_call_id`가 거꾸로 가리킴, 단순 RAG 기준선)은 비운다. Voyage 검색 호출(질의 임베딩·리랭커)도 1행씩 남겨(`model_alias` `voyage`, `backend/usage.py`, 단가는 `config/llm.yaml`) 하루 상한이 센다 — 사용량은 검색기(요청)마다 모아 챗과 기록 검색이 각각 남긴다. 색인(`make index`) 임베딩은 넣지 않고 실행 끝에 비용만 출력한다. `fallback_used` 건의 집계 제외는 [[치과 AX - 평가 설계]] §1.

## 3. 검색

**색인**(`make index EMBEDDING=<모델>` — 모델 필수, 벡터가 없거나 글이 바뀐 청크만 계산): 크기는 `backend/rag_index.py` `MAX_CHARS`. 문서는 마크다운 헤더 우선 + 글자 겹침 `OVERLAP`, 통화는 발화 사이에서만 자르고 다음 조각을 직전 발화로 시작한다(`split_dialog`, 직전 발화 상한 `CARRY_MAX`, 화자 표기가 없으면 글자 기준). 문자는 건별이 아니라 **환자별 월 스레드 1청크**(`[환자: 김○○(C-00001) | 2026-07] 07-03 예약 확인 … 07-10 답장: …`). 색인 코드는 앱 쪽이고 `app.sqlite`에 쓴다 — 생성기는 `source.sqlite`만 쓰고, `make index`는 `make data`와 따로 실행한다([[치과 AX - 가상 데이터 생성 설계]] §5.1). 색인 상태는 `ax_chunks`로 판단한다(생성 테이블에 색인 칸을 두지 않음). 새로 추가한 통화는 앱이 색인. 청크 id의 통화 부분은 `call_uid`.

**`ax_chunks`**: id는 출처에서 결정적으로(`'doc:12:3'` / `'call:c-0481:0'` / `'thread:C-00001:2026-07'`), `source_type(document/call/message_thread/chart_note/doc_draft)`(id `'chart:<treatment_id>'`, 진료 1건 = 1청크. 게시한 문서 초안은 `'draft:<id>:<순번>'`이고 문서 버킷에 들고 RAG 평가 색인에서 뺀다), 필터용 실컬럼(`chart_no`·`event_date`)과 카드 표시용 `doc_type`.
`text`는 **출처 prefix 의무**: 문서 `제목 > 섹션`(섹션이 제목과 같으면 제목만), 통화 `[환자: 김○○(C-00001) | 2026-07-03 | 수신]`(환자 없는 통화는 `[상대: 보험사 | 2026-07-03 | 수신]`), 스레드 `[환자: … | 2026-07]`, 차트 `[차트 C-00001 | 2026-07-03 | 스케일링]`(환자 이름 없음 — 케이스 내용으로 찾고 차트 번호로 환자에 닿는다). 차트는 서술 끝 괄호에 다른 표현을 더한다 — 치아 번호의 부위, 그 차트 시술의 별칭, 정해 둔 동의어, 기간의 다른 표기(`backend/rag_index.py` `chart_terms`). 다른 시술 이름은 따라가지 않는다 — 받지 않은 시술이 붙는다. 괄호는 검색용이라 화면과 챗 모델에는 내지 않는다.
- FTS5 `ax_chunks_fts(chunk_id, text)` — `unicode61` + 질의 측 `원본어절* OR 조사제거어절*`.
- 벡터 `ax_chunk_vectors(chunk_id = ax_chunks.id, model, embedding)` — sqlite-vec, float32 BLOB, 임베딩 모델마다 한 벌. 필터는 `ax_chunks` 실컬럼으로 먼저 거르고 남은 행만 `vec_distance_cosine` 전수 계산(정확 검색 — 근사 인덱스 없음, top-k가 필터에 깎이지 않음). 다중 조건 필터도 SQL 그대로

**`ax_search_logs`**: 챗·검색 요약 1회 = 1행. 칸은 `shared/app_schema.sql`(`tool_calls`의 `args`는 Pydantic 정규화 후 기록). eval.py는 이 표를 읽지 않는다(챗 채점은 `chat()` 반환값 — 평가 설계 §3.3). 챗은 끝내는 도구 `answer(text, cited_ids[])`로 답한다(모델이 근거 청크를 스스로 명시 — ④ `used_facts`와 같은 패턴), 화면의 출처 = `cited_ids`. `cited_ids`가 비어도 되는 것은 `query_patient`·`query_schedule`·`query_claims`를 쓴 답변뿐.
- **인용 확인**(챗 후처리 — 훅은 도구 호출만 보므로 답을 낸 직후 코드가 검사): `cited_ids`가 이번 질문의 `retrieved_ids`(검색 결과 + `query_charts` 결과) 밖이면 같은 루프에서 한 번 다시 답하게 하고(다시 검색할 수 있다), 또 어긋나면 `cited_ids`를 비우고 상태 `cite_failed`로 끝낸다(마지막 호출이 `refuse`가 아니라 문서 초안 입력에 들지 않는다). 막힐 때마다 `ax_rule_counts`에 `chat.cite_outside`로 더한다. ③ 문서 초안이 같은 검사를 쓰고, ⑤-4는 인용 문장이 실제로 있는지까지 본다(평가 설계 §5)
- **숫자 후검증**(인용 확인 다음, 같은 다시 답하기): 답의 숫자가 이번 질문의 도구 결과·질문·오늘 날짜·날짜와 필터 인자(검색어·`top_k`·`limit`은 빼고 — `backend/chat.py` `EVIDENCE_SKIP`)에 있거나 도구 결과 안 목록의 행 수여야 한다(`backend/verify.py` — 합계·날짜 계산은 막는다). 막힐 때마다 `chat.number_unverified`
- **상태**(`ax_search_logs.status`, 칸은 `shared/app_schema.sql`): `answered` / `refused` / `cite_failed` / `number_failed` / `call_limit` / `cost_limit` / `budget` / `failed`(`running`은 실행 중). 화면에 답을 내는 것은 `answered`뿐, `refused`는 "문서함에 없음", 나머지는 `backend/chat.py` `FAILED_TEXT`. 챗은 `POST /chat` SSE(이벤트 `step`·`answer`·`error`). 모델이 도구 없이 글로 끝내면 하네스가 한 번 되돌려 `answer`·`refuse`로 내게 한다(`AgentSpec.end_tools`) — 또 글이면 `failed`
- **기록 검색 요약**(`POST /search/summary`, `backend/search_summary.py`): 같은 조건으로 다시 검색해 그 결과만 입력·인용 기준으로 쓴다 — 옛 공지는 빼고(목록에는 "옛 버전"으로 남는다), 차트는 괄호 없는 원문. 하네스 없이 게이트웨이 한 번이고 출력은 `{text, cited_ids[], refused}`. 검색어에 답하거나 결과를 세지 않고 결과가 말하는 바를 정리하며, 문장마다 끝에 `[n]`(= `cited_ids[n-1]`)을 붙인다. 검사: 인용이 비었거나 입력 밖이거나 번호가 1..`len(cited_ids)`를 빠짐없이 쓰지 않으면 `search_summary.cite_outside`, 끝에 번호가 없는 문장은 `search_summary.cite_missing`(숫자 뒤·마침표 뒤 마침표 — `2026. 3. 1.`·`...` — 에서는 문장을 나누지 않는다), 표지를 뗀 본문에 검색어·필터 값(기간·환자번호)·입력에 없는 숫자는 `search_summary.number_unverified`(행 수 허용 없음). 어긋나면 한 번 다시 부르고, 또 어긋나면 `cite_failed`·`number_failed`. 상태는 `answered`·`refused`·`cite_failed`·`number_failed`·`budget`·`failed`이고 끝난 뒤 1행을 쓴다(예상 밖 예외도 `failed` 행을 남기고 다시 던진다, `kind='search_summary'`, `tool_calls` = 정규화한 검색 인자 + 프롬프트 버전). 같은 `tool_calls`·`retrieved_ids`의 `answered`·`refused` 행이 있으면 다시 부르지 않는다. 입력이 비면(옛 공지뿐) 부르지 않고 기록도 남기지 않는다. 화면 문구는 `REFUSED_TEXT`·`FAILED_TEXT`

**도구 6개 — 시그니처 고정** (평가 설계 §3.2 `expected_args` 채점의 기준, 코드 `backend/query_tools.py`·`backend/rag_search.py` `search_tool`)

| 도구 | 인자 | 반환 |
|---|---|---|
| `search_docs` | `query`, `source_types?[]`, `date_from?/date_to?`, `patient_chart_no?`, `top_k?`(기본값은 `SearchArgs`) | `{manual_results, case_results}` — 버킷별 청크 목록 |
| `query_patient` | `chart_no?` / `name?` (하나 필수) | `{matches[], summary?}` — 동명이인이면 summary 없이 후보만. summary: 기본정보·주의사항(기록일 ≤ 기준일 < 끝나는 날, `emr_patient_alerts`)·마지막 내원·잔액·다음 예약·가족 리콜·그해 스케일링 급여 사용 여부(`emr_claims`)·열린 치료계획 항목 |
| `query_schedule` | `date_from, date_to`(단일 날짜는 같은 값), `status?, type?, has_unpaid_balance?, is_new_patient?` | 예약 목록 + `{count, by_status, by_type}` |
| `query_claims` | `date_from?/date_to?, chart_no?, procedure_code?, status?` | 청구 목록 + `{count, total_amount, by_status}`. 확정 월은 결과·사유 포함 |
| `query_charts` | `procedure`(이름·별칭), `prev_procedure?`, `min_gap_days?/max_gap_days?`(앞 시술과의 간격, 일), `same_tooth?`, `date_from?/date_to?`, `chart_no?`, `limit?`(기본값은 `ChartArgs`) | `{count, results[]}` — `id`(`chart:<treatment_id>`, 인용 가능)·차트 번호·진료일·시술·치아·상병·서술·`prev{procedure, performed_at, gap_days}`. 환자 이름 없음. 이름은 띄어쓰기 무시 — 이름 일치 → 별칭 일치(여러 시술에 걸린 별칭은 결과 없이 `candidates`만) → 이름에 그 말이 든 시술 전부(`matched`, 예: 발치). 없는 이름은 `unknown` |
| `refuse` | `reason`(`not_found` / `restricted` — 비공개 행정정보) | 없음. 호출 시 턴 종료, 앱이 고정 거절 문구 표시 |

- `query_schedule` 칸
  - `has_unpaid_balance` = 그 예약일 개원 전 잔액 > 0 (정의 원본)
  - `is_new_patient` = 창 안에서 처음 등록한 환자 (생성 설계 §2.2)
  - `count`는 조건에 맞는 행 전체(`status`를 주지 않으면 취소·변경 포함)
  - `by_status`는 변경으로 대체된 원래 예약(새 예약의 `rescheduled_from_id`가 가리키는 `cancelled`)을 취소와 따로 `rescheduled`로 센다 — 예약 변경은 취소가 아니다
- 날짜 전부 `YYYY-MM-DD`. 도구 description에 TODAY 값(§6) 예시를 넣음
- 호출 상한 6회/질문(`backend/chat.py` `CHAT_MAX_TOOL_CALLS` — 채점 상수와 같음을 테스트가 비교), `answer`는 세지 않는다. 넘는 호출은 훅 `chat.call_limit`이 막고 그 자리에서 실행을 끝낸다(`AgentSpec.stop_rules`, 상태 `call_limit`). 경계: 건수·목록 → `query_*`, 기간·간격 조건이 있는 차트 → `query_charts`, 절차·규정·과거 대화·차트 내용 → `search_docs`, 근거가 없거나 비공개 정보면 `refuse`
- **검색은 버킷 분리**: `document`와 `call/message_thread/chart_note`를 따로 찾아 각각 상위 `CANDIDATES`(`backend/rag_search.py`) → 리랭커 → `manual_results` / `case_results`. `event_date ≤ TODAY` 강제. 리랭커 뒤에 옛 공지(같은 제목에서 시행일이 이른 것) 자리에 후보의 최신 공지를 올리고 옛 공지를 바로 뒤에 둔다. 날짜 필터를 주면 시행일 없는 문서는 빠지고, 환자 필터를 주면 환자 기록만 찾는다

**문서 초안 에이전트의 입력** = `ax_search_logs` 중 `refuse(not_found)`로 끝난 질문. 오늘 화면에는 초안이 오른다. **문서함**은 `ax_documents`를 그대로 보여 주고, 공지는 최신 버전만(`GET /documents`·`/documents/{id}`, `backend/documents.py` — 최신 규칙은 검색의 옛 공지 정렬과 같다. 본문에는 같은 제목 공지의 모든 버전과 `latest_id`). `source.sqlite`만 읽어 색인 없이도 열린다.

## 3b. 에이전트 하네스

코드 위치는 `backend/harness/`. 다섯 축의 뜻은 [[치과 AX - 기능 정의]] §4 에이전트 하네스. 랭체인·랭그래프를 쓰지 않고 직접 구현한다 — 권한·훅·자동 처리·저장소 주입처럼 코드로 강제할 부분이 하네스의 본체라 프레임워크가 덜어 주는 것은 도구 루프와 재개 정도이고, 체크포인트를 따로 두면 상태가 `ax_agent_steps`와 둘로 갈라진다. 게이트웨이(§7.1)는 그대로 쓰고 모든 LLM 호출은 그곳을 지난다(임베딩·Voyage 리랭커는 밖 — 비용 기록은 §2).

**상태·제약 테이블**
| 테이블 | 목적 · 핵심 제약 |
|---|---|
| `ax_agent_runs` | 실행 1건 — 에이전트 이름·대상·상태·비용·`prompt_version`·부모 실행(운영 에이전트가 부른 경우)·실행 잠금(하트비트). 재개해도 비용과 호출 수는 누적하고 `prompt_version`은 처음 값을 쓴다 |
| `ax_agent_steps` | 단계 — 입력·모델 응답·도구 호출과 결과 전체(재생·재개용)·실행 뒤 막힌 건의 원래 결과·훅 결과. 쓰기와 같은 트랜잭션으로 기록한다. 화면의 조사 과정은 이 표를 읽는다 |
| `ax_auto_actions` | 자동 처리 — 작업 종류·대상·결과값(증빙 id·계정 과목 등)·이유·실행 id·되돌린 사람·시각. UNIQUE 중복 방지 키(작업 종류·대상·기간 — ⑨ 안내처럼 반복되는 일은 기간이 키. 기간은 코드가 계산하고, LLM은 실행 대상에만 요청할 수 있다). 되돌리면 앱 상태만 복원하고 해당 큐로 돌려보낸다 — 사람이 되돌린 건은 다시 처리하지 않는다. 조건이 바뀌어 시스템이 푸는 경우(① 자동 종결 — 활성 버전이 바뀌어 조건을 못 넘음)는 행을 지워, 다시 조건을 넘으면 다시 처리된다. 판정 풀 통화는 대상이 아니다 |
| `ax_patient_flags` | 운영 에이전트가 묶은 환자 — 신호 목록·이유·실행 id. UNIQUE (환자·날짜). 오늘 화면이 읽는다 |
| `ax_outbox` | ⑨ 자동 안내 기록(2단계 — [[치과 AX - 기능 정의]] §5 ⑨) — 환자·문구·실행 id·되돌린 상태. 실제 발송 없음. |
| `ax_doc_drafts` | ③ 문서 초안 — 원 질문·초안·근거·게시 결정. 게시한 초안은 문서함과 색인에 합쳐 보인다(`ax_documents`는 쓰지 않음) |
| `ax_failure_cases` | 재발 방지 — 출처(되돌리기·훅 차단·표본 오류·관문 탈락)·실행 id(문자열 참조, 외래 키 없음)·원인 분류(enum)·재현 테스트 경로·고칠 곳·승인 상태. 라벨·녹취 인용은 넣지 않는다(쓰기 전 검사는 [[치과 AX - 평가 설계]] §2.7) |
| `ax_rule_counts` | 규칙 id별 차단 누계 |

**에이전트별 도구** (호출 상한·세부 인자는 착수 시)
| 에이전트 | 도구 |
|---|---|
| ③ 챗 | §3 도구 6개 |
| ① 후속 | `query_patient`, `query_schedule`, `search_docs`(색인이 있을 때), `submit_followup`(끝내는 도구 — 인자는 `backend/followup.py` `SubmitArgs`) |
| ③ 문서 초안 | `search_docs` |
| ④ 조사 | `query_patient`, `search_docs` |
| ⑥ 조사 | `query_schedule`, `query_claims`, 지출 조회 |
| ⑧-1 원인 | 조회 도구(`backend/recon_tools.py` `LOOKUPS` — 그날 장부·카드 승인·정산 입금·통장·그날 내원·환자 미수·가족·구강용품 가격표·금액으로 다른 날 찾기, 읽기 전용), `submit_cause`(끝내는 도구 — 인자는 `backend/recon_agent.py` `SubmitArgs`, 근거 조회 번호를 훅 `recon.evidence`가 검사). `request_auto_action`은 주지 않는다 — 자동 마감은 코드 경로(`backend/recon_auto.py`) |
| ⑧-2 증빙 | 조회 도구(`backend/expense_tools.py` `LOOKUPS` — 증빙 검색·증빙 본문·거래처 표·계정 과목 목록, 그 출금의 확정 후보 범위만, 읽기 전용), `submit_match`(끝내는 도구 — 인자는 `backend/expense_agent.py` `SubmitArgs`, 과목·후보 범위·근거 조회 번호를 훅 `expense.evidence`가 검사). 입력에 규칙 답을 넣지 않는다. `request_auto_action`은 주지 않는다 — 자동 확정은 코드 경로(`backend/expense_auto.py`) |
| ⑨ 근거 | `query_patient`, `search_docs`, `request_auto_action` |
| 운영 | 신호 조회, 기능 에이전트 호출, `flag_patient` |
| 개선(`evaluation/`, 실행 기록은 `evaluation/runs/`) | 재료 오류 조회, 프롬프트 후보 쓰기, 관문 실행. `ground_truth/`·`reports/`·`data.json`을 직접 읽는 도구는 주지 않는다 |

- **쓰기**: 도구는 `source.sqlite`에 쓰지 못한다. 자동 처리는 LLM 경로(`request_auto_action`)와 코드 경로(① 자동 종결) 모두 하나의 `apply_auto_action()`을 지나고, 이 함수가 `auto_policy`와 작업별 조건을 검사해 `ax_auto_actions`에 쓴다
- **스케줄러**: `make nightly`가 코드 순서를 먼저 돌린다 — 소스 지문 비교(§7.3) → 분류 → 판정기 → 엇갈림 → 자동 종결 → 후속 에이전트. 두 모델 결과가 모두 있는 통화만 자동 종결을 본다. 판정 풀은 엇갈림까지만 돈다. 그다음 운영 에이전트를 시작하고, 운영 에이전트 착수 전에는 코드가 기능 에이전트를 고정 순서로 호출한다. 실행은 잠금으로 겹치지 않게 하고, `POST /agents/…/run`도 같은 잠금을 쓴다. 기준일은 §6대로 고정이고 실행 시각만 실제 시계다
- **신뢰 경계**: 시스템 지시와 외부 텍스트를 메시지 구조로 나누고, 도구 결과와 그 요약에 출처 표시를 유지한다. 외부 텍스트는 정책 검사의 직접 입력이 아니다. 다만 모델 라벨은 외부 텍스트에서 나와 자동 종결 조건의 입력이 되므로, 라벨과 최종 처리까지 시나리오로 잰다([[치과 AX - 평가 설계]] §5)
- **훅**: 하네스의 `before`·`after` 두 지점(`backend/harness/hooks.py` `Rule`). 금지 도구·인자 범위 검사(예: `source_types` 제한)와 후검증은 에이전트마다 쓰지 않고 여기에 등록한다. 쓰기 도구로 등록하지 않은 도구가 쓰면 하네스가 되돌리고 막는다(`tool.read_only`)
- **저장소 주입**: 하네스는 실행 기록·LLM 로그 저장소를 주입받는다. 앱은 `app.sqlite`에, 개선 에이전트는 `evaluation/runs/`에 쓴다. 앱으로 넘기는 범위는 [[치과 AX - 평가 설계]] §2.7
- **재생**: `ax_agent_steps`의 모델 응답·도구 결과로 실행을 다시 돌린다. 도구는 저장된 결과로 대신하고 호출·훅 결과·종료 상태를 비교한다. 훅·권한·인자·상한의 변화를 잡고 프롬프트 변화는 잡지 못한다. 저장된 응답을 `Gateway.complete()`와 같은 인터페이스로 내고(`backend/harness/replay.py`) 테스트 기록은 `tests/fixtures/agent_runs/`
- **재개**: 잠금이 만료되고 끝나지 않은 `ax_agent_runs`는 마지막 단계 다음부터 이어 간다. 쓸 때마다 잠금 소유자를 확인해 잠금을 뺏긴 쪽은 멈춘다. 일반 재시도의 호출 최악 시간은 잠금 만료보다 짧아야 한다(`backend/harness/llm.py`). 서버가 준 `Retry-After` 대기는 상한이 없어 이 계산 밖이고, 그동안 잠금이 만료되면 소유자 확인으로 멈춘다. 부모가 끊기면 끝나지 않은 자식은 부모 재개 때 함께 이어 간다. 모든 앱 쓰기는 중복 방지 키로 한 번만 들어간다
- **컨텍스트**: 에이전트별 결과 크기 상한은 `AgentSpec.result_limit`, 도구별 상한은 착수 시([[치과 AX - 기능 정의]] §9). 기능 에이전트는 운영 에이전트에 요약과 실행 id만 돌려준다
- **재발 방지**: 훅·정책 규칙은 코드에 id를 달아 두고, 차단이 날 때 `ax_rule_counts`에 더한다(자동 처리 거절은 `auto.<이유>`, 챗·검색 요약 규칙 id는 §3). 승인 규칙은 [[치과 AX - 평가 설계]] §2.7

## 4. 준비표·오늘 화면·리포트

**`ax_prep_sheets`**: 날짜·환자·예약별 준비표 행 — 칸은 `shared/app_schema.sql`. `facts`는 `backend/prep.py` `prep_facts` 행 그대로, `facts_hash`는 제안 입력(`llm_facts`)의 해시 — 해시·`prompt_version`·`model`이 같고 `suggestion_valid`면 다시 부르지 않는다. 예약당 1행이라 새 버전으로 만들면 덮어쓴다. 준비표·체어 보기가 같은 행을 쓴다

**오늘 화면**(`GET /today`, `backend/today.py` `sheet`): 쿼리로 모으고 LLM을 부르지 않는다. 숫자마다 그 화면과 같은 함수로 센다 — 예약·신환·주의·내원 확인 전은 `backend/prep.py` `prep_facts` 칩, 할 일·급한 통화는 `backend/calls.py` `today_board`, 확인할 통화는 같은 모듈 `review_calls`, 판정 대기는 `backend/judge_queue.py` `pending_count`(다음 건과 같은 대상), 재심 신청 기한 임박·청구 전 상병 경고·청구 전 AI 줄은 `backend/claims.py` `adjusted`(`soon` 칩)·`pre_claim`(경고 줄·`counts.ai_missing`), 일계표 불일치는 `backend/recon.py` `open_summary`(확정·자동 마감 안 된 건), 지출 증빙 확인은 `backend/expense.py` `open_summary`(기준일의 지난달). 미예약 환자 줄은 숫자 없는 고정 링크라 세지 않는다. 기준은 앱 기준일, 근거 없는 질문(`ax_search_logs`)만 실제 시계. 자동 처리·환자 묶음은 §3b 테이블을 읽는다(항목은 [[치과 AX - 기능 정의]] §4 앱 구성). **⑦ 브리핑**은 `ax_briefings`(날짜, 문장, 쓴 숫자, 후검증 통과 여부 — ④와 같은 틀), 슬롯 착수 시 추가.
**⑥ 리포트**(`backend/report.py`): 끝난 기간(종류별 수는 `backend/report.py` `COUNTS`, 진료일은 취소 뺀 예약이 있는 날)마다 숫자와 이전 기간 대비 변화 분해를 `metrics`(키 → 정수)로 모은다 — 재료비 과목은 `backend/expense.py` `sheet` 줄 과목. 인건비가 들어간 키는 `est.`를 포함하고(`is_est`) 화면이 `[추정]`을 붙인다. `ax_reports`(기간 종류·기간당 1행, 칸은 `shared/app_schema.sql`)는 해석을 만든 때의 `metrics`·해시와 점 목록을 저장한다 — 해시·버전·모델이 같고 통과한 기간은 다시 부르지 않고(`make report`), 지금 숫자와 해시가 다르면 `stale`. API `GET /report?scope=&period=`(모르는 기간 400, LLM을 부르지 않음). 튄 숫자 조사 결과는 3단계에서 근거 쿼리·실행 id와 함께 저장한다. 항목은 [[치과 AX - 기능 정의]] §5 ⑥.

## 5. 슬롯 테이블

**⑤ 청구 점검**(코드명 `claim_review`, `backend/claims.py`): 심사결과는 이미 `emr_claims`(결과·사유·통보일)에 있다. 조정 목록·칩·규칙 판단 제안과 청구 전 상병 확인 경고는 표마다 한 번 훑는 집합 쿼리라 청구 수와 무관하게 쿼리 수가 같다. 사유는 `adjust_note` 문구로 가리고 유형·기준 절·재심 기한 일수·허용 상병은 `source.ax_master_values`에서 읽는다. 앱 테이블은 사람 판단 `ax_claim_judgments`(청구 id·판단·파트·실제 시각, 다시 누르면 덮어씀). API `GET /claims`(조정·불능 청구 전부와 칩 집계 — 기한 지난 건 포함, 화면이 행의 `chips`로 거른다), `GET /claims/pre`(대기 청구 줄과 경고), `POST /claims/{claim_id}/judgment`(`{judgment, part}`, 모르는 값·조정/불능이 아닌 청구·기한 지남·상병 착오에 정당 외는 400). ⑤-2 AI 점검 결과는 `ax_claim_checks`(청구 id 하나에 한 행 — 입력·해시·결과·통과 여부·프롬프트 버전·모델, `backend/claim_check.py`), `make claim-check`(화면 범위, `SCOPE=all`이면 전 기간)와 `POST /jobs/claim-check`(`ax_jobs` `kind='claim_check'`, 준비표 갱신과 같은 꼴)로 만든다. ⑤-2b 추가 처치 누락은 `ax_claim_omissions`(칸은 `ax_claim_checks`와 같음, AI 원답을 저장하고 급여 거름은 읽을 때 코드가, `backend/claim_omission.py`) — `make claim-omission`(대기 줄, `SCOPE=all`이면 제출·대기 줄), 같은 갱신 작업이 2a 뒤에 돌린다. 행 `ai.omissions`는 2b 답만 쓴다. ⑤-3 서류 사유 차트 문장은 `ax_claim_evidence`(같은 칸, `backend/claim_evidence.py`, 저장·재사용·Batch는 2b와 함께 `backend/claim_store.py`) — `make claim-evidence`(기한 남은 서류 사유 건, `SCOPE=all`이면 전부), 같은 갱신 작업이 2b 뒤에 돌린다. `GET /claims` 행에 `standard`(그 사유의 청구 기준 절 — 문서 제목·절 제목·본문)·`chart_quotes`(서류 사유만), `GET /claims/standards?q=`는 청구 기준 절 5개(`backend/claim_search.py` — §3 검색을 `doc_type` claim_standard 절 청크로 좁힘, 색인이 없으면 503). `GET /claims/pre`는 행마다 `ai`와 `counts.ai`·`counts.ai_missing`·`counts.ai_checked`, `ai_at`·`ai_pending`(탭별 `pre`·`adjust` — `pre`는 2a·2b 다시 부를 대기 줄의 합집합)을, `GET /claims` 요건 사유 행은 `suggest_by`·`ai_evidence`를 더한다. ⑤-4 재심 사유서 초안은 `ax_claim_appeals`(청구 하나에 한 행 — `ax_claim_omissions`와 같은 칸 + 검토 확인·제출 결정의 파트·시각, `backend/claim_appeal.py`), `POST /claims/{claim_id}/appeal`(초안 쓰기·다시 쓰기, 일반 호출)·`/appeal/review`(`{part, checked}`)·`/appeal/submit`(`{part}`) — 재심 신청이 아닌 건·검토 없는 제출·제출 뒤 다시 쓰기는 400, 제출 결정한 건은 판단 기록도 400. `GET /claims` 행에 `appeal`(저장된 초안, 화면이 재심 신청일 때만 보인다). 단계는 [[치과 AX - 기능 정의]] §5 ⑤.

**⑧ 재무 대조**: 입력은 `fin_*`와 `emr_payments`(읽기 전용). ⑧-1은 `backend/recon_match.py`가 장부·금융 행을 표마다 한 번 읽어 불일치 건([[치과 AX - 기능 정의]] §5 ⑧-1, 키 = 관련 행 id 해시)을 내고, `backend/recon.py`가 건의 규칙 원인·일계표·확정을 맡는다(대조 경계는 앱 기준일 — 정산·현금 입금이 기준일 이후인 날은 대조 전). API `GET /finance/recon?month=`(모르는 달 400), `POST /finance/recon/{key}/confirm`(`{cause, part}`, 지금 탐지에 없는 건·모르는 원인 400). 조사 에이전트(`backend/recon_agent.py`, 3단계)는 건 키를 대상으로 하네스 실행을 남기고(`ax_agent_runs`·`ax_agent_steps` — 화면 줄·확정·자동 마감·채점이 모두 이 기록을 읽는다), 일계표 건에 지금 프롬프트 버전 결과(`agent` — 원인 1·2순위·근거 조회 번호·조사 과정 줄)와 자동 마감(`auto`)을 붙인다. `POST /finance/recon/{key}/investigate` — 조사 과정 줄(`line`)·결과(`done`)·오류(`error`) SSE, 지금 탐지에 없는 건 400, 지금 버전으로 이미 조사했거나 조사 중(잠금이 살아 있음)이면 409(다시 조사하지 않는다). 끊긴 조사(잠금 만료)·실패는 결과가 아니라 다시 조사한다. 일괄은 `make recon`. 확정의 제안 출처는 지금 버전 조사 결과가 있으면 에이전트 1순위. 앱 테이블은 사람 확정 2개 — `ax_recon_confirmations`(⑧-1: 불일치 건 키 단위 — 관련 `fin_` 거래 id·수납 id, 그때 보이던 제안의 출처(규칙·에이전트)와 원인, 고른 원인(같으면 맞음), 파트·실제 시각, 다시 누르면 덮어씀), `ax_expense_confirmations`(⑧-2: 출금 표·id 단위 — 출금처 글자 그대로(`label`), 증빙 id들(빈 목록 = 증빙 없음), 계정 과목, 파트·실제 시각, 다시 누르면 덮어씀). ⑧-2는 `backend/expense.py`가 규칙 기준선(`rule_match`·`rule_account` — 채점도 이 함수를 부른다)과 화면 집계·확정을 맡는다. 계정 과목 기본값은 이 출금의 확정 > 같은 `label`의 가장 최근 사람 확정 > 거래처 기본 과목(`source.ax_master_values` `expense_vendors`) 순이고, `by_item` 거래처(출금처 글자 안에 그 거래처 이름·가맹점명이 들어 있으면 — 결제대행사가 덧붙은 표기도)는 사람 확정을 건너뛴다([[치과 AX - 기능 정의]] §5 ⑧-2). 증빙 탐색 에이전트(`backend/expense_agent.py`, 4단계)는 출금 키(`expense.outflow_key`)를 대상으로 하네스 실행을 남기고, 지출 증빙 줄에 지금 프롬프트 버전 결과(`agent`)·규칙과 같음(`agree` — 증빙 집합과 거래처 표 기본 과목, `backend/expense_auto.py` `agree`)·자동 확정(`auto`)을 붙인다. 일괄은 `make expense-ai`. 확정 제한: 같은 증빙이 다른 출금의 살아 있는 확정(사람 확정 + 되돌리지 않은 자동 확정)에 있거나 기준일 이후 출금이면 막는다. 사람이 확정하면 그 출금의 자동 확정은 되돌린 것이 된다. API `GET /finance/expenses?month=`(모르는 달 400), `GET /finance/expenses/{table}/{outflow_id}/candidates`(`DOC_WINDOW` 안 증빙과 출금한 달 발행 증빙 전부 — 별칭 발행처·잘린 적요여도 고를 수 있게. 모르는 표·없는 출금 400), `POST /finance/expenses/{table}/{outflow_id}/confirm`(`{document_ids, account, part}`, 없는 출금·모르는 과목·후보 밖 증빙·확정 제한 400). 자동 처리는 §3b `ax_auto_actions`에 결과값과 함께 남기고 사람 확정 테이블에는 쓰지 않는다. ⑥ 재료비는 두 테이블을 합쳐 읽되 되돌린 건은 뺀다.

**⑨ 미예약 환자**(`backend/missed.py`): 사실·칩·점수는 쿼리 — 예약·청구·수납·문자·치료계획을 표마다 한 번 훑는 집합 쿼리라 환자 수와 무관하게 쿼리 수가 같다(④ 도우미는 예약 한 건 단위라 쓰지 않고 같은 규칙을 테스트로 대조). 앱 테이블은 연락 기록 `ax_missed_contacts`(환자·결과·수단·연락한 파트·실제 시각) — 환자마다 가장 최근 기록이 목록 빠짐·감점을 정하고 "연락함 숨기기"가 읽는다. API `GET /missed`(목록·칩별 인원수·순위 규칙 설명·한 번에 보이는 수 `PAGE`, 행의 `contact.active` = 최근 연락의 감점 기간이 남음), `POST /missed/{patient_id}/contacts`(`{result, channel, part}`, 모르는 값·미예약 환자가 아니면 400)

## 6. 설정·작업·고정 쿼리

**`ax_settings(key, value)`**: `active_prompt_version`, `pending_prompt_version`(관문을 넘고 재분류를 기다리는 classify 버전 — `make improve`가 쓰고 전환 때 비움, 자동 개선 착수 때), `source_fingerprint`(§7.3), `prompt_versions`(classify 밖 task → 활성 버전), `auto_policy`(작업 종류 → 켬/끔, 기본 끔), `active_models`(쓰는 모델의 model_actual), `blind_versions`(블라인드 비교할 두 `prompt_version` — 평가 설계 §2.4), `daily_usd_limit`, `missed_weights`(⑨ 점수 값 중 바꾼 것만 — 기본값과 값마다 이유는 `backend/missed.py` `WEIGHTS`·`REASONS`), `labor_cost_assumed`(⑥ 역할별 월 인건비 가정값 — 위생사·데스크·상담실장, 대표 원장 없음. 실제 급여가 아니며 없는 역할은 `backend/report.py` `LABOR_DEFAULT`)
**기준일 TODAY** (원본은 여기): `config` `today` 고정, 앱은 `shared/clinic.py`로 읽는다. 앱에서 바꾸는 기능은 두지 않는다 — 데이터가 그날 개원 전 스냅샷이라 앱에서만 옮기면 예약 상태·기록·청구 구간이 어긋난다. 바꾸려면 config를 고치고 재생성
**동시 쓰기**: 앱 연결은 WAL + `busy_timeout`(`backend/db.py`) — 챗 SSE가 쓰는 동안 다른 쓰기가 막히지 않게. 그래서 `app.sqlite`를 복사하는 곳(`save-demo`, `make e2e` — `frontend/playwright.config.ts`)은 `-wal` 파일까지 함께 복사한다(또는 체크포인트 뒤 복사)
**`ax_jobs`**: 30초 넘는 작업(배치 분류·준비표 AI 제안 갱신·야간 실행)의 상태·진행률 — 칸은 `shared/app_schema.sql`, 지금 쓰는 것은 준비표 AI 제안 갱신(`kind` `prep`)·⑤ AI 점검(`claim_check` — 2a·2b·⑤-3 차트 문장). 같은 작업이 `running`이면 새로 만들지 않고, `backend/api.py` `JOB_STALE`보다 오래 `running`인 행은 죽은 것으로 본다. 프론트는 폴링

**판정 큐 "다음 건"**: 대상 = `ax_judge_state` pending/skipped인 판정 풀 밖 통화·새로 추가한 통화 중 엇갈림 또는 무작위 표본이고 활성 버전 분류 `ok`. 순서 = 건너뛴 건 뒤로 → 새로 추가한 통화 먼저 → 핵심 엇갈림 → 나머지 엇갈림 → 표본. 표본은 엇갈림 없는 대상 중 `call_uid` 해시로(비율 `backend/judge_queue.py` `SAMPLE_SHARE`). 엇갈림을 계산하거나 새 통화를 분류하면 큐를 채우고, 활성 쌍의 엇갈림을 다시 계산하면 대상이 아니게 된 pending 건을 지운다(`backend/judge_queue.py` `prune` — skipped·done·사람 판정은 그대로). 저장 뒤 두 모델 라벨이 보인다
**dev 판정 풀**: `in_eval_pool`이고 `eval_sort_order ≤ ⌈config eval_pool_size ÷ 2⌉`(순번 앞 절반이 dev — [[치과 AX - 평가 설계]] §2.1). 앱 화면의 판정 풀 수치와 LLM 쌍 비교는 이것만 쓴다(최종 비교의 예외는 [[치과 AX - 평가 설계]] §2.4)
**블라인드 큐**: [[치과 AX - 평가 설계]] §2.4.

## 7. 기술 선택

| 항목 | 선택 |
|---|---|
| 스택 | **Next.js + TypeScript**(Tailwind CSS + shadcn/ui) / **FastAPI** / **SQLite**(FTS5 + **sqlite-vec** 벡터) |
| DB | SQLite 2파일 — `source.sqlite`(생성기 출력, 읽기 전용으로 ATTACH) + `app.sqlite`(앱 테이블, 색인 `ax_chunks`·FTS5·벡터). DDL 원본도 `shared/`에서 두 파일로 나눈다. 벡터 접근은 `VectorStore` 인터페이스 뒤, 두 번째 구현 안 함 |
| 벡터 저장소 결정 근거 | **규모**: 청크 수만 건(실측은 [[치과 AX - AI 실험 기록]] §1·§3)이면 전수 계산도 빨라서 근사 인덱스(ANN)가 필요 없다. **한 파일**: `ax_chunks`·FTS5·벡터가 `app.sqlite` 하나라 하이브리드 검색·메타데이터 필터가 SQL 조인 하나이고 두 저장소 동기화가 없다. **fixture**: `make fixture`·`save-demo`가 두 sqlite 파일만 복사. Chroma는 이 규모에서 얻는 것(ANN·서버 모드) 없이 저장소만 늘린다 |
| Chroma로 옮길 때 | 조건: 청크가 수십만 이상으로 늘어 전수 계산이 느려질 때. 벡터는 `VectorStore`(`upsert(ids, vectors)`·`query(vector, where, k)`) 뒤에만 있어 청킹·임베딩·`ax_chunks`·FTS5·리랭커는 그대로 — 구현 하나 추가 + 필터용 메타데이터 복사·다중 조건 필터는 Chroma `where`(`$and`)로 번역("SQL 조인 하나" 이점은 사라짐) + `make index` 재실행(임베딩은 다시 계산하거나 `ax_chunk_vectors`에서 복사) |
| FTS | SQLite FTS5 — 토크나이저·질의 방식은 §3 |
| 리랭커·임베딩 | Voyage — 모델은 `backend/rag_search.py` `DEFAULT_EMBEDDING`·`DEFAULT_RERANKER`, `haiku`는 비교용 조건(`backend/eval_hooks.py`). 근거는 [[치과 AX - AI 실험 기록]]. 리랭커가 실패하면 합친 순서로 답하고 로그를 남긴다(평가는 실패 횟수를 알린다). 벡터가 없는 청크가 있으면 검색기를 만들지 않는다(`MissingVectors`) |
| 모델 (확정) | 프로바이더 **Anthropic**·**Upstage**·**OpenAI**·로컬 **MLX**(LLM). ① 분류기 라벨·요약·할 일 경량 `claude-haiku-5-5` → 최종 vN-final `claude-sonnet-5-5`, 판정기(모델 B) 파인튜닝한 Qwen3.5-2B 로컬(`judge v3`, 분류기와 다른 계열 — 옛 v1·v2는 `solar-mini4`, 별칭은 `config/llm.yaml`, 근거는 `docs/superpowers/specs/2026-11-09-qwen-judge-design.md`), 쌍 비교 판정기는 `config/llm.yaml` `blind_judge`, 골드 점검 중재는 `gold_audit`(평가 설계 §2.3 — `evaluation/`에서만), 처리 정답 라벨은 `action_label`(평가 설계 §5 — `evaluation/`에서만), 그 밖의 에이전트·③ 챗·④ 제안·⑤-4 초안·⑦ 브리핑·⑨ 문자 `claude-sonnet-5-5`, 생성기(통화 녹취록·문서·차트 서술) `claude-sonnet-5-5`, 어려운 통화 녹취 `gen_hard`(OpenAI — 녹취·분류·라벨이 서로 다른 계열이 되게), 어려운 통화 정답 라벨 `hard_label`(평가 설계 §2.1 — `evaluation/`에서만) |
| LLM | 게이트웨이 모듈 1개 `shared/llm.py` — §7.1 |
| 프롬프트 | `prompts/` — §7.2 |
| 테스트 fixture | **`make fixture`**: fake 게이트웨이로 생성한 데이터로 판정 풀 전체와 판정 풀 밖 일부 통화를 가짜 프롬프트 버전 v1·v2 두 벌로 분류하고 판정기 판정까지 적재(판정 큐에 엇갈림·표본이 생기고 블라인드 큐가 돌게) + 해시 기반 결정적 fake 임베딩으로 벡터 테이블까지 → `tests/fixtures/` 커밋. 테스트는 복사본 사용. CI 키 없이 판정 큐·검색이 동작 |
| 사람 데이터 보존 | 재생성해도 `app.sqlite`는 덮어쓰지 않는다 — 테이블별 처리는 §7.3 |
| 시간 | DB naive KST, API `+09:00`. `ax_*` 활동은 실제 시계. 기준일은 §6 |
| 실행 | `make dev`(API + 화면) · `make index / fixture / save-demo / reset-demo / set KEY=… VALUE=…`(설정 변경, 허용 키는 [[치과 AX - 기능 정의]] §7) |

### 7.1 LLM (원본은 여기)

- **게이트웨이**: 모듈 1개 `shared/llm.py`(생성기 7·9·9b·9e단계의 API 채우기도 경유). 프로바이더마다 공식 SDK를 직접 쓴다 — Anthropic, Upstage·OpenAI(둘 다 openai SDK — Upstage는 OpenAI 호환 API), 로컬 MLX(파인튜닝 판정기 — `mlx-lm`, `finetune` 그룹이라 `make judge`·`make dev`가 `uv run --group finetune`으로 띄운다. 그룹 없이 띄우면 판정만 실패로 남는다). 모델의 프로바이더는 `config/llm.yaml` 모델 항목의 `provider`. 넷뿐이라 LiteLLM 같은 변환 계층을 두지 않는다
- **설정** `config/llm.yaml`: 별칭 → 모델 순서(앞 모델이 일시 오류(재시도 소진)·거절이면 다음 모델로 폴백, `fallback_used` 기록), 모델별 단가, 일일 비용 상한(`daily_usd_limit` — 넘으면 즉시 호출을 하지 않음. Batch는 상한 밖 — [[치과 AX - 기능 정의]] §7). 서버 측 거절 폴백을 지원하는 모델은 켠다(`server_fallback`)
- **구조화 출력**: JSON 스키마(Anthropic `output_config.format`, Upstage·OpenAI `response_format` `json_schema` strict). 로컬 MLX는 구조화 출력이 없어 출력의 첫 JSON 객체를 읽고, 판정기는 라벨로 맞는 객체를 다시 찾는다(`backend/classify.py` `first_labels`)
- **로컬 모델(MLX) 보기 강제**: 로컬 생성은 구조화 출력을 강제하지 못해, 결과가 스키마(보기·불)에 맞지 않으면 같은 입력을 다시 생성하지 않고 칸마다 허용 값 중 모델 확률이 가장 높은 것을 고르는 제약 디코딩으로 다시 낸다(`shared/llm.py` `_mlx_constrain`). 맞게 나온 결과는 그대로라 판정기 수치가 바뀌지 않는다. 보기 밖 값은 막지만 보기 중 틀린 값은 막지 못한다
- **기록**: 호출 1건 = 기록 1행 — 생성기와 골드가 든 `evaluation/` 호출(평가 설계 §2.3·§2.7)은 jsonl, 앱은 `ax_llm_calls`
- **Batch**(생성기 API 채우기·① 일괄 분류): Anthropic·OpenAI 모델(Upstage·로컬 MLX는 일반 호출). OpenAI는 요청 JSONL을 올려 `/v1/chat/completions` 배치로 내고 결과·오류 파일을 읽는다. 배치 상태·결과를 볼 프로바이더는 배치 id가 아니라 별칭으로 고른다. 별칭의 첫 모델만 쓰고 서버 측 거절 폴백을 쓰지 않으며(Batch에서 거부) Batch 단가(`config/llm.yaml` 단가 × `shared/llm.py` `BATCH_PRICE_SHARE`)로 비용을 센다 — 거절 건은 생성기 들여오기에서 실패로 남아 다음 시도로 다시 나온다
- **`FakeGateway`**: 같은 인터페이스로 입력 해시에서 결정적 응답(스키마가 있으면 스키마에 맞는 JSON) — 테스트·CI·fixture용
- **도구 호출**: `complete(tools=…)`는 `tool_use` 종료도 성공으로 받아 호출과 블록을 돌려준다. 도구를 주지 않은 호출의 `tool_use`는 실패다. 루프는 §3b

### 7.2 프롬프트

- **앱**: `prompts/<task>/<version>.md` — 파일명이 곧 `prompt_version`. task: `classify`(분류기 — 호출이 둘이라 버전 폴더 `prompts/classify/<version>/`의 `labels.md`·`summary.md`, 폴더명이 `prompt_version`이고 둘 중 하나의 텍스트나 모델이 바뀌면 버전을 올린다), `chat`(챗 시스템), `prep`(준비표 제안), `judge`(판정기 — 판정 기준 포함), `blind_judge`(요약 쌍 비교 — 평가 설계 §2.4), `followup`(후속 에이전트), `call_actions`(처리 정답 라벨 — `evaluation/`에서만), `gold_audit`(골드 점검 중재 — 평가 설계 §2.3, `evaluation/`에서만 읽고 버전은 `evaluation/gold_audit.py` `PROMPT_VERSION`), 에이전트마다 task 하나
  - 버전 파일은 고치지 않고 새 버전으로 추가(비교 재현성) — 모델만 바꿔도 같은 텍스트로 새 버전(§1 `prompt_version` 정의 — 모델 별칭이 있는 classify)
  - 호출 설정: 버전 파일 머리말(`---` 사이 YAML)의 `effort`(추론 단계 — Anthropic `output_config.effort`, Upstage·OpenAI `reasoning_effort`)를 호출에 넘기고, 추론 토큰 몫만큼 출력 상한을 늘린다(`backend/classify.py` `REASONING_MAX_TOKENS`). 없으면 모델 기본값. 지금은 judge·gold_audit·call_actions만 읽는다. judge 버전 파일 머리말의 `model`은 그 버전을 낸 모델이다 — 지금 `judge` 별칭의 첫 모델과 다르면 그 버전은 다시 판정하지 않는다(`backend/classify.py` `judge_model_ok`, 옛 기록을 다른 모델로 덮지 않게)
  - 쓰는 버전: classify는 `ax_settings.active_prompt_version`, 나머지는 `prompt_versions`. 새 버전 파일은 관문(`make prompt-gate`)을 넘은 것만 쓴다. 자동 개선이 대기 버전으로 올리는 규칙은 평가 설계 §2.7 전환
  - **classify 전환**: 사람이 `POST /jobs/classify-batch`로 대기 버전을 전체 통화에 돌린다(지금은 `make classify`와 `ax_settings` 직접 설정 — 작업 API는 [[치과 AX - 기능 정의]] §9). 그동안 화면·큐·야간은 이전 활성 버전을 쓴다. 전부 `ok`가 되면 같은 트랜잭션에서 활성 버전을 바꾸고 엇갈림을 다시 계산한다. `failed`가 남으면 전환하지 않는다
  - **전환 뒤 상태**: 자동 종결은 `make autoclose`가 새 활성 버전 쌍(분류·판정기)으로 다시 계산한다(그 전에는 옛 쌍의 종결을 종결로 보지 않는다) — 풀기와 사람 되돌림 규칙은 §3b `ax_auto_actions`. 할 일 체크(`ax_todo_checks`)와 처리안(`ax_call_followups`)은 분류 행 단위라 버전을 바꾸면 이어지지 않는다(새 분류 행에 처리안을 다시 낸다)
  - 되돌리기는 이전 버전으로 다시 설정한다. 이전 분류 행이 남아 있어 재분류가 필요 없다
- **생성기**(`gen_transcript`·`gen_document`·`gen_chart`): 작업당 파일 하나 `prompts/<task>.md`를 고쳐 쓴다 — LLM 캐시 키에 그 파일 해시가 들어가 출력을 구분하고, 이력은 git

### 7.3 사람 데이터 보존

재생성해도 `app.sqlite`는 덮어쓰지 않는다. 다만 앱 테이블이 가리키는 생성 데이터의 키(`call_uid`·환자·예약·`fin_` id)가 다른 실체를 가리킬 수 있어 테이블마다 처리를 정한다.
- **다시 만듦**: 색인(`ax_chunks`·FTS·벡터)·분류·판정기 판정·LLM 쌍 비교·엇갈림·준비표·⑦ 브리핑·⑥ 리포트(`ax_reports`)·⑤ AI 점검(`ax_claim_checks`·`ax_claim_omissions`·`ax_claim_evidence`)·에이전트 실행 기록·되돌리지 않은 자동 처리·환자 묶음·발송함·`ax_jobs`
- **해시로 다시 붙임**: 판정(메모 포함)·투표 — `hash(transcript)`로만(`export-human/import-human`), 미스 시 버리고 "복원 실패 N건" 리포트. 투표는 `(hash, left_model, left_version, right_model, right_version, choice)`. 판정 상태는 판정 유무에서 파생한다
- **자연키로 보존**: 되돌린 자동 처리 — 아래 키로 다시 붙이고, 못 붙이면 리포트한다. 되돌린 건은 같은 키로 다시 자동 처리하지 않는다

  | 작업 | 자연키 |
  |---|---|
  | ① 자동 종결 | `hash(transcript)` |
  | ⑧-1 자동 마감 | 불일치 건 키(관련 행 id 해시 — 생성 데이터가 바뀌면 붙지 않는다) |
  | ⑧-2 자동 확정 | 출금 키(`backend/expense.py` `outflow_key` — 출금 표·시각·금액·출금처 글자 해시) |
  | ⑨ 안내 기록 | 차트 번호·안내 종류·기간 |

- **그대로 둠**: 실수 후보(승인 상태 포함, 실행 id는 끊겨도 된다)·`ax_rule_counts`·게시한 문서 초안·`ax_settings`(`auto_policy` 제외)
- **강제로 끔**: 앱 기동과 `make nightly` 시작 때 `source.sqlite` 지문을 `source_fingerprint`와 비교해, 다르면 `auto_policy`를 전부 끄고 지문을 갱신한다. 다시 측정한 뒤 사람이 켠다
- **버림**: 할 일 체크·확인할 통화 확인함(`ax_call_checks` — 재생성 뒤 확인할 통화가 다시 뜬다)·⑧ 확정·⑨ 연락 기록(`ax_missed_contacts`)·⑤ 판단 기록·재심 사유서(`ax_claim_judgments`·`ax_claim_appeals` — 재생성 뒤 청구 id가 다른 청구를 가리킬 수 있다)·게시 전 문서 초안·새로 추가한 통화·`ax_search_logs`·`ax_llm_calls`

## 8. 미결

- [ ] 인증을 붙일 때 저장된 질문 읽기(`GET /chat/{log_id}`)도 같이 막는다 — 번호가 차례라 번호만 알면 질문·답(환자 이름 포함)을 읽는다

## 9. 프론트

화면의 레이아웃·동작 기준은 와이어프레임 `docs/wireframe/index-v2.html`이고(일별 예약표의 소요 시간 카드·환자 요약 카드, 전환 전 EMR 설명 카드, 통화·AI 평가 화면, 메뉴 순서와 물어보기 창은 [[치과 AX - 기능 정의]] §4와 아래 "배치"가 기준, 미예약 환자 화면은 [[치과 AX - 기능 정의]] §5 ⑨, 청구 점검 화면은 §5 ⑤가 기준), 색·모양은 `frontend/app/globals.css` 토큰과 `frontend/components/ui`에서 나온다(전환 전 EMR 창 안은 `--emr-*` 토큰과 `components/emr/emr-ui.tsx`). 기능과 표시 규칙은 [[치과 AX - 기능 정의]] §4 앱 구성.

**페이지**

| 라우트 | 화면 | API 원본 |
|---|---|---|
| `/` | 시작 화면 | 없음 |
| `/today` | 오늘 — 요약 카드, 사람이 볼 일, AI가 한 일(접힌 요약 + 펼치기) | [[치과 AX - 기능 정의]] §4 앱 구성 |
| `/calls` | 오늘 할 일(할 일·연락할 사람), 통화 기록, 새 통화 추가, 상세(패널 ↔ 넓게 보기) | [[치과 AX - 기능 정의]] §4 ① |
| `/schedule` | 일별 예약표(읽기 전용) | 여기: `GET /emr/schedule?date=`(`backend/emr.py` `schedule_day` — 영업시간·휴진, 진료 담당자 열, 그날 예약 전부(취소·노쇼 포함 — 숨김은 화면에서). 예약마다 내원 확인 전(기준일만)·예약 확인(`confirm` — 직전 진료일 리마인드 응답인 `confirmed`와 리마인드 유무)·신환·치료계획 몇 번째(대체 항목 제외)·잡은 경로와 시각·잡은 통화(같은 환자·통화 시각 = `created_at`). 응급 여부는 ① 착수 때 그 통화의 `ax_call_classifications`로 붙이고 판정 풀 통화는 뺀다), 패널은 `GET /emr/patients/{chart_no}`의 `summary`(`patient_summary` — 미수·주의사항·마지막 내원·앞으로의 예약·노쇼·취소·남은 항목(`open_items`), 모두 기준일 기준) |
| `/prep` | 준비표·체어 보기(`?view=chair`) — 와이어프레임의 회의 보기는 두지 않는다(기능 정의 §4 ④ 제외) | `GET /prep?date=`·`POST /jobs/prep-regenerate`·`GET /jobs/{id}`(`backend/prep.py`) |
| `/missed` | 미예약 환자(칩·연락함 숨기기 `?chip=…&hide=1`, 행마다 연락 결과) | §5 ⑨ |
| `/ask` | 메뉴에 없음([[치과 AX - 기능 정의]] §4 앱 구성 "물어보기 창"). 질문하기(`?tab=chat&log=<id>`는 저장된 그 질문·답·출처를 다시 묻지 않고 첫 줄에 보인다, `?draft=`는 입력칸만 채운다), 기록 검색(결과 위에 AI 요약 — 결과가 1건 이상일 때만, 본문 `[n]`과 같은 번호의 출처 칩, "이걸로 질문하기"는 검색어를 질문 입력칸에 넣기만 한다), 문서함(종류 칩 — 처음은 공지, 상태는 `?tab=docs&doc=<id>`, 옛 공지를 열면 최신 보기 줄). 문서는 검색 결과 문서 카드의 "문서함에서 보기"·요약 출처 칩에서 연다 | [[치과 AX - 기능 정의]] §4 ③, 여기: `GET /chat/{log_id}`(저장된 질문 1개를 답 이벤트 모양으로 — 평가·요약·아직 도는 질문은 404, `backend/api.py` `get_chat`) |
| `/eval` | 판정, 블라인드, 정확도(자동 개선은 개선 에이전트 때) | [[치과 AX - 기능 정의]] §4 ② |
| `/emr` | 전환 전 EMR(데스크·예약·전자차트) + 설명 카드 | 여기: `GET /emr/patients?q=`, `GET /emr/patients/{chart_no}`(`patient_record`), `GET /emr/schedule?date=`, `GET /emr/tour`(따라 보기 기준 환자 — `tour_patient`) |
| `/claims` | 삭감 대응(조정 목록 5칸·칩·행 펼침·판단 기록), 청구 전 점검(상병 확인 경고·AI 점검), 주소 `?tab=pre&chip=…` | §5 ⑤ — `GET /claims`·`GET /claims/pre`·`POST /claims/{claim_id}/judgment`·`POST /jobs/claim-check`·`GET /jobs/{id}`·`GET /claims/standards`(`backend/claims.py`·`backend/claim_check.py`·`backend/claim_omission.py`·`backend/claim_evidence.py`·`backend/claim_search.py`) — 맨 위 청구 기준 찾기 검색칸, 펼친 곳 기준 조항 카드(본문은 문서함과 같은 마크다운 — 고시 발췌는 인용 블록·출처 줄), 재심 신청 건 ③ 재심 사유서(`POST /claims/{claim_id}/appeal`·`/appeal/review`·`/appeal/submit`, `backend/claim_appeal.py`) |
| `/report` | 경영 리포트 | [[치과 AX - 기능 정의]] §5 ⑥ |
| `/finance` | 탭 일계표·지출 증빙. 일계표(달 선택·날짜별 결제수단 대조·불일치 건 펼침·조사 과정(조사 시작 → 줄이 하나씩)·에이전트 원인과 규칙 추정·자동 마감 되돌리기·확정), 지출 증빙(달 선택·칩으로 거르기(규칙과 다름 포함)·출금별 찾은 증빙·금액 차이·계정 과목과 그 출처·확정, 줄 아래 AI 제안 칸(규칙과 같으면 한 줄, 다르면 결론·근거·직원이 볼 것·조사 과정과 이 제안으로 확정)·자동 확정 되돌리기, 펼치면 증빙 본문과 근처 증빙 체크로 바꾸기). 주소 `?tab=expense&month=…&day=…&chip=…` | §5 ⑧ — `GET /finance/recon`·`POST /finance/recon/{key}/confirm`(`backend/recon.py`)·`…/investigate`(`backend/recon_agent.py`), `GET /finance/expenses`·`…/candidates`·`…/confirm`(`backend/expense.py`) |

- `/emr/*`은 `source.sqlite`만 읽기 전용으로 연다(색인 없이도 동작). 탭·패널 이동 수는 화면 상태이고 저장하지 않는다.

**공통 레이아웃**: 상단에 로고(시작 화면으로) + 메뉴 + 화면 테마(시스템·라이트·다크, 브라우저에 저장 — `frontend/lib/theme.ts`) + 담당 파트 선택 + 기준일. 병원 이름·기준일은 `GET /clinic`. 시작 화면은 메뉴가 없고, `/eval`·`/emr`은 메뉴 자리에 공간 이름과 돌아가기가 나온다. 포털 layout에 떠 있는 물어보기 버튼·창(`frontend/components/ask/ask-floating.tsx` — 규칙은 [[치과 AX - 기능 정의]] §4 앱 구성 "물어보기 창")을 두어 메뉴를 옮겨도 창이 남고, 창 안은 물어보기 화면과 같은 `ChatPanel`이다.

**배치**: 하는 일·결과는 왼쪽, 근거 자료(녹취록)는 오른쪽 — 통화 상세 넓게 보기·판정·블라인드. 화면 말은 하는 일로 짓고(시스템 말·코드 값을 보이지 않는다) 라벨 이름은 `GET /labels`(`shared/call_labels.py` `*_KO`, 처리 종류는 `shared/followup.py` `ACTION_KO`).

**이름 표시**: 통화·문자 화면은 환자 이름과 차트 번호를 함께 보인다. 차트 서술 화면은 차트 번호만 보이고 이름을 붙이지 않는다(§3 prefix). 환자에는 차트 번호로 닿는다.

**데이터 가져오기**: 화면 진입 때 조회한다. 배치(①·④·⑤-2)와 야간 실행은 작업을 시작한 뒤 `ax_jobs`를 폴링하고, ③ 챗은 SSE 진행 이벤트를 받는다.

**공통 컴포넌트**: `AiBadge`(AI가 만듦), `Confirmed`(사람이 정함), `Estimated`(`[추정]`), `AgentTrace`(조사 과정), `AutoAction`(자동 처리 이유·되돌리기). 코드 값에는 표시를 붙이지 않는다.

**판정 키 매핑**: 항목을 순서대로 입력하고 입력이 끝나면 다음 항목으로 넘어간다. 값은 enum 순서를 따른다([[치과 AX - 가상 데이터 생성 설계]] §4.1).

| 항목 | 자리 | 값 |
|---|---|---|
| 목적 | 2 | `01`부터 enum 순서, `00` 모르겠음·애매 |
| 상대 | 1 | `1`부터 enum 순서, `0` 모르겠음·애매 |
| 급함 | 1 | `1` 낮음, `2` 보통, `3` 급함, `0` 모르겠음·애매 |
| 불리언 6개 | 1 | `1` 아니오, `2` 예, `0` 모르겠음·애매 |
