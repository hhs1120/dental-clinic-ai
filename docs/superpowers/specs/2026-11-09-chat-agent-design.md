# ③ 물어보기 설계 — 질문하기·검색 요약·문서함

원본: `docs/치과 AX - 기능 정의.md` §4 ③(목적·검색 대상·화면·API·완료 기준)·§9 "③ 챗 착수 시" 줄, `docs/치과 AX - 앱 설계.md` §2(`ax_llm_calls`)·§3(검색·`ax_search_logs`·인용 확인·도구)·§3b(하네스)·§7.1(LLM)·§9(`/ask`), `docs/치과 AX - 평가 설계.md` §3(문항·채점·지표·단순 RAG 기준선), `docs/치과 AX - AI 실험 기록.md` §4(남은 차트 실패)·§6(청크 겹침). 이 문서는 그 규칙을 코드로 옮길 때 새로 정한 것만 적는다.

## 왜

직원이 매뉴얼·공지·지난 통화·문자·차트·환자 사실을 물으면 확인된 사실만 출처와 함께 답한다. 틀린 답은 직원이 환자에게 그대로 전하므로, 답을 잘하는 것보다 틀린 답을 내지 않는 것이 먼저다.

## 우선순위 (사람이 정함)

둘이 부딪치면 앞을 지킨다.

1. **틀리면 안 되는 것 — 최신 공지·숫자·거절.** 프롬프트가 아니라 코드로 막는다: 인용 확인, 숫자 후검증, 옛 공지 정렬, 근거 없으면 고정 거절. 엄격해서 맞는 답이 거절되는 쪽을 틀린 답보다 낫게 본다
2. **자주 쓰는 것 — 환자 조회·매뉴얼.** 도구 선택 정확도·Hit@5·인용 정확도로 올린다
3. **보여 줄 것 — 차트 케이스.** 이름 없이 진료 내용으로 차트를 찾는다. 범위 조건 도구와 데모·화면(차트 번호 표시)

## 범위 — 브랜치 셋, 이 문서는 앞의 둘

1. **`feat/chat-agent`**: 질문하기 에이전트(도구 6개·SSE·인용 확인·숫자 후검증·거절), `ax_search_logs`, `POST /chat`, 질문하기 탭, `eval_hooks.chat`·단순 RAG 기준선·새 지표 둘, 마지막 단계로 청크 겹침 비교
2. **`feat/search-summary-docs`**: 기록 검색 AI 요약 + "이걸로 질문하기"·"근거 더 보기" 연결, 문서함(`GET /documents`·`/documents/{id}`, 탭)
3. **`feat/doc-draft`**: 문서 초안 에이전트 — 착수할 때 따로 설계한다(기능 정의 §9 "③ 문서 초안 착수 시")

각 브랜치는 앞 PR이 머지된 뒤 main에서 딴다.

안 하는 것: 멀티턴, 토큰 스트리밍(진행 이벤트만), 문서 초안(브랜치 3), `backend`가 `master` 값을 읽는 방법(앱 설계 §8 — ④·⑤·⑧ 착수 때. 기존 `query_tools.SCALING`은 그대로 둔다), 검색 지표 실험(이미 끝남 — 실험 기록 §1~§5), `search_docs`의 `doc_type` 필터(내 결정 — `source_types`의 문서/사례 구분으로 충분하다고 보고 기능 정의 §9의 이 항목을 "안 씀"으로 닫는다).

실패로 볼 것: 검색·조회하지 않은 청크를 인용한 답이 화면에 나가는 것, 도구 결과·질문에 없는 숫자가 든 답이 화면에 나가는 것, 공지형 질문에 옛 값만으로 답하는 것, 거절형이 아닌 질문을 `refuse`하는 비율이 기준선보다 크게 느는 것, 차트 결과에 환자 이름이 나오는 것, 평가 실행이 운영 로그(`is_eval=0`)에 섞이는 것.

## 사람이 정함

- **브랜치 셋**(위), 이 문서는 ①~③ 중 1·2를 담는다
- **도구 6개** — 앱 설계 §3의 5개 + `query_charts`(범위 조건 차트 조회). 실험 기록 §4 "범위 조건 4개는 검색으로 풀 수 없어 ③ 챗의 조회 도구 몫"
- **모델**: `config/llm.yaml` `chat` 그대로 — Sonnet 5.5, 폴백 Sonnet 5(장애 때 화면이 멈추지 않게. 폴백 호출은 평가 집계에서 빠진다)
- **새 지표 셋(보고 항목, `make gate` 조건 아님)**
  - **차트형 인용 정확도**: 챗 답변 `cited_ids`에 정답 차트(`answer_doc_ids`의 `chart:<treatment_id>`)가 하나 이상 **and** `cited_ids` ⊆ `retrieved_ids`. 차트 Hit@5는 검색기 직접 호출이라 `query_charts`의 효과가 보이지 않아서 더한다
  - **공지형 현재 값**: 공지형 문항의 답변에 정답 사실(`answer_chunk_hint` → `truth_doc_meta.planted_facts`의 그 id)의 `words`가 하나 이상 나오는지. 옛 공지의 같은 사실 `words`만 나오고 현재 값이 없으면 실패. 최신 공지 청크를 인용하면서 "기존 ○○" 옛 값으로 답하는 경우를 잡는다
  - **답 내용**(사례형·매뉴얼형): 매뉴얼형은 정답 사실(`answer_chunk_hint`의 심은 사실 id)의 `words`가, 사례형은 `answer_chunk_hint` 본문 구절(공백·쉼표를 뗀 비교)이 답변에 나오는지. LLM 채점 없음. 사례형 구절을 모델이 바꿔 말하면 거짓 음성이 나지만 짝 비교의 양쪽에 같이 걸린다 — 겹침 비교의 판단 지표
- **청크 겹침 비교**: 브랜치 1의 마지막 단계. 판단 지표는 위 "답 내용"(사례형·매뉴얼형 — 경계에서 질문과 답이 갈리면 떨어지는 값). 인용 정확도는 문서 단위라 겹침 효과를 못 잰다(실험 기록 §6과 같은 한계). 10%(지금)와 15~20% 중 한 값을 조건마다 dev 3회씩 돌려, 3회 모두 같은 방향으로 2문항 이상 차이 날 때만 값을 바꾸고 아니면 10% 유지(구제/손실 문항과 원인을 함께), 결과는 실험 기록 §6. 새 겹침 색인은 `app.sqlite` 사본에서 만든다 — 결정 전에 앱 검색과 링크로 같은 DB를 쓰는 다른 작업 폴더가 바뀌지 않게. 과금 실행(약 $6~12)은 돌리기 전에 사람에게 묻는다

## 내가 정함 (검토 바람)

### 에이전트

- **모듈** `backend/chat.py` — `chat(conn, gw, searcher, clinic, question, *, is_eval=False, on_event=None) -> dict`. 반환은 `evaluation/rag.py` 머리 독스트링의 형식 `{tool_calls: [{name, args}], retrieved_ids, answer_text, cited_ids}`에 `status`·`log_id`를 더한 것. `on_event`는 SSE용 진행 콜백
- **하네스**: `backend/harness/loop.py`의 `AgentSpec`·루프를 그대로 쓰고 하네스 공용 코드는 고치지 않는다. `agent="chat"`, `target`은 `ax_search_logs.id`. 상한은 둘로 나눈다(내 결정): 하네스 `max_tool_calls = CHAT_MAX_TOOL_CALLS + 2`(답변 두 번 몫), 그리고 `before` 훅 규칙 `chat.call_limit`이 `answer`·`refuse` 밖 도구의 7번째 호출을 막고 실행을 `call_limit`으로 끝낸다. `CHAT_MAX_TOOL_CALLS = 6`은 테스트로 `evaluation.metrics.MAX_TOOL_CALLS`와 같은지 확인한다(후속 에이전트의 `test_max_tool_calls_matches_eval`과 같은 방식)
- **최종 답변**: 종결 도구 `answer(text, cited_ids[])`로 받는다(후속 에이전트의 `submit_followup`과 같은 모양). `answer`는 채점의 도구 호출로 세지 않는다 — `chat()`이 돌려주는 `tool_calls`에서 뺀다. `refuse(reason)`는 앱 설계 §3 그대로 호출 시 턴 종료이고 `tool_calls`에 남는다(거절 판정이 마지막 호출로 본다)
- **`retrieved_ids`**: 이번 질문에서 `search_docs`가 돌려준 청크 id + `query_charts`가 돌려준 `chart:<treatment_id>`의 합집합. `query_patient`·`query_schedule`·`query_claims`는 청크를 돌려주지 않는다 — 데이터형만 쓴 답은 `cited_ids`가 비어도 된다(앱 설계 §3)
- **후처리 순서**(`answer`를 받은 직후 코드, 앱 설계 §3 인용 확인을 확장)
  1. 인용 확인: `cited_ids` ⊆ `retrieved_ids`
  2. 숫자 후검증: 답변 텍스트의 숫자가 모두 "허용 숫자"에 있다 — 이번 질문의 도구 결과·질문 원문·오늘 날짜의 숫자(`backend/followup.py` `verify_facts`와 같은 규칙 — 쉼표 제거 후 정수) + 도구 결과 안 목록의 행 수(사람이 정함 — 모델이 목록을 세어 낸 건수는 허용, 합계·날짜 계산은 막음). 프롬프트에 "숫자는 원문 표기 그대로 아라비아 숫자로"를 넣는다. 함수는 `backend/`의 공용 자리로 옮겨 후속 에이전트와 같이 쓰고, 행 수 허용은 챗 쪽 인자로만 켠다(후속 에이전트 동작은 그대로). 막혔을 때 다시 답하게 하는 메시지에 "건수는 필터를 주고 `count`를 읽는다"를 넣는다
  3. 둘 중 하나라도 어긋나면 `answer`의 `after` 훅이 막고 어긋난 이유(검색 결과 밖 id / 근거 없는 숫자)를 도구 오류로 돌려줘 같은 루프에서 한 번 다시 답하게 한다(내 결정 — 하네스를 고치지 않는 길. 모델이 다시 검색할 수는 있고 그 호출도 6회 상한에 든다). 두 번째도 어긋나면 고정 거절 문구로 끝내고 `cited_ids`를 비운다. 이때 마지막 호출은 `refuse`가 아니고 상태가 남는다(앱 설계 §3)
  4. 막힐 때마다 `ax_rule_counts`에 더한다 — `chat.cite_outside`(앱 설계 §3에 이미 있음), `chat.number_unverified`(새로)
- **상태**(`ax_search_logs.status`): `answered` / `refused`(모델이 `refuse`) / `cite_failed` / `number_failed` / `call_limit` / `cost_limit`(질문당 `max_usd`) / `budget`(하루 상한, 하네스 `handed_off` reason `budget`) / `failed`(오류·종결 도구 없음). 화면에 답을 내는 것은 `answered`뿐, `refused`는 "문서함에 없음", 나머지는 고정 문구 "확인된 근거로 답하지 못했습니다"(문구 상수는 `backend/chat.py`)
- **문서 초안 입력**: `status='refused'`이고 `refuse` 인자가 `not_found`인 행만(앱 설계 §3). `call_limit`·`cite_failed`·`number_failed`는 들지 않는다
- **차트 괄호**: 모델에도 보이지 않는다. 챗 전용 `search_docs` 래퍼가 차트 청크 `text`를 화면 카드(`backend/search_cards.py`)처럼 `emr_chart_notes.note` 원문 + prefix로 바꿔 준다(내 결정 — 공용 `search_tool`은 그대로 둬 후속 에이전트 v3의 입력이 바뀌지 않게). 괄호의 동의어·기간 표기는 검색에만 쓴다 — 모델이 괄호의 "60일" 같은 말을 사실로 옮겨 숫자 후검증과 어긋나는 일을 막는다
- **프롬프트** `prompts/chat/v1.md`(앱 설계 §7.2) — 도구 경계(건수·목록 → `query_*`, 기간·간격 조건이 있는 차트 → `query_charts`, 절차·규정·과거 대화·차트 내용 → `search_docs`, 근거 없음·비공개 → `refuse`), 답에는 확인한 사실만·인용 필수, 오늘 날짜. 외부 텍스트는 하네스 `external()`로 감싼다
- **비용 상한**: `AgentSpec.max_usd`는 질문 1개 기준 상수(`CHAT_MAX_USD`, 구현 때 dev 1회 실측의 3배 정도로)

### `query_charts`

- **인자**(`backend/query_tools.py` `ChartArgs`, Pydantic): `procedure`(시술 이름 또는 별칭, 필수), `prev_procedure?`(바로 앞에 같은 환자가 받은 시술), `min_gap_days?`·`max_gap_days?`(앞 시술과의 간격), `same_tooth?`(앞 시술과 같은 치아, 기본 거짓), `date_from?/date_to?`, `chart_no?`, `limit?`(기본 10)
- **시술 이름 해석**: `source.sqlite` `emr_procedures`의 `name`·`aliases`에서 찾는다(새 상수를 박지 않는다 — 앱 설계 §8을 풀지 않고 비켜 간다). 여러 시술에 걸리는 별칭은 후보 목록을 돌려주고 결과는 비운다(실험 기록 §4·§5 별칭 오탐과 같은 이유)
- **반환**: `{count, results: [{id: "chart:<treatment_id>", chart_no, performed_at, procedure, tooth_no, dx_code, note, prev: {procedure, performed_at, gap_days}?}]}` — 환자 이름 없음(차트 서술은 비식별, 앱 설계 §3). `event_date ≤ TODAY` 강제
- **경계**: 간격은 같은 환자의 `emr_treatments.performed_at` 날짜 차이(일). "닷새도 안 돼서" = `max_gap_days=4`처럼 해석은 모델이 하고 도구는 숫자만 받는다

### `ax_search_logs`·`ax_llm_calls`

- **`ax_search_logs`**(`shared/app_schema.sql`, 앱 설계 §3에 칸 이름을 적는다): `id`, `kind`(`chat` / `search_summary` — 브랜치 2가 쓴다), `question`, `tool_calls` JSON(Pydantic 정규화 인자), `retrieved_ids` JSON, `answer_text`, `cited_ids` JSON, `status`, `refuse_reason`, `run_id`(`ax_agent_runs.id`), `is_eval`, `created_at`. 평가 실행은 `is_eval=1`
- **입력 덤프 경로 칸은 만들지 않는다**: 챗 입력·도구 결과·모델 응답은 하네스가 `ax_agent_steps`에 전부 남기고(재개·재생용), `ax_llm_calls.ref_type='agent_run'`·`ref_id`로 그 실행을 가리키면 다시 볼 수 있다. 앱 설계 §2의 "입력 덤프 경로 칸" 미결을 이렇게 닫는다
- **`ref_type` 목록**(앱 설계 §2에 적고 가상 데이터 생성 설계 §5.3 참조 검사에 맞춘다): 지금 코드가 쓰는 값을 모두 모으고 `agent_run`을 포함한다. 챗 호출은 `agent_run`
- **Voyage 비용**(내 결정): 검색의 질의 임베딩·리랭커 호출을 `ax_llm_calls`에 1행씩 남기고(`task`는 `search_embed`/`search_rerank`, `model_alias`는 `voyage`, `model_actual`은 모델 이름) `cost_usd`를 채워 `daily_usd_limit.runtime`이 센다. 앱 설계 §2의 "게이트웨이 호출 1건 = 1행"과 §3b의 "임베딩·Voyage 리랭커는 밖"을 "Voyage 검색 호출도 기록·상한에 든다"로 고친다(호출 경로는 게이트웨이 밖 그대로). 기록은 도구 트랜잭션 밖에서 쓴다 — 하네스가 읽기 도구 안의 쓰기를 막는다(`loop.py` 읽기 전용 검사). 색인(`make index`)의 임베딩은 하루 상한에 넣지 않고 색인 실행이 끝날 때 비용만 출력한다(겹침 비교 재색인이 챗을 막지 않게). 단가는 `config/llm.yaml` `models`에 Voyage 모델을 더하고, 무료 토큰 구간도 정가로 적는다

### API·화면

- **`POST /chat`** `{question}` → SSE. 이벤트: `step`(도구 이름·한 줄 요약 — "문서 찾는 중", "환자 조회 중"), `answer`(`{status, text, sources[]}` — `sources`는 `cited_ids`를 기록 검색 카드 형식(`backend/search_cards.py`)으로), `error`. 한 질문이 끝나면 스트림을 닫는다
- **동시 쓰기**: 앱 DB 연결에 WAL + `busy_timeout`을 켠다(`backend/db.py`). SSE 요청이 쓰는 동안 다른 화면의 쓰기가 막히지 않게. `save-demo`가 생기면 `-wal` 파일도 함께 복사해야 한다는 것을 앱 설계 §6에 적는다
- **질문하기 탭**(`/ask`, 와이어프레임 `index-v2.html` `askChat`): 질문 입력 → 진행 단계 표시 → 답변 카드(답 + 출처 카드, 차트 출처는 차트 번호). 거절·실패는 고정 문구. 탭을 바꿔도 입력 유지(기능 정의 §4 ③). "근거 더 보기"·"이걸로 질문하기" 연결은 브랜치 2

### 평가

- **평가 실행은 사본에서**(내 결정): 후속 처리 평가(`evaluation/followup.py`)처럼 `app.sqlite`를 `evaluation/runs/`에 복사해 그 사본으로 `make_app`을 연다 — `ax_agent_runs`·`ax_rule_counts`에는 `is_eval` 칸이 없어 운영 누계·화면의 조사 과정에 평가가 섞이고, test 문항과 답이 `data/`에 남기 때문. `backend/eval_hooks.py`는 `supports_chat = True`, `chat()`이 `backend.chat.chat(..., is_eval=True)`를 부르고 조건(`embedding=…,reranker=…`)의 Searcher를 쓴다. 문항 하나의 실행에 폴백 호출이 하나라도 있으면 그 문항은 집계에서 빼고, 짝 비교는 양쪽에서 함께 뺀다(평가 설계 §1 원칙 5) — `chat()` 반환에 `fallback_used`를 더한다
- **`evaluation/rag.py`**
  - 차트형도 챗을 돌린다(지금은 `q["type"] != "chart"`로 뺀다) — 차트형 인용 정확도 `chat.chart_citation`
  - 공지형 현재 값 `chat.notice_value` — 정답 사실(`answer_chunk_hint` → `truth_doc_meta.planted_facts`의 그 id)의 `words`가 답변에 하나도 없으면 실패. 옛 값과 짝짓지 않는다(현재 값이 없으면 옛 값으로 답했든 빠뜨렸든 실패라 짝이 필요 없다 — `seollal.days` ↔ `old.seollal`처럼 id 꼬리가 다른 공지도 그대로 된다)
  - 답 내용 `chat.answer_content` — 사례형·매뉴얼형, 규칙은 위 "사람이 정함"
  - 단순 RAG 기준선(평가 설계 §3.3): `RagApp`에 `baseline(question, qa_id, condition) -> {retrieved_ids, answer_text, cited_ids, refused}`를 더하고 문서 검색형·거절형에서 챗과 짝 비교(인용 정확도·환각 억제율의 구제/손실, 호출 수·비용). 기준선 답변 모델도 `chat` 별칭
- **평가 설계 §3.3 표에 세 줄**을 더하고(차트형 인용 정확도·공지형 현재 값·답 내용, 보고 항목), §3.1~3.2는 바꾸지 않는다
- **비용**: dev 1회는 챗 35문항(차트 포함) + 기준선 — 기능 정의 §7 표의 "③ 답변 평가" 줄 범위 안. 돌리기 전에 사람에게 묻는다

### 브랜치 2 (요약만, 착수 때 이 절을 다시 본다)

- 기록 검색 AI 요약: `GET /search` 결과로 같은 `chat` 모델이 `{text, cited_ids}`를 낸다(앱 설계 §3 "검색 화면의 AI 요약도 같은 출력"). 인용 확인·숫자 후검증은 브랜치 1의 함수를 그대로 쓴다. `ax_search_logs`에 `kind='search_summary'`
- "이걸로 질문하기"는 검색어를 질문하기 탭 입력으로, "근거 더 보기"는 질문을 기록 검색으로 넘긴다
- 문서함: `ax_documents` 그대로, 공지는 최신 버전만·시행일 표시, 읽기 전용

## 문서 고칠 곳 (브랜치 1에서)

- "도구 5개" → 6개: 기능 정의 §1 표·§4 ③, 앱 설계 §3(도구 표에 `query_charts` 행)·§3b 표, 개발자 소개, 와이어프레임 `index-v2.html`의 도구 안내 문구
- 앱 설계 §2(`ref_type` 목록·덤프 칸 결론·Voyage 행)·§3(`ax_search_logs` 칸·상태·숫자 후검증·`chat.number_unverified`·`chat.call_limit`)·§3b(Voyage 기록)·§6(WAL과 `save-demo`)
- 평가 설계 §3.3(세 지표)
- 기능 정의 §9 "③ 챗 착수 시" 줄에서 끝난 항목을 지운다
- 실험 기록: 챗 첫 평가 결과와 겹침 비교(§6 갱신)

## 빌더에게 남긴 것

함수 내부 구조, SSE 구현 방식(FastAPI `StreamingResponse` 등), 진행 이벤트 문구, 프롬프트 문장, 화면 세부(와이어프레임 따름), `CHAT_MAX_USD` 값, Voyage 기록 시점, 테스트 구성(가짜 게이트웨이 스크립트는 `tests/backend/test_followup.py`의 `Script` 방식 참고).
