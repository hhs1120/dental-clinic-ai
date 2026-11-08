# 처리 정답 + 후속 에이전트 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 할 일 있는 통화마다 후속 에이전트가 환자 기록을 조회해 처리안(7가지 종류 + 요지 + 근거)을 붙여 오늘 할 일·상세에 보이고, 골드에서 파생한 처리 정답으로 에이전트와 규칙 기준선의 정확도를 잰다.

**Architecture:** 처리 정답은 채점 모수 통화에 Opus가 녹취록만 보고 붙인 라벨(Batch)과 사람 표본 30통화로 만들어 `evaluation/call_actions.jsonl`에 둔다(골드에 할 일 내용이 없어서 — 설계 "PR ② 처리 정답"). 에이전트는 기존 하네스(`Runner`)에 조회 도구 + 문서 검색 + 끝내는 도구 `submit_followup`을 얹은 `backend/followup.py`이고, 결과는 `ax_call_followups`에 남는다. 평가 실행은 `app.sqlite` 사본에서 라운드 배치(남은 실행 30건 이하부터 일반 호출)로 돌려 `evaluation/followup_runs.jsonl`에 쌓고, `eval.py`는 채점만 한다.

**Tech Stack:** Python 3 · sqlite3 · FastAPI · Anthropic Batch(`shared/llm.py`) · Next.js · pytest

**Spec:** `docs/superpowers/specs/2026-11-08-followup-autoclose-design.md` "PR ②" (원본: 기능 정의 §4 ①·§9, 앱 설계 §1·§3b, 평가 설계 §5·§6, 가상 데이터 생성 설계 §5.1)

## Global Constraints

- 브랜치 `feat/followup-agent`(PR #25 머지 뒤 main에서). 커밋 `<영역>: <무엇>` + 본문 글머리 + 트레일러 두 줄(`Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`, `Claude-Session: https://claude.ai/code/session_01G62cGwhYixc5LpBLhj8iGp`). `git add`는 파일 이름으로. 커밋 전 `make fmt` → `make lint` → `make test`
- `backend/`는 `evaluation/`·`generator/`·`ground_truth/`를 import하지 않는다. 골드·판정 풀 통화의 실행 기록은 `data/app.sqlite`에 쓰지 않는다(평가 실행은 `evaluation/runs/`의 사본에서 — 그 폴더는 이미 커밋하지 않는다)
- 처리 종류 7가지 코드 값·우리말·뜻(`shared/followup.py`, 앱·평가가 같이 씀 — 에이전트 프롬프트와 정답 라벨 프롬프트가 같은 뜻을 읽는다): `appointment` 예약 잡기·변경 / `doctor_reply` 원장님 확인 후 회신 / `billing` 수납·미수 확인 / `document` 서류 준비 / `insurance_cost` 보험·비용 안내 / `scaling_timing` 스케일링 시기 안내 / `other` 기타(사람 판단)
- 게이트웨이 별칭 `followup: [claude-sonnet-5-5]`. 도구 호출 상한은 앱 상수 `FOLLOWUP_MAX_TOOL_CALLS`(값은 `evaluation/metrics.py` `MAX_TOOL_CALLS`와 같게 — 같은지 테스트로 비교), 실행당 비용 상한 `FOLLOWUP_MAX_USD = 0.5`
- 쓰기 도구 없음. 근거 후검증: `summary`·`facts`의 숫자(날짜·금액·횟수)가 그 실행의 도구 결과나 녹취록에 있어야 한다
- 채점 모수: 골드 `has_todo`도 활성 분류 `has_todo`도 참인 통화. dev 판정 풀과 silver 검증 구간을 따로 보고
- 라운드 배치: 남은 실행이 `ROUND_BATCH_MIN = 30`건 이하가 되면 그 라운드부터 일반 호출
- 데이터 고정 — `emr_*`·`fin_*`·`truth.sqlite`·생성기는 바꾸지 않는다

## Review Focus

1. 차트 번호가 없는 통화(처음 연락한 사람) — 에이전트가 환자 조회를 못 해도 실패하지 않고 녹취록만으로 처리안을 내거나 `other`로 → Task 3 `test_no_chart_still_plans`
2. 에이전트가 `submit_followup` 없이 글로만 답하거나 상한에 걸림 — 처리안 없이 `status='failed'`(또는 `handed_off`)로 남고 화면에 "처리안 실패" → Task 3 `test_no_submit_is_failure`
3. 후검증 실패(근거에 지어낸 날짜) — 한 번 다시 돌리고 또 실패면 실패로 남는다 → Task 3 `test_unverified_retries_once`
4. 라운드 배치 도중 끊김 — 다시 실행하면 제출한 배치를 다시 내지 않고 이어 받는다(배치 id를 남김) → Task 5 `test_rounds_resume_submitted_batch`
5. 분류 버전이 바뀐 통화 — 옛 분류 행의 처리안은 보이지 않고 새 분류로 다시 돈다 → Task 4 `test_followup_is_per_classification`

---

### Task 1: 처리 종류와 처리 정답(LLM 라벨 + 사람 표본)

**Files:**
- Create: `shared/followup.py`, `evaluation/call_actions.py`, `prompts/call_actions/v1.md`, `tests/evaluation/test_call_actions.py`
- Modify: `evaluation/metrics.py`(`ACTION_AUDIT_SAMPLE = 30`), `config/llm.yaml`(별칭 `action_label: [claude-opus-5-5]`), `Makefile`(`action-label`·`action-audit`)

**Interfaces:**
- Consumes: `evaluation.gold_audit`의 `load`·`save`·`transcript_sha`·`_hash_order`·`connect` 방식, `evaluation.splits.silver_split`, `backend.prompts`(머리말 effort), `shared.llm` Batch
- Produces:
  - `shared/followup.py`: `ACTIONS: tuple[str, ...]`(위 7개 순서), `ACTION_KO: dict`, `ACTION_DESC: dict`(종류마다 한 줄 뜻 — 예: `scaling_timing` "스케일링을 언제 받을 수 있는지·건강보험 적용 시기를 확인해 알리는 일")
  - `evaluation/call_actions.py`:
    - `eval_calls(conn) -> dict[str, list[str]]` — `{"dev": [...], "validation": [...]}`(골드 `has_todo`·활성 분류 `has_todo` 모두 참, dev = dev 판정 풀, validation = `silver_split` 검증 구간). Task 5·6이 같이 쓴다
    - `label_request(transcript, direction) -> tuple[str, dict]` — (user 메시지, 스키마 `{action: enum ACTIONS}`), 분류기 결과·골드는 넣지 않는다
    - `label(gw, conn, path, batch=True, poll_seconds=60) -> dict` — 기록이 없는(또는 녹취록 해시가 바뀐) 통화만, Opus Batch(골드 점검 `arbitrate_batch`처럼 상태 파일 `evaluation/call_actions_batch.json`·`.gitignore`), 결과는 `{call_uid, transcript_sha, llm_action, human_action: None, prompt_version}`
    - `audit(conn, path, read=input, write=print)` — 표본 `ACTION_AUDIT_SAMPLE`통화(해시 순)를 한 통화씩: 녹취록 → 7가지 종류와 뜻 목록 → 번호(1~7) 입력, 라벨은 저장 전까지 보이지 않음, 통화마다 저장·이어 하기
    - `truth(records) -> dict[str, str]` — 사람 답이 있으면 사람, 없으면 LLM
    - `agreement(records) -> dict` — LLM vs 사람 `{k, n, by_action}`
    - CLI `python -m evaluation.call_actions label|audit`, `make action-label`·`make action-audit`

- [ ] **Step 1: 실패하는 테스트**(FakeGateway·평가 테스트 DB)

```python
def test_eval_calls_population(conn): ...  # 골드·분류 모두 할 일인 통화만, dev 풀과 검증 구간 분리
def test_label_request_hides_classifier_and_gold(): ...  # 메시지에 녹취록·방향만, "has_todo"·분류 라벨 없음, 스키마 enum == ACTIONS
def test_label_writes_and_skips_done(
    conn, tmp_path
): ...  # 배치 가짜 게이트웨이 → 기록, 두 번째 호출은 제출 없음
def test_audit_hides_label_and_saves(
    conn, tmp_path
): ...  # 마지막 입력 전 출력에 llm 라벨 없음, 저장·이어 하기, 잘못된 번호 다시 묻기
def test_truth_prefers_human(): ...
def test_agreement(): ...
```

- [ ] **Step 2: 실패 확인** → ImportError
- [ ] **Step 3: 구현** — 프롬프트 머리말 `effort: medium`, 본문은 `ACTION_DESC`를 그대로 옮긴 7가지 정의 + "통화 뒤 직원이 해야 할 일을 보고 가장 맞는 하나"
- [ ] **Step 4: 통과 확인** → PASS
- [ ] **Step 5: 커밋** — `eval: 처리 정답 — Opus 라벨(배치)과 사람 표본 화면`

### Task 2: 게이트웨이 Batch에 도구 호출

**Files:**
- Modify: `shared/llm.py`(`submit_batch`·`batch_results`), `tests/shared/test_llm.py`(기존 batch 테스트 옆)

**Interfaces:**
- Produces:
  - `submit_batch` 요청 dict에 `tools`(있으면 params에), `max_tokens` 그대로
  - `batch_results`의 `LLMResult`에 `stop_reason`·`tool_calls`(`{id, name, input}`)·`content`(`_blocks`) — 단건 `complete`와 같은 모양. `stop_reason == "tool_use"`도 성공(지금은 `end_turn`만 성공). 스키마가 없고 도구 호출이면 `data=None`, text는 글 블록 합

- [ ] **Step 1: 실패하는 테스트** — 가짜 배치 결과 객체(기존 테스트의 흉내 방식)로 `tool_use` 메시지 → `ok`, `tool_calls[0]["name"] == "query_patient"`, `content`에 tool_use 블록; 요청에 tools가 들어감
- [ ] **Step 2: 실패 확인** → FAIL
- [ ] **Step 3: 구현**
- [ ] **Step 4: 통과 확인** — `uv run pytest tests/shared -q` → PASS
- [ ] **Step 5: 커밋** — `chore: 게이트웨이 Batch가 도구 호출을 주고받음`

### Task 3: 후속 에이전트(앱)

**Files:**
- Create: `backend/followup.py`, `prompts/followup/v1.md`, `tests/backend/test_followup.py`
- Modify: `shared/app_schema.sql`(`ax_call_followups`), `config/llm.yaml`(`followup`), `Makefile`(`followups`), `backend/calls.py`(`classify_one` 끝에서 처리안)

**Interfaces:**
- Consumes: Task 1 `ACTIONS`·`ACTION_DESC`; `backend.harness.loop.AgentSpec`·`Runner`; `backend.harness.store.RunStore`; `backend.query_tools.query_tools`(이 중 `query_patient`·`query_schedule`); `backend.rag_search.Searcher`·`search_tool`(검색 모델은 `backend/api.py` `/search`와 같은 방식으로 만든다)
- Produces:
  - `ax_call_followups(id, call_uid, classification_id, action, summary, facts JSON, run_id, status CHECK IN ('done','failed'), reason, created_at, UNIQUE(classification_id))`
  - `FOLLOWUP_MAX_TOOL_CALLS`, `FOLLOWUP_MAX_USD`, `PROMPT_VERSION = "v1"`
  - `SubmitArgs`(pydantic): `action: Literal[ACTIONS]`, `summary: str`(1~80자), `facts: list[str]`(최대 6)
  - `spec(clinic, searcher | None) -> AgentSpec` 와 `registry(...)` — 도구: `query_patient`·`query_schedule`·`search`(searcher가 있을 때만)·`submit_followup`(`ends_run`, 인자를 결과로 저장)
  - `input_text(call: dict, cls: dict) -> str` — 방향·녹취록·분류 9항목(우리말)·요약·할 일·차트 번호(없으면 "차트 없음")
  - `verify_facts(summary: str, facts: list[str], sources: list[str]) -> bool` — 숫자 토큰(쉼표 뺀 숫자열, `\d+`)이 모두 sources 합친 글에 있으면 True
  - `run_for_call(conn, gw, call_uid: str, clinic, searcher=None) -> dict | None` — 활성 분류가 `ok`이고 `has_todo`인 통화만. 실행 → `submit_followup` 인자 → 후검증(실패면 새 실행으로 한 번 더) → `ax_call_followups` 저장(실패면 `status='failed'`, `reason`). 이미 그 분류 행에 처리안이 있으면 아무것도 안 함
  - `targets(conn, today) -> list[str]` — 오늘 할 일 범위(`calls.today_board`와 같은 범위)의 활성 분류 `has_todo` 통화 중 처리안 없는 것
  - CLI `python -m backend.followup run`, `make followups`

- [ ] **Step 1: 실패하는 테스트** — `FakeGateway`를 감싼 스크립트 게이트웨이(정해 둔 순서로 도구 호출 → `submit_followup`)

```python
def test_plan_saved(
    conn,
): ...  # query_patient → submit(action="scaling_timing", summary="…2월 7일…", facts=[…]) → 행 1, status done, facts JSON
def test_no_chart_still_plans(
    conn,
): ...  # 차트 없는 통화 — 입력에 "차트 없음", 도구 없이 submit → done
def test_no_submit_is_failure(conn): ...  # 글로만 답 → status failed
def test_unverified_retries_once(
    conn,
): ...  # 첫 실행 summary에 없는 날짜 → 두 번째 실행 → 그래도 실패면 failed, 실행 2개
def test_skips_without_todo_or_existing(conn): ...  # has_todo 0 → None, 이미 있으면 새 실행 없음
def test_max_tool_calls_matches_eval(): ...  # FOLLOWUP_MAX_TOOL_CALLS == evaluation.metrics.MAX_TOOL_CALLS (tests/는 둘 다 import 가능)
```

- [ ] **Step 2: 실패 확인** → ImportError
- [ ] **Step 3: 구현** — 프롬프트 `prompts/followup/v1.md`: 7가지 종류의 뜻(`ACTION_DESC`와 같은 문장), "조회한 사실만 근거로, 지어내지 않기", "차트가 없으면 녹취록만으로", 끝은 반드시 `submit_followup`. `classify_one` 끝(자동 종결 계산 뒤)에서 게이트웨이가 있으면 `run_for_call` — 예외는 삼키고 실패로 남김(분류 흐름을 막지 않게)
- [ ] **Step 4: 통과 확인** — `uv run pytest tests/backend -q` → PASS
- [ ] **Step 5: 커밋** — `backend: 후속 에이전트 — 조회 도구·처리안 후검증·make followups`

### Task 4: 화면 A안 — 카드 한 줄, 상세 처리안 절

**Files:**
- Modify: `backend/calls.py`(`today_board` 항목·`call_detail`에 처리안), `frontend/lib/calls-api.ts`, `frontend/components/calls/today-board.tsx`, `frontend/components/calls/call-detail.tsx`, `tests/backend/test_api_calls.py`

**Interfaces:**
- Consumes: Task 3 `ax_call_followups`, `ACTION_KO`
- Produces:
  - 보드 항목에 `followup: {action, summary, status} | None`(그 통화의 활성 분류 행 처리안)
  - 상세 `followup: {action, summary, facts, status, reason, trace: [{tool, args}]} | None` — trace는 그 실행의 `ax_agent_steps` 도구 단계(`submit_followup` 제외)
  - `GET /labels`에 `action`(`ACTION_KO`)
  - 카드: 통화 카드 할 일 줄 아래 한 줄 — 종류 딱지(`Badge`) + summary, 실패면 흐린 "처리안 실패". 상세: 할 일 위 "처리안" 절 — 딱지·facts 목록·"과정 보기"(접힘)로 trace

- [ ] **Step 1: 실패하는 테스트**

```python
def test_board_and_detail_show_followup(
    client, tmp_path
): ...  # ax_call_followups 행을 넣고 보드 항목 followup.action, 상세 facts·trace
def test_followup_is_per_classification(
    client, tmp_path
): ...  # 처리안이 옛 분류 행(v1)에 걸려 있으면 활성 v2 상세에는 None
def test_labels_has_action(client): ...
```

- [ ] **Step 2: 실패 확인** → FAIL
- [ ] **Step 3: 구현** — 화면은 `make lint`(tsc)와 개발 서버에서 처리안 한 건을 넣어 눈으로 확인(브라우저 스크린샷)
- [ ] **Step 4: 통과 확인** → PASS
- [ ] **Step 5: 커밋** — `frontend: 오늘 할 일 카드에 처리안 한 줄, 상세에 처리안·과정`

### Task 5: 라운드 배치 실행기와 평가 실행

**Files:**
- Create: `backend/harness/rounds.py`, `evaluation/followup.py`, `tests/backend/test_rounds.py`, `tests/evaluation/test_followup_eval.py`
- Modify: `Makefile`(`followup-eval`)

**Interfaces:**
- Consumes: Task 2 배치 도구 호출, Task 3 `spec`·`input_text`·`verify_facts`·`SubmitArgs`
- Produces:
  - `rounds.run_rounds(runner: Runner, batch_gw, run_ids: list[int], state_path: Path, min_batch: int = ROUND_BATCH_MIN, poll_seconds: int = 60) -> dict[int, Outcome]` — 방법: 실행마다 `runner.drive`를 "기록 게이트웨이"로 돌린다. 기록 게이트웨이의 `complete`는 그 실행(`ref`)의 결과가 준비돼 있으면 돌려주고, 없으면 요청을 모으고 `Pending`을 던져 그 실행의 drive를 멈춘다(하네스는 이미 저장된 단계로 다시 세워 이어 가므로 상태가 맞다). 모인 요청이 `min_batch`보다 많으면 Batch 제출(배치 id·요청 키를 `state_path`에 남김) → 끝나면 결과를 채워 다음 라운드, 적으면 같은 요청을 일반 게이트웨이로 바로 부른다. 모든 실행이 끝날 때까지 반복
  - `evaluation/followup.py`:
    - 대상은 Task 1 `evaluation.call_actions.eval_calls`
    - `run(app_db, source_db, out: Path, *, batch=True) -> dict` — `app.sqlite`를 `evaluation/runs/followup.sqlite`로 복사(이미 있으면 그대로 이어 씀)해 거기서 실행, 끝난 처리안을 `evaluation/followup_runs.jsonl`(한 줄 = `call_uid, classification_id, prompt_version, action, summary, facts, status, reason, tool_calls, cost_usd`)에 쓴다. 이미 줄이 있는 통화는 건너뜀
    - CLI `python -m evaluation.followup run [--no-batch]`, `make followup-eval`

- [ ] **Step 1: 실패하는 테스트**

```python
def test_rounds_switch_to_direct_below_min(...): ...  # 실행 3개, min_batch=2 → 첫 라운드 배치 1번, 남은 1개는 일반 호출
def test_rounds_resume_submitted_batch(...): ...  # 제출 뒤 끊김(배치 미완료) → 다시 run_rounds → 새 제출 없이 같은 배치 결과로 이어짐
def test_rounds_outcome_matches_direct(...): ...  # 같은 스크립트 게이트웨이로 배치·일반 경로의 최종 처리안이 같음
def test_run_writes_jsonl_and_skips_done(tmp_path): ...  # 두 번째 run은 새 실행 없음, app.sqlite 원본은 그대로(행 수 비교)
```

- [ ] **Step 2: 실패 확인** → FAIL
- [ ] **Step 3: 구현**
- [ ] **Step 4: 통과 확인** — `uv run pytest tests/backend/test_rounds.py tests/evaluation -q` → PASS
- [ ] **Step 5: 커밋** — `eval: 후속 에이전트 평가 실행 — 라운드 배치(30건 이하 일반 호출)`

### Task 6: 규칙 기준선과 채점(`eval.py`)

**Files:**
- Modify: `evaluation/followup.py`(`baseline`·`score`), `evaluation/eval.py`(SECTIONS `followup`·렌더), `tests/evaluation/test_followup_eval.py`, `tests/evaluation/test_eval_cli.py`

**Interfaces:**
- Produces:
  - `baseline(todo: list) -> str` — 할 일 문장(`text`)을 합친 글의 낱말 규칙, 위에서부터 먼저 맞는 것: "스케일링" → `scaling_timing` / "영수증·진단서·소견서·서류·사본" → `document` / "미납·수납·결제·남은 금액·잔액" → `billing` / "보험·실손·비용·금액·가격" → `insurance_cost` / "예약·일정·날짜·시간·변경·취소" → `appointment` / "원장" → `doctor_reply` / 그 밖 `other`. 정답 파생(골드 목적·사실)을 쓰지 않는다
  - `score(conn, runs: list[dict], truth: dict[str, str], split_calls: dict) -> dict` — 정답은 Task 1 `truth(records)`. 구간마다 `{"n", "agent": {"k","n","by_action"}, "baseline": {...}, "failed": k, "unverified": k, "cost_usd", "missed_by_classifier": k(골드 할 일인데 분류기 아님), "extra_by_classifier": k}`. 실패한 실행은 오답
  - 리포트에 처리 정답의 LLM vs 사람 일치(Task 1 `agreement`)도 함께
  - `eval.py`: dev 기본 포함, test 묶음 `followup`(키 = 후속 프롬프트 버전, `--prompt-version`) — test 실행 줄이 없으면 "평가 실행 전". 렌더 "## ① 후속 처리" — 구간별 에이전트 vs 기준선 k/n·종류별·실패·비용·분류기 놓침

- [ ] **Step 1: 실패하는 테스트** — `baseline`의 각 규칙 한 건씩, 같은 처리안 줄로 `score`(에이전트 2/3, 기준선 1/3), dev 리포트에 "## ① 후속 처리"
- [ ] **Step 2: 실패 확인** → FAIL
- [ ] **Step 3: 구현**
- [ ] **Step 4: 통과 확인** → PASS
- [ ] **Step 5: 커밋** — `eval: 후속 처리안 정확도 — 에이전트 vs 규칙 기준선`

### Task 7: 문서와 실제 실행

**Files:**
- Modify: `docs/치과 AX - 앱 설계.md`(§1 `ax_call_followups` 칸, §7.2 `followup` task·별칭), `docs/치과 AX - 평가 설계.md`(§5 후속 처리 채점 모수·기준선·라운드 배치·처리 정답 만드는 법, §6 `truth_call_actions` 행을 `evaluation/call_actions.jsonl`로), `docs/치과 AX - 가상 데이터 생성 설계.md`(§5.1 "처리 정답" 줄 — 생성기가 아니라 평가 쪽 라벨로), `docs/치과 AX - 기능 정의.md`(§4 ① 화면·API, §9 후속 에이전트 줄 정리), `CLAUDE.md`(명령 `make action-label`·`make action-audit`·`make followups`·`make followup-eval`)
- Create(실행 결과): `evaluation/followup_runs.jsonl`, `evaluation/call_actions.jsonl`

- [ ] **Step 1: 문서** → `make fmt`·`make lint`·`make test` → 커밋 `docs: 처리 정답·후속 에이전트 규칙·명령 반영`
- [ ] **Step 2: 실제 실행** — `make action-label`(약 330통화, Opus 배치 약 $1~2); `make followups`(오늘 할 일 수십 건, 일반 호출); `make followup-eval`(약 330통화, 라운드 배치 — 설계에서 승인된 범위)
- [ ] **Step 3: 확인** — `eval.py --split dev`의 "## ① 후속 처리"에 두 구간 수치, 개발 서버에서 카드·상세 처리안
- [ ] **Step 4: 커밋** — `eval: 처리 정답 라벨·후속 에이전트 평가 실행 결과(v1)` — `evaluation/call_actions.jsonl`·`evaluation/followup_runs.jsonl`
- [ ] **Step 5: 사용자에게 `make action-audit` 안내** — 사람 표본 30통화(새 터미널). 끝나면 리포트 다시 뽑고 기록 커밋
