# 랭그래프 비교 실험 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** ⑧-1 불일치 조사 에이전트를 랭그래프로 한 벌 더 만들어(`evaluation/langgraph_recon.py`) dev 전부를 돌리고 하네스 v3 결과와 결과·호출·비용·재개·코드를 나란히 적는다.

**Architecture:** `StateGraph`(노드 `model`·`tools`, 조건 분기) + SQLite 체크포인터. 모델 호출은 `shared/llm.py` 게이트웨이, 도구·프롬프트·규칙은 `backend/recon_agent.py`·`recon_tools.py` 것을 그대로, 하네스 저장소에 기대는 부분은 그래프 상태 위 어댑터로. 채점은 `evaluation/recon.py` `score`에 에이전트 결과를 넘겨 같은 규칙으로.

**Tech Stack:** Python 3 · langgraph · langgraph-checkpoint-sqlite · sqlite3 · pytest

**Spec:** `docs/superpowers/specs/2026-11-16-langgraph-recon-design.md`

## Global Constraints

- 브랜치 `feat/langgraph-recon`(main에서). `git add`는 파일 이름으로, 커밋 `eval: …`/`docs: …`/`chore: …` + 본문 글머리 + 세션 귀속 두 줄
- `backend/`는 고치지 않는다(예외 없음). `backend/`는 이 모듈을 import하지 않는다. `app.sqlite`에 쓰지 않는다(읽기 전용 연결)
- 모든 LLM 호출은 게이트웨이(`Gateway.complete`), 기록은 `JsonlLog(evaluation/langgraph_recon_llm.jsonl)`
- 의존성은 uv 그룹 `langgraph`만(기본 그룹 아님), lock 커밋. 버전은 2주 이상 된 안정판
- 과금 실행은 사용자 승인 뒤(훅 확인 창). `--plan`·`--help`·`score`는 무료
- 주석·독스트링 한국어, 근거 문서 절. 커밋 전 `make fmt` → `make lint` → `make test`

## Review Focus

1. 같은 건에서 하네스와 다르게 움직이는 숨은 동작 — nudge 한 번, 결과 자르기, 막힘·오류 돌려주기, 상한 순서(Task 2 테스트)
2. 근거 번호 규칙·조회 번호가 어댑터 위에서 하네스와 같은 번호를 내는가(Task 1 테스트)
3. 재개 — 끊긴 뒤 같은 thread로 다시 돌릴 때 모델 호출이 다시 나가지 않는가(Task 3 테스트, 결과를 숨기지 않고 기록)
4. 결과 줄이 있는 건 건너뛰기·실패 건 다시 돌기(Task 4 테스트)
5. 채점 — 하네스 결과와 같은 정답 대응·규칙(Task 4 테스트)

---

### Task 1: 의존성·상태·어댑터·도구 실행

**Files:** Modify `pyproject.toml`·`uv.lock`; Create `evaluation/langgraph_recon.py`, `tests/evaluation/test_langgraph_recon.py`

**Produces:**
- `State(TypedDict)`: `key`, `messages: list[dict]`(Anthropic 형식), `steps: list[dict]`(하네스 `parse_step` 모양 — `kind`·`tool`·`args`·`result`·`is_error`·`rule_id`·`response` …), `model_calls`, `tool_calls`, `cost_usd`, `input_tokens`, `output_tokens`, `status`, `reason`, `pending: list[dict]`
- `class GraphStore`: `conn`(읽기 전용 앱 연결) + `steps(run_id) -> list[dict]`(지금 상태의 steps) — `recon_tools`·`recon_agent._evidence`가 보는 `ctx.store` 자리
- `call_tool(conn, state, call: dict) -> tuple[dict, dict]` — (모델에 줄 tool_result 블록, 새 step). 하네스 `Runner._tool`과 같은 순서: 허용 밖 → `NOT_ALLOWED` 막힘, 인자 검증 실패 → `BAD_ARGS`, before 규칙(`recon_agent.rules()`) → 막힘, 실행(쓰기 아닌데 `total_changes` 바뀌면 `READ_ONLY` 막힘), 예외 → is_error 결과. 결과 블록은 `backend.harness.loop._tool_message`(자르기·`external` 감싸기)로

- [ ] **Step 1:** `uv add --group langgraph langgraph langgraph-checkpoint-sqlite` → lock 갱신 확인
- [ ] **Step 2: 실패 테스트**(`pytest.importorskip("langgraph")` 맨 위) — 실데이터 없이 작은 앱·소스 DB 픽스처(기존 `tests/backend/test_recon_agent.py` 픽스처를 재사용할 수 있으면 그것)
  - `test_lookup_numbers_follow_steps`: 조회 두 번 → 결과의 `lookup_no` 1, 2
  - `test_evidence_rule_blocks_unknown_number`: 조회 1번 뒤 `submit_cause(evidence=[3])` → 막힘, rule_id `recon.evidence`, step `is_error`
  - `test_not_allowed_and_bad_args_blocked`
  - `test_tool_error_returned_to_model`
  - `test_result_truncated_like_harness`: 긴 결과 → `_tool_message`와 같은 블록
- [ ] **Step 3:** 구현 → **Step 4:** `uv run --group langgraph pytest tests/evaluation/test_langgraph_recon.py -q` PASS → **Step 5:** 커밋

### Task 2: 그래프(모델 노드·분기·상한)

**Produces:** `build_graph(gw, conn, checkpointer) -> CompiledGraph`; `investigate(graph, key, text) -> dict`(최종 State)

- 노드 `model`: 비용 상한(`RECON_MAX_USD`) 넘었으면 handed_off(`cost_limit`) → `gw.complete(ALIAS, task=TASK, messages, system=prompts v3 system + TRUST_NOTICE, tools=registry 스키마, prompt_version=v3)`; `BudgetExceeded` → handed_off(`budget`); 실패 → failed; 도구 없음 → nudge 한 번(`nudge_text`, 두 번째 글은 done)
- 노드 `tools`: pending을 차례로 `call_tool`, 호출 상한(`RECON_MAX_TOOL_CALLS`) 넘으면 handed_off(`call_limit`), `submit_cause` 통과면 done
- 입력 첫 메시지는 `external("input", recon_tools.input_text(...))`

- [ ] **Step 1: 실패 테스트**(FakeGw — 정해진 tool_calls 순서를 돌려주는 가짜)
  - `test_submit_ends_done`: 조회 → submit → status done, `recon_agent._summary`로 읽은 cause가 제출값
  - `test_text_answer_nudged_once`: 글 → nudge → 글 → done(제출 없음 → unknown)
  - `test_call_limit_hands_off`: 조회만 13번 → handed_off `call_limit`
  - `test_cost_limit_hands_off`
  - `test_lines_from_graph_steps`: `recon_agent.lines(state["steps"])`가 조회·결론 줄을 낸다(§4-5 — 상태 모양이 같으면 화면 줄이 나온다는 확인)
- [ ] **Step 2~5:** 실패 확인 → 구현 → PASS → 커밋

### Task 3: 재개 시험

- [ ] **Step 1: 테스트** `test_resume_after_interrupt`: 도구 실행 k번째에서 예외로 끊고(FakeGw는 호출 수를 센다) 같은 thread id로 다시 `invoke` → 최종 결과가 끊지 않은 실행과 같은지, 모델 호출 수 합, 도구 실행 횟수 합. 단정은 "결과가 같다"까지만, 호출 수는 값을 측정해 테스트 이름·주석에 남기고(노드 단위 재실행이면 그만큼 많다) 실험 기록에 그대로 적는다
- [ ] **Step 2~5:** → 커밋. 측정값을 레저에 기록

### Task 4: 실행·채점·문서

**Produces:**
- CLI `python -m evaluation.langgraph_recon {run|score} [--limit N] [--plan]`
  - 대상: 앱 DB의 하네스 v3 `ax_agent_runs`(agent `recon_cause`, done·handed_off) 키 중 지금 탐지에 있는 것 — 결과 줄(`evaluation/langgraph_recon_runs.jsonl`)이 있으면 건너뜀, failed는 다시
  - `--plan`: 대상 수 × 하네스 v3 건당 평균 비용 × 2(일반 호출) 출력, 게이트웨이를 만들지 않음
  - 결과 줄: `key`·`status`·`reason`·`cause`·`runner_up`·`evidence`·`model_calls`·`tool_calls`·`input_tokens`·`output_tokens`·`cost_usd`
- `evaluation/recon.py` `score(conn, split, today, agents=None)` — `agents`를 주면 `recon_agent.results` 대신 그것으로(기존 호출은 그대로)
- `score` 명령: 하네스(기존 결과)와 랭그래프 결과를 같은 `recon.score`로 나란히 + 모델·도구 호출 평균·최대, 토큰, 비용(랭그래프 실제·하네스 실제 Batch·하네스 토큰의 일반 호출 환산)

- [ ] **Step 1: 실패 테스트**: `test_run_skips_done_keys`, `test_plan_no_gateway`, `test_score_agents_override`(`recon.score`에 넘긴 결과가 쓰인다)
- [ ] **Step 2~4:** 구현 → PASS. 훅 `PAID_MODULES`에 `"evaluation.langgraph_recon": {"run"}`·라벨(+ 훅 테스트), CLAUDE.md 명령 한 줄
- [ ] **Step 5:** `make fmt`·`make lint`·`make test` → 커밋

### Task 5: 실측(사용자 승인됨 — 범위 가, dev 전부)

- [ ] `--plan` 숫자 확인 → `run`(과금, 훅 확인 창) → `score`
- [ ] 코드 비교: 랭그래프 모듈 줄 수, "프레임워크가 대신한 것 / 손으로 짠 것" 목록
- [ ] AI 실험 기록 새 절, 앱 설계 §3b 한 줄, 기능 정의 §9에서 삭제 → 커밋
