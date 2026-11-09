# ③ 질문하기 에이전트 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 브랜치 `feat/chat-agent` — 도구 6개 질문하기 에이전트(인용 확인·숫자 후검증·거절), `ax_search_logs`, `POST /chat` SSE, 질문하기 탭, 챗 평가(새 지표 셋·단순 RAG 기준선), 청크 겹침 비교.

**Architecture:** `backend/chat.py`가 기존 하네스(`Runner`)로 도구 루프를 돌리고, 종결 도구 `answer`의 `after` 훅이 인용·숫자를 검사해 첫 실패만 막아 같은 루프에서 다시 답하게 한다. 실행이 끝나면 `chat()`이 상태를 정해 `ax_search_logs`에 1행을 쓴다. 평가는 `app.sqlite` 사본에서 `eval_hooks.App.chat`을 부른다.

**Tech Stack:** Python 3 · sqlite3(SQL 그대로) · Pydantic · FastAPI `StreamingResponse` · Anthropic SDK(게이트웨이 경유) · Voyage · Next.js(Tailwind + shadcn/ui) · pytest

**Spec:** `docs/superpowers/specs/2026-11-09-chat-agent-design.md`

## Global Constraints

- `backend/`는 `evaluation/`·`ground_truth/`·`generator/`를 import하지 않는다 — 주석·독스트링에도 "evaluation" 낱말을 쓰지 않는다(`tests/test_no_ground_truth_import.py`)
- 정답(`truth_*`)은 `data/`에 넣지 않는다. 챗 평가 실행은 `app.sqlite` 사본(`evaluation/runs/`)에서만 쓴다
- 주석·독스트링은 한국어, 근거 문서 절을 가리킨다. 모듈 첫 독스트링에 하는 일과 원본 문서
- 값은 이름으로 — 시술명은 `source.sqlite` `emr_procedures`에서, 모델·단가는 `config/llm.yaml`에서
- 챗 모델 별칭 `chat`(Sonnet 5.5, 폴백 Sonnet 5). `CHAT_MAX_TOOL_CALLS = 6`
- 프론트: 색·모서리·글꼴은 `frontend/app/globals.css` 토큰만, 부품은 `frontend/components/ui/`만
- 커밋 전 `make fmt`(따로 실행) → `make lint` → `make test`. `git add`는 파일 이름으로. 커밋 메시지 `<영역>: <무엇>` + 본문 글머리 + 트레일러 두 줄:
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` / `Claude-Session: https://claude.ai/code/session_01G62cGwhYixc5LpBLhj8iGp`
- 과금 실행(Task 10·11)은 돌리기 전에 사람에게 묻는다. 푸시·PR은 요청이 있을 때만

## Review Focus

1. 같은 질문을 화면에서 두 번 연달아 보낼 때 — 각 질문이 자기 `ax_search_logs` 행·실행을 갖고 서로 막지 않는다(실행 `target`이 로그 id라 겹치지 않음) → Task 5 `test_two_chats_do_not_block_each_other`
2. 도구가 빈 결과를 돌려줄 때(검색 0건·차트 0건) — 모델이 `refuse(not_found)`로 끝내면 `refused`, 아무 인용 없이 숫자 없는 답을 내면 데이터형이 아닌 한 인용 없는 답이 화면에 나가지 않는다 → Task 5 `test_answer_without_citation_after_search_is_cite_failed`
3. 질문이 공백·너무 긴 글일 때 — `POST /chat`이 422로 거절하고 실행을 만들지 않는다 → Task 6 `test_chat_rejects_blank_and_long_question`
4. SSE 도중 클라이언트가 끊을 때 — 서버 실행은 끝까지 돌고 로그 행이 남는다(반쯤 쓴 행 없음) → Task 6 `test_chat_log_written_even_if_stream_closed`
5. 하루 비용 상한에 이미 닿은 상태에서 질문 — 모델을 부르지 않고 `budget` 상태로 고정 문구 → Task 5 `test_budget_exceeded_is_budget_status`

---

### Task 1: DB 준비 — WAL·`busy_timeout`, `ax_search_logs`

**Files:**
- Modify: `backend/db.py` (`connect`)
- Modify: `shared/app_schema.sql` (새 테이블)
- Test: `tests/backend/test_db.py`

**Interfaces:**
- Produces: `ax_search_logs(id INTEGER PK, kind TEXT NOT NULL CHECK (kind IN ('chat','search_summary')), question TEXT NOT NULL, tool_calls TEXT NOT NULL DEFAULT '[]', retrieved_ids TEXT NOT NULL DEFAULT '[]', answer_text TEXT, cited_ids TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL CHECK (status IN ('running','answered','refused','cite_failed','number_failed','call_limit','cost_limit','budget','failed')), refuse_reason TEXT, run_id INTEGER, fallback_used INTEGER NOT NULL DEFAULT 0, is_eval INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)`. `running`은 실행 중 행(실행 `target` = 이 행 id를 먼저 만들기 위해)

- [ ] **Step 1: 실패하는 테스트**

```python
def test_connect_uses_wal_and_busy_timeout(tmp_path):
    conn = make_conn(tmp_path)  # 이 파일의 기존 도우미 방식으로 app·source 경로를 만든다
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == db.BUSY_TIMEOUT_MS


def test_search_logs_table(tmp_path):
    conn = make_conn(tmp_path)
    db.init_schema(conn)
    conn.execute(
        "INSERT INTO ax_search_logs (kind, question, status, created_at) VALUES ('chat','q','running','2026-12-01 09:00:00')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO ax_search_logs (kind, question, status, created_at) VALUES ('chat','q','weird','x')"
        )
```

- [ ] **Step 2: 실행해 실패 확인** — `uv run pytest tests/backend/test_db.py -k "wal or search_logs" -v` → FAIL(journal_mode `delete`, 테이블 없음)
- [ ] **Step 3: 구현** — `db.BUSY_TIMEOUT_MS = 5000`, `connect`에서 `PRAGMA journal_mode = WAL`·`PRAGMA busy_timeout`. 스키마에 위 테이블(주석: 앱 설계 §3)
- [ ] **Step 4: 통과 확인** — 같은 명령 PASS, 이어서 `make test` 전체 PASS(기존 DB 테스트가 WAL로 깨지지 않는지)
- [ ] **Step 5: 커밋** — `git add backend/db.py shared/app_schema.sql tests/backend/test_db.py` · `backend: WAL·busy_timeout, ax_search_logs`

---

### Task 2: 숫자 후검증 공용 함수

**Files:**
- Create: `backend/verify.py`
- Modify: `backend/followup.py` (`_numbers`·`verify_facts`를 옮기고 import)
- Test: `tests/backend/test_verify.py`, `tests/backend/test_followup.py`(기존 `test_verify_facts` 그대로 통과)

**Interfaces:**
- Produces:
  - `numbers(text: str) -> set[int]` — 쉼표 제거 후 정수
  - `allowed_numbers(sources: list[str], results: list = (), count_rows: bool = False) -> set[int]` — 글의 숫자 + (`count_rows`면) `results` 안 모든 리스트의 길이(중첩 포함)
  - `numbers_ok(text: str, allowed: set[int]) -> bool`
  - `verify_facts(summary, facts, sources) -> bool`(후속 에이전트용, 동작 그대로 — 위 함수로 다시 씀)

- [ ] **Step 1: 실패하는 테스트**

```python
def test_row_count_allowed_only_when_asked():
    results = [{"count": 20, "appointments": [{"id": 1}] * 9}]
    assert numbers_ok("9건", allowed_numbers([], results, count_rows=True))
    assert not numbers_ok("9건", allowed_numbers([], results))


def test_sum_and_new_numbers_blocked():
    assert not numbers_ok("합계 30,000원", allowed_numbers(["10,000원", "20,000원"]))


def test_commas_stripped():
    assert numbers_ok("16,000원", allowed_numbers(["본인부담 16000원"]))
```

- [ ] **Step 2: 실패 확인** — `uv run pytest tests/backend/test_verify.py -v` → FAIL(모듈 없음)
- [ ] **Step 3: 구현** — `backend/verify.py`(독스트링: 기능 정의 §4 ③ 숫자 후검증·④ 후검증과 같은 방식). `followup.py`는 `from backend.verify import verify_facts`
- [ ] **Step 4: 통과 확인** — `uv run pytest tests/backend/test_verify.py tests/backend/test_followup.py -v` PASS
- [ ] **Step 5: 커밋** — `backend: 숫자 후검증을 공용 모듈로(목록 행 수 허용 선택)`

---

### Task 3: `query_charts` 도구

**Files:**
- Modify: `backend/query_tools.py` (`ChartArgs`, `_charts`, `query_tools`에 등록)
- Test: `tests/backend/test_query_tools.py`

**Interfaces:**
- Produces: 도구 `query_charts`, `ChartArgs(procedure: str(min 1), prev_procedure: str|None, min_gap_days: int|None(ge 0), max_gap_days: int|None(ge 0), same_tooth: bool = False, date_from: date|None, date_to: date|None, chart_no: str|None, limit: int = 10 (1~20))`. 반환 `{count, results: [{id: "chart:<treatment_id>", chart_no, performed_at, procedure, tooth_no, dx_code, note, prev?: {procedure, performed_at, gap_days}}]}`, 별칭이 여러 시술에 걸리면 `{count: 0, results: [], candidates: [이름…]}`. `query_tools(clinic)`가 돌려주는 목록 순서는 기존 넷 + `query_charts`를 `refuse` 앞에

- [ ] **Step 1: 실패하는 테스트** — 이 파일의 기존 source 픽스처에 `emr_procedures`(스케일링·치주치료, 별칭 JSON)·`emr_treatments`·`emr_chart_notes` 행을 더해서:

```python
def test_gap_condition_finds_short_interval(conn):
    out = run_tool(
        conn,
        "query_charts",
        {"procedure": "잇몸치료", "prev_procedure": "스케일링", "max_gap_days": 4},
    )
    assert [r["id"] for r in out["results"]] == ["chart:2"]  # 3일 뒤 치주치료만
    assert out["results"][0]["prev"]["gap_days"] == 3
    assert "name" not in out["results"][0]  # 환자 이름 없음


def test_ambiguous_alias_returns_candidates(conn):
    out = run_tool(conn, "query_charts", {"procedure": "임플란트"})  # 1차·2차 공용 별칭
    assert out["results"] == [] and set(out["candidates"]) == {"임플란트1차", "임플란트2차"}


def test_future_treatments_hidden(conn):  # performed_at > CLINIC.today 행은 나오지 않는다
    ...
```

- [ ] **Step 2: 실패 확인** — `uv run pytest tests/backend/test_query_tools.py -k charts -v` → FAIL
- [ ] **Step 3: 구현** — 이름 해석은 `emr_procedures.name` 일치 우선, 없으면 `aliases`(JSON)에 든 시술들. 간격은 같은 환자 직전 `prev_procedure` 진료와의 `julianday` 차(일). `same_tooth`면 `tooth_no` 일치. `performed_at ≤ clinic.today`. 정렬 `performed_at DESC`. 도구 설명에 "기간·간격 조건이 있는 차트 질문", 오늘 날짜
- [ ] **Step 4: 통과 확인** — 같은 명령 PASS
- [ ] **Step 5: 커밋** — `backend: 범위 조건 차트 조회 도구 query_charts`

---

### Task 4: Voyage 검색 비용 기록

**Files:**
- Modify: `backend/embeddings.py` (`VoyageEmbedder`가 호출마다 `usage`에 `(model, total_tokens)`), `backend/rag_search.py` (`voyage_rerank`도 같은 `usage`; `Searcher.drain_usage() -> list[tuple[str, str, int]]` — `(task, model, tokens)`, task `search_embed`/`search_rerank`)
- Modify: `backend/harness/store.py` 또는 새 함수 `backend/usage.py` `record_voyage(conn, usage, *, is_eval, ref) -> float`
- Modify: `config/llm.yaml` (`models`에 `voyage-4`·`rerank-3` 단가 — Voyage 가격 페이지 값, 주석에 출처), `backend/rag_index.py` (끝에 색인 임베딩 비용 출력만)
- Test: `tests/backend/test_usage.py`

**Interfaces:**
- Consumes: `config/llm.yaml` `models[<model>].input`($/1M 토큰)
- Produces: `record_voyage(conn, usage, *, is_eval: bool, ref: tuple[str, str] | None) -> float` — `ax_llm_calls`에 1행씩(`task`, `model_alias='voyage'`, `model_actual`=모델, `input_tokens`=토큰, `cost_usd`), 합계 반환. 하네스 도구 트랜잭션 밖에서 부른다

- [ ] **Step 1: 실패하는 테스트**

```python
def test_voyage_rows_count_toward_daily_limit(conn):
    record_voyage(conn, [("search_embed", "voyage-4", 1_000_000)], is_eval=False, ref=None)
    row = conn.execute("SELECT model_alias, cost_usd FROM ax_llm_calls").fetchone()
    assert (
        row["model_alias"] == "voyage"
        and row["cost_usd"] == load_config()["models"]["voyage-4"]["input"]
    )
    assert SqliteLlmLog(conn).spent(date.today(), "runtime") == row["cost_usd"]


def test_fake_embedder_has_no_usage(): ...  # FakeEmbedder로 만든 Searcher.drain_usage() == []
```

- [ ] **Step 2: 실패 확인** — `uv run pytest tests/backend/test_usage.py -v` → FAIL
- [ ] **Step 3: 구현** — 위 인터페이스대로. `drain_usage`는 쌓인 목록을 돌려주고 비운다
- [ ] **Step 4: 통과 확인** — 같은 명령 + `uv run pytest tests/backend/test_rag_search.py tests/backend/test_rag_index.py` PASS
- [ ] **Step 5: 커밋** — `backend: Voyage 검색 호출을 ax_llm_calls·하루 상한에`

---

### Task 5: 질문하기 에이전트 `backend/chat.py`

**Files:**
- Create: `backend/chat.py`, `prompts/chat/v1.md`
- Test: `tests/backend/test_chat.py` (게이트웨이는 `tests/backend/test_followup.py`의 `Script(FakeGateway)` 방식, 검색은 `FakeEmbedder` 색인)

**Interfaces:**
- Consumes: Task 1 `ax_search_logs`, Task 2 `allowed_numbers`·`numbers_ok`, Task 3 `query_charts`, Task 4 `Searcher.drain_usage`·`record_voyage`
- Produces:
  - 상수 `AGENT = "chat"`, `ALIAS = "chat"`, `TASK = "chat"`, `PROMPT_VERSION = "v1"`, `CHAT_MAX_TOOL_CALLS = 6`, `CHAT_MAX_USD`(dev 실측 전 임시 0.5), `ANSWER = "answer"`, `REFUSED_TEXT = "문서함에 없음"`, `FAILED_TEXT = "확인된 근거로 답하지 못했습니다"`
  - `chat(conn, gw, searcher, clinic, question: str, *, is_eval=False, on_event=None) -> dict` — `{log_id, status, tool_calls: [{name, args}], retrieved_ids, answer_text, cited_ids, refuse_reason, fallback_used}`. `tool_calls`에서 `answer`는 뺀다. `answer_text`는 `answered`일 때 모델 답, `refused`면 `REFUSED_TEXT`, 그 밖은 `FAILED_TEXT`. `on_event(dict)`는 도구 실행 직전 `{"type": "step", "tool": 이름}`
- 규칙(스펙 "에이전트" 절):
  - 등록 도구 = `search_docs`(챗 래퍼 — 차트 청크 `text`를 `emr_chart_notes.note` 원문 + 청크 prefix로 바꿈, 공용 `search_tool`은 그대로) + `query_tools(clinic)` 다섯(`query_patient`·`query_schedule`·`query_claims`·`query_charts`·`refuse`) + `answer(text: str(min 1), cited_ids: list[str])`(`ends_run`)
  - `AgentSpec.max_tool_calls = CHAT_MAX_TOOL_CALLS + 2`
  - 훅 `chat.call_limit`(before): `answer`·`refuse` 밖 도구 단계가 이미 6개면 막는다
  - 훅 `chat.cite_outside`·`chat.number_unverified`(after, `answer`만): 앞선 `answer` 단계가 막힌 적이 없을 때만 검사해 막는다(두 번째 답은 통과시켜 실행을 끝내고 `chat()`이 상태로 거른다). 숫자 허용은 `allowed_numbers([질문, 오늘], 도구 결과들, count_rows=True)`. 막는 이유 문구에 "건수는 필터를 주고 count를 읽는다"
  - `retrieved_ids` = `search_docs` 결과 청크 id + `query_charts` 결과 `id`(오류·막힌 단계 제외)
  - 상태 결정 순서: 하네스 `handed_off` reason `budget`/`cost_limit` → 그 상태 · `chat.call_limit`으로 막힌 단계가 있으면 `call_limit` · 마지막 정상 도구가 `refuse` → `refused`(`refuse_reason`) · 정상 `answer` 단계 → 다시 검사해 `answered`/`cite_failed`/`number_failed`(실패면 `cited_ids` 비우고 `store.add_block(규칙)`) · 그 밖 `failed`
  - 흐름: `ax_search_logs` 행을 `running`으로 넣고 커밋 → `Runner.start(target=str(log_id), text=질문)` → `drain_usage`를 `record_voyage(ref=("agent_run", run_id))` → 행을 최종 상태로 갱신·커밋. `fallback_used` = 그 실행의 `ax_llm_calls` 중 하나라도 폴백

- [ ] **Step 1: 실패하는 테스트** (각각 `Script` 대본으로)

```python
def test_answer_with_search_citation(conn):           # search_docs → answer(cited=[검색된 id]) → answered, tool_calls == [search_docs]
def test_cite_outside_retries_once_then_answers(conn): # answer(밖 id) → answer(안 id) → answered, ax_rule_counts chat.cite_outside == 1
def test_cite_outside_twice_is_cite_failed(conn):      # 두 번 다 밖 → cite_failed, cited_ids == [], answer_text == FAILED_TEXT
def test_unverified_number_twice_is_number_failed(conn):
def test_row_count_number_passes(conn):                # query_schedule 결과 목록 9행, 답 "9건" → answered
def test_refuse_is_refused_with_reason(conn):          # refuse(not_found) → refused, refuse_reason == "not_found", tool_calls[-1]["name"] == "refuse"
def test_seventh_tool_call_is_call_limit(conn):        # search_docs × 7 → call_limit
def test_chart_text_has_no_search_brackets(conn):      # 모델에 간 search_docs 결과의 차트 text에 괄호 꼬리 없음
def test_query_charts_ids_are_citable(conn):           # query_charts → answer(cited=["chart:2"]) → answered
def test_data_answer_without_citation_ok(conn):        # query_schedule → answer(cited=[]) "26명" → answered
def test_answer_without_citation_after_search_is_cite_failed(conn):  # Review Focus 2 — search_docs만 쓰고 cited=[] → cite_failed (query_patient·query_schedule·query_claims를 쓴 실행은 예외)
def test_two_chats_do_not_block_each_other(conn):      # Review Focus 1 — 같은 질문 두 번, 로그 2행·실행 2개
def test_budget_exceeded_is_budget_status(conn):       # Review Focus 5 — 하루 상한 넘긴 ax_llm_calls 행을 심고 질문 → budget, 모델 호출 0
def test_log_row_matches_return(conn):                 # ax_search_logs 행의 tool_calls·retrieved_ids·cited_ids·status가 반환과 같다
def test_max_tool_calls_matches_eval():
    from evaluation.metrics import MAX_TOOL_CALLS
    assert chat.CHAT_MAX_TOOL_CALLS == MAX_TOOL_CALLS
```

규칙 경계: `cited_ids`가 빈 `answer`는 데이터 도구(`query_patient`·`query_schedule`·`query_claims`)를 정상 실행한 적이 있으면 허용(스펙 "데이터형만 쓴 답은 `cited_ids`가 비어도 된다"), 없으면 인용 실패 — 이 검사도 `chat.cite_outside` 훅 안에서 한다.

- [ ] **Step 2: 실패 확인** — `uv run pytest tests/backend/test_chat.py -v` → FAIL(모듈 없음)
- [ ] **Step 3: 구현** — 위 인터페이스·규칙대로. `prompts/chat/v1.md`는 `prompts/followup/v3.md`와 같은 형식: 하는 일(확인한 사실만, 인용 필수, 숫자는 원문 표기 그대로 아라비아 숫자, 끝은 `answer` 또는 `refuse`), 도구 경계(건수·목록 → `query_*`, 기간·간격 조건이 있는 차트 → `query_charts`, 절차·규정·공지·과거 대화·차트 내용 → `search_docs`, 근거 없음·비공개 → `refuse`), 오늘 날짜는 입력에
- [ ] **Step 4: 통과 확인** — 같은 명령 PASS, `make test` 전체 PASS
- [ ] **Step 5: 커밋** — `git add backend/chat.py prompts/chat/v1.md tests/backend/test_chat.py` · `backend: 질문하기 에이전트(인용 확인·숫자 후검증·상태)`

---

### Task 6: `POST /chat` SSE

**Files:**
- Modify: `backend/api.py`
- Test: `tests/backend/test_api.py` (기존 API 테스트 방식 — 의존성 덮어쓰기로 가짜 게이트웨이·임베더)

**Interfaces:**
- Consumes: Task 5 `chat()`
- Produces: `POST /chat` body `{question: str}`(공백 제거 후 1~500자 — `CHAT_MAX_QUESTION = 500`) → `text/event-stream`. 이벤트 `event: step` `data: {"tool": …}` / `event: answer` `data: {"status", "text", "sources": [카드]}`(`sources` = `cited_ids`를 `backend/search_cards.py` `card` 형식으로, 차트는 차트 번호) / `event: error` `data: {"message"}`. 색인이 없으면 503(검색 엔드포인트와 같은 문구). 게이트웨이 의존성 `get_chat_gateway`(테스트가 덮어씀)
- 구현 방침: 작업 스레드가 자기 DB 연결을 열어 `chat()`을 돌리고 `queue.Queue`로 이벤트를 넘긴다. 스트림이 끊겨도 스레드는 끝까지 돈다

- [ ] **Step 1: 실패하는 테스트**

```python
def test_chat_streams_steps_then_answer(client): # 이벤트 순서 step… → answer, answer.status == "answered", sources[0]["id"] == 인용 id
def test_chat_rejects_blank_and_long_question(client):  # Review Focus 3 — "  " → 422, "가"*501 → 422, ax_search_logs 0행
def test_chat_log_written_even_if_stream_closed(client): # Review Focus 4 — 첫 이벤트만 읽고 닫아도 스레드 종료 뒤 로그 행 status != "running"
def test_chat_without_index_is_503(client):
```

- [ ] **Step 2: 실패 확인** — `uv run pytest tests/backend/test_api.py -k chat -v` → FAIL
- [ ] **Step 3: 구현** — 위대로
- [ ] **Step 4: 통과 확인** — 같은 명령 PASS
- [ ] **Step 5: 커밋** — `backend: POST /chat 진행 이벤트 SSE`

---

### Task 7: 질문하기 탭

**Files:**
- Create: `frontend/components/ask/chat-panel.tsx`, `frontend/lib/chat-api.ts`
- Modify: `frontend/components/ask/ask-view.tsx` (`chat` 탭 내용), 필요하면 `frontend/components/ask/result-card.tsx` 재사용
- Test: `make lint`(ESLint·Prettier·tsc), `npm --prefix frontend run build`, 수동 확인(`make dev`)

**Interfaces:**
- Consumes: Task 6 SSE 형식
- Produces: `streamChat(question: string, onEvent: (e: ChatEvent) => void, signal: AbortSignal): Promise<void>` — `fetch` POST 후 `ReadableStream`을 `event:`/`data:` 줄로 나눠 넘긴다. `ChatEvent = {type: "step"; tool: string} | {type: "answer"; status: string; text: string; sources: SearchCard[]} | {type: "error"; message: string}`
- 화면(와이어프레임 `docs/wireframe/index-v2.html` `askChat`): 질문 입력(탭을 바꿔도 유지 — `AskView` 상태에 둔다) → 질문 말풍선 + 진행 단계 줄(도구 이름을 한국어로: `search_docs` "문서·기록 찾는 중", `query_patient` "환자 조회 중", `query_schedule` "예약 조회 중", `query_claims` "청구 조회 중", `query_charts` "차트 조회 중") → 답변 카드(답 + 출처 카드, 차트는 차트 번호). `refused`·실패 상태는 서버가 준 고정 문구를 그대로. 이 세션의 질문들은 위에서 아래로 쌓인다(멀티턴 아님 — 각 질문은 독립)

- [ ] **Step 1: 구현** — 위대로, 부품은 `components/ui`만
- [ ] **Step 2: 검사** — `make lint` PASS, `npm --prefix frontend run build` PASS
- [ ] **Step 3: 수동 확인** — 실행 중인 `make dev`에서 `/ask?tab=chat`에 질문 하나(과금 소액 — Sonnet 질문 1개). 진행 단계·답·출처가 보이는지. 질문 전에 사람에게 한 줄 묻는다
- [ ] **Step 4: 커밋** — `frontend: 질문하기 탭(진행 단계·답변·출처)`

---

### Task 8: 챗 평가 — 사본·새 지표·기준선

**Files:**
- Modify: `backend/eval_hooks.py` (`supports_chat = True`, `chat()`, `baseline()`)
- Modify: `backend/chat.py` (`baseline(conn, gw, searcher, question, *, is_eval) -> dict` — 검색 1회(필터 없음, 기본 `top_k`) + `chat` 별칭 1회, 구조화 출력 `{text, cited_ids, refused}`), `prompts/chat_baseline/v1.md`
- Modify: `evaluation/rag.py`, `evaluation/eval.py`
- Test: `tests/evaluation/test_rag.py`, `tests/evaluation/test_eval_cli.py`, `tests/backend/test_chat.py`(기준선)

**Interfaces:**
- Consumes: Task 5 `chat()` 반환
- Produces:
  - `RagApp.baseline(question, qa_id, condition) -> {retrieved_ids, answer_text, cited_ids, refused}`
  - `rag.evaluate(...)["chat"]`에 `chart_citation`·`notice_value`·`answer_content`(각 `{k, n}`)와 `baseline`(`{citation, refusal, calls, cost_usd}` + 문항별 구제/손실 — 기존 `compare`·`paired` 재사용)
  - `chat()` 반환의 `fallback_used`가 참인 문항은 챗 지표 모수에서 빼고 `excluded_fallback` 수를 적는다
  - `eval.py`가 챗 부분을 돌릴 때 `app.sqlite`를 `evaluation/runs/rag.sqlite`로 새로 복사하고 그 사본으로 `load_app`(검색만이면 원본 그대로 읽기)
- 채점 규칙(스펙 "사람이 정함" 새 지표 셋):
  - 차트형도 챗을 돌린다. `chart_citation` = `cited_ids` ⊆ `retrieved_ids` and 정답 차트 하나 이상
  - `notice_value` = 정답 사실(`answer_chunk_hint` → `truth.truth_doc_meta.planted_facts`의 그 id) `words` 중 하나가 답에 있음
  - `answer_content` = 매뉴얼형은 위와 같은 `words` 규칙, 사례형은 `answer_chunk_hint` 구절(공백·쉼표 제거)이 답(공백·쉼표 제거)에 있음

- [ ] **Step 1: 실패하는 테스트** (`tests/evaluation/test_rag.py`의 가짜 앱·`make_rag_db`에 `truth_doc_meta` 행 추가)

```python
def test_chart_questions_run_chat_and_count_citation(conn):
def test_notice_value_fails_when_only_old_value(conn):     # 최신 청크 인용 + 답 "1시간"(옛 값) → citation 통과, notice_value 실패
def test_answer_content_case_phrase_normalized(conn):      # hint "350,000원", 답 "350000 원" → 통과
def test_fallback_questions_excluded(conn):                # fallback_used 문항은 모수에서 빠지고 excluded_fallback == 1
def test_baseline_paired_with_chat(conn):                  # 문서 검색형·거절형만, 구제/손실 목록
def test_chat_runs_on_copy(tmp_path):                      # eval 실행 뒤 원본 app.sqlite의 ax_search_logs 0행, 사본에 행
```

- [ ] **Step 2: 실패 확인** — `uv run pytest tests/evaluation/test_rag.py tests/evaluation/test_eval_cli.py -v` → 새 테스트 FAIL
- [ ] **Step 3: 구현** — 위대로. 리포트 표에 세 지표 줄과 기준선 짝 비교 줄(보고 항목 표시)
- [ ] **Step 4: 통과 확인** — 같은 명령 + `make test` PASS
- [ ] **Step 5: 커밋** — `eval: 챗 평가 — 사본 실행·차트형 인용·공지형 현재 값·답 내용·단순 RAG 기준선` (backend 파일은 같은 커밋에 넣지 말고 `backend:` 커밋을 먼저)

---

### Task 9: 문서 맞추기

**Files:**
- Modify: `docs/치과 AX - 기능 정의.md`(§1 표·§4 ③ "도구 5개" → 6개·`query_charts`, §9 "③ 챗 착수 시" 줄에서 끝난 항목 지우기 — `doc_type`은 안 씀으로 닫음), `docs/치과 AX - 앱 설계.md`(§2 `ref_type` 목록·덤프 칸 결론·Voyage 행, §3 도구 표 `query_charts` 행·`ax_search_logs` 칸·상태·숫자 후검증·`chat.number_unverified`·`chat.call_limit`, §3b 표·Voyage 기록, §6 WAL과 `save-demo`), `docs/치과 AX - 평가 설계.md`(§3.3 표 세 줄, 보고 항목), `docs/치과 AX - 개발자 소개.md`, `docs/wireframe/index-v2.html` 도구 안내 문구, `CLAUDE.md`(명령에 챗 관련이 생기면만)
- Test: `make test`(`tests/generator/test_doc_sync.py` 등 문서 비교 테스트)

- [ ] **Step 1: 고치기** — 값은 코드 이름으로 가리킨다(문서에 상수 숫자 복사 금지). 경위·날짜는 넣지 않는다
- [ ] **Step 2: 검사** — `grep -rn "도구 5개\|5개 도구" docs CLAUDE.md` 0건, `make test` PASS
- [ ] **Step 3: 커밋** — `docs: ③ 챗 — 도구 6개·ax_search_logs·새 지표·Voyage 기록`

---

### Task 10: dev 챗 평가 첫 실행 (과금 — 사람에게 먼저 묻는다)

- [ ] **Step 1: 묻기** — 예상 비용(dev 35문항 챗 + 기준선, 기능 정의 §7 "③ 답변 평가" 범위)과 명령을 알리고 승인 받기
- [ ] **Step 2: 실행** — `uv run --env-file .env python -m evaluation.eval --split dev` → exit 0, 리포트(`reports/`)
- [ ] **Step 3: 확인** — 완료 기준: 데이터형 도구 선택 ≥ 1/2, `--retrieval-only` Hit@5 ≥ 1/2. 실패 문항 원인 분류. `CHAT_MAX_USD`를 실측 질문당 최대 비용의 약 3배로 고치고(`backend/chat.py`) 테스트 PASS
- [ ] **Step 4: 기록** — `docs/치과 AX - AI 실험 기록.md` 새 절(문제→원인→시도→결과→한계): 지표 표, 기준선 짝 비교, 막힘 상태 분포(`cite_failed`·`number_failed`·`call_limit`), 비용
- [ ] **Step 5: 커밋** — `docs: ③ 챗 dev 첫 평가 기록` (+ `CHAT_MAX_USD` 변경은 `backend:` 커밋)

---

### Task 11: 청크 겹침 비교 (과금 — 사람에게 먼저 묻는다)

**Files:**
- Modify: `backend/rag_index.py` (`--overlap N`·`--db PATH` 인자 — 기본은 `OVERLAP`·`data/app.sqlite`. 청크 글이 바뀐 id는 그 모델 벡터를 지우고 다시 계산 — 지금은 "없는 청크만" 계산해 글이 바뀐 청크의 옛 벡터가 남는다)
- Modify: `evaluation/eval.py` (`--db`로 겹침 사본을 채점할 수 있는지 확인 — 이미 있으면 그대로)
- Test: `tests/backend/test_rag_index.py`

- [ ] **Step 1: 실패하는 테스트** — `test_changed_chunk_text_is_reembedded`: 같은 id 청크의 글이 바뀌면 다시 색인할 때 그 청크 벡터가 새 글로 계산된다(FakeEmbedder 호출 텍스트로 확인), `test_overlap_argument_changes_chunks`
- [ ] **Step 2: 실패 확인 → 구현 → 통과 확인** — `uv run pytest tests/backend/test_rag_index.py -v`
- [ ] **Step 3: 커밋** — `backend: 색인 겹침·대상 DB 인자, 글이 바뀐 청크 다시 임베딩`
- [ ] **Step 4: 묻기** — 비교값(500자에 90자 = 18%), 비용(조건당 dev 3회, 합 약 $6~12 + 재색인 임베딩)을 알리고 승인 받기
- [ ] **Step 5: 실행** — `data/app.sqlite`를 `evaluation/runs/overlap90.sqlite`로 복사 → `uv run --env-file .env python -m backend.rag_index --embedding voyage-4 --overlap 90 --db evaluation/runs/overlap90.sqlite` → 10%·18% 각 dev 3회 챗 평가
- [ ] **Step 6: 판정·기록** — `answer_content`(사례형·매뉴얼형)가 3회 모두 같은 방향으로 2문항 이상 차이 날 때만 `OVERLAP`을 바꾸고 원본 다시 색인(사람 확인 뒤). 아니면 10% 유지. 실험 기록 §6을 결과로 고친다(구제/손실 문항과 원인)
- [ ] **Step 7: 커밋** — `docs: 청크 겹침 비교 결과` (값을 바꾸면 `backend:` 커밋 따로)
