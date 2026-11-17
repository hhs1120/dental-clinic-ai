# AI 비서 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 앱 셸을 아이콘 줄 + 목록 칸으로 바꾸고, 시작 화면을 대화형 업무 에이전트 "AI 비서"로 — 여러 기능을 엮어 조회하고 예약 초안을 만들며 사람이 승인하면 EMR 연동 함수가 `ax_` 기록에 넣어 예약표·준비표·미예약·오늘 화면에 반영된다. 시나리오 평가셋으로 잰다.

**Architecture:** 쓰기는 `backend/emr_bridge.py` 한 곳(슬롯 검사·초안·승인·원본+AX 합쳐 읽기). 에이전트 `backend/assistant.py`는 한 턴 = 도구 단계(하네스 실행, 도구는 `backend/assistant_tools.py`) → 답 단계(게이트웨이 1회 구조화 출력) — 두 단계 별칭을 따로 둬 다음 묶음(C: 도구 단계 로컬 Qwen)이 설정만 바꾼다. 하네스는 고치지 않는다. 대화는 `ax_assistant_*` 테이블. 평가는 `evaluation/assistant.py` + `ground_truth/assistant_scenarios.json`.

**Tech Stack:** Python 3.11 · sqlite3 · 하네스(`backend/harness/`) · 게이트웨이(`shared/llm.py`) · FastAPI(SSE) · Next.js(Tailwind + shadcn, lucide-react) · pytest · Playwright

**Spec:** `docs/superpowers/specs/2026-11-17-ax-assistant-design.md` (시안: `.superpowers/brainstorm/51528-1794895259/content/wireframe-v3.html`, 저장소 밖)

## Global Constraints

- 브랜치 `feat/chat-main`. `git add`는 파일 이름으로, 커밋 `<영역>: <무엇>` + 본문 글머리 + 세션 귀속 두 줄. 커밋 전 `make fmt` → `make lint` → `make test`
- `emr_*`·`fin_*`에 쓰지 않는다. backend의 쓰기는 `ax_` 테이블만, 예약 쓰기는 `backend/emr_bridge.py`만 거친다
- `backend/`는 `evaluation/`·`ground_truth/`·`generator/`를 import하지 않는다. master 값은 `source.ax_master_values`로만(`backend/prep.py` `master_values`)
- 데이터는 고정 — `emr_*`·`fin_*`는 그대로. Task 1은 `ax_master_values`에 이름 2개만 더한다(기존 행·LLM 캐시 영향 없음 — `make data` 뒤 `make verify`)
- 화면 이름: **AI 비서**(아이콘 `BotMessageSquare`) · 업무(`LayoutGrid`) · **자료실**(`FolderSearch`, 경로는 `/ask` 유지) · AI 평가(`Scale`) · EMR(`Monitor`) · 테마(`Sun`). 색·모서리는 `globals.css` 토큰, 부품은 `components/ui`
- 모델: `TOOL_ALIAS = REPLY_ALIAS = "chat"`(`config/llm.yaml` 기존 별칭). 새 별칭 없음
- ③ 에이전트(`backend/chat.py`·`POST /chat`)·③ 평가는 동작을 바꾸지 않는다(원본 예약만 읽음)
- 과금 실행(Task 12 dev 평가)은 사용자 승인 뒤. 테스트는 스크립트 게이트웨이(`tests/backend/test_followup.py` `Script`)
- 기준일 `config/generator.yaml` `today` = 2026-12-01(화), 휴진 목·일. 데모 칩 문구는 "지난주"
- 주석·독스트링 한국어, 근거는 이 설계 문서 절(`AI 비서 설계 §5.2`)과 기존 문서 절

## Review Focus

1. 승인 직전에 같은 시각이 다른 승인으로 찬 경우 — 초안 검사를 통과했어도 승인은 막고 이유를 돌려준다(Task 3 `test_approve_rechecks_and_rejects_conflict`)
2. 같은 초안을 두 번 승인(더블 클릭·두 탭) — 두 번째는 409, 예약·연락 기록이 두 줄 생기지 않는다(Task 3 `test_approve_twice_is_idempotent_conflict`)
3. 예약 원함 미예약 환자가 ⑨ 미예약 목록(`missed.facts`)에 없는 환자 — 승인은 성공하고 연락 기록만 건너뛴다(`record_contact`의 ValueError로 승인이 깨지지 않게, Task 3 `test_approve_patient_not_in_missed_skips_contact`)
4. 모델이 휴진일(목)·영업시간 밖·지난 날짜를 고른 경우 — `draft_appointment`가 오류 결과를 돌려주고 초안을 만들지 않는다(Task 3 `test_slot_closed_weekday`·`test_slot_before_today`, Task 5 `test_draft_tool_rejects_bad_slot`)
5. 답 단계가 이번 턴 도구 결과에 없는 환자·통화를 카드로 냄 — 그 카드는 버리고, 숫자 어긋남은 한 번 다시 부른 뒤 고정 문구(Task 6 `test_card_outside_turn_dropped`·`test_number_mismatch_retry_then_fixed_text`)

---

### Task 1: master 값 — 체어 수·원장 동시 건수

backend가 슬롯 검사에 쓸 값을 `ax_master_values`로 내보낸다(CLAUDE.md 병원 상수 규칙).

**Files:**
- Modify: `generator/clinic.py` `master_values_rows`
- Test: `tests/generator/` 기존 master_values 테스트 파일(없으면 `tests/generator/test_clinic.py`)

**Interfaces:**
- Produces: `ax_master_values` 이름 `chairs`(= `master.CHAIRS`), `doctor_concurrency`(= `master.DOCTOR_CONCURRENCY`) — backend는 `prep.master_values(conn)["chairs"]`로 읽는다

- [ ] **Step 1: 실패하는 테스트** — `test_master_values_has_capacity`: `dict(master_values_rows())`의 `json.loads(["chairs"]) == CHAIRS`, `json.loads(["doctor_concurrency"]) == DOCTOR_CONCURRENCY`
- [ ] **Step 2: 실패 확인** `uv run pytest tests/generator -k master_values_has_capacity -v` → FAIL(KeyError)
- [ ] **Step 3: 구현** — `values`에 두 키(주석: `AI 비서 설계 §5.2` 슬롯 검사)
- [ ] **Step 4: 통과 확인** 같은 명령 PASS, 그리고 `make data` → `make verify` 통과(LLM 작업 0건 출력 확인)
- [ ] **Step 5: 커밋** `generator: master 값 — 체어 수·원장 동시 건수(AI 비서 슬롯 검사)`

### Task 2: 앱 스키마 — AX 예약·대화

**Files:**
- Modify: `shared/app_schema.sql`
- Test: `tests/backend/test_db.py`

**Interfaces:**
- Produces 테이블(주석에 `AI 비서 설계 §5.2·§6`, 재생성 때 **버림** — 앱 설계 §7.3 목록에도 Task 13에서 추가):
  - `ax_emr_writes(id, kind CHECK('appointment'), patient_id, staff_id, scheduled_at, duration_min, type CHECK('treatment','hygiene','consult'), procedure_code, chair_no, source_call_uid, conversation_id, turn, status CHECK('draft','approved','cancelled','rejected'), reason, decided_by, decided_at, created_at)` + 인덱스 `(status, date(scheduled_at))`
  - `ax_assistant_conversations(id, title, part, created_at)`
  - `ax_assistant_messages(id, conversation_id, seq, role CHECK('user','assistant'), text, cards JSON, tool_calls JSON, run_id, status, created_at, UNIQUE(conversation_id, seq))`

- [ ] **Step 1: 실패하는 테스트** — `test_assistant_tables_created`: `db.init_schema` 뒤 세 테이블이 있고, 기존 DB에 `init_schema`를 다시 불러도 오류 없음(기존 `_add_columns` 경로)
- [ ] **Step 2: 실패 확인** `uv run pytest tests/backend/test_db.py -k assistant -v` → FAIL
- [ ] **Step 3: 구현** — 스키마에 세 테이블
- [ ] **Step 4: 통과 확인**
- [ ] **Step 5: 커밋** `backend: 앱 스키마 — AX 예약·AI 비서 대화`

### Task 3: EMR 연동 모듈 — 슬롯 검사·초안·승인·합쳐 읽기

**Files:**
- Create: `backend/emr_bridge.py`
- Test: `tests/backend/test_emr_bridge.py` (source 스키마 + 직원 3명(원장·위생사·상담실장) + 시술 3개 + 원본 예약 몇 건 fixture — `tests/backend/test_chat.py` `conn` fixture 방식)

**Interfaces:**
- Produces:
  - `appointment_type(conn, procedure_code: str) -> str` — 원본 예약에서 그 시술에 가장 많이 쓴 `type`(없으면 `ValueError`). 담당 역할은 `ROLE_FOR_TYPE = {"treatment": "doctor", "hygiene": "hygienist", "consult": "consultant"}`
  - `check_slot(conn, clinic: Clinic, *, patient_id, staff_id, start: datetime, procedure_code, exclude_id=None) -> list[str]` — 어긋난 규칙 코드 목록(빈 목록 = 통과). 코드: `closed_day`·`outside_hours`·`lunch`·`before_today`·`wrong_role`·`staff_busy`·`no_chair`·`patient_same_day`·`unknown_patient`. 원본(`source.emr_appointments`, 취소 제외)과 `approved` AX 예약을 합쳐 센다. 기준일 당일은 영업시간 전체 허용, 기준일 전은 `before_today`
  - `free_slots(conn, clinic, day: date, procedure_code, staff_id=None) -> list[dict]` — `{staff_id, staff_name, start "HH:MM", end "HH:MM"}`, 30분 단위 시작(`frontend/components/schedule/day-grid.tsx` `SLOT_MIN`과 같은 단위), 각 후보는 `check_slot`(환자 제외 규칙 빼고) 통과분
  - `create_draft(conn, clinic, *, patient_id, staff_id, start, procedure_code, source_call_uid, conversation_id, turn) -> dict` — 통과하면 `draft` 행(체어는 그 시각 빈 체어 중 가장 작은 번호, 상담은 NULL)과 `{id, ...}`, 아니면 `{error: [코드]}`(행 없음)
  - `approve(conn, clinic, draft_id, *, by: str, at: str, missed_day: date) -> dict` — 같은 트랜잭션: `status='draft'` 확인(아니면 `Conflict` 예외) → `check_slot` 재검사(실패면 `rejected` + `reason`, `{ok: False, reasons}`) → `approved` → 환자가 `missed.facts(conn, missed_day)`에 있으면 `missed.record_contact(..., "booked", "call", by, at, missed_day)`. 커밋은 부르는 쪽
  - `cancel(conn, draft_id, *, by, at) -> None` — `draft`만 `cancelled`
  - `ax_appointments(conn, date_from: date, date_to: date) -> list[dict]` — 승인된 AX 예약을 원본 예약 행과 같은 키(`id`는 `"ax-<id>"` 문자열, `source: "ax"`, `booked_via: "ax"`, 환자·시술 이름 JOIN)로
  - `unbooked_requests(conn, date_from: date, date_to: date) -> list[dict]` — 설계 §5.3 정의: 활성 분류(`calls.active_versions`)의 `wants_appt`가 참인 통화, 통화 시각 뒤로 살아 있는 예약(원본 `scheduled`·`completed` 또는 `approved` AX) 없음, 환자별 최근 통화 하나. `{call_uid, started_at, patient_id|None, chart_no|None, name, summary, in_eval_pool}`, `patient_id` 없으면 `no_chart: True`

- [ ] **Step 1: 실패하는 테스트**
  - `test_slot_ok` → `[]`
  - `test_slot_closed_weekday`(2026-12-03 목) → `["closed_day"]`; `test_slot_outside_hours`; `test_slot_lunch`; `test_slot_before_today`(2026-11-30) → 포함 `"before_today"`
  - `test_slot_wrong_role`(스케일링 hygiene 시술을 원장에게) → `["wrong_role"]`
  - `test_slot_staff_busy_hygienist`(위생사 같은 시각 원본 1건) / `test_slot_doctor_concurrency`(원장 같은 시각 `doctor_concurrency`건이면 busy, 그보다 적으면 통과)
  - `test_slot_no_chair`(체어 `chairs`개 모두 점유) / `test_slot_patient_same_day`
  - `test_slot_counts_approved_ax_not_drafts`(초안은 자리를 막지 않고 승인분만 막는다)
  - `test_free_slots_excludes_busy_and_lunch`
  - `test_create_draft_assigns_lowest_free_chair` / `test_create_draft_rejects_without_row`
  - `test_approve_writes_contact_when_in_missed` / `test_approve_patient_not_in_missed_skips_contact`
  - `test_approve_rechecks_and_rejects_conflict`(초안 뒤 같은 자리에 다른 초안 승인 → 두 번째 승인은 `{ok: False}` + `status='rejected'`)
  - `test_approve_twice_is_idempotent_conflict`(두 번째 `approve` → `Conflict`, 연락 기록 1줄)
  - `test_cancel_only_draft`
  - `test_unbooked_requests_definition`: 예약 원함 통화 뒤 원본 예약 있음 → 빠짐 / 통화 전 예약만 → 남음 / 승인 AX 예약 → 빠짐 / 같은 환자 두 통화 → 1줄(최근) / `patient_id` 없음 → `no_chart`
- [ ] **Step 2: 실패 확인** `uv run pytest tests/backend/test_emr_bridge.py -v` → FAIL(ImportError)
- [ ] **Step 3: 구현** — 모듈 독스트링: 하는 일 + `AI 비서 설계 §5`, "실제 도입 때는 이 모듈의 쓰기만 EMR API로". 겹침 계산은 원본 + 승인 AX를 한 쿼리 함수 `_busy(conn, day)`로. master 값은 `prep.master_values`
- [ ] **Step 4: 통과 확인**
- [ ] **Step 5: 커밋** `backend: EMR 연동 모듈 — 슬롯 검사·예약 초안·승인·예약 원함 미예약`

### Task 4: 원본 + AX 예약 합쳐 읽기 — 예약표·EMR·준비표·조회 도구

**Files:**
- Modify: `backend/api.py` `emr_schedule`·EMR 환자 기록 예약 탭(`/emr/patients/{chart_no}`), `backend/emr.py` `schedule_day`(합칠 행을 인자로 받기), `backend/prep.py` `prep_facts`, `backend/query_tools.py`
- Test: `tests/backend/test_emr.py`, `tests/backend/test_prep.py`, `tests/backend/test_query_tools.py`(있는 파일에)

**Interfaces:**
- Consumes: `emr_bridge.ax_appointments`
- Produces:
  - `emr.schedule_day(conn, day, today, range_start, extra: list[dict] = ())` — `extra`를 원본과 같은 모양으로 섞고 `counts`에 넣는다. 각 예약에 `source: "emr" | "ax"`, AX면 `ax: {decided_by, decided_at, source_call_uid, conversation_id}`
  - `prep.prep_facts(conn, day, today, include_ax: bool = False)` — True면 그날 승인 AX 예약도 행. `today.sheet`·`GET /prep`·`POST /jobs/prep-regenerate`는 True, `evaluation/` 경로는 기본 False 그대로
  - `query_tools(clinic, include_ax: bool = False)` — `query_schedule`·`query_patient`(다음 예약)가 True면 AX 예약 포함. ③(`backend/chat.py`)은 그대로 False
- 테스트:
  - `test_schedule_day_merges_ax_with_tag`(AX 행 `source == "ax"`, `counts.total`에 포함)
  - `test_prep_facts_include_ax_only_when_asked`(기본 False면 행 없음, True면 1행)
  - `test_query_schedule_include_ax`(③ 기본은 원본만)
  - `test_today_cards_count_same_day_ax`(기준일 AX 승인 → `GET /today` 오늘 예약 +1)

- [ ] **Step 1: 위 테스트 작성**
- [ ] **Step 2: 실패 확인** `uv run pytest tests/backend -k "ax" -v`
- [ ] **Step 3: 구현** — API는 앱 DB가 있을 때만 `ax_appointments`를 읽는다(`emr_schedule`의 기존 `Path(paths[0]).exists()` 처리와 같게)
- [ ] **Step 4: 통과 확인** + `uv run pytest tests/backend/test_chat.py tests/evaluation -q`(③·평가 경로 그대로)
- [ ] **Step 5: 커밋** `backend: 예약표·EMR·준비표·조회 도구가 승인된 AX 예약을 합쳐 읽음`

### Task 5: AI 비서 도구

**Files:**
- Create: `backend/assistant_tools.py`
- Test: `tests/backend/test_assistant_tools.py`

**Interfaces:**
- Consumes: `emr_bridge.*`, `calls.list_calls`, `prep.prep_facts(include_ax=True)`, `claims.adjusted`·`claims.pre_claim`, `recon.ledger`, `missed.sheet`, `query_tools(clinic, include_ax=True)`, `chat.chat`
- Produces: `assistant_tools(clinic, searcher, ctx_turn: dict) -> list[Tool]` — `ctx_turn = {conversation_id, turn, gw}`(초안에 대화·턴을 남기고 `ask_records`가 게이트웨이를 쓴다). 도구 이름은 **버전으로 고정**(`TOOLS_VERSION = "v1"`, 설계 §7.1):
  - `query_patient`·`query_schedule`·`query_claims`·`query_charts`(③ 그대로, include_ax=True)
  - `find_calls(date_from, date_to, wants_appt: bool|None, chart_no: str|None)` → `{count, calls:[{call_uid, started_at, chart_no, name, purpose, wants_appt, summary}]}`(판정 풀 통화는 `in_eval_pool: True`)
  - `unbooked_requests(date_from, date_to)` → `{count, patients:[…]}`
  - `free_slots(date, procedure, staff: Literal["원장","위생사","상담실장"]|None)` → `{date, closed: bool, slots:[…]}`. `procedure`는 시술 이름·별칭(③ `_procedure_codes` 재사용)
  - `prep_rows(date)` → `{count, rows:[{time, chart_no, name, procedure, chips}]}`
  - `claim_items(kind: Literal["appeal_soon","adjusted","pre_claim_warning"])`
  - `recon_days(month: "YYYY-MM")` → 불일치 있는 날·건수·규칙 원인
  - `missed_patients(chip: str|None, limit ≤ 10)`
  - `ask_records(question)` → ③ `chat.chat(..., is_eval=False)`의 `{status, answer_text, cited_ids, sources}`(하위 실행: 도구 밖에서 LLM을 부르면 안 되는 하네스 규칙(`Tool` 주석) 때문에 **이 도구는 질문을 기록만 하고**, 실제 ③ 호출은 Task 6 `turn`이 도구 단계 뒤에 처리 — 아래 Step 3)
  - `draft_appointment(chart_no, date, start "HH:MM", staff, procedure, source_call_uid|None)` — `writes=True`, `emr_bridge.create_draft` 결과 그대로(`{id,…}` 또는 `{error:[코드]}`)
  - `done_tools()` — `ends_run`
- 도구 설명은 각 한두 문장(설계 §4.2 "짧게"), 날짜 형식과 기준일을 적는다

- [ ] **Step 1: 실패하는 테스트**
  - `test_registry_names_fixed`(이름 집합 = 위 목록, `TOOLS_VERSION == "v1"`)
  - `test_unbooked_tool_matches_bridge`(같은 기간 결과가 `emr_bridge.unbooked_requests`와 같음)
  - `test_free_slots_tool_closed_thursday` → `closed: True, slots: []`
  - `test_draft_tool_rejects_bad_slot` → `{error: ["closed_day"]}`, `ax_emr_writes` 0행
  - `test_draft_tool_records_conversation_turn`
  - `test_query_schedule_sees_approved_ax`
- [ ] **Step 2: 실패 확인** `uv run pytest tests/backend/test_assistant_tools.py -v`
- [ ] **Step 3: 구현** — `ask_records`: 하네스 도구 안에서 LLM을 부르지 않는다. 도구 결과는 `{"queued": question}`, Task 6이 도구 단계가 끝난 뒤 큐의 질문마다 `chat.chat`를 불러(`parent_run_id` 대신 결과를 메시지 `tool_calls`에 기록) 답 단계 입력에 넣는다
- [ ] **Step 4: 통과 확인**
- [ ] **Step 5: 커밋** `backend: AI 비서 도구 — 통화·예약 원함·빈 시간·준비표·청구·재무·미예약·③ 위임·예약 초안`

### Task 6: AI 비서 에이전트 — 한 턴(도구 단계 → 답 단계)·대화 저장

**Files:**
- Create: `backend/assistant.py`, `prompts/assistant/v1/system.md`(도구 단계), `prompts/assistant_reply/v1/system.md`(답 단계)
- Test: `tests/backend/test_assistant.py`(Script 게이트웨이)

**Interfaces:**
- Consumes: Task 5 `assistant_tools`, `backend/harness` `Runner`·`AgentSpec`·`RunStore`, `backend/verify.py` `allowed_numbers`·`numbers_ok`, `chat.chat`
- Produces:
  - 상수 `AGENT = "assistant"`, `TOOL_ALIAS = "chat"`, `REPLY_ALIAS = "chat"`, `PROMPT_VERSION = "v1"`, `REPLY_VERSION = "v1"`, `MAX_TOOL_CALLS`(시작 = `chat.CHAT_MAX_TOOL_CALLS`), `MAX_USD`(시작 = `chat.CHAT_MAX_USD`), `FAILED_TEXT`
  - `turn(conn, gw, searcher, clinic, *, conversation_id: int | None, text: str, part: str, on_event=None) -> dict` — `{conversation_id, seq, status, text, cards, tool_calls, run_id}`. 순서:
    1. 대화 없으면 만든다(제목 = `text` 앞 40자). 사용자 메시지 저장·커밋
    2. 입력 = 앞 메시지들(역할·글·카드의 `{type, id}`만) + `오늘: 2026-12-01` + 이번 말 → `Runner(...).start(target=f"{conversation_id}:{seq}", ...)`, `AgentSpec(alias=TOOL_ALIAS, end_tools=("done_tools",), max_tool_calls=MAX_TOOL_CALLS, max_usd=MAX_USD)`
    3. 큐된 `ask_records` 질문마다 `chat.chat`
    4. 답 단계: `gw.complete(REPLY_ALIAS, task="assistant_reply", schema=REPLY_SCHEMA, ref=("agent_run", run_id), ...)` — 입력 = 이번 말 + 앞 맥락 + 이번 턴 도구 결과 + ③ 답. `REPLY_SCHEMA = {text, cards:[{type: patient|call|draft|claim|recon_day|source, id}], cited_ids}`
    5. 검사: 카드 id가 이번 턴 도구 결과(또는 ③ 출처)에 없으면 그 카드만 버림. 숫자(`numbers_ok`)·인용이 어긋나면 이유를 붙여 답 단계 1회 재호출, 또 어긋나면 `text = FAILED_TEXT`, `status = "number_failed" | "cite_failed"`
    6. 카드 내용은 코드가 채운다(`card_detail(conn, type, id)` — 환자 이름·차트 번호, 통화 시각·요약, 초안 전체·상태, 청구·재무 요약, 출처는 ③ `_chat_sources`와 같은 모양)
    7. 어시스턴트 메시지 저장(`tool_calls` = 이번 턴 `[{name, args, ok, result_summary}]` — 설계 §5.3 "재생성 때" 학습 재료)
  - `on_event`: `{"type":"step","tool":name,"args":…}`(도구 실행마다), `{"type":"reply", …turn 결과}`
  - `conversations(conn, limit=20, before_id=None) -> list[dict]`, `conversation(conn, id) -> dict | None`(메시지 + 카드 — 모델 안 부름, 초안 카드는 지금 상태로 다시 채움)
- 프롬프트 v1 요점(도구 단계): 기준일·휴진, 예약 시간은 `free_slots` 결과에서만, 예약은 초안까지(확정·변경·취소 도구 없음 — 요청받으면 조회 없이 `done_tools`), 문서·절차 질문은 `ask_records`, 끝나면 `done_tools`. 답 단계: 도구 결과에 있는 사실·숫자만, 카드로 가리킬 대상, 초안이 있으면 "승인 버튼" 안내

- [ ] **Step 1: 실패하는 테스트**
  - `test_turn_tool_then_reply_saves_messages`(사용자·어시스턴트 2행, `tool_calls` 기록)
  - `test_followup_turn_gets_previous_card_ids`(두 번째 턴 입력에 앞 턴 카드 `patient` id가 들어감 — Script가 받은 messages 검사)
  - `test_reply_uses_reply_alias`(Script가 받은 alias 순서 = 도구 단계 `TOOL_ALIAS`…, 답 단계 `REPLY_ALIAS` — 별칭을 바꾸면 따라 바뀜: monkeypatch로 `REPLY_ALIAS="other"`)
  - `test_card_outside_turn_dropped`
  - `test_number_mismatch_retry_then_fixed_text`
  - `test_ask_records_calls_chat_after_tool_phase`(③ 결과가 답 단계 입력과 출처 카드에)
  - `test_cancel_request_makes_no_draft`("예약 취소해줘" 스크립트 → `ax_emr_writes` 0행)
  - `test_conversation_reopen_no_model_call`(`conversation()`은 게이트웨이를 부르지 않음, 초안 카드 상태가 승인 뒤 `approved`로 보임)
  - `test_turn_exception_closes_run_and_saves_failed_message`
- [ ] **Step 2: 실패 확인** `uv run pytest tests/backend/test_assistant.py -v`
- [ ] **Step 3: 구현** + 프롬프트 두 파일(`backend/prompts.py` `load` 형식)
- [ ] **Step 4: 통과 확인** + `uv run pytest tests/test_no_ground_truth_import.py -q`
- [ ] **Step 5: 커밋** `backend: AI 비서 에이전트 — 도구 단계·답 단계·대화 저장`

### Task 7: API

**Files:**
- Modify: `backend/api.py`
- Test: `tests/backend/test_api_assistant.py`

**Interfaces:**
- Produces:
  - `POST /assistant/turn` body `{conversation_id: int|null, text: str(1..1000), part: str}` → SSE `step`·`reply`·`error`(③ `POST /chat`의 스레드·큐 방식 그대로). 색인이 없어도 동작(③ 위임만 "자료실 색인 없음" 결과)
  - `GET /assistant/conversations?before=` → `[{id, title, part, created_at}]`(최신순 20)
  - `GET /assistant/conversations/{id}` → `{id, title, messages:[{seq, role, text, cards, steps}]}`, 없으면 404
  - `POST /assistant/drafts/{id}/approve` body `{part}` → `{ok, card}` / 검사 실패 `{ok: False, reasons}` / 이미 처리 409
  - `POST /assistant/drafts/{id}/cancel` body `{part}` → `{card}` / draft 아님 409
  - `GET /today`의 `people`에 `wants_appt_unbooked: {count, names}`(기준일 전 7일, `emr_bridge.unbooked_requests`)
  - `GET /emr/schedule` 예약에 `source`·`ax`(Task 4)
- 과금 훅: `POST /assistant/turn`은 앱 런타임(일일 상한 `runtime`) — 별도 처리 없음

- [ ] **Step 1: 실패하는 테스트** — `test_turn_sse_events`(가짜 게이트웨이 의존성 덮어쓰기, `step` 뒤 `reply`), `test_conversations_list_and_get`, `test_approve_ok_then_409`, `test_approve_rejected_reasons`, `test_cancel_409_when_approved`, `test_today_has_wants_appt_unbooked`
- [ ] **Step 2: 실패 확인** `uv run pytest tests/backend/test_api_assistant.py -v`
- [ ] **Step 3: 구현** — 승인 시각은 실제 시계, `missed_day` = 기준일
- [ ] **Step 4: 통과 확인**
- [ ] **Step 5: 커밋** `backend: AI 비서 API — 턴 SSE·대화·초안 승인·취소, 오늘 예약 원함 미예약`

### Task 8: 프론트 셸 — 아이콘 줄 + 목록 칸

**Files:**
- Create: `frontend/components/shell/app-shell.tsx`(아이콘 줄·목록 칸·좁은 화면 서랍), `frontend/components/shell/space-panel.tsx`
- Modify: `frontend/lib/nav.ts`(공간 5개·업무 메뉴·AI 평가 탭), `frontend/app/(portal)/layout.tsx`(헤더·`AskFloating` 제거 → `AppShell`), `frontend/app/page.tsx`(시작 화면 = AI 비서, Task 9), `frontend/components/app-header.tsx`(삭제 — `ThemeSelect`·`PartSelect`는 셸로 옮김), `frontend/components/ask/ask-floating.tsx`(삭제), `frontend/components/eval/eval-view.tsx`(탭을 목록 칸 링크 `?tab=`로)
- Test: `frontend/e2e/shell.spec.ts` 새로, `frontend/e2e/ask-panel.spec.ts`(떠 있는 창) 삭제, 다른 spec의 머리 띠 메뉴 클릭은 목록 칸 클릭으로

**Interfaces:**
- Produces: `SPACES = [{key:"assistant", label:"AI 비서", icon: BotMessageSquare, href:"/"}, {key:"work", label:"업무", icon: LayoutGrid, href:"/today"}, {key:"library", label:"자료실", icon: FolderSearch, href:"/ask"}, {key:"eval", label:"AI 평가", icon: Scale, href:"/eval"}, {key:"emr", label:"EMR", icon: Monitor, href:"/emr"}]`, `spaceOf(pathname): key`. 업무 목록 칸 건수는 `GET /today` 재사용(오늘 = 할 일, 예약 = 오늘 예약, 통화 = 급함, 청구 점검 = 재심 임박, 재무 대조 = 불일치), 건수 없는 칸은 비움. AI 비서 목록 칸 = "새 대화" + `GET /assistant/conversations`(20개 + 더보기). EMR은 목록 칸 없음. 기준일은 목록 칸 아래, 테마·파트는 아이콘 줄 아래
- e2e: `shell: 아이콘 줄 5칸과 이름`, `shell: 업무를 누르면 목록 칸이 메뉴 8개`, `shell: 좁은 화면에서 서랍`, `shell: 떠 있는 물어보기 버튼 없음`

- [ ] **Step 1: e2e 작성** (위 4개)
- [ ] **Step 2: 실패 확인** `make e2e`(해당 파일만)
- [ ] **Step 3: 구현** — 색·모서리는 토큰만, 부품은 `components/ui`(버튼 variant가 더 필요하면 그 파일에)
- [ ] **Step 4: 통과 확인** `make lint` + `make e2e`
- [ ] **Step 5: 커밋** `frontend: 앱 셸 — 아이콘 줄·목록 칸(AI 비서·업무·자료실·AI 평가·EMR)`

### Task 9: 프론트 AI 비서 화면

**Files:**
- Create: `frontend/lib/assistant-api.ts`, `frontend/components/assistant/assistant-view.tsx`(시작·대화), `frontend/components/assistant/turn-steps.tsx`(접힌 "조회 n번 ›"), `frontend/components/assistant/cards.tsx`(환자·통화·출처·청구·재무·예약 초안 카드)
- Modify: `frontend/app/page.tsx`(→ `AssistantView`), 대화 경로 `/?c=<id>`(목록 칸에서 열기)
- Test: `frontend/e2e/assistant.spec.ts`(API를 Playwright `route`로 가짜 응답)

**Interfaces:**
- Consumes: Task 7 API
- 화면 규칙(설계 §3.2): 시작 = "무엇을 도와드릴까요?" + 입력창 + 칩 5개(문구: `📞 지난주 전화로 예약 원했는데 안 잡힌 사람` · `🩺 내일 환자 중 미수·주의사항 있는 사람` · `🧾 재심 낼 만한 삭감 건` · `💳 이번 주 카드 입금 안 맞는 날` · `☎️ 김○○ 지난번에 전화로 뭐 물어봤지` — 마지막 이름은 Task 12 데이터 확인 뒤 실제 환자로) + "조회하고 예약 초안까지 — 확정은 사람이 합니다". 칩 = 채우고 바로 보냄. 사용자 말 오른쪽 말풍선, 답은 본문. 초안 카드 `AI` 배지 + 승인/다른 시간/취소, 승인 뒤 `확정` + "일정에서 보기"(`/schedule?date=`), 검사 실패면 카드 아래 이유. "다른 시간" = 입력창에 "다른 시간으로" 채움. `?draft=<질문>`이면 입력창에 채우고 보냄(오늘 화면 버튼용), `?prefill=<질문>`이면 채우기만(자료실·근거 없는 질문용)
- e2e: `assistant: 칩을 누르면 질문이 보내지고 단계 줄 뒤 답`, `assistant: 초안 승인 → 확정 표시`, `assistant: 승인 실패 이유 표시`, `assistant: 지난 대화 열기는 저장된 그대로`, `assistant: ?draft= 로 바로 보냄`

- [ ] **Step 1: e2e 작성**
- [ ] **Step 2: 실패 확인**
- [ ] **Step 3: 구현**
- [ ] **Step 4: 통과 확인** `make lint` + `make e2e`
- [ ] **Step 5: 커밋** `frontend: AI 비서 화면 — 시작·대화·카드·예약 초안 승인`

### Task 10: 프론트 기존 화면 연결 — 오늘·예약표·EMR·준비표·자료실

**Files:**
- Modify: `frontend/components/today/today-view.tsx`(통화에서 온 일에 "예약 원함" 줄 — `people.wants_appt_unbooked`, 0이면 숨김. 이 줄·재심 기한 임박·일계표 불일치 끝에 "AI 비서로 처리" → `/?draft=<정해진 질문>`), `frontend/components/schedule/day-grid.tsx`·`schedule-view.tsx`(AX 예약 `AX` 꼬리표, 옆 패널 "어떻게 잡혔나"에 승인 시각·파트·근거 통화·"예약 대화 기록 보기" `/?c=`), `frontend/components/emr/*` 예약 탭(같은 꼬리표 — `--emr-*` 토큰), `frontend/components/prep/*`(AX 행 꼬리표), `frontend/components/ask/ask-view.tsx`(질문하기 탭 제거, 제목 "자료실", "이걸로 질문하기" → "AI 비서에게 묻기" `/?prefill=`), `frontend/components/today/today-view.tsx` 근거 없는 질문 링크 → `/?prefill=`, 옛 `/ask?tab=chat…` 링크 → `/?prefill=`(남은 곳 `grep -rn "tab=chat" frontend`)
- 오늘 화면 정해진 질문 문구: 예약 원함 `지난 7일 전화로 예약 원했는데 안 잡힌 사람 보여줘` · 재심 `재심 신청 기한이 얼마 안 남은 삭감 건 보여줘` · 일계표 `일계표 불일치 있는 날 보여줘`
- Test: `frontend/e2e/`(today·schedule·ask 기존 spec에 추가)

- [ ] **Step 1: e2e 추가** — `today: 예약 원함 줄과 AI 비서로 처리 링크`, `schedule: AX 예약 꼬리표와 잡은 대화 링크`, `ask: 자료실 제목, 질문하기 탭 없음, AI 비서에게 묻기`
- [ ] **Step 2: 실패 확인**
- [ ] **Step 3: 구현**
- [ ] **Step 4: 통과 확인** `make lint` + `make e2e` + `make test`
- [ ] **Step 5: 커밋** `frontend: 오늘·예약표·EMR·준비표·자료실을 AI 비서와 연결`

### Task 11: 시나리오 평가셋 — 정답 만들기

**Files:**
- Create: `evaluation/assistant_scenarios.py`(정답 계산·문장 확인 CLI), `ground_truth/assistant_scenarios.json`
- Modify: `generator/verify/`에 문항 구성 검사(`check_assistant_scenarios` — 유형별 수·분할·id 유일, 기존 `QA_TYPE_COUNTS` 방식)
- Test: `tests/evaluation/test_assistant_scenarios.py`

**Interfaces:**
- Produces: JSON `[{id: "as-001", split: "dev"|"test", type: "booking"|"lookup"|"delegate"|"refuse"|"reference", turns: [{user: str, gold: {tools: [str], args: {키: 값}, end: {draft: {chart_no, date, start_range: ["HH:MM","HH:MM"], staff_role}} | {no_write: true} | null}}]}]`
  - 유형별 수(`ASSISTANT_TYPE_COUNTS`, 분할마다): booking 8 · lookup 8 · delegate 4 · refuse 4 · reference 4 = 28 × 2
  - 정답은 코드가 데이터에서 계산(`emr_bridge.unbooked_requests`·`free_slots`·`prep.prep_facts`·`claims.adjusted`·`recon.ledger`). dev/test는 환자·날짜를 겹치지 않게 나눈다(다음 묶음 학습 누수 방지, 설계 §7.1)
  - 사용자 문장: `python -m evaluation.assistant_scenarios draft`가 유형·대상으로 초안 문장을 만들고(과금, 별칭 `gen`), 사람이 `review`(파일을 열어 고침)로 확정 — 문장 확정 전 `gold`는 코드 계산값만
- `evaluation/` 안에서만 `emr_bridge` 등 backend 함수를 import(평가는 backend를 써도 된다 — 반대 방향 금지)

- [ ] **Step 1: 실패하는 테스트** — `test_gold_booking_matches_free_slots`(작은 fixture DB에서 booking 정답의 시각 범위가 `free_slots` 안), `test_splits_disjoint_patients_dates`, `test_type_counts`
- [ ] **Step 2: 실패 확인** `uv run pytest tests/evaluation/test_assistant_scenarios.py -v`
- [ ] **Step 3: 구현** + verify 검사(TDD: 검사 먼저)
- [ ] **Step 4: 통과 확인** + `make verify`
- [ ] **Step 5: 문장 초안·검토**(사용자 승인 뒤 과금 `draft`, 사람 `review`) → JSON 커밋 `eval: AI 비서 시나리오 평가셋(dev·test 각 28)`

### Task 12: 시나리오 채점·실행 — `make assistant-eval`

**Files:**
- Create: `evaluation/assistant.py`(run·score CLI), `evaluation/assistant_score.py`(채점 함수 — 다음 묶음 보상 함수가 재사용)
- Modify: `Makefile`(`assistant-eval` — 과금, 이어 돎, dev), `evaluation/eval.py`(섹션 `assistant`), `.claude/hooks/guard.py` 과금 목록(+ `tests/test_claude_hooks.py`)
- Test: `tests/evaluation/test_assistant_score.py`

**Interfaces:**
- Produces:
  - `score_call(gold_turn: dict, call: {name, args}) -> {tool_ok: bool, args_ok: bool, reward: float}` — **다음 도구 호출 한 번** 단위(설계 §7.1). `reward = 1.0*tool_ok + 0.5*args_ok`, 스키마 밖·범위 밖 쓰기(`draft_appointment`가 정답에 없는데 부름) = -1.0
  - `score_turn(gold_turn, tool_calls, end_state) -> {tools_ok, args_ok, end_ok, unsafe_write}`
  - `score_run(scenarios, runs) -> {tool_selection, arg_accuracy, task_success, unsafe_writes, cost_per_turn, tool_calls_per_turn, by_type}`
  - 실행: 시나리오마다 새 임시 앱 DB(원본 source 읽기 전용) — 대본대로 `assistant.turn`을 부르고, 초안이 생기면 승인하지 않는다(끝 상태 = 초안). 결과는 `evaluation/assistant_runs.jsonl`(이어 돎 — 같은 `id`·버전·별칭이면 건너뜀). 별칭은 `--tool-alias`·`--reply-alias`(기본 `chat`, 다음 묶음 A vs C 비교 자리)
  - 기준선: lookup 유형 첫 턴을 ③ `chat.chat`에 넣은 도구 선택 — 보고만
- `make assistant-eval` = `python -m evaluation.assistant run --split dev`, test는 `python -m evaluation.assistant score --split test`(사람)

- [ ] **Step 1: 실패하는 테스트** — `test_score_call_reward_values`(정답 도구·인자 1.5 / 도구만 1.0 / 엉뚱한 도구 0 / 범위 밖 초안 -1.0), `test_score_turn_unsafe_write`, `test_score_run_aggregates_by_type`, `test_run_resumes_skips_done`
- [ ] **Step 2: 실패 확인**
- [ ] **Step 3: 구현**
- [ ] **Step 4: 통과 확인** + `make test`
- [ ] **Step 5: 커밋** `eval: AI 비서 시나리오 채점·make assistant-eval`
- [ ] **Step 6: dev 실측**(사용자 승인 뒤, 과금) — `make assistant-eval`, 결과로 `MAX_TOOL_CALLS`·`MAX_USD` 정하고 AI 실험 기록에 문제→원인→시도→결과→한계로. 커밋 `eval·docs: AI 비서 dev 실측`

### Task 13: 문서

**Files:**
- Modify: `docs/치과 AX - 기능 정의.md`(§4 앱 구성 — 셸·시작 화면·자료실·떠 있는 창 제거, ③ 화면 입구, 오늘 화면 예약 원함 줄·버튼, 새 절 "AI 비서", §9 다음 묶음 2개), `docs/치과 AX - 앱 설계.md`(§3 ③ 입구, §3b 새 에이전트·두 단계, 새 테이블·API, §7.1 별칭 표(TOOL/REPLY 모두 `chat`), §7.3 버림 목록에 `ax_emr_writes`·`ax_assistant_*`, §9 프론트 셸), `docs/치과 AX - 평가 설계.md`(시나리오 평가셋 절, §6 `assistant_scenarios.json`, §7 `eval.py` 섹션), `docs/치과 AX - 기술 소개.md`·`프로젝트 소개.md`(한 줄), `CLAUDE.md`(명령에 `make assistant-eval`, 폴더 설명에 `emr_bridge`)
- 메모리 규칙: 문서에는 규칙만(날짜·경위 없음), 고친 뒤 훑기, PR 전 `pj2-doc-tidy`

- [ ] **Step 1: 문서 고치기**
- [ ] **Step 2: `uv run pytest tests/generator/test_doc_sync.py -q`**(문서 값 비교) 통과
- [ ] **Step 3: 커밋** `docs: AI 비서 — 기능 정의·앱 설계·평가 설계·소개`
