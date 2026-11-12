# ⑧ 4단계 — ⑧-2 지출 증빙 AI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 출금마다 증빙을 찾고 품목을 읽어 계정 과목을 추천하는 에이전트를 붙여, 규칙과 같은 줄은 자동 확정 후보로, 다른 줄은 AI 제안과 근거를 붙여 직원에게 보이고, 확정 제한·규칙 기준선과의 짝 비교 채점까지 만든다.

**Architecture:** ⑧-1 조사 에이전트(`backend/recon_agent.py`·`recon_tools.py`·`recon_auto.py`)와 같은 틀 — 하네스 `Runner` 위에 읽기 전용 조회 도구 + 끝내는 도구 `submit_match`, 실행 기록(`ax_agent_runs`·`ax_agent_steps`, 대상 = 출금 키)만이 상태. 규칙(`backend/expense.py` `rule_match`·`rule_account`)은 그대로 두고, 끝난 실행을 코드가 규칙 답과 비교한다. 일괄은 라운드 배치, 화면은 결과만(SSE 없음).

**Tech Stack:** Python 3 + sqlite3(SQL 그대로) + Pydantic + FastAPI, Next.js(shadcn/ui), pytest, Playwright.

**Spec:** `docs/superpowers/specs/2026-11-11-finance-recon-design.md` §4(브레인스토밍 결정 반영). 원본 규칙은 기능 정의 §5 ⑧-2, 평가 설계 §5 ⑧-2, 앱 설계 §3b·§5·§7.3.

## Global Constraints

- 도구는 모두 읽기 전용. `fin_*`·`emr_*`에 쓰지 않는다. `backend/`는 `ground_truth/`·`generator/`·`evaluation/`를 import하지 않는다(master 값은 `backend.prep.master_values(conn)`로만)
- 에이전트 입력에 규칙 답(`rule_match` 증빙·`rule_account` 과목·`check`/`증빙 못 찾음` 꼬리표)을 넣지 않는다. 입력 = 출금일·출금처 글자 그대로·금액·통장/카드
- 증빙 검색 범위 = 그 출금의 확정 후보(`expense._candidates` — `DOC_WINDOW` 안 + 출금한 달 발행)
- 조사 과정 줄은 조회 인자 `purpose`(필수)·`checked`(선택) — 모델에게 생각 글을 시키지 않는다(Sonnet 5.5 `reasoning_extraction` 거절, AI 실험 기록 §26)
- 별칭 `expense: [claude-sonnet-5-5]`(폴백 없음), 프롬프트 `prompts/expense/v1.md`
- 상한 시작값 `EXPENSE_MAX_TOOL_CALLS = 10`(끝내기 포함)·`EXPENSE_MAX_USD = 0.3` — Task 8 dev 분포로 다시 본다
- 과목은 출금 한 건에 하나. 품목이 두 과목에 걸치면 금액이 큰 과목
- 규칙과 같음(`agree`) = 규칙 증빙 집합 = AI 증빙 집합 **이고** AI 과목 = 거래처 표 기본 과목(`rule_account`). 다르면 "규칙과 다름"
- 자동 확정: `auto_policy` 키 `expense_autoconfirm` 기본 끔. 조건 = 규칙과 같음 **이고** 금액 차이 0 **이고** 품목으로 정하는 거래처가 아님 **이고** 같은 증빙을 고른 다른 후보가 없음
- 확정 제한(사람·AI 제안·자동 모두): 같은 증빙이 다른 출금에 살아 있는 확정(사람 확정 + 되돌리지 않은 자동 확정)에 있으면 막음, 기준일 이후 출금은 막음
- 사람이 자동 확정된 출금을 확정하면 그 자동 확정은 되돌린 것(`reverted_by = "사람이 바꿈"`)
- 프롬프트는 dev(2026-08·09)로만. `--split test` 채점은 사람만. 과금 명령은 건수·비용을 보이고 사용자 승인 뒤
- 커밋 전 `make fmt` → `make lint` → `make test`. `git add`는 파일 이름으로. 커밋 `<영역>: <무엇>` + 본문 글머리. 주석·독스트링은 한국어, 근거 문서 절

## Review Focus

1. 같은 증빙 집합을 다른 순서·중복으로 낸 AI 답 — 규칙과 "같음" 비교는 정렬한 집합으로(과목도 같아야 같음). Task 4 `test_agree_compares_sorted_sets`
2. AI가 후보 범위 밖 증빙이나 없는 증빙 id를 제출 — 제출 훅이 막고 모델이 다시 고른다(확정 때 400이 나지 않게). Task 3 `test_submit_outside_candidates_blocked`
3. 자동 확정된 줄을 사람이 다른 과목으로 확정 — 자동 확정은 되돌린 것이 되고 살아 있는 확정은 하나(⑥ 이중 합산 없음). Task 4 `test_human_confirm_reverts_auto`
4. 자동 확정 후보 둘이 같은 증빙을 고름 — 둘 다 자동 확정하지 않는다. Task 4 `test_shared_doc_candidates_not_auto`
5. 탐지 범위가 바뀌어(기준일·달) 사라진 출금의 실행·자동 확정 행 — 화면·오늘 화면 수에 끼지 않는다. Task 4 `test_auto_ignores_missing_outflows`

---

## File Structure

- Modify `backend/expense.py` — 출금 키(`outflow_key`), 확정 제한, 화면 줄에 AI 결과·비교·자동 확정, 칩 `differ`, 오늘 화면 수
- Modify `backend/recon_tools.py` — `_Note`(purpose·checked)와 조사 과정 줄 공통 부분을 ⑧-2도 쓰게 이름 공개(`Note`, `trace_lines`)
- Create `backend/expense_tools.py` — 조회 도구 4개, 에이전트 입력 글
- Create `backend/expense_agent.py` — 레지스트리·spec·제출 훅·`result`/`results`·`lines`·일괄 CLI(`make expense-ai`)
- Create `backend/expense_auto.py` — 규칙 비교(`agree`), 자동 확정 종류·조건·다시 계산·`active`
- Modify `backend/api.py`(되돌리기 종류), `backend/today.py`(자동 처리 목록)
- Create `prompts/expense/v1.md`; Modify `config/llm.yaml`, `Makefile`
- Modify `evaluation/expense.py`, `evaluation/eval.py` — AI 채점·짝 비교·묶음별·확신도별·자동 확정 오류율, test 채점 명령
- Modify `frontend/lib/finance-api.ts`, `frontend/components/finance/expense-table.tsx`, `frontend/lib/today-api.ts`, `frontend/components/today/today-view.tsx`, `frontend/e2e/finance.spec.ts`, `frontend/e2e/today.spec.ts`
- Docs: 앱 설계 §3b(도구 표 ⑧-2 행)·§5 ⑧·§9 `/finance`, 평가 설계 §5 ⑧-2, 기능 정의 §9, `CLAUDE.md` 명령, AI 실험 기록 새 절(Task 8)

---

### Task 1: 출금 키와 확정 제한

**Files:**
- Modify: `backend/expense.py`
- Test: `tests/backend/test_expense.py`(픽스처 `Outflows`·`DAY`·`AT`), `tests/backend/test_api_expense.py`

**Interfaces:**
- Produces:
  - `outflow_key(o: dict) -> str` — `o = {table, at, amount, label}`, 앞 12자리 sha256 of `f"{table}|{at}|{amount}|{label}"`(앱 설계 §7.3 "출금일·금액·적요 해시" — 같은 날 같은 금액 두 건이 겹치지 않게 시각·표까지)
  - `confirm(...)`이 새로 내는 ValueError 두 가지: `"기준일 이후 출금: …"`, `"이미 다른 출금에 확정된 증빙: {id} ({출금일} {출금처})"`
  - `confirm(conn, table, outflow_id, document_ids, account, by, at, today: date)` — `today` 인자 추가(API는 `clinic.today`)
  - `live_doc_owners(conn, data) -> dict[int, tuple[str, int]]` — 증빙 id → 그 증빙을 가진 살아 있는 확정의 출금(사람 확정 + 되돌리지 않은 `expense_autoconfirm` 행). Task 4가 자동 확정 결과(`result.document_ids`)를 같은 표에 더한다 — 이 Task에서는 사람 확정만, 자동 쪽은 `ax_auto_actions` 읽기를 함께 넣되 아직 행이 없다

- [ ] **Step 1: 실패하는 테스트**

```python
def test_confirm_rejects_doc_owned_by_other_outflow(o):
    a = o.bank("2026-11-10 10:00:00", "하늘치재", 50_000)
    b = o.bank("2026-11-12 10:00:00", "하늘치재", 50_000)
    d = o.doc("tax_invoice", "하늘치재", "2026-11-09", 50_000)
    c = o.app()
    expense.confirm(c, "fin_bank_transactions", a, [d], "재료비", by="desk", at=AT, today=DAY)
    with pytest.raises(ValueError, match="이미 다른 출금에 확정된 증빙"):
        expense.confirm(c, "fin_bank_transactions", b, [d], "재료비", by="desk", at=AT, today=DAY)
    expense.confirm(
        c, "fin_bank_transactions", a, [d], "소모품비", by="desk", at=AT, today=DAY
    )  # 같은 출금 덮어쓰기는 된다


def test_confirm_rejects_outflow_after_today(o):
    oid = o.bank("2026-12-02 10:00:00", "하늘치재", 50_000)
    with pytest.raises(ValueError, match="기준일 이후 출금"):
        expense.confirm(
            o.app(), "fin_bank_transactions", oid, [], "재료비", by="desk", at=AT, today=DAY
        )


def test_outflow_key_stable_and_distinct():
    a = {
        "table": "fin_bank_transactions",
        "at": "2026-11-10 10:00:00",
        "amount": 50_000,
        "label": "하늘치재",
    }
    assert (
        expense.outflow_key(a) == expense.outflow_key(dict(a))
        and len(expense.outflow_key(a)) == 12
    )
    assert expense.outflow_key(a) != expense.outflow_key(a | {"at": "2026-11-10 10:01:00"})
```
`test_api_expense.py`에 `test_confirm_doc_owned_elsewhere_400`(같은 증빙을 두 출금에 POST → 두 번째 400).

- [ ] **Step 2: 실행해 실패 확인** — `uv run pytest tests/backend/test_expense.py tests/backend/test_api_expense.py -q` → `today` 인자 없음/막지 않음으로 FAIL
- [ ] **Step 3: 구현** — `confirm`에 `today` 인자, 출금 `at >= today.isoformat()`이면 막음, `live_doc_owners`로 다른 출금 소유를 막음. 기존 `confirm` 호출(테스트·API)에 `today` 전달
- [ ] **Step 4: 통과 확인** — 같은 명령 PASS, `uv run pytest tests/backend -q` PASS
- [ ] **Step 5: 커밋** — `backend: ⑧-2 확정 제한 — 같은 증빙 두 출금·기준일 이후 출금 막기`

---

### Task 2: 조회 도구와 공통 조사 과정 줄

**Files:**
- Modify: `backend/recon_tools.py` — `_Note` → `Note`(공개), `lookup_steps` 그대로
- Modify: `backend/recon_agent.py` — `lines`의 조회·막힘·purpose/checked·모델 글 부분을 `recon_tools.trace_lines(steps, submit, conclusion, finished)`로 옮기고 감싼다(동작 그대로 — 기존 테스트가 기준)
- Create: `backend/expense_tools.py`
- Test: `tests/backend/test_expense_tools.py`, 기존 `tests/backend/test_recon_agent.py`(회귀)

**Interfaces:**
- Consumes: `expense._load`·`_candidates`·`_doc_view`, `recon_tools.Note`·`lookup_steps`
- Produces:
  - `recon_tools.trace_lines(steps: list[dict], submit: str, conclusion: Callable[[dict], dict], finished: bool) -> list[dict]` — 끝내는 도구 단계 인자 → 결론 줄 dict(`kind="conclusion"` + 기능별 칸). ⑧-1 `lines`는 이걸 부른다
  - `expense_tools.LOOKUPS = ("search_docs", "read_doc", "vendors", "accounts")`
  - `expense_tools.tools(submit: str) -> list[Tool]` — 결과에 `lookup_no`·`summary`. 도구는 실행 대상(ctx.target = 출금 키)의 후보 범위만 본다
    - `search_docs(issuer?: str, amount_min?: int, amount_max?: int, text?: str, date_from?: date, date_to?: date)` — 후보 중 조건에 맞는 것(본문 제외: id·kind·issuer·issued_on·amount), 상한 `SEARCH_ROWS = 30`. 발행처는 부분 일치
    - `read_doc(document_id: int)` — 후보 안의 증빙 본문. 후보 밖 id는 도구 오류
    - `vendors()` — 거래처 표(이름·카드 가맹점명·결제 방식·기본 과목·품목으로 정함)
    - `accounts()` — 과목 목록
  - `expense_tools.outflow_by_key(conn, key: str, today: date) -> dict | None`
  - `expense_tools.input_text(o: dict) -> str` — 출금일·출금처·금액·통장/카드만

- [ ] **Step 1: 실패하는 테스트**

```python
def test_search_limited_to_candidates(
    o,
): ...  # 창 밖·다른 달 증빙은 안 나온다, 같은 달 별칭 발행처((주)…)는 나온다
def test_read_doc_outside_candidates_errors(o): ...
def test_purpose_required(o): ...  # 네 도구 모두 purpose 없으면 ValidationError
def test_input_text_has_no_rule_answer(o):
    # 규칙이 증빙을 찾는 출금이어도 입력에 증빙 id·발행처·과목·"증빙 못 찾음"·"확인 필요"가 없다
    ...
    assert str(doc_id) not in text and "재료비" not in text and "못 찾음" not in text
```
- [ ] **Step 2: 실패 확인** — `uv run pytest tests/backend/test_expense_tools.py -q` → ImportError
- [ ] **Step 3: 구현** — `trace_lines` 추출 후 `uv run pytest tests/backend/test_recon_agent.py tests/backend/test_api_recon.py -q`가 그대로 PASS인지 먼저 본다. 이어 `expense_tools`
- [ ] **Step 4: 통과 확인** — 위 두 명령 PASS
- [ ] **Step 5: 커밋** — `backend: ⑧-2 증빙 조회 도구·조사 과정 줄 공통화`

---

### Task 3: 증빙 탐색 에이전트와 일괄

**Files:**
- Create: `backend/expense_agent.py`, `prompts/expense/v1.md`
- Modify: `config/llm.yaml`(`expense: [claude-sonnet-5-5]` — 폴백 없음 주석), `Makefile`(`expense-ai` — `MONTHS=`·`NO_BATCH=`, ⑧-1 `recon`과 같은 꼴)
- Test: `tests/backend/test_expense_agent.py`(대본 게이트웨이는 `tests/backend/test_recon_agent.py` `Script` 재사용)

**Interfaces:**
- Consumes: Task 2 `expense_tools.*`, `recon_tools.trace_lines`·`lookup_steps`, 하네스 `Runner`·`rounds`
- Produces:
  - `AGENT = "expense_match"`, `ALIAS = TASK = "expense"`, `PROMPT_VERSION = "v1"`, `SUBMIT = "submit_match"`, `EVIDENCE_RULE = "expense.evidence"`
  - `SubmitArgs(document_ids: list[int], account: str, confidence: Literal["high","mid","low"], reasons: list[Reason] (1~3), see: str | None (≤120))`, `Reason(text: str ≤120, lookups: list[int] ≥1)`
  - 제출 훅: `account` ∈ 과목 목록, `document_ids` ⊂ 그 출금 후보, 모든 `reasons[].lookups` ⊂ 1..n(그 실행의 조회 번호)
  - `result(conn, key) -> dict | None` / `results(conn, keys) -> dict[str, dict]` — `{run_id, status, tool_calls, cost_usd, document_ids, account, confidence, reasons, see, lines}`. ⑧-1처럼 끝났거나(done·handed_off) 잠금이 살아 있는 running만, 제출 없이 끝나면 `account=None`(결과 없음과 구분: status)
  - `targets(conn, today, months) -> list[dict]` — 지금 버전으로 끝난 실행이 없는 출금(실패는 다시 대상)
  - `run_batch(conn, gw, today, months, *, batch=True, state=STATE, poll_seconds=60) -> dict` — 끝나면 `expense_auto.refresh`
  - `main` — `python -m backend.expense_agent run [--month …] [--no-batch]`

- [ ] **Step 1: 실패하는 테스트**

```python
def test_submit_saves_match(
    o,
): ...  # 조회 1번 → 제출 → result의 document_ids·account·confidence·reasons
def test_submit_outside_candidates_blocked(
    o,
): ...  # 후보 밖 id 제출 → 훅 막힘(rule_id expense.evidence) → 다시 제출 통과
def test_reason_lookups_must_exist(o): ...  # reasons[].lookups에 없는 번호 → 막힘
def test_empty_documents_is_no_evidence(o): ...  # document_ids [] 제출 = 증빙 없음, 정상
def test_call_limit_reads_no_result(o): ...  # 조회만 되풀이 → handed_off, account None
def test_targets_skip_done_and_month_filter(o): ...
def test_batch_runs_targets(o, tmp_path): ...  # batch=False로 일괄 → done 1
```
- [ ] **Step 2: 실패 확인** — `uv run pytest tests/backend/test_expense_agent.py -q` → ImportError
- [ ] **Step 3: 구현** — `recon_agent`의 구조(`_LIVE` 쿼리·`ENDED`·`_close_stuck`·`run_batch` 옛 버전 running 처리)를 따른다. 프롬프트 v1: 과목 9개 설명 없이 거래처 표·과목 목록은 도구로, "품목이 두 과목에 걸치면 금액이 큰 과목·see에 섞였음", "월말 이체는 그달 명세서 합계(+세금계산서)", "증빙이 없으면 빈 목록", 조회 인자 purpose·checked, 상한 10번
- [ ] **Step 4: 통과 확인** — 같은 명령 PASS
- [ ] **Step 5: 커밋** — `backend: ⑧-2 증빙 탐색 에이전트·make expense-ai`

---

### Task 4: 규칙 비교와 자동 확정

**Files:**
- Create: `backend/expense_auto.py`
- Modify: `backend/expense.py`(`confirm`이 살아 있는 자동 확정을 되돌림, `live_doc_owners`에 자동 확정 더하기)
- Modify: `backend/api.py` — `AUTO_KINDS`에 `expense_auto.KINDS`
- Test: `tests/backend/test_expense_auto.py`

**Interfaces:**
- Consumes: Task 3 `expense_agent.results`, Task 1 `outflow_key`·`live_doc_owners`
- Produces:
  - `KIND = "expense_autoconfirm"`, `REASON = "규칙과 AI 증빙·과목이 같음"`, `KINDS`(조건은 저장된 AI 결과와 규칙을 다시 맞춰 봄, apply/undo는 앱 상태가 행 자체라 no-op — `recon_auto`와 같은 꼴)
  - `agree(rule_ids: list[int], rule_account: str | None, agent: dict | None) -> bool` — 증빙은 정렬한 집합으로, 과목은 `rule_account`(거래처 표 기본 — 사람 확정 기억 아님)와 같아야 같음. agent 결과 없음·제출 없음이면 False
  - `autoconfirm_reason(agreed: bool, diff: int | None, by_item: bool, shared: bool) -> str | None` — Global Constraints의 자동 확정 조건, 맞으면 `REASON`
  - `refresh(conn, today) -> dict` — 정책 꺼짐 `{"off": True}`. 사람 확정·되돌린 건 그대로, 조건을 못 넘게 된 행은 지움, 같은 증빙을 고른 후보가 둘 이상이면 모두 빼기. 커밋은 부르는 쪽
  - `active(conn, keys: set[str]) -> dict[str, dict]` — 되돌리지 않은 자동 확정(정책 꺼지면 `{}`), `{id, document_ids, account, at}`
  - `expense.confirm`: 그 출금에 살아 있는 자동 확정이 있으면 `reverted_by="사람이 바꿈"`으로 되돌린다

- [ ] **Step 1: 실패하는 테스트**

```python
def test_agree_compares_sorted_sets():
    a = {"document_ids": [1, 3], "account": "재료비"}
    assert (
        agree([3, 1], "재료비", a)
        and not agree([3, 1], "소모품비", a)
        and not agree([], None, None)
    )


def test_refresh_off_by_default(o): ...
def test_autoconfirm_when_rule_equals_agent(o): ...  # 정책 켬 → closed 1, 오늘 화면 수에서 빠짐
def test_by_item_vendor_not_auto(o): ...  # 쇼핑몰: 규칙 = AI여도 자동 확정 없음
def test_account_must_be_vendor_default(o): ...  # AI 과목이 거래처 기본과 다르면 없음
def test_shared_doc_candidates_not_auto(o): ...  # 두 출금이 같은 증빙 → 둘 다 없음
def test_human_confirm_reverts_auto(o):
    # 자동 확정 뒤 사람이 다른 과목으로 확정 → active() 비고, ax_auto_actions 행 reverted_by = "사람이 바꿈"
    ...


def test_auto_ignores_missing_outflows(o): ...  # 기준일을 앞당겨 출금이 사라지면 active()에 없음
def test_revert_endpoint_accepts_expense_auto():
    assert expense_auto.KIND in api.AUTO_KINDS
```
- [ ] **Step 2: 실패 확인** — `uv run pytest tests/backend/test_expense_auto.py -q` → ImportError
- [ ] **Step 3: 구현** — `recon_auto`를 따르고, 대상 키는 `outflow_key`
- [ ] **Step 4: 통과 확인** — `uv run pytest tests/backend -q` PASS
- [ ] **Step 5: 커밋** — `backend: ⑧-2 규칙 = AI 자동 확정(기본 끔)·사람 확정이 되돌림`

---

### Task 5: 화면 데이터·오늘 화면

**Files:**
- Modify: `backend/expense.py`(`_row`·`sheet`·`open_summary`), `backend/today.py`
- Test: `tests/backend/test_expense.py`, `tests/backend/test_api_expense.py`, `tests/backend/test_api_today.py`, 쿼리 수 테스트(있으면 같은 꼴로)

**Interfaces:**
- Consumes: Task 3 `results`, Task 4 `active`·`agree`
- Produces:
  - 줄에 `key: str`, `agent: {document_ids, docs(증빙 보기), account, confidence, reasons, see, status, lines} | None`, `agree: bool | None`(결과 없으면 None), `auto: {id, at} | None`
  - 자동 확정된 줄: `docs`·`account`는 자동 확정 결과, `account_from = "auto"`, `confirmed`는 None(사람 확정 아님)
  - `chips.differ` = 확정·자동 확정 안 된 줄 중 `agree is False`
  - `open_summary`: 지난달 확정·자동 확정 안 된 출금 중 (증빙 없음 또는 `check`) **또는** `agree is False`(설계 §4 사용자 결정 — 규칙 = AI는 세지 않는다)
  - `today.sheet`에 `ai.expense_auto = {on, total, recent[{id, key, at, label, amount, account}]}`(⑧-1 `recon_auto`와 같은 꼴)
  - `results`·`active`는 줄마다가 아니라 한 번에(쿼리 수가 줄 수와 무관)

- [ ] **Step 1: 실패하는 테스트** — `test_row_has_agent_and_agree`, `test_differ_chip_and_open_summary_counts_differ`, `test_auto_row_shows_auto_result`, `test_open_summary_skips_agreeing_rows`, `test_today_expense_auto_list`, `test_sheet_query_count_independent_of_rows`
- [ ] **Step 2: 실패 확인** — `uv run pytest tests/backend/test_expense.py tests/backend/test_api_today.py -q`
- [ ] **Step 3: 구현**
- [ ] **Step 4: 통과 확인** — `uv run pytest tests/backend -q` PASS
- [ ] **Step 5: 커밋** — `backend: ⑧-2 지출 증빙 줄에 AI 제안·규칙과 다름·자동 확정, 오늘 화면 수`

---

### Task 6: 화면

**Files:**
- Modify: `frontend/lib/finance-api.ts`(타입 `ExpenseAgent`·`ExpenseReason`, `ExpenseRow.key·agent·agree·auto`, `chips.differ`, `ACCOUNT_FROM_LABEL.auto = "자동 확정"`, `CONFIDENCE_LABEL`), `frontend/components/finance/expense-table.tsx`, `frontend/lib/today-api.ts`, `frontend/components/today/today-view.tsx`
- Test: `frontend/e2e/finance.spec.ts`, `frontend/e2e/today.spec.ts`(가짜 응답 `page.route`)

**Interfaces:**
- Consumes: Task 5 응답 모양
- Produces(화면, 설계 §4 시안 v3):
  - AI 칸: 맨 위 한 줄(증빙 종류·발행처·일자·금액 또는 "증빙 없음" → 과목, 확신도 배지 높음/중간/낮음), "왜" 글머리(근거 글 + "조회 n"), "직원이 볼 것" 주황 줄(`see`가 있을 때만), "조사 과정 보기" 접힘(⑧-1 `Line` 부품 재사용), 버튼 `이 제안으로 확정`(증빙 없음이면 `증빙 없이 확정`) → 기존 확정 API에 AI 증빙·과목, `바꾸기` → 기존 후보 고르기
  - `agree === true`: "규칙·AI 같음" 짧은 표시(AI 칸 접힘), `false`: "규칙과 다름" 꼬리표 + AI 칸 펼침
  - 칩 `규칙과 다름 n`(거르기), 자동 확정 줄 배지 + 되돌리기(기존 `/auto-actions/{id}/revert`)
  - 오늘 화면 자동 처리 목록에 지출 증빙 자동 확정
  - 색은 `globals.css` 토큰, 부품은 `components/ui`(필요하면 variant 추가)

- [ ] **Step 1: 실패하는 e2e** — `"규칙과 다른 줄은 AI 칸이 펼쳐지고 이 제안으로 확정"`(POST body에 AI 증빙 id·과목), `"규칙과 같은 줄은 짧게"`, `"증빙 없음 제안은 증빙 없이 확정"`, `"직원이 볼 것은 있을 때만"`, `"규칙과 다름 칩으로 거른다"`, `"자동 확정 줄은 배지와 되돌리기"`, 오늘 화면 `"지출 증빙 자동 확정 목록"`
- [ ] **Step 2: 실패 확인** — `npm --prefix frontend run -s e2e -- e2e/finance.spec.ts e2e/today.spec.ts`
- [ ] **Step 3: 구현**
- [ ] **Step 4: 통과 확인** — 같은 명령 PASS, `make lint` PASS
- [ ] **Step 5: 커밋** — `frontend: ⑧-2 AI 제안 칸·규칙과 다름·자동 확정 표시`

---

### Task 7: 채점

**Files:**
- Modify: `evaluation/expense.py`, `evaluation/eval.py`
- Test: `tests/evaluation/test_expense_eval.py`

**Interfaces:**
- Consumes: Task 3 `expense_agent.results`, Task 4 `agree`·`autoconfirm_reason`, `backend.expense.outflow_key`
- Produces:
  - `score(conn, split, vendors=None)`에 더함: `prompt_version`, `agent.match {k,n}`·`agent.account {k,n}`, `pair_match`·`pair_account` `{rescued, lost, both, neither}`, `by_group`(규칙 결과 묶음 `passed`/`none`/`check`마다 규칙·AI 매칭·과목), `by_confidence`(high/mid/low마다 AI 매칭·과목 맞은 수), `auto {k: 틀림, n: 조건을 넘은 줄}`(정책과 무관하게 셈 — 틀림 = 증빙 집합 또는 과목이 정답과 다름), `missing`(실행 없음, 오답으로 셈), 건별 목록(규칙 틀림·AI 틀림)은 dev만
  - `eval.py`: dev 결과에 `expense`를 넣고 `_render_expense` 표(규칙 vs AI, 묶음별, 확신도별, 짝 비교, 자동 확정 오류, 실행 없음 경고)
  - `python -m evaluation.expense score --split dev|test [--force]` — test는 `reports/expense_runs.jsonl`에 버전마다 한 번(⑧-1과 같은 꼴, 사람)

- [ ] **Step 1: 실패하는 테스트** — `test_agent_score_and_pair`, `test_by_group_and_confidence`, `test_auto_error_counts_condition_rows_only`, `test_missing_run_counts_wrong`, `test_test_split_hides_rows`(test는 건별 목록 None)
- [ ] **Step 2: 실패 확인** — `uv run pytest tests/evaluation/test_expense_eval.py -q`
- [ ] **Step 3: 구현** — 기존 규칙 채점 값(dev 48/59 · 48/59)은 바뀌지 않아야 한다(기존 테스트 그대로 통과)
- [ ] **Step 4: 통과 확인** — `uv run pytest tests/evaluation -q` PASS
- [ ] **Step 5: 커밋** — `eval: ⑧-2 AI 매칭·과목 정확도·짝 비교·확신도별·자동 확정 오류율`

---

### Task 8: 문서·dev 실행·실험 기록

**Files:**
- Modify: 앱 설계 §3b 도구 표 ⑧-2 행·자동 처리 키 표·§5 ⑧(API 설명에 확정 제한·`expense` 별칭·`make expense-ai`)·§9 `/finance` 줄, 평가 설계 §5 ⑧-2(AI 채점 항목), 기능 정의 §9(⑧ 4단계 항목 지우고 사람 할 일 — 버전 정한 뒤 전체 달 `make expense-ai`·test 채점·정책), `CLAUDE.md` ⑧ 명령 줄, `docs/치과 AX - AI 실험 기록.md` 새 절
- Test: `uv run pytest tests/generator/test_doc_sync.py -q`

- [ ] **Step 1: 문서** — 규칙만(경위·수치는 실험 기록). 값은 코드 이름으로
- [ ] **Step 2: 시험 실행(과금, 승인 뒤)** — dev 몇 건(규칙 통과·못 찾음·과목 확인 필요가 섞이게, 스크래치 스크립트) → 결과·단계 수·비용 보고
- [ ] **Step 3: dev 전부(과금, 승인 뒤)** — `make expense-ai MONTHS="2026-08 2026-09"` → `uv run python -m evaluation.expense score --split dev`
- [ ] **Step 4: 상한·프롬프트 다시 보기** — 단계 분포로 상한, 틀린 줄로 프롬프트(바꾸면 버전을 올리고 dev 다시 — 승인 뒤)
- [ ] **Step 5: 실험 기록** — 문제 → 원인 → 시도 → 결과 → 한계(규칙 vs AI 표, 묶음별, 확신도별, 자동 확정 오류, 비용)
- [ ] **Step 6: 커밋** — `docs: ⑧-2 AI — 도구·자동 확정 키·채점·명령` / `docs: AI 실험 기록 ⑧-2 지출 증빙 AI`
