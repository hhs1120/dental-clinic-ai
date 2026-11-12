# ⑧ 3단계 — ⑧-1 조사 에이전트 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 일계표 불일치 건마다 조회 도구로 원인을 찾는 에이전트를 붙여, 화면에서 조사 과정을 한 줄씩 보이고, 일괄 실행·자동 마감·규칙 기준선과의 짝 비교 채점까지 만든다.

**Architecture:** 하네스(`backend/harness/`)의 `Runner` 위에 읽기 전용 조회 도구 9개 + 끝내는 도구 `submit_cause`를 얹는다(① 후속 에이전트 `backend/followup.py`와 같은 꼴). 실행 기록은 `ax_agent_runs`·`ax_agent_steps`(대상 = 건 키)에만 두고, 화면 줄·채점·자동 마감·확정이 모두 그 기록을 읽는다. 일괄은 라운드 배치(`backend/harness/rounds.py`), 화면은 ③ 챗과 같은 SSE.

**Tech Stack:** Python 3 + sqlite3(SQL 그대로) + Pydantic + FastAPI SSE, Next.js(shadcn/ui), pytest.

**Spec:** `docs/superpowers/specs/2026-11-11-finance-recon-design.md` §3(이 브랜치에서 정한 것 반영됨). 원본 규칙은 기능 정의 §5 ⑧-1, 평가 설계 §5 ⑧-1, 앱 설계 §3b.

## Global Constraints

- 도구는 모두 읽기 전용(`Tool.writes=False`). 장부·금융 원본에 쓰지 않는다
- 에이전트 입력에 규칙 추정 원인(`recon.rule_cause`)과 생성 메타(연동 실패일 등)를 넣지 않는다
- `backend/`는 `ground_truth/`·`generator/`·`evaluation/`를 import하지 않는다(`tests/test_no_ground_truth_import.py`). master 값은 `backend.prep.master_values(conn)`(= `source.ax_master_values`)로만
- 원인 enum = `mv["recon_causes"]`의 키 9개 + `unknown`. 묶음 = `mv["recon_causes"][cause]`(`rule`/`ambiguous`)
- 단계 상한 `RECON_MAX_TOOL_CALLS = 8`(끝내는 도구 포함, 시작값 — Task 8에서 다시 정함), 실행당 `RECON_MAX_USD = 0.3`
- 모델 별칭 `recon: [claude-sonnet-5-5]`(폴백 없음 — 섞이면 짝 비교가 흐려진다), 프롬프트 `prompts/recon/v1.md`
- 자동 마감: `auto_policy` 키 `recon_autoclose` 기본 끔. 조건 = 규칙 원인 묶음이 `rule` **이고** 에이전트 1순위 = 규칙 원인
- 프롬프트는 dev(2026-08·09)로만 고친다. `--split test` 채점은 사람만(평가 설계 §2.7). 과금 명령은 실행 전에 건수·비용을 사용자에게 보이고 승인받는다
- 커밋 전 `make fmt` → `make lint` → `make test`. `git add`는 파일 이름으로. 커밋 메시지 `<영역>: <무엇>` + 본문 글머리
- 주석·독스트링은 한국어, 근거 문서 절을 가리킨다

## Review Focus

1. 조회 번호와 근거 — 도구 오류·훅 차단으로 끝난 단계는 번호를 받지 않아야 하고, 화면·훅·채점이 같은 번호를 본다(`lookup_steps` 하나로). Task 2 `test_lookup_numbers_skip_errors`
2. 상한에 걸려 끝난 실행(`handed_off`, `call_limit`)은 `unknown`으로 읽혀야 한다 — 확정 "맞음"이 `unknown`을 확정하지 못하게. Task 2 `test_call_limit_reads_unknown`, Task 4 `test_confirm_agent_unknown_needs_cause`
3. 탐지가 바뀌어 건 키가 사라진 실행·자동 마감 행은 화면·오늘 화면 수에 끼지 않는다. Task 3 `test_auto_ignores_stale_keys`
4. 조사 중 같은 건에 두 번째 `조사 시작`(다른 탭) — 409, 첫 실행은 끝까지. Task 4 `test_investigate_twice_409`
5. 기간 인자가 너무 넓은 조회(`find_amount` 32일 이상 등)는 막히고(검증 오류) 모델이 다시 고를 수 있다. Task 1 `test_range_limits`

---

## File Structure

- Create `backend/recon_tools.py` — 조회 도구 9개, 조회 번호 매기기(`lookup_steps`), 에이전트 입력 글(`input_text`)
- Create `backend/recon_agent.py` — 레지스트리·`AgentSpec`·근거 훅, 건 하나 실행(`investigate`), 실행 결과 읽기(`result`), 화면 줄(`lines`), 일괄 CLI(`make recon`)
- Create `backend/recon_auto.py` — 자동 마감 종류·조건·다시 계산·되돌리기 대상 목록
- Modify `backend/recon.py` — 화면 건에 에이전트 결과·자동 마감, 확정의 제안 출처, 열린 건 수에서 자동 마감 빼기
- Modify `backend/api.py` — `POST /finance/recon/{key}/investigate`(SSE), 되돌리기 종류 합치기
- Modify `backend/today.py` — 자동 처리 목록에 일계표 자동 마감
- Create `prompts/recon/v1.md`; Modify `config/llm.yaml`(별칭), `Makefile`(`recon`)
- Modify `evaluation/recon.py`, `evaluation/eval.py` — 에이전트 채점·짝 비교·자동 마감 오류율, test 채점 명령
- Modify `frontend/lib/finance-api.ts`, `frontend/components/finance/recon-item.tsx`, `frontend/lib/today-api.ts`, `frontend/components/today/today-view.tsx`, `frontend/e2e/finance.spec.ts`
- Docs: 앱 설계 §3b·§5 ⑧·§9 표, 평가 설계 §5 ⑧-1, 기능 정의 §9, `CLAUDE.md` 명령, AI 실험 기록 새 절(Task 8)

---

### Task 1: 조회 도구

**Files:**
- Create: `backend/recon_tools.py`
- Test: `tests/backend/test_recon_tools.py` (픽스처는 `tests/backend/test_recon_match.py`의 `Books`·`DAY`)

**Interfaces:**
- Produces:
  - `LOOKUPS: tuple[str, ...]` = `("ledger_day", "card_sales", "card_deposits", "bank_tx", "visits_day", "patient_balance", "family", "oral_care_prices", "find_amount")`
  - `tools(submit: str) -> list[Tool]` — 위 9개(`submit` = 끝내는 도구 이름, 조회 번호를 셀 때 뺀다), 각 결과 dict에 `lookup_no: int`·`summary: str`(한국어 한 줄, 화면 줄에 그대로)
  - `lookup_steps(steps: list[dict], submit: str) -> list[dict]` — 조회 번호 순 단계(도구 단계 중 `submit`이 아니고 `hook_status='pass'`·`is_error=0`인 것). 번호 = 목록 위치 + 1
  - `input_text(item: dict, books: dict, today: date) -> str` — `recon_match.detect` 건 하나 → 에이전트 입력

도구 인자·결과(SQL은 `source.` 접두어, 행은 시각 순):

| 도구 | 인자 | 결과 행 |
|---|---|---|
| `ledger_day` | `day: date` | 그날 `emr_payments` 전부(0원 진료 행 포함): `payment_id, time, patient_id, name, kind, amount_due, amount_paid, method, approval_no` + `prev_open_day`·`next_open_day`(수납이 있는 이웃 날) |
| `card_sales` | `date_from, date_to`(≤7일), `amount_min?, amount_max?` | `id, time, issuer, amount, approval_no, cancel_of` |
| `card_deposits` | `sales_date: date`, `issuer?` | `id, issuer, sales_date, sales_total, approved_total`(그 카드사·그날 승인 합계, 취소 차감), `fee, expected_fee`(`int(sales_total × 율)`), `deposited_on, expected_on`(`recon_match.add_bank_days`), `bank_in`(같은 날 같은 순액 통장 입금이 있나) |
| `bank_tx` | `date_from, date_to`(≤14일), `direction: "in"|"out"="in"` | `id, time, amount, memo` |
| `visits_day` | `day: date` | 취소 아닌 예약: `appointment_id, patient_id, name, start, end`(시작 + `duration_min`), `status, procedures`(`emr_treatments`→`emr_procedures` 이름) |
| `patient_balance` | `patient_id: int`, `before: date` | `balance`(`backend.query_tools._balance`와 같은 식) + 남은 금액이 있는 수납 행 최대 10개 |
| `family` | `patient_id: int` | 환자·보호자(`guardian_patient_id`)·같은 `family_id` 사람 `id, name` |
| `oral_care_prices` | 없음 | `mv["oral_care_prices"]` |
| `find_amount` | `amount: int > 0`, `date_from, date_to`(≤31일) | 그 금액인 장부 행(`amount_paid` 또는 `amount_due`), 카드 승인, 통장 입금 — 표마다 `table` 칸 |

- 기간 상한은 Pydantic `model_validator`로(넘으면 하네스가 `tool.bad_args`로 막아 모델이 다시 고른다)
- `lookup_no` = `len(lookup_steps(ctx.store.steps(ctx.run_id), SUBMIT)) + 1` — 도구 함수가 도는 때에는 자기 단계가 아직 없다. `submit` 이름은 `recon_agent`가 넘긴다(순환 import를 피해)
- `input_text`: `오늘`, 건 날짜(들)·결제수단, 장부 행(시각·`patient_id`·이름·종류·낼 돈·받은 돈·결제수단·승인번호), 금융 행(`recon._rows`/`_view`와 같은 값 — 정산 행은 기대 수수료·예상 입금일 포함), 장부 합계·금융 합계·차이. **`rule_cause`를 넣지 않는다**

- [ ] **Step 1: 테스트 작성**

```python
def test_ledger_day_includes_zero_rows_and_neighbors(b):
    ...
    # 11-03에 카드 1건 + 0원 진료 행 1건, 11-05에 수납 1건 → rows 2개, next_open_day == "2026-11-05"


def test_find_amount_spans_days(b):
    ...
    # 11-03 장부 55_000, 11-06 통장 입금 55_000 → find_amount(55_000, 11-01, 11-10)의 table 집합 == {"emr_payments", "fin_bank_transactions"}


def test_card_deposits_expected_values(b):
    ...
    # deposit(fee=기대+300) → fee - expected_fee == 300, expected_on == add_bank_days(...)


def test_range_limits():
    ...
    # find_amount 32일, card_sales 8일, bank_tx 15일 → ValidationError


def test_lookup_numbers_skip_errors():
    ...
    # steps: 도구 pass, 도구 is_error, 도구 blocked, submit → lookup_steps 길이 1


def test_input_text_has_no_rule_cause(b):
    ...
    # 금액 오입력 건 → "금액 오입력"·"amount_typo"가 글에 없고, 장부 3,500·금융 35,000은 있음


def test_tools_read_only():
    ...
    # 모든 Tool.writes is False, 이름 == LOOKUPS
```

- [ ] **Step 2: 실패 확인** — `uv run pytest tests/backend/test_recon_tools.py -q` → ImportError
- [ ] **Step 3: `backend/recon_tools.py` 구현** (모듈 독스트링: 하는 일 + 설계 §3)
- [ ] **Step 4: 통과 확인** — 같은 명령 → 전부 PASS
- [ ] **Step 5: 커밋** — `git add backend/recon_tools.py tests/backend/test_recon_tools.py` → `backend: ⑧-1 조사 에이전트 조회 도구`

---

### Task 2: 조사 에이전트

**Files:**
- Create: `backend/recon_agent.py`, `prompts/recon/v1.md`
- Modify: `config/llm.yaml`(별칭 `recon`), `Makefile`(`recon` 대상·`.PHONY`)
- Test: `tests/backend/test_recon_agent.py` (게이트웨이는 `tests/backend/test_followup.py`의 `Script` 꼴 — 실행마다 단계 대본, 글+도구 호출을 함께 내는 턴도 만들 수 있게 `("say", 글, 이름, 인자)` 꼴을 더한다)

**Interfaces:**
- Consumes: Task 1 `tools`, `lookup_steps`, `input_text`
- Produces:
  - `AGENT = "recon_cause"`, `ALIAS = TASK = "recon"`, `PROMPT_VERSION = "v1"`, `SUBMIT = "submit_cause"`, `EVIDENCE_RULE = "recon.evidence"`
  - `registry() -> tuple[ToolRegistry, tuple[str, ...]]`, `spec(names) -> AgentSpec`(`end_tools=(SUBMIT,)`), `rules() -> tuple[Rule, ...]`
  - `result(conn, key: str) -> dict | None` — 그 건의 지금 버전 최신 실행(`done`/`handed_off`/`running`) 요약: `{run_id, status, cause, runner_up, evidence, reason, tool_calls, cost_usd}`. 제출이 없으면 `cause="unknown"`(상한·비용·예산·실패), `running`이면 `cause=None`
  - `lines(steps: list[dict]) -> list[dict]` — `{kind: "hypothesis"|"compare"|"note"|"lookup"|"blocked"|"conclusion", text, tool?, args?, lookup_no?}`
  - `investigate(conn, gw, key: str, today: date, on_line=None) -> dict` — 건 하나 실행(일반 호출), 끝나면 `result()` 값. 지금 탐지에 없는 키 `ValueError`, 지금 버전 끝난 실행이 있으면 `AlreadyDone`, 다른 곳이 돌리면 `RunLocked`
  - `targets(conn, today, months: list[str] | None) -> list[dict]` — 지금 버전 `done`/`handed_off` 실행이 없는 건(실패는 다시)
  - `main(argv)` — `python -m backend.recon_agent run [--month YYYY-MM ...] [--no-batch]`

정한 것:
- `SubmitArgs`: `cause: Literal[원인 9개 + "unknown"]`, `runner_up: Literal[같은 것] | None`, `evidence: list[int]`(≤8), `reason: str`(1~200자). 원인 목록은 `generator`를 읽을 수 없으므로 상수 `CAUSES`로 두고 테스트가 `master.RECON_CAUSE_GROUPS` 키와 같은지 본다(`tests/`는 generator를 읽을 수 있다)
- 근거 훅(`Rule(EVIDENCE_RULE, before=…)`, `submit_cause`에만): `cause != "unknown"`인데 근거가 비었거나, 근거 번호가 `lookup_steps`의 번호 밖이면 막는다(이유 문장은 모델이 다시 내게 "조회 번호 1~n 중에서")
- `lines`: 모델 글을 빈 줄로 나눠 `가설:`→hypothesis, `비교:`→compare, 나머지→note(머리말 떼고). 조회 → lookup(`text` = 결과 `summary`, `lookup_no`), 막힌 도구 → blocked, `submit_cause` → conclusion. 실행이 `handed_off`면 마지막에 conclusion "조사 상한에 걸려 원인 미정"
- 실시간 줄: `investigate`가 게이트웨이를 감싸 `complete` 직전마다, 그리고 끝날 때 아직 안 보낸 줄을 `on_line`으로 보낸다(앞 턴의 글·조회가 다음 호출 전에 모두 저장돼 있다)
- 끊긴 실행: `followup._close_stuck`과 같은 처리(일반 호출 경로). 일괄은 `rounds.create` + `rounds.run_rounds`(상태 파일 `data/recon_batch.json`, `--no-batch`면 `min_batch = len + 1`), 끝나면 `recon_auto.refresh(conn, today)`(Task 3 — Task 3 전에는 이 줄을 두지 않는다)
- 시작 출력: 대상 건수와 예상 비용(건당 $0.08 × 건수, 추정)을 찍고 바로 돈다(과금 승인은 명령 전에 사람이 — 다른 `make` 과금 명령과 같게)
- `Makefile`: `recon:` → `uv run $(if $(wildcard .env),--env-file .env) python -m backend.recon_agent run $(foreach m,$(MONTHS),--month $(m)) $(if $(NO_BATCH),--no-batch)` + 위 주석 한 줄(과금, 이어 돎)
- 프롬프트 v1(머리말 없이 본문): 역할(동네 치과 데스크 마감 도우미), 원인 9개 정의(한국어 이름·장부 쪽/카드·통장 쪽), 할 일 순서(가설 → 조회 → 비교 되풀이, 글은 `가설:`·`비교:`로 시작), 근거 = 조회 번호, 조회 상한 8(끝내기 포함)이라 아껴 쓰기, 확신 없으면 `unknown`, 며칠 늦게 적은 정산은 `find_amount`로 다른 날을 보라는 한 줄. 규칙 기준선 순서나 답을 직접 알려 주는 예시는 넣지 않는다

- [ ] **Step 1: 테스트 작성**

```python
def test_causes_match_master():
    assert set(recon_agent.CAUSES) == set(master.RECON_CAUSE_GROUPS) | {"unknown"}


def test_submit_after_lookup_saves_cause(conn):
    ...
    # 대본: say("가설: 입력 누락 같다", ledger_day) → submit(entry_missing, evidence=[1]) → result()["cause"] == "entry_missing", status done


def test_evidence_must_be_lookup(conn):
    ...
    # submit(evidence=[3]) 조회 1번뿐 → 막힘(rule_id == EVIDENCE_RULE), 다음 턴 submit([1])이면 done


def test_unknown_needs_no_evidence(conn): ...
def test_call_limit_reads_unknown(conn):
    ...
    # 조회만 9번 → status handed_off, reason call_limit, result()["cause"] == "unknown"


def test_lines_kinds(conn):
    ...
    # say("가설: …\n\n비교: …", ledger_day) + submit → kinds == ["hypothesis", "compare", "lookup", "conclusion"], lookup_no == 1


def test_live_lines_match_saved(conn):
    ...
    # investigate(on_line=list.append) 결과 == lines(저장된 steps)


def test_input_has_no_rule_cause(conn):
    ...
    # 게이트웨이가 받은 첫 messages 글에 rule_cause 값·한국어 이름이 없음


def test_investigate_twice_raises(conn): ...  # AlreadyDone
def test_targets_skip_done_and_handed_off(conn): ...  # failed 실행 건은 다시 대상
def test_batch_month_filter(conn): ...  # targets(months=["2026-11"])만
```

- [ ] **Step 2: 실패 확인** — `uv run pytest tests/backend/test_recon_agent.py -q` → ImportError
- [ ] **Step 3: 구현** — `backend/recon_agent.py`, `prompts/recon/v1.md`, `config/llm.yaml` `recon: [claude-sonnet-5-5]  # ⑧-1 조사 에이전트(backend/recon_agent.py) — 폴백 없음(짝 비교가 섞이지 않게)`, `Makefile`
- [ ] **Step 4: 통과 확인** — 같은 명령 PASS, `uv run pytest tests/backend/test_prompts.py tests/test_no_ground_truth_import.py -q` PASS
- [ ] **Step 5: 커밋** — 위 파일 이름으로 add → `backend: ⑧-1 조사 에이전트·make recon`

---

### Task 3: 자동 마감

**Files:**
- Create: `backend/recon_auto.py`
- Modify: `backend/recon.py`(`open_summary`가 자동 마감 건 제외), `backend/recon_agent.py`(일괄 끝에 `refresh`)
- Test: `tests/backend/test_recon_auto.py`

**Interfaces:**
- Consumes: Task 2 `recon_agent.result`, `recon.rule_cause`, `backend.harness.auto`
- Produces:
  - `KIND = "recon_autoclose"`, `REASON = "규칙과 에이전트 원인이 같음 · 장부 수정 불필요"`, `KINDS: dict[str, AutoKind]`(apply·undo는 no-op — 앱 상태는 `ax_auto_actions` 행 자체, `autoclose._noop`과 같은 뜻)
  - `autoclose_reason(rule: str, agent: str | None, groups: dict) -> str | None` — `groups[rule] == "rule"`이고 `agent == rule`이면 `REASON`(채점도 이 함수를 부른다)
  - `refresh(conn, today) -> dict` — 정책 꺼짐이면 `{"off": True}`. 지금 탐지 건마다: 사람 확정이 있는 건·되돌린 건은 건너뜀, 조건을 넘고 행이 없으면 `apply_auto_action(..., result={"cause", "run_id"})`, 조건을 못 넘게 된 행은 지움. 커밋은 부르는 쪽
  - `active(conn, keys: set[str]) -> dict[str, dict]` — 되돌리지 않은 자동 마감 행 중 `keys`(지금 탐지 건 키)에 있는 것만 `{key: {id, cause, reason, at}}`

- [ ] **Step 1: 테스트 작성**

```python
def test_reason_only_rule_group_and_same():
    ...
    # ("fee_mismatch","fee_mismatch") → REASON, ("fee_mismatch","deposit_delay") → None, ("amount_typo","amount_typo") → None, agent None → None


def test_refresh_off_by_default(conn): ...
def test_refresh_closes_and_open_count_drops(conn):
    ...
    # 수수료 차이 건 + 에이전트 fee_mismatch 실행, 정책 켬 → refresh → recon.open_count == 0


def test_revert_stays_open(conn): ...  # revert_auto_action 뒤 refresh해도 다시 닫지 않음
def test_confirmed_items_skipped(conn): ...
def test_auto_ignores_stale_keys(conn): ...  # 탐지에 없는 키의 행은 active()에 없음
```

- [ ] **Step 2: 실패 확인** — `uv run pytest tests/backend/test_recon_auto.py -q`
- [ ] **Step 3: 구현** — `backend/recon_auto.py`, `recon.open_summary`에서 `active()` 키 제외(칩 `open_all`·`open_items`도 같은 기준 — `ledger`의 칩도 고친다), `recon_agent` 일괄 끝 `refresh` + 커밋
- [ ] **Step 4: 통과 확인** — `uv run pytest tests/backend/test_recon_auto.py tests/backend/test_recon.py tests/backend/test_today.py -q`
- [ ] **Step 5: 커밋** — `backend: ⑧-1 일계표 자동 마감(기본 끔)`

---

### Task 4: 화면 데이터·API

**Files:**
- Modify: `backend/recon.py`(`_view`·`confirm`), `backend/api.py`, `backend/today.py`
- Test: `tests/backend/test_api_recon.py`, `tests/backend/test_recon.py`, `tests/backend/test_api_today.py`

**Interfaces:**
- Consumes: Task 2 `result`·`lines`·`investigate`, Task 3 `active`·`KINDS`
- Produces:
  - 화면 건에 `agent: {status, cause, runner_up, evidence, reason, lines} | None`, `auto: {id, reason, at} | None`
  - `confirm`: 지금 버전 에이전트 결과(`cause`가 None이 아님)가 있으면 `proposed_by='agent'`, `proposed_cause`=에이전트 1순위, 없으면 지금처럼 규칙
  - `POST /finance/recon/{key}/investigate` → SSE 이벤트 `line`(줄 하나)·`done`(`agent` 값)·`error`. 없는 키 400, 이미 끝난 실행 409, 다른 곳이 돌리는 중 409. 게이트웨이는 의존성 `get_recon_gateway`(테스트가 바꿔 끼움, 기본은 앱 `runtime` 게이트웨이 — `get_chat_gateway`와 같은 꼴). 작업 스레드는 `_chat_worker` 꼴
  - `POST /auto-actions/{id}/revert`: `autoclose.KINDS | recon_auto.KINDS`
  - 오늘 화면 `ai.recon_auto: {on: bool, total: int, recent: [{id, key, date, cause, at}]}`(되돌리지 않았고 지금 탐지에 있는 건, 최근 `AUTO_RECENT`개)

- [ ] **Step 1: 테스트 작성**

```python
def test_item_has_agent_after_investigate(client):
    ...
    # 대본 게이트웨이로 POST investigate → 응답 본문에 "event: line"과 "event: done", 이어 GET의 item["agent"]["cause"]


def test_investigate_twice_409(client): ...
def test_investigate_unknown_key_400(client): ...
def test_confirm_records_agent_proposal(conn): ...  # proposed_by == "agent"
def test_confirm_agent_unknown_needs_cause(
    conn,
): ...  # 에이전트 unknown이어도 고른 원인으로 저장, proposed_cause "unknown"
def test_revert_recon_auto(client): ...  # 되돌리면 item["auto"] is None, open_items 다시 1
def test_today_recon_auto_list(client): ...
```

- [ ] **Step 2: 실패 확인** — `uv run pytest tests/backend/test_api_recon.py tests/backend/test_recon.py tests/backend/test_api_today.py -q`
- [ ] **Step 3: 구현**
- [ ] **Step 4: 통과 확인** — 같은 명령 PASS
- [ ] **Step 5: 커밋** — `backend: ⑧-1 조사 SSE·에이전트 제안 확정·자동 마감 표시`

---

### Task 5: 채점

**Files:**
- Modify: `evaluation/recon.py`, `evaluation/eval.py`(`_render_recon`)
- Test: `tests/evaluation/test_recon_eval.py`

**Interfaces:**
- Consumes: Task 2 `recon_agent.result`·`PROMPT_VERSION`, Task 3 `recon_auto.autoclose_reason`
- Produces: `score(conn, split, today)` 결과에 더함 —
  - `agent: {k, n}`(제출 없음·실행 없음·`unknown`은 오답), `agent_by_group`, `abstain: {k, n}`(unknown 수), `missing: int`(지금 버전 실행이 없는 정답 수 — 0이 아니면 렌더가 경고 줄)
  - `pair: {rescued, lost, both, neither}`(규칙·에이전트 맞음 짝), dev만 건별 목록 `pair_rows`
  - `steps_mean`(끝난 실행의 `tool_calls` 평균), `steps_hist`(dev만 — Task 8에서 상한을 다시 정할 자료)
  - `auto: {by_group: {g: {k: 틀린 수, n: 조건을 넘은 수}}}` — 정책과 상관없이 `autoclose_reason`으로 센다
  - `python -m evaluation.recon score --split test`(사람만) — 같은 렌더를 찍고 `reports/eval_runs.jsonl`에는 쓰지 않는다(평가 설계 §2.7의 test 실행 꼴을 따른다: `evaluation/claim_check.py` `--split test`를 보고 같은 방식으로)

- [ ] **Step 1: 테스트 작성**

```python
def test_agent_pair_counts(...): ...
    # 정답 3건: 규칙만 맞음 1, 에이전트만 맞음 1, 둘 다 1 → pair == {rescued 1, lost 1, both 1, neither 0}
def test_unknown_counts_wrong_and_abstain(...): ...
def test_missing_runs_reported(...): ...
def test_auto_error_rate_by_group(...): ...
def test_test_split_hides_rows(...): ...  # split test면 pair_rows·steps_hist None
```

- [ ] **Step 2: 실패 확인** — `uv run pytest tests/evaluation/test_recon_eval.py -q`
- [ ] **Step 3: 구현** — `_render_recon` 제목 "⑧-1 원인 정확도 — 규칙 기준선 vs 조사 에이전트", 표 `| 묶음 | 규칙 | 에이전트 |`, 짝 비교 한 줄, 기권률·단계 수·자동 마감 오류율 줄
- [ ] **Step 4: 통과 확인** — `uv run pytest tests/evaluation -q`
- [ ] **Step 5: 커밋** — `eval: ⑧-1 에이전트 원인 정확도·짝 비교·자동 마감 오류율`

---

### Task 6: 화면

**Files:**
- Modify: `frontend/lib/finance-api.ts`(타입·`investigate(key, onLine)` — `frontend/lib/chat-api.ts`의 SSE 읽기 꼴, `TOOL_LABEL`), `frontend/components/finance/recon-item.tsx`, `frontend/lib/today-api.ts`, `frontend/components/today/today-view.tsx`, `frontend/e2e/finance.spec.ts`

정한 것(와이어프레임 `index-v2.html` `AUTO_ACTIONS` "일계표 불일치 자동 마감" 문구):
- 건 펼침에 "조사 과정" 칸: 실행이 없으면 `조사 시작` 버튼, 누르면 줄이 하나씩. 줄 = 종류 배지(가설·조회 n·비교·결론·막힘 — 생각은 배지 없이 흐린 글) + 글. 조회 줄은 `TOOL_LABEL[tool]` + 요약. `다시 조사` 버튼 없음
- 결과 줄: `에이전트: <원인>`(2순위 흐리게, 근거 "조회 2·4") 옆에 기존 `규칙 추정`
- `맞음`은 에이전트 결과가 있으면 에이전트 1순위를, 없으면 규칙을 확정. 에이전트가 `unknown`이면 `맞음`을 끄고 원인 고르기만
- 자동 마감 건: `자동 마감` 배지 + `되돌리기`(오늘 화면 자동 종결과 같은 `POST /auto-actions/{id}/revert`)
- 오늘 화면 AI 칸 자동 처리 목록에 `recon_auto` 줄(정책 꺼짐이면 숨김)
- 색·부품은 `globals.css` 토큰과 `components/ui`만(새 모양은 `badge` variant로)

- [ ] **Step 1: e2e 시나리오 추가** — 실데이터 복사본에서 확정 안 된 건 하나를 펼쳐 조사 과정 칸이 보이는지(과금 호출은 하지 않는다 — 실행이 있는 건이면 저장된 줄, 없으면 `조사 시작` 버튼이 보이는지만)
- [ ] **Step 2: 구현**
- [ ] **Step 3: 확인** — `make lint`(프론트 ESLint·Prettier·tsc) 통과, `cd frontend && npm run build` 통과. `make e2e`는 로컬에서 돌리고 결과를 보고에 적는다
- [ ] **Step 4: 커밋** — `frontend: ⑧-1 조사 과정 줄·에이전트 제안·자동 마감 표시`

---

### Task 7: 문서

**Files:**
- Modify: `docs/치과 AX - 앱 설계.md`(§3b 도구 표 ⑧ 행 = 조회 도구 9개 + `submit_cause`, `request_auto_action` 빼고 자동 마감은 코드 경로. 자동 처리 키 표 ⑧-1 "날짜·결제수단·차이 금액" → 건 키. ⑧ 재무 대조 절에 `backend/recon_agent.py`·`recon_tools.py`·`recon_auto.py`와 investigate API·SSE 이벤트, 화면 표 `/finance` 행), `docs/치과 AX - 평가 설계.md`(§5 ⑧-1 — 에이전트 채점 항목·짝 비교·`unknown` 오답·자동 마감 오류율·test 명령), `docs/치과 AX - 기능 정의.md`(§9 ⑧ 항목에서 3단계 할 일 지우고 4단계만), `CLAUDE.md`(명령에 `⑧ make recon [MONTHS=] [NO_BATCH=1]`(과금, 이어 돎, `backend/recon_agent.py`))
- 값은 코드 이름으로 가리킨다(상한 숫자를 문서에 쓰지 않고 `RECON_MAX_TOOL_CALLS`)

- [ ] **Step 1: 고치기** → **Step 2:** `uv run pytest tests/generator/test_doc_sync.py -q` PASS → **Step 3:** 커밋 `docs: ⑧-1 조사 에이전트 — 도구·자동 마감 키·채점·명령`

---

### Task 8: dev 실행·프롬프트 다듬기·기록 (과금 — 사용자 승인 필요)

이 태스크는 실행 전에 멈추고 사용자에게 건수·예상 비용을 보여 승인을 받는다. 승인 없이는 진행하지 않는다.

- [ ] **Step 1: 승인 요청** — dev 달(2026-08·09) 탐지 건 수와 예상 비용(Batch 반값 반영)을 보인다
- [ ] **Step 2: 실행** — `make recon MONTHS="2026-08 2026-09"` → `make eval`(dev). 보는 것: 에이전트 vs 규칙(두 묶음), 짝 비교, 기권률, 단계 수 분포, 상한(`call_limit`)에 걸린 건 수, 자동 마감 오류율
- [ ] **Step 3: 상한 다시 정하기** — 상한에 걸린 건이 dev의 10% 이상이면 `RECON_MAX_TOOL_CALLS`를 올리는 안을, 대부분 5번 이하면 그대로 두는 안을 사용자에게 보이고 정한다
- [ ] **Step 4: 프롬프트 개정(필요하면)** — dev 틀린 건을 읽고 고친다. 버전 파일을 새로(`v2.md`), `PROMPT_VERSION`과 주석에 바뀐 점. 다시 돌릴 때마다 Step 1처럼 승인. 고친 횟수와 각 결과를 남긴다
- [ ] **Step 5: 기록** — `docs/치과 AX - AI 실험 기록.md` 새 절(문제 → 원인 → 시도 → 결과 → 한계): 규칙 vs 에이전트 숫자, 짝 비교, 늦게 적은 정산의 결과, 비용, 한계(같은 생성 설계가 만든 모양이라는 점 등). `reports/eval_runs.jsonl` 줄 포함 커밋 `eval: ⑧-1 조사 에이전트 dev 결과`
- [ ] **Step 6: 사람 몫 안내** — 버전을 정한 뒤 전체 달 `make recon`(앱 화면용, 과금 — 승인)과 `python -m evaluation.recon score --split test`(사람이 실행)를 안내한다
