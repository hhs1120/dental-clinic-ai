# 자동 개선 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 개선 에이전트가 silver 재료의 `has_todo` 오류를 보고 분류 프롬프트 후보를 쓰고, 기존 관문(+ 자동 처리 조건)이 거르고, 넘으면 대기 버전 → 사람이 재분류하면 전환되는 루프(`make improve`)와 읽기 전용 이력 탭을 만든다.

**Architecture:** `evaluation/improve.py`(재료·후보·검사·루프·CLI)와 `evaluation/improve_agent.py`(하네스 위 에이전트 — 도구·규칙·스펙)가 `evaluation/`에서만 돈다. 하네스 저장소는 `evaluation/runs/improve/harness.sqlite`(앱 스키마로 만든 별도 파일). 관문은 `evaluation/prompt_gate.py`를 넓혀 자동 처리 조건을 멈춤 대신 조건으로. 전환은 `backend/classify_batch.py`, 이력은 `reports/improve_runs.jsonl` → `GET /improve/runs` → AI 평가 탭.

**Tech Stack:** Python 3.11 · sqlite3 · 하네스(`backend/harness/`) · 게이트웨이(`shared/llm.py`) · FastAPI · Next.js(shadcn) · pytest · Playwright

**Spec:** `docs/superpowers/specs/2026-11-17-auto-improve-design.md`

## Global Constraints

- 브랜치 `feat/auto-improve`. `git add`는 파일 이름으로, 커밋 `<영역>: <무엇>` + 본문 글머리 + 세션 귀속 두 줄. 커밋 전 `make fmt` → `make lint` → `make test`
- `backend/`는 `evaluation/`·`ground_truth/`를 import하지 않는다(`tests/test_no_ground_truth_import.py`). backend는 `reports/improve_runs.jsonl` 파일만 읽는다
- 개선 에이전트의 실행 기록·LLM 로그는 `evaluation/runs/improve/`(커밋 안 함) — `app.sqlite`에 쓰지 않는다. 자동 개선이 앱 DB에 쓰는 것은 `ax_settings.pending_prompt_version` 하나
- test·dev·hard 행을 읽으면 실패(평가 설계 §2.7 금지). 입력은 silver 재료 구간만
- 고치는 곳은 `prompts/classify/<기준>/labels.md`의 `- has_todo:` 줄 하나. few-shot·예시 문장 금지
- 관문 값: 기존 `GATE_*` + 새 `GATE_AUTOCLOSE_ERROR_CAP = 0.05`(Wilson 95% 상한, `evaluation/metrics.py` `wilson`) — `GATE_VALUES`에 `"autoclose_error_cap"`으로
- 모델 별칭 `improve: [claude-opus-5-5]`(폴백 없음) — `config/llm.yaml`
- 과금 실행(Task 7 실측)은 사용자 승인 뒤(훅 확인 창). 테스트는 가짜 게이트웨이
- 주석·독스트링 한국어, 근거 문서 절(평가 설계 §2.7 등)

## Review Focus

1. 탈락 후보가 `prompts/classify/`에 남는 경로 — 관문 도중 예외(빈 통화로 `SystemExit`, 예산 초과)에서도 옮겨지는가(Task 4 `test_candidate_moved_on_gate_error`)
2. `improve_runs.jsonl`·실패 기록에 통화 id·녹취 조각·수치가 새는가 — 에이전트가 오류 유형 이름에 녹취 문구를 넣는 경우(Task 4 `test_free_text_leak_dropped`)
3. 재분류를 샤드·이어 돌기로 나눠 돌릴 때 전환 판단이 이번 실행 결과가 아니라 DB 전체 `ok`로 되는가(Task 5 `test_switch_needs_all_calls_ok_across_runs`)
4. 기준 버전이 대기 버전과 같은 이름이거나 후보 이름이 이미 있는 폴더일 때 덮어쓰지 않는가(Task 2 `test_candidate_name_never_overwrites`)
5. 자동 종결 조건이 기준 v2에 저장된 할 일 목록을 후보에 빌려 쓰고, 후보 `has_todo` 다수결이 참이면 종결되지 않는가(Task 1 `test_autoclose_condition_uses_candidate_labels`)

---

### Task 1: 관문 — 자동 처리 조건과 시도 수

**Files:** Modify `evaluation/metrics.py`, `evaluation/prompt_gate.py`; Test `tests/evaluation/test_prompt_gate.py`

**Interfaces — Produces:**
- `metrics.GATE_AUTOCLOSE_ERROR_CAP = 0.05`, `prompt_gate.GATE_VALUES["autoclose_error_cap"]`
- `prompt_gate.IMPROVE_REPORT = Path("reports/improve_runs.jsonl")`
- `prompt_gate.attempts(base, report=REPORT, improve_report=IMPROVE_REPORT)` — 지금 집합 ∪ `improve_runs.jsonl`의 관문 전 탈락 행(`gate` 가 None, 같은 `base`·`gate_hash`)의 `(candidate, candidate_sha256)`
- `prompt_gate.auto_conditions(cand: dict[str, dict], gold: dict, policy: dict, *, base_todo: dict[str, tuple[list, bool]], judge: dict[str, dict], trust: dict | None) -> list[str]` — 못 넘은 조건 이름 목록. `cand`는 후보 다수결 라벨(통화 → 9항목 원래 값), `base_todo`는 통화 → (기준 버전 할 일 목록, 요약 폴백), `judge`는 통화 → 활성 판정기 `_arg` 모양, `trust`는 후보 버전의 dev 입력 신뢰 경계 집계(없으면 None)
  - 정책 `call_autoclose` 켜짐: `autoclose_reason`으로 종결된 통화의 골드 오류(`evaluation/autoclose.py`와 같은 오류 정의 — has_todo·emergency·complaint·urgent 중 하나) Wilson 상한 > cap → `"autoclose_error"`. 종결 0건이면 통과
  - 정책이 하나라도 켜짐: `trust is None` → `"trust_unmeasured"`, 위반 > 0 → `"trust_violation"`
- `run_gate(..., policy, app_conn=None)` — 정책이 켜져 있으면 `auto_policy_on`으로 멈추지 않고, `app_conn`(앱 DB 읽기 전용)에서 기준 할 일·판정기 결과·후보 신뢰 경계 결과를 모아 `auto_conditions`의 결과를 `failed`에 더한다. 후보 다수결 라벨은 비교용 `value()` 전 원래 값이 필요하다 — `majority` 대신 다수결 원래 라벨을 고르는 작은 함수를 더한다(빌더 몫: 이름)

- [ ] **Step 1: 실패 테스트**
  - `test_gate_values_include_autoclose_cap`: `"autoclose_error_cap" in GATE_VALUES` and 값 0.05
  - `test_autoclose_condition_fails_on_one_error_of_88`: 88통화 종결·골드 오류 1 → `"autoclose_error" in auto_conditions(...)`; 오류 0 → 없음
  - `test_autoclose_condition_uses_candidate_labels`: 후보 라벨 `has_todo=True`인 통화는 종결되지 않아 오류에 안 잡힘, 기준 할 일 목록이 비어 있지 않으면 종결 안 됨
  - `test_trust_condition`: 정책 켜짐 + `trust=None` → `trust_unmeasured`; 위반 1 → `trust_violation`; 정책 꺼짐 → 둘 다 없음
  - `test_policy_on_no_longer_stops`: 정책 켜짐에서 `run_gate`가 `SystemExit("auto_policy_on")`을 내지 않고 행을 쓴다(가짜 gw)
  - `test_attempts_counts_precheck_rejects`: `improve_runs.jsonl`에 `gate: null` 행 하나 → `attempts`에 포함; 다른 `gate_hash`면 제외
- [ ] **Step 2:** `uv run pytest tests/evaluation/test_prompt_gate.py -q` → 새 테스트 FAIL
- [ ] **Step 3:** 구현. 신뢰 경계 집계는 `evaluation/input_trust.py`의 저장 결과에서 후보 버전 × 활성 판정기 쌍으로 읽는다(빌더 몫: 그 모듈의 기존 점수 함수 재사용)
- [ ] **Step 4:** 같은 명령 PASS, `uv run pytest tests/evaluation -q` PASS
- [ ] **Step 5:** 커밋 `eval: 관문 — 켜진 자동 처리 조건(오류율 상한·입력 신뢰 경계), 관문 전 탈락도 시도로`

### Task 2: 재료·후보·후보 검사

**Files:** Create `evaluation/improve.py`, `tests/evaluation/test_improve.py`

**Interfaces — Produces:**
- `ITEM = "has_todo"`, `RUNS = Path("evaluation/runs/improve")`, `REPORT = prompt_gate.IMPROVE_REPORT`
- `material_errors(app_conn, gold_rows: list[dict], base: str) -> list[dict]` — silver 재료 구간(`silver_split`) 중 기준 버전 `ok` 분류의 `has_todo` ≠ 골드인 통화 `[{call_uid, transcript, direction, model, gold}]`, `call_uid` 정렬. `gold_rows`에 silver 밖 행이 있으면 `SystemExit("holdout_in_material")`
- `rule_line(labels_md: str) -> str` — `- has_todo:` 줄. 없거나 둘 이상이면 `ValueError`
- `candidate_name(base: str, root: Path = prompts.PROMPTS_DIR) -> str` — `f"{base}-imp{n}"`, n은 `root/classify`·`RUNS/candidates`에 없는 가장 작은 1 이상 정수
- `write_candidate(base: str, name: str, new_rule: str, root=prompts.PROMPTS_DIR) -> Path` — 기준 폴더 복사 후 `labels.md`의 규칙 줄만 교체. 폴더가 이미 있으면 `FileExistsError`
- `check_candidate(base_md: str, cand_md: str, material: list[dict]) -> list[str]` — 실패 이름: `"scope"`(규칙 줄 밖 변경), `"example"`(새 규칙에 따옴표 `“ ” " '` 안 글), `"copy"`(새 규칙과 재료 녹취의 연속 `COPY_WORDS`어절 겹침 — `evaluation/claim_qa.py` `_grams`와 `COPY_WORDS`를 재사용)
- `leaks(text: str, material: list[dict]) -> bool` — 자유 글 칸 검사(같은 겹침 + 통화 id 패턴 `c-\d+`)

- [ ] **Step 1: 실패 테스트**(작은 DB 픽스처 — 기존 `tests/evaluation/test_prompt_gate.py` 픽스처를 재사용할 수 있으면 그것)
  - `test_material_only_silver_material_errors`: 검증 구간·일치 통화는 빠지고 재료 오류만, dev 행이 섞이면 `SystemExit`
  - `test_rule_line_unique`
  - `test_candidate_changes_only_rule_line`: `write_candidate` 뒤 `labels.md` diff가 한 줄, `summary.md` 같음
  - `test_candidate_name_never_overwrites`: `v2-imp1` 폴더가 있으면 `v2-imp2`, `RUNS/candidates/v2-imp2`가 있으면 `v2-imp3`; 같은 이름 `write_candidate` → `FileExistsError`
  - `test_check_scope_example_copy`: 각 조건을 하나씩 일으키는 후보 → 그 이름만; 깨끗한 후보 → `[]`
  - `test_leaks`: 녹취 4어절 조각·`c-0123` → True
- [ ] **Step 2~4:** FAIL 확인 → 구현 → `uv run pytest tests/evaluation/test_improve.py -q` PASS
- [ ] **Step 5:** 커밋 `eval: 자동 개선 — 재료·후보·후보 검사`

### Task 3: 개선 에이전트(하네스)

**Files:** Create `evaluation/improve_agent.py`, `prompts/improve/v1.md`, `tests/evaluation/test_improve_agent.py`; Modify `config/llm.yaml`(별칭 `improve`, 모델 단가가 없으면 `models`에 — 이미 `claude-opus-5-5` 있음)

**Interfaces:**
- Consumes: Task 2 `material_errors` 결과 모양
- Produces:
  - `open_store(path: Path = RUNS / "harness.sqlite") -> sqlite3.Connection` — 없으면 만들고 하네스 표(`shared/app_schema.sql` 전체를 적용해도 된다)
  - `spec() -> AgentSpec` — name `improve_classify`, alias `improve`, task `improve`, prompt_version `v1`, end_tools `("submit_rule",)`, `max_tool_calls` 40, `max_usd` 2.0(Claude 판단 — 재료 오류 수십 건 읽기)
  - 도구: `read_call(call_uid)` → `{transcript, direction, model, gold}`(재료 오류 목록 밖이면 훅 `improve.material_only`로 막힘); `submit_types(types: [{name, n}])`(한 번, 이름 40자 이내); `submit_rule(rule: str)`(끝내는 도구 — `- has_todo:`로 시작하는 한 줄, 300자 이내, 인자 검증 실패는 `BAD_ARGS`)
  - `input_text(base_rule: str, errors: list[dict], failures: list[dict]) -> str` — 기준 규칙 줄, 오류 통화 목록(id·모델 값·골드 값만, 녹취는 도구로), 앞선 탈락(`{"rule", "failed"}` — enum만)
  - `propose(gw, store_conn, base_rule, errors, failures, target: str) -> dict` — `{"status", "rule", "types", "cost_usd"}`; 하네스 `Runner`로 돌리고 `submit_rule` 없이 끝나면 `rule=None`
- 시스템 프롬프트(`prompts/improve/v1.md`): 일반 규칙만, 녹취 문장·예시·따옴표 금지, 다른 항목 언급 금지, 오류를 양쪽(0→1, 1→0) 모두 보고 묶을 것, 확신이 없으면 기준 규칙을 덜 바꿀 것

- [ ] **Step 1: 실패 테스트**(가짜 gw — 정해진 tool_calls 순서)
  - `test_read_call_blocks_outside_material`: 목록 밖 id → 막힘, rule_id `improve.material_only`
  - `test_submit_rule_ends_run`: read → types → rule → `propose` 결과 `rule`·`types` 채워짐, status done
  - `test_bad_rule_rejected`: 두 줄·`has_todo` 아닌 줄 → BAD_ARGS
  - `test_input_has_no_numbers_from_gate`: `failures` 입력에 수치 칸이 있어도 `input_text`에는 `rule`·`failed`만
  - `test_store_is_not_app_db`: `open_store` 경로가 `evaluation/runs/` 아래
- [ ] **Step 2~4:** → `uv run pytest tests/evaluation/test_improve_agent.py -q` PASS
- [ ] **Step 5:** 커밋 `eval: 자동 개선 — 개선 에이전트(하네스, 도구·훅·프롬프트 v1)`

### Task 4: 루프·기록·CLI

**Files:** Modify `evaluation/improve.py`, `Makefile`, `.claude/hooks/guard.py`, `tests/test_claude_hooks.py`, `CLAUDE.md`; Test `tests/evaluation/test_improve.py`

**Interfaces:**
- Consumes: Task 1 `run_gate`·`attempts`, Task 2 전부, Task 3 `propose`
- Produces:
  - `improve(gw, app_conn, gold_rows, calls, *, base: str, item="has_todo", config, gate_runner=prompt_gate.run_gate, prompts_root=prompts.PROMPTS_DIR, report=REPORT) -> list[dict]` — 행 목록. 순서: 멈춤(`pending_prompt_version` 있음 → `SystemExit("pending_exists")`, 남은 시도 0 → `SystemExit("attempt_cap")`) → 재료 → (후보 작성 → 검사 → 관문) × 남은 시도. 합격이면 `ax_settings.pending_prompt_version` = 후보, 멈춤
  - 후보 폴더는 관문이 끝나면(합격 외 모든 경우 — 예외 포함, `try/finally`) `RUNS/candidates/<이름>/`으로 옮긴다
  - 행(spec §6): `at`·`task`·`item`·`base`·`candidate`·`candidate_sha256`·`gate_hash`·`attempt`·`error_types`·`changed_rule`·`gate`(관문 행에서 `passed`·`failed`·`items`만, 관문 전 탈락이면 None)·`failed`(검사 실패 이름 또는 관문 `failed`)·`adopted`·`cost_usd`. `error_types` 이름·`changed_rule`이 `leaks`면 그 칸은 `None`, `failed`에 `"leak"`을 더한다
  - CLI `python -m evaluation.improve run --task classify --item has_todo` · `--plan`(재료 오류 수·남은 시도·예상 비용만, 게이트웨이 안 만듦). `make improve TASK= ITEM=`. 훅 `PAID_MAKE`에 `improve`, `PAID_MODULES`에 `"evaluation.improve": {"run"}`

- [ ] **Step 1: 실패 테스트**(가짜 `propose`·가짜 `gate_runner`)
  - `test_pass_sets_pending`: 관문 합격 → 대기 버전 = 후보, 후보 폴더가 `prompts/classify/`에 남음
  - `test_fail_twice_no_pending`: 둘 다 불합격 → 대기 없음, 행 2, 둘째 `propose` 입력의 `failures`에 첫 후보의 `rule`·`failed`
  - `test_precheck_reject_counts_and_skips_gate`: 검사 탈락 → gate_runner 안 부름, 행 `gate: None`, 시도 1 소모
  - `test_candidate_moved_on_gate_error`: gate_runner가 `SystemExit` → 후보 폴더가 `RUNS/candidates/`로
  - `test_stops_before_llm`: 대기 있음 / 시도 상한 → `propose` 0회
  - `test_free_text_leak_dropped`: 유형 이름에 녹취 4어절 → 행의 `error_types` None, `failed`에 `leak`
  - `test_rows_have_no_call_ids`: 행 JSON에 `c-\d+` 없음
  - `test_plan_no_gateway`
  - 훅 테스트: `make improve`·`python -m evaluation.improve run`은 확인 창, `--plan`은 통과
- [ ] **Step 2~4:** → `uv run pytest tests/evaluation/test_improve.py tests/test_claude_hooks.py -q` PASS
- [ ] **Step 5:** `make fmt`·`make lint`·`make test` → 커밋 `eval: 자동 개선 — 루프·기록·make improve`

### Task 5: classify 전환

**Files:** Modify `backend/classify_batch.py`(또는 새 `backend/prompt_switch.py` — 빌더 몫), `backend/db.py`(설정 읽기·쓰기 함수가 있으면 그것); Test `tests/backend/test_classify_switch.py`

**Interfaces — Produces:** `switch_if_ready(conn, version: str) -> bool` — `version == pending_prompt_version`이고 분류 대상 전체 통화(`classify_batch.targets`와 같은 모집단)의 그 버전 행이 모두 `ok`면 한 트랜잭션에서 `active_prompt_version` = version, `pending_prompt_version` 삭제, `disagreements.compute(conn, version, 판정기)`·`judge_queue.refresh_if_active`. `main`의 classify 실행 끝에서 부르고 결과를 출력

- [ ] **Step 1: 실패 테스트**
  - `test_no_switch_with_failed`: 한 통화 `failed` → False, 활성 그대로, 대기 그대로
  - `test_switch_needs_all_calls_ok_across_runs`: 샤드 두 번에 나눠 저장 — 첫 실행 뒤 False, 둘째 뒤 True
  - `test_switch_only_pending_version`: 대기가 아닌 버전을 다 돌려도 False
  - `test_switch_recomputes_disagreements`
- [ ] **Step 2~4:** → `uv run pytest tests/backend/test_classify_switch.py -q` PASS
- [ ] **Step 5:** 커밋 `backend: classify 대기 버전 전환 — 전부 ok면 활성 교체`

### Task 6: API·자동 개선 탭

**Files:** Modify `backend/api.py`, `frontend/components/eval/eval-view.tsx`; Create `frontend/components/eval/improve-tab.tsx`; Test `tests/backend/test_api_improve.py`, E2E(기존 AI 평가 e2e 파일에 한 시나리오)

**Interfaces — Produces:** `GET /improve/runs` → `{"runs": [행, 최신 먼저], "pending": str | None, "active": str}` — `reports/improve_runs.jsonl`(경로는 `Paths`처럼 주입 가능하게, 없으면 빈 목록)과 `ax_settings`

- 탭 이름 "자동 개선", 읽기 전용 표: 시각 · 후보 · 오류 유형(이름·수) · 바꾼 규칙 · 관문 결과(합격 / 못 넘은 조건 이름을 한국어 이름으로 — 매핑은 탭 파일에) · 반영. 대기 버전이 있으면 표 위에 한 줄("v2-imp1 대기 중 — 재분류(`make classify V=…`)가 끝나면 전환"). 빈 상태 문구. 부품은 `components/ui/`만, 색은 토큰만
- [ ] **Step 1: 실패 테스트**: `test_improve_runs_reads_report`, `test_improve_runs_empty`, `test_improve_runs_has_pending`
- [ ] **Step 2~4:** → PASS, `make lint`(프론트), `make e2e`(로컬)
- [ ] **Step 5:** 커밋 `backend·frontend: 자동 개선 이력 API·탭`

### Task 7: 실측·문서

- [ ] `uv run --env-file .env python -m evaluation.improve run --task classify --item has_todo --plan` → 숫자를 사용자에게 보고하고 승인 뒤 `make improve TASK=classify ITEM=has_todo`(과금, 훅 확인 창)
- [ ] 합격이면 재분류(`make classify V=<후보>`)는 사람 몫이라 하지 않고 보고만
- [ ] 문서: AI 실험 기록 새 절(문제→시도→결과(관문 표·탈락 이유)→한계→비용), 기능 정의 §9에서 삭제·§4 ② "착수 때" 정리, 평가 설계 §2.7(멈춤 문장 → 조건, `GATE_AUTOCLOSE_ERROR_CAP`)·§1 원칙 9 연결, 앱 설계 §7.2(전환 코드 이름)·§3b·`ax_settings` 설명, 기술 소개 ② "다음 단계" 정리, CLAUDE.md 명령 한 줄
- [ ] 커밋 `eval·docs: 자동 개선 실측`
