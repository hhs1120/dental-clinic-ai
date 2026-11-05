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

`ax_calls` 정의는 [[치과 AX - 가상 데이터 생성 설계]] §3. 앱 테이블은 통화를 `call_uid`로 가리킨다(생성 통화 `c-0481`, 붙여넣기 통화 `p-0001`) — 붙인 DB로는 외래 키를 걸 수 없고 붙여넣기 통화는 앱 DB에 있다.

**분류기 입력 = `transcript` + `direction`만** (환자·번호·시각은 정답 힌트. 함수 시그니처 `classify(transcript, direction)`로 강제).

eval.py가 읽는 칸(아래 분류·판정기 판정·투표, §2 LLM 로그)은 `shared/app_schema.sql`이 정한다 — 앱은 그 칸을 그대로 쓰고 나머지 칸만 착수 때 추가한다. RAG 평가는 앱이 `evaluation/rag.py`의 `search`·`chat`을 `backend/eval_hooks.py`의 `make_app(app_db, source_db)`로 내준다.

| 테이블 | 목적 · 핵심 제약 |
|---|---|
| `ax_call_classifications` | 분류기(모델 A)의 요약·할 일·분류 labels(9항목)·`status(ok/failed)`. 라벨 호출과 요약·할 일 호출이 따로라 호출마다 `model_actual`·`llm_call_id`(라벨)와 `summary_model_actual`·`summary_llm_call_id`(요약) — 둘 중 하나라도 실패면 `failed`. UNIQUE `(call_uid, model_alias, prompt_version)`. **`prompt_version`은 (프롬프트 텍스트 × 모델)의 버전** — 모델만 바꿔도 올림(`vN-final`). 대시보드는 버전마다 `model_actual` 표기 |
| `ax_judgments` | 사람 판정(운영 구간 엇갈림·표본). `judged_at`은 실제 시계. `note`(메모, 선택) |
| `ax_model_judgments` | 판정기(모델 B) 판정. 사람 판정과 섞지 않으려고 테이블을 나눈다. UNIQUE `(call_uid, model_alias, prompt_version)` |
| `ax_call_disagreements` | 엇갈림 `(call_uid, a_version, b_version)` 1행, `is_core` = 목적·급함·응급 불일치. 어느 쪽이든 폴백이면 표시(`fallback`)하고 자동 종결하지 않는다. 분류기 할 일 목록이 있는데 분류기 `has_todo` = 0이면 라벨이 같아도 1행(`todo_mismatch`). 계산은 `backend/disagreements.py` |
| `ax_judge_state` | 판정 상태(pending / skipped / done) — 생성 DB가 아니라 앱 DB에 둔다. 엇갈림·표본이 정해질 때 채운다 |
| `ax_pasted_calls` | 붙여넣기 통화 — 앱이 만든 통화라 앱 DB. 목록·색인·분류에서는 생성 통화와 합쳐 보인다 |
| `ax_call_followups` | 후속 큐 처리 기록(처리한 파트·시각)과 후속 에이전트의 처리안(enum·근거·실행 id). 처리한 통화는 큐에서 빠짐 |
| `ax_model_blind_votes` | LLM 쌍 비교 판정 — 좌·우 분류 ID·쌍 비교 판정기(`blind_judge`) 버전·두 번 판정(좌우 바꿈)과 합친 `choice`·두 호출의 LLM 호출 id(폴백 제외용). 규칙은 [[치과 AX - 평가 설계]] §2.4 |
| `ax_blind_votes` | 사람 블라인드 투표(최종 비교만). 좌·우 분류 ID(FK → classifications) + `choice(left/right/tie/both_wrong)`. 좌우 배치·승률·큐 규칙은 [[치과 AX - 평가 설계]] §2.4 |

## 2. LLM 로그

**`ax_llm_calls`**: 게이트웨이 호출 1건 = 1행. 칸은 `shared/app_schema.sql`, 쓰는 곳은 `backend/harness/store.py` `SqliteLlmLog`. `ref_type/ref_id`는 다형 참조이고 타입별 조인 검사는 [[치과 AX - 가상 데이터 생성 설계]] §5.3 '참조'다. 입력 덤프 경로 칸과 `ref_type` 목록은 ③ 챗 착수 때 정한다([[치과 AX - 기능 정의]] §9). `fallback_used` 건의 집계 제외는 [[치과 AX - 평가 설계]] §1.

## 3. 검색

**색인**(`make index EMBEDDING=<모델>` — 모델 필수, 없는 청크만 계산): 크기·겹침은 `backend/rag_index.py` `MAX_CHARS`·`OVERLAP`, 마크다운 헤더 우선. 문자는 건별이 아니라 **환자별 월 스레드 1청크**(`[환자: 김○○(C-00001) | 2026-07] 07-03 예약 확인 … 07-10 답장: …`). 색인 코드는 앱 쪽이고 `app.sqlite`에 쓴다 — 생성기는 `source.sqlite`만 쓰고, `make index`는 `make data`와 따로 실행한다([[치과 AX - 가상 데이터 생성 설계]] §5.1). 색인 상태는 `ax_chunks`로 판단한다(생성 테이블에 색인 칸을 두지 않음). 붙여넣기 통화는 앱이 색인. 청크 id의 통화 부분은 `call_uid`.

**`ax_chunks`**: id는 출처에서 결정적으로(`'doc:12:3'` / `'call:c-0481:0'` / `'thread:C-00001:2026-07'`), `source_type(document/call/message_thread/chart_note/doc_draft)`(id `'chart:<treatment_id>'`, 진료 1건 = 1청크. 게시한 문서 초안은 `'draft:<id>:<순번>'`이고 문서 버킷에 들고 RAG 평가 색인에서 뺀다), 필터용 실컬럼(`chart_no`·`event_date`·`doc_type`).
`text`는 **출처 prefix 의무**: 문서 `제목 > 섹션`(섹션이 제목과 같으면 제목만), 통화 `[환자: 김○○(C-00001) | 2026-07-03 | 수신]`(환자 없는 통화는 `[상대: 보험사 | 2026-07-03 | 수신]`), 스레드 `[환자: … | 2026-07]`, 차트 `[차트 C-00001 | 2026-07-03 | 스케일링]`(환자 이름 없음 — 케이스 내용으로 찾고 차트 번호로 환자에 닿는다). 차트는 서술 끝 괄호에 다른 표현을 더한다 — 치아 번호의 부위, 그 차트 시술의 별칭, 정해 둔 동의어, 기간의 다른 표기(`backend/rag_index.py` `chart_terms`). 다른 시술 이름은 따라가지 않는다 — 받지 않은 시술이 붙는다. 괄호는 검색용이라 화면에는 내지 않는다.
- FTS5 `ax_chunks_fts(chunk_id, text)` — `unicode61` + 질의 측 `원본어절* OR 조사제거어절*`.
- 벡터 `ax_chunk_vectors(chunk_id = ax_chunks.id, model, embedding)` — sqlite-vec, float32 BLOB, 임베딩 모델마다 한 벌. 필터는 `ax_chunks` 실컬럼으로 먼저 거르고 남은 행만 `vec_distance_cosine` 전수 계산(정확 검색 — 근사 인덱스 없음, top-k가 필터에 깎이지 않음). 다중 조건 필터도 SQL 그대로

**`ax_search_logs`**: 검색·챗 1회 = 1행. 도구 호출(`args`는 Pydantic 정규화 후 기록)·`retrieved_ids`·답변·`cited_ids`·`is_eval`. eval.py는 이 표를 읽지 않는다(챗 채점은 `chat()` 반환값 — 평가 설계 §3.3). 답변은 `{text, cited_ids[]}` 구조화 출력으로 강제(모델이 근거 청크를 스스로 명시 — ④ `used_facts`와 같은 패턴), 화면의 출처 = `cited_ids`. 데이터형(`query_*`)만 쓴 답변은 `cited_ids`가 비어도 됨. 검색 화면의 AI 요약도 같은 출력.
- **인용 확인**(챗 후처리 — 훅은 도구 호출만 보므로 답을 낸 직후 코드가 검사): `cited_ids`가 이번 질문의 `retrieved_ids` 밖이면 같은 검색 결과로 한 번 다시 답하게 하고, 또 어긋나면 고정 거절 문구로 끝낸다. 이때 마지막 호출은 `refuse`가 아니고 `cited_ids`는 비우며 상태는 인용 실패로 `ax_search_logs`·`chat()` 반환값에 남긴다(문서 초안 입력은 `refuse(not_found)`만이라 여기에 들지 않는다). 막힐 때마다 `ax_rule_counts`에 `chat.cite_outside`로 더한다. ③ 문서 초안이 같은 검사를 쓰고, ⑤-4는 인용 문장이 실제로 있는지까지 본다(평가 설계 §5)

**도구 5개 — 시그니처 고정** (평가 설계 §3.2 `expected_args` 채점의 기준, 코드 `backend/query_tools.py`·`backend/rag_search.py` `search_tool`)

| 도구 | 인자 | 반환 |
|---|---|---|
| `search_docs` | `query`, `source_types?[]`, `date_from?/date_to?`, `patient_chart_no?`, `top_k?`(기본값은 `SearchArgs`) | `{manual_results, case_results}` — 버킷별 청크 목록 |
| `query_patient` | `chart_no?` / `name?` (하나 필수) | `{matches[], summary?}` — 동명이인이면 summary 없이 후보만. summary: 기본정보·주의사항(기록일 ≤ 기준일 < 끝나는 날, `emr_patient_alerts`)·마지막 내원·잔액·다음 예약·가족 리콜·그해 스케일링 급여 사용 여부(`emr_claims`)·열린 치료계획 항목 |
| `query_schedule` | `date_from, date_to`(단일 날짜는 같은 값), `status?, type?, has_unpaid_balance?, is_new_patient?` | 예약 목록 + `{count, by_status, by_type}` |
| `query_claims` | `date_from?/date_to?, chart_no?, procedure_code?, status?` | 청구 목록 + `{count, total_amount, by_status}`. 확정 월은 결과·사유 포함 |
| `refuse` | `reason`(`not_found` / `restricted` — 비공개 행정정보) | 없음. 호출 시 턴 종료, 앱이 고정 거절 문구 표시 |

- `query_schedule` 칸
  - `has_unpaid_balance` = 그 예약일 개원 전 잔액 > 0 (정의 원본)
  - `is_new_patient` = 창 안에서 처음 등록한 환자 (생성 설계 §2.2)
  - `count`는 조건에 맞는 행 전체(`status`를 주지 않으면 취소·변경 포함)
  - `by_status`는 변경으로 대체된 원래 예약(새 예약의 `rescheduled_from_id`가 가리키는 `cancelled`)을 취소와 따로 `rescheduled`로 센다 — 예약 변경은 취소가 아니다
- 날짜 전부 `YYYY-MM-DD`. 도구 description에 TODAY 값(§6) 예시를 넣음
- 호출 상한 6회/질문. 경계: 건수·목록 → `query_*`, 절차·규정·과거 대화 → `search_docs`, 근거가 없거나 비공개 정보면 `refuse`
- **검색은 버킷 분리**: `document`와 `call/message_thread/chart_note`를 따로 찾아 각각 상위 `CANDIDATES`(`backend/rag_search.py`) → 리랭커 → `manual_results` / `case_results`. `event_date ≤ TODAY` 강제. 리랭커 뒤에 옛 공지(같은 제목에서 시행일이 이른 것) 자리에 후보의 최신 공지를 올리고 옛 공지를 바로 뒤에 둔다. 날짜 필터를 주면 시행일 없는 문서는 빠지고, 환자 필터를 주면 환자 기록만 찾는다

**문서 초안 에이전트의 입력** = `ax_search_logs` 중 `refuse(not_found)`로 끝난 질문. 오늘 화면에는 초안이 오른다. **문서함**은 `ax_documents`를 그대로 보여 주고, 공지는 최신 버전만.

## 3b. 에이전트 하네스

코드 위치는 `backend/harness/`. 다섯 축의 뜻은 [[치과 AX - 기능 정의]] §4 에이전트 하네스. 랭체인·랭그래프를 쓰지 않고 직접 구현한다 — 권한·훅·자동 처리·저장소 주입처럼 코드로 강제할 부분이 하네스의 본체라 프레임워크가 덜어 주는 것은 도구 루프와 재개 정도이고, 체크포인트를 따로 두면 상태가 `ax_agent_steps`와 둘로 갈라진다. 게이트웨이(§7.1)는 그대로 쓰고 모든 LLM 호출은 그곳을 지난다(임베딩·Voyage 리랭커는 밖).

**상태·제약 테이블**
| 테이블 | 목적 · 핵심 제약 |
|---|---|
| `ax_agent_runs` | 실행 1건 — 에이전트 이름·대상·상태·비용·`prompt_version`·부모 실행(운영 에이전트가 부른 경우)·실행 잠금(하트비트). 재개해도 비용과 호출 수는 누적하고 `prompt_version`은 처음 값을 쓴다 |
| `ax_agent_steps` | 단계 — 입력·모델 응답·도구 호출과 결과 전체(재생·재개용)·실행 뒤 막힌 건의 원래 결과·훅 결과. 쓰기와 같은 트랜잭션으로 기록한다. 화면의 조사 과정은 이 표를 읽는다 |
| `ax_auto_actions` | 자동 처리 — 작업 종류·대상·결과값(증빙 id·계정 과목 등)·이유·실행 id·되돌린 사람·시각. UNIQUE 중복 방지 키(작업 종류·대상·기간 — ⑨ 안내처럼 반복되는 일은 기간이 키. 기간은 코드가 계산하고, LLM은 실행 대상에만 요청할 수 있다). 되돌리면 앱 상태만 복원하고 해당 큐로 돌려보낸다. 판정 풀 통화는 대상이 아니다 |
| `ax_patient_flags` | 운영 에이전트가 묶은 환자 — 신호 목록·이유·실행 id. UNIQUE (환자·날짜). 오늘 화면이 읽는다 |
| `ax_outbox` | ⑨ 자동 안내 기록 — 환자·문구·실행 id·되돌린 상태. 실제 발송 없음. 점수의 최근 연락이 함께 읽는다 |
| `ax_doc_drafts` | ③ 문서 초안 — 원 질문·초안·근거·게시 결정. 게시한 초안은 문서함과 색인에 합쳐 보인다(`ax_documents`는 쓰지 않음) |
| `ax_failure_cases` | 재발 방지 — 출처(되돌리기·훅 차단·표본 오류·관문 탈락)·실행 id(문자열 참조, 외래 키 없음)·원인 분류(enum)·재현 테스트 경로·고칠 곳·승인 상태. 라벨·녹취 인용은 넣지 않는다(쓰기 전 검사는 [[치과 AX - 평가 설계]] §2.7) |
| `ax_rule_counts` | 규칙 id별 차단 누계 |

**에이전트별 도구** (호출 상한·세부 인자는 착수 시)
| 에이전트 | 도구 |
|---|---|
| ③ 챗 | §3 도구 5개 |
| ① 후속 | `query_patient`, `query_schedule`, `search_docs`, `query_claims` |
| ③ 문서 초안 | `search_docs` |
| ④ 조사 | `query_patient`, `search_docs` |
| ⑤-3 근거 | `search_docs`, `query_claims`, 차트 조회 |
| ⑥ 조사 | `query_schedule`, `query_claims`, 지출 조회 |
| ⑧ 원인·증빙 | `request_auto_action`, 나머지는 슬롯 착수 시 |
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
- **재발 방지**: 훅·정책 규칙은 코드에 id를 달아 두고, 차단이 날 때 `ax_rule_counts`에 더한다(자동 처리 거절은 `auto.<이유>`, 챗 인용 실패는 `chat.cite_outside` — §3). 승인 규칙은 [[치과 AX - 평가 설계]] §2.7

## 4. 준비표·오늘 화면·리포트

**`ax_prep_sheets`**: 날짜·환자·예약별 준비표 행(`id` PK). `facts json` + `facts_hash`(같으면 LLM 생략) + `suggestion{action, text, used_facts[{key,value}]}` + `suggestion_valid`. UNIQUE `(sheet_date, patient_id, appointment_id)`. `facts`에 남은 치료계획·체어 번호가 들어간다(회의 보기·체어 보기가 같은 행을 쓴다)

**오늘 화면**(`GET /today`): 쿼리로 모은다. 자동 처리·환자 묶음은 §3b 테이블을 읽는다(항목은 [[치과 AX - 기능 정의]] §4 앱 구성). **⑦ 브리핑**은 `ax_briefings`(날짜, 문장, 쓴 숫자, 후검증 통과 여부 — ④와 같은 틀), 슬롯 착수 시 추가.
**⑥ 리포트**: 숫자는 쿼리로 모으고 `metrics`로 저장한다. 앱 테이블 `ax_reports`(기간 종류·기간, `metrics`, 문장, 후검증 통과 여부 — ④와 같은 틀)는 슬롯 착수 시 추가. 튄 숫자 조사 결과는 근거 쿼리·실행 id와 함께 저장한다. 인건비가 들어간 값은 `metrics`에서 추정 항목으로 구분해 화면이 `[추정]`을 붙인다. 항목은 [[치과 AX - 기능 정의]] §5 ⑥.

## 5. 슬롯 테이블

**⑤ 청구 점검**(코드명 `claim_review`): 심사결과는 이미 `emr_claims`(결과·사유·통보일)에 있다. 앱 테이블(⑤-2 점검 결과, ⑤-3 판단, ⑤-4 초안·추측 목록·검토 확인)은 `ax_`로, 슬롯 착수 시 추가. ⑤-3 조정 사유 검색은 §3 검색 구조를 재활용한다. 단계는 [[치과 AX - 기능 정의]] §5 ⑤.

**⑧ 재무 대조**: 입력은 `fin_*`와 `emr_payments`(읽기 전용). 앱 테이블은 사람 확정 2개 — `ax_recon_confirmations`(⑧-1: 관련 `fin_` 거래 id, 고른 원인, 맞음/아님, 사람·시각), `ax_expense_confirmations`(⑧-2: 출금 id, 증빙 id들, 계정 과목, 사람·시각). "확정한 거래처는 다음부터 기본값"은 이 테이블에서 읽는다. 자동 처리는 §3b `ax_auto_actions`에 결과값과 함께 남기고 사람 확정 테이블에는 쓰지 않는다. ⑥ 재료비와 거래처 기본값은 두 테이블을 합쳐 읽되 되돌린 건은 뺀다.

**⑨ 후속 관리**: 칩·점수는 쿼리. 앱 테이블은 연락 기록 `ax_followup_contacts`(환자, 연락한 사람·시각·결과) — "연락함 숨기기"와 점수의 최근 연락이 읽는다(생성 데이터의 발신 통화·문자와 함께).

## 6. 설정·작업·고정 쿼리

**`ax_settings(key, value)`**: `active_prompt_version`, `pending_prompt_version`(관문을 넘고 재분류를 기다리는 classify 버전 — `make improve`가 쓰고 전환 때 비움), `source_fingerprint`(§7.3), `prompt_versions`(classify 밖 task → 활성 버전), `auto_policy`(작업 종류 → 켬/끔, 기본 끔), `active_models`(쓰는 모델의 model_actual), `blind_versions`(블라인드 비교할 두 `prompt_version` — 평가 설계 §2.4), `daily_usd_limit`, `labor_cost_assumed`(⑥ 역할별 월 인건비 가정값 — 위생사·데스크·상담실장, 대표 원장 없음. 실제 급여가 아니며 값은 슬롯 착수 때 넣는다)
**기준일 TODAY** (원본은 여기): `config` `today` 고정, 앱은 `shared/clinic.py`로 읽는다. 앱에서 바꾸는 기능은 두지 않는다 — 데이터가 그날 개원 전 스냅샷이라 앱에서만 옮기면 예약 상태·기록·청구 구간이 어긋난다. 바꾸려면 config를 고치고 재생성
**`ax_jobs`**: 30초 넘는 작업(배치 분류·준비표 재생성·야간 실행)의 상태·진행률. 프론트는 폴링

**판정 큐 "다음 건"**: 대상 = `ax_judge_state` pending/skipped인 판정 풀 밖 통화·붙여넣기 통화 중 엇갈림 또는 무작위 표본이고 활성 버전 분류 `ok`. 순서 = 건너뛴 건 뒤로 → 붙여넣기 통화 먼저 → 핵심 엇갈림 → 나머지 엇갈림 → 표본. 표본은 `call_uid` 해시로 고른다. 저장 뒤 두 모델 라벨이 보인다
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
| 모델 (확정) | 프로바이더 **Anthropic**·**Upstage**(LLM). ① 분류기 라벨·요약·할 일 경량 `claude-haiku-5-5` → 최종 vN-final `claude-sonnet-5-5`, 판정기(모델 B) `solar-mini4`(분류기와 다른 계열 — 별칭은 `config/llm.yaml`), 그 밖의 에이전트·③ 챗·④ 제안·⑤-4 초안·⑦ 브리핑·⑨ 문자 `claude-sonnet-5-5`, 생성기(통화 녹취록·문서·차트 서술) `claude-sonnet-5-5` |
| LLM | 게이트웨이 모듈 1개 `shared/llm.py` — §7.1 |
| 프롬프트 | `prompts/` — §7.2 |
| 테스트 fixture | **`make fixture`**: fake 게이트웨이로 생성한 데이터로 판정 풀 전체와 판정 풀 밖 일부 통화를 가짜 프롬프트 버전 v1·v2 두 벌로 분류하고 판정기 판정까지 적재(판정 큐에 엇갈림·표본이 생기고 블라인드 큐가 돌게) + 해시 기반 결정적 fake 임베딩으로 벡터 테이블까지 → `tests/fixtures/` 커밋. 테스트는 복사본 사용. CI 키 없이 판정 큐·검색이 동작 |
| 사람 데이터 보존 | 재생성해도 `app.sqlite`는 덮어쓰지 않는다 — 테이블별 처리는 §7.3 |
| 시간 | DB naive KST, API `+09:00`. `ax_*` 활동은 실제 시계. 기준일은 §6 |
| 실행 | `make dev`(API + 화면) · `make index / fixture / save-demo / reset-demo / set KEY=… VALUE=…`(설정 변경, 허용 키는 [[치과 AX - 기능 정의]] §7) |

### 7.1 LLM (원본은 여기)

- **게이트웨이**: 모듈 1개 `shared/llm.py`(생성기 7·9·9b단계의 API 채우기도 경유). 프로바이더마다 공식 SDK를 직접 쓴다 — Anthropic, Upstage(OpenAI 호환 API라 openai SDK). 모델의 프로바이더는 `config/llm.yaml` 모델 항목의 `provider`. 두 곳뿐이라 LiteLLM 같은 변환 계층을 두지 않는다
- **설정** `config/llm.yaml`: 별칭 → 모델 순서(앞 모델이 일시 오류(재시도 소진)·거절이면 다음 모델로 폴백, `fallback_used` 기록), 모델별 단가, 일일 비용 상한(`daily_usd_limit` — 넘으면 즉시 호출을 하지 않음. Batch 제출은 보지 않는다 — [[치과 AX - 기능 정의]] §7). 서버 측 거절 폴백을 지원하는 모델은 켠다(`server_fallback`)
- **구조화 출력**: JSON 스키마(Anthropic `output_config.format`, Upstage `response_format` `json_schema` strict)
- **기록**: 호출 1건 = 기록 1행 — 생성기는 jsonl, 앱은 `ax_llm_calls`
- **Batch**(생성기 API 채우기·① 일괄 분류): Anthropic 모델만(Upstage는 일반 호출). 별칭의 첫 모델만 쓰고 서버 측 거절 폴백을 쓰지 않으며(Batch에서 거부) Batch 단가(`config/llm.yaml` 단가 × `shared/llm.py` `BATCH_PRICE_SHARE`)로 비용을 센다 — 거절 건은 생성기 들여오기에서 실패로 남아 다음 시도로 다시 나온다
- **`FakeGateway`**: 같은 인터페이스로 입력 해시에서 결정적 응답(스키마가 있으면 스키마에 맞는 JSON) — 테스트·CI·fixture용
- **도구 호출**: `complete(tools=…)`는 `tool_use` 종료도 성공으로 받아 호출과 블록을 돌려준다. 도구를 주지 않은 호출의 `tool_use`는 실패다. 루프는 §3b

### 7.2 프롬프트

- **앱**: `prompts/<task>/<version>.md` — 파일명이 곧 `prompt_version`. task: `classify`(분류기 — 호출이 둘이라 버전 폴더 `prompts/classify/<version>/`의 `labels.md`·`summary.md`, 폴더명이 `prompt_version`이고 둘 중 하나의 텍스트나 모델이 바뀌면 버전을 올린다), `chat`(챗 시스템), `prep`(준비표 제안), `judge`(판정기 — 판정 기준 포함), `blind_judge`(요약 쌍 비교 — 평가 설계 §2.4), 에이전트마다 task 하나
  - 버전 파일은 고치지 않고 새 버전으로 추가(비교 재현성) — 모델만 바꿔도 같은 텍스트로 새 버전(§1 `prompt_version` 정의 — 모델 별칭이 있는 classify)
  - 쓰는 버전: classify는 `ax_settings.active_prompt_version`, 나머지는 `prompt_versions`. 자동 개선은 새 버전 파일을 추가한다. 저장 결과가 없는 task는 바로 전환하고, classify는 `pending_prompt_version`으로 올린다
  - **classify 전환**: 사람이 `POST /jobs/classify-batch`로 대기 버전을 전체 통화에 돌린다. 그동안 화면·큐·야간은 이전 활성 버전을 쓴다. 전부 `ok`가 되면 같은 트랜잭션에서 활성 버전을 바꾸고 엇갈림을 다시 계산한다. `failed`가 남으면 전환하지 않는다
  - **전환 뒤 상태**: 처리함과 되돌림은 통화 단위라 그대로 둔다. 자동 종결은 새 분류로 조건을 다시 계산해 못 넘는 통화의 종결을 풀고 큐로 돌린다
  - 되돌리기는 이전 버전으로 다시 설정한다. 이전 분류 행이 남아 있어 재분류가 필요 없다
- **생성기**(`gen_transcript`·`gen_document`·`gen_chart`): 작업당 파일 하나 `prompts/<task>.md`를 고쳐 쓴다 — LLM 캐시 키에 그 파일 해시가 들어가 출력을 구분하고, 이력은 git

### 7.3 사람 데이터 보존

재생성해도 `app.sqlite`는 덮어쓰지 않는다. 다만 앱 테이블이 가리키는 생성 데이터의 키(`call_uid`·환자·예약·`fin_` id)가 다른 실체를 가리킬 수 있어 테이블마다 처리를 정한다.
- **다시 만듦**: 색인(`ax_chunks`·FTS·벡터)·분류·판정기 판정·LLM 쌍 비교·엇갈림·준비표·⑦ 브리핑·⑥ 리포트·⑤ 결과·에이전트 실행 기록·되돌리지 않은 자동 처리·환자 묶음·발송함·`ax_jobs`
- **해시로 다시 붙임**: 판정(메모 포함)·투표 — `hash(transcript)`로만(`export-human/import-human`), 미스 시 버리고 "복원 실패 N건" 리포트. 투표는 `(hash, left_model, left_version, right_model, right_version, choice)`. 판정 상태는 판정 유무에서 파생한다
- **자연키로 보존**: 되돌린 자동 처리 — 아래 키로 다시 붙이고, 못 붙이면 리포트한다. 되돌린 건은 같은 키로 다시 자동 처리하지 않는다

  | 작업 | 자연키 |
  |---|---|
  | ① 자동 종결 | `hash(transcript)` |
  | ⑧-1 자동 마감 | 날짜·결제수단·차이 금액 |
  | ⑧-2 자동 확정 | 출금일·금액·적요 해시 |
  | ⑨ 안내 기록 | 차트 번호·안내 종류·기간 |

- **그대로 둠**: 실수 후보(승인 상태 포함, 실행 id는 끊겨도 된다)·`ax_rule_counts`·게시한 문서 초안·`ax_settings`(`auto_policy` 제외)
- **강제로 끔**: 앱 기동과 `make nightly` 시작 때 `source.sqlite` 지문을 `source_fingerprint`와 비교해, 다르면 `auto_policy`를 전부 끄고 지문을 갱신한다. 다시 측정한 뒤 사람이 켠다
- **버림**: 후속 큐 처리·⑧ 확정·⑨ 연락 기록·게시 전 문서 초안·붙여넣기 통화·`ax_search_logs`·`ax_llm_calls`

## 8. 미결

- [ ] `prep_facts` fact 키 목록 (④ 착수 시 — 후검증·회귀 테스트의 기준)
- [ ] backend가 `master` 값을 읽는 방법 (④·⑤·⑧ 착수 시) — 주의 칩의 관련 시술·행동(④, `ALERT_TYPES`), 시술별 허용 상병(⑤-2 상병 확인 경고, `PROCEDURE_DIAGNOSES`), 재심 기한 일수(⑤-3, `APPEAL_DAYS`), 거래처 계정 과목(⑧ 전 ⑥ 재료비, `VENDORS`), 급여 적용 연령 범위(⑤-2 나이 규칙), ⑧ 원인·계정 과목 enum과 구강용품 가격표·카드사 수수료율·정산 지연·현금 입금 요일(⑧-1 규칙 기준선). 원본은 `generator/master.py`인데 backend는 `generator/`를 import하지 않는다(CLAUDE.md). 앱이 읽는 `source.sqlite`에는 코드·이름(`emr_alert_types`·`emr_diagnoses`)만 있다. 후보: 생성기가 `ax_` 테이블로 적재 / 매핑을 `shared/`로 옮겨 생성기·backend가 같이 읽음. 평가(반영률·과잉 언급률)도 같은 매핑을 써야 한다
- [ ] ⑨ 점수 규칙과 값 (⑨ 착수 시) — 앱 로직이라 `master`가 아니라 앱 쪽 설정이 원본

## 9. 프론트

화면의 레이아웃·동작 기준은 와이어프레임 `docs/wireframe/index-v2.html`이고(일별 예약표의 소요 시간 카드·환자 요약 카드, 전환 전 EMR 설명 카드는 [[치과 AX - 기능 정의]] §4가 기준), 색·모양은 `frontend/app/globals.css` 토큰과 `frontend/components/ui`에서 나온다(전환 전 EMR 창 안은 `--emr-*` 토큰과 `components/emr/emr-ui.tsx`). 기능과 표시 규칙은 [[치과 AX - 기능 정의]] §4 앱 구성.

**페이지**

| 라우트 | 화면 | API 원본 |
|---|---|---|
| `/` | 시작 화면 | 없음 |
| `/today` | 오늘 | [[치과 AX - 기능 정의]] §4 앱 구성 |
| `/calls` | 통화 목록·상세, 후속 큐, 붙여넣기 | [[치과 AX - 기능 정의]] §4 ① |
| `/schedule` | 일별 예약표(읽기 전용) | 여기: `GET /emr/schedule?date=`(`backend/emr.py` `schedule_day` — 영업시간·휴진, 진료 담당자 열, 그날 예약 전부(취소·노쇼 포함 — 숨김은 화면에서). 예약마다 예약 미확인(기준일만)·예약 확인(`confirm` — 전날 리마인드 응답인 `confirmed`와 리마인드 유무)·신환·치료계획 몇 번째(대체 항목 제외)·잡은 경로와 시각·잡은 통화(같은 환자·통화 시각 = `created_at`). 응급 여부는 ① 착수 때 그 통화의 `ax_call_classifications`로 붙이고 판정 풀 통화는 뺀다), 패널은 `GET /emr/patients/{chart_no}`의 `summary`(`patient_summary` — 미수·주의사항·마지막 내원·앞으로의 예약·노쇼·취소·남은 항목(`open_items`), 모두 기준일 기준) |
| `/prep` | 준비표(표·회의·체어) | [[치과 AX - 기능 정의]] §4 ④ |
| `/ask` | 질문하기, 기록 검색, 문서함 | [[치과 AX - 기능 정의]] §4 ③ |
| `/eval` | 판정, 블라인드, 정확도, 자동 개선 | [[치과 AX - 기능 정의]] §4 ② |
| `/emr` | 전환 전 EMR(데스크·예약·전자차트) + 설명 카드 | 여기: `GET /emr/patients?q=`, `GET /emr/patients/{chart_no}`(`patient_record`), `GET /emr/schedule?date=`, `GET /emr/tour`(따라 보기 기준 환자 — `tour_patient`) |
| `/claims` | 삭감 대응, 청구 전 점검 | [[치과 AX - 기능 정의]] §5 ⑤, 착수 때 |
| `/report` | 경영 리포트 | [[치과 AX - 기능 정의]] §5 ⑥, 착수 때 |
| `/finance` | 수입 일계표, 지출 증빙 | [[치과 AX - 기능 정의]] §5 ⑧, 착수 때 |
| `/followup` | 후속 관리 | [[치과 AX - 기능 정의]] §5 ⑨, 착수 때 |

- `/emr/*`은 `source.sqlite`만 읽기 전용으로 연다(색인 없이도 동작). 탭·패널 이동 수는 화면 상태이고 저장하지 않는다.

**공통 레이아웃**: 상단에 로고(시작 화면으로) + 메뉴 + 담당 파트 선택 + 기준일. 병원 이름·기준일은 `GET /clinic`. 시작 화면은 메뉴가 없고, `/eval`·`/emr`은 메뉴 자리에 공간 이름과 돌아가기가 나온다.

**이름 표시**: 통화·문자 화면은 환자 이름과 차트 번호를 함께 보인다. 차트 서술 화면은 차트 번호만 보이고 이름을 붙이지 않는다(§3 prefix). 환자에는 차트 번호로 닿는다.

**데이터 가져오기**: 화면 진입 때 조회한다. 배치(①·④)와 야간 실행은 작업을 시작한 뒤 `ax_jobs`를 폴링하고, ③ 챗은 SSE 진행 이벤트를 받는다.

**공통 컴포넌트**: `AiBadge`(AI가 만듦), `Confirmed`(사람이 정함), `Estimated`(`[추정]`), `AgentTrace`(조사 과정), `AutoAction`(자동 처리 이유·되돌리기). 코드 값에는 표시를 붙이지 않는다.

**판정 키 매핑**: 항목을 순서대로 입력하고 입력이 끝나면 다음 항목으로 넘어간다. 값은 enum 순서를 따른다([[치과 AX - 가상 데이터 생성 설계]] §4.1).

| 항목 | 자리 | 값 |
|---|---|---|
| 목적 | 2 | `01`부터 enum 순서, `00` 모르겠음·애매 |
| 상대 | 1 | `1`부터 enum 순서, `0` 모르겠음·애매 |
| 급함 | 1 | `1` 낮음, `2` 보통, `3` 급함, `0` 모르겠음·애매 |
| 불리언 6개 | 1 | `1` 아니오, `2` 예, `0` 모르겠음·애매 |
