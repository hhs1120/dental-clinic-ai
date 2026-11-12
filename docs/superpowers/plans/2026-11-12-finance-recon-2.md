# ⑧ 재무 대조 2단계(⑧-2 지출 증빙, 코드) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 통장 출금·병원 카드 사용마다 규칙으로 증빙을 찾고 계정 과목 기본값을 달아 `/finance` 지출 증빙 탭에서 사람이 확정하게 하고, backend에 박힌 병원 값을 `ax_master_values`로 옮긴다.

**Architecture:** 고정된 ⑧-2 규칙 기준선(`evaluation/expense.py`)을 `backend/expense.py`로 옮기고 평가가 그것을 부른다. 화면 집계·확정·오늘 화면 줄은 `backend/expense.py`, API는 `backend/api.py`, 화면은 `frontend/components/finance/`. 병원 값은 생성기 `generator/clinic.py` `master_values_rows`가 싣고 backend는 `backend/prep.py` `master_values`로 읽는다.

**Tech Stack:** Python(sqlite3 SQL 그대로)·FastAPI·pytest, Next.js(Tailwind + shadcn/ui)·Playwright

**Spec:** `docs/superpowers/specs/2026-11-11-finance-recon-design.md` §2(2단계)와 "하면 실패인 것"

## Global Constraints

- backend는 `ground_truth/`·`generator/`·`evaluation/`을 import하지 않는다(`tests/test_no_ground_truth_import.py`). 정답은 `data/`에 넣지 않는다
- 병원 상수는 `generator/master.py`에만, backend는 `source.ax_master_values`로 읽는다
- 규칙(`rule_match`·`rule_account`·`DOC_WINDOW`)의 동작을 바꾸지 않는다 — dev 매칭 48/59·과목 48/59가 옮긴 뒤에도 같아야 한다(지금 데이터 실측)
- 채점·옮기기 전후 비교는 사람 확정 덮어쓰기 없는 순수 규칙만 부른다
- `make data`가 내보내는 LLM 작업 0건(값만 옮기는 것이라 캐시가 그대로)
- test 분할을 읽지 않는다(평가 설계 §2.7) — 채점은 dev만
- 화면 색·모서리는 `globals.css` 토큰, 부품은 `components/ui/`만
- 커밋 전 `make fmt` → `make lint` → `make test`(fmt는 커밋과 따로 실행 — 커밋 훅이 lint를 먼저 돈다). `git add`는 파일 이름으로
- 주석·독스트링은 한국어, 근거 문서 절을 가리킨다

## Review Focus

1. 거래처 표에 없는 적요·가맹점명(잘린 적요 "한결빌딩관", `KCP*…`) — 과목이 비고 `과목 확인 필요`로 보이며, 사람이 한 번 확정하면 같은 글자 그대로의 다음 출금에 그 과목이 채워진다(Task 3 테스트)
2. "품목으로 정함" 거래처(쇼핑몰 `페이온*모아덴탈몰`)를 사람이 복리후생비로 확정해도 다음 주문의 기본값은 거래처 과목 그대로, 늘 `과목 확인 필요`(Task 3 테스트)
3. 지금 탐지와 맞지 않는 확정 요청 — 없는 출금 id·모르는 과목·출금 범위 밖 증빙 id는 400(Task 4 테스트)
4. 월말 이체가 그달 명세서 합계와 안 맞는 달 — 증빙 없음으로 보이고 사람이 후보 목록에서 여러 장을 골라 확정할 수 있다(Task 3·4 테스트)
5. 오늘 화면 줄은 앱 기준일의 지난달만 센다 — 기준일이 달 첫날이어도 지난달, 확정한 건은 빠진다(Task 3 테스트)

---

### Task 1: 병원 값을 `ax_master_values`로

**Files:**
- Modify: `generator/master.py` (`DAYS_PER_MONTH` 새로, `_AGE_GROUP_RANGE` → `AGE_GROUP_RANGE`, `EXPENSE_ITEM_ACCOUNT_KINDS` 새로)
- Modify: `generator/demand.py:20` (`DAYS_PER_MONTH`를 master에서 import — `generator/schedule.py`의 import도 master로)
- Modify: `generator/clinic.py` `master_values_rows`
- Modify: `backend/query_tools.py` (`DAYS_PER_MONTH`·`SCALING` 지움), `backend/prep.py` (`DAYS_PER_MONTH`·`FULL_MOUTH` 지움)
- Test: `tests/generator/test_master_values.py`, `tests/backend/test_query_tools.py`, `tests/backend/test_prep.py`

**Interfaces:**
- Produces: `master_values(conn)`에 새 키 — `expense_vendors: list[{name, card_name, pay, account, by_item}]`, `expense_accounts: list[str]`(= `master.ACCOUNTS` 순서), `age_group_range: {name: [lo, hi|None]}`, `conditional_insured_min_age: int`, `days_per_month: float`, `full_mouth: str`
- `master.EXPENSE_ITEM_ACCOUNT_KINDS = ("shopping",)` — 품목으로 과목이 정해지는 거래처 종류(생성기 `generator/finance.py`의 `"shopping"` 비교도 이 이름으로)

- [ ] **Step 1: 실패하는 테스트** — `test_master_values_rows_match_master`에 더한다

```python
# ⑧-2 지출 증빙(설계 2026-11-11 재무 대조 §2)
assert got["expense_vendors"] == [
    {
        "name": v["name"],
        "card_name": v["card_name"],
        "pay": v["pay"],
        "account": v["account"],
        "by_item": v["kind"] in EXPENSE_ITEM_ACCOUNT_KINDS,
    }
    for v in VENDORS
]
assert got["expense_accounts"] == list(ACCOUNTS)
assert [v["name"] for v in got["expense_vendors"] if v["by_item"]] == ["모아덴탈몰"]
# ⑤ 급여 나이·이미 backend에 박혀 있던 값
assert got["age_group_range"] == {k: list(v) for k, v in AGE_GROUP_RANGE.items()}
assert got["conditional_insured_min_age"] == CONDITIONAL_INSURED_MIN_AGE
assert got["days_per_month"] == DAYS_PER_MONTH == 365.25 / 12
assert got["full_mouth"] == FULL_MOUTH
```

`test_no_generation_meta`에 `assert all(set(v) == {"name", "card_name", "pay", "account", "by_item"} for v in got["expense_vendors"])`(건수 범위·금액 범위·증빙 종류 같은 생성 값이 새지 않게).

- [ ] **Step 2: 실행해 실패 확인** — `uv run pytest tests/generator/test_master_values.py -q` → ImportError(`AGE_GROUP_RANGE`) 또는 KeyError
- [ ] **Step 3: master 상수와 `master_values_rows` 구현** — 위 키를 주석(쓰는 기능·근거 절)과 함께 더한다. `verify`의 `check_master_values`는 `master_values_rows`와 비교하므로 따로 고치지 않는다
- [ ] **Step 4: backend가 새 키를 읽게** — `query_tools._family_recall`·`prep`의 리콜 기한은 `mv["days_per_month"]`, `prep`의 치아 단위 아닌 시술 비교는 `mv["full_mouth"]`, `query_tools._summary`의 올해 스케일링 여부는 시술 이름 조인 대신 `c.procedure_code = mv["scaling_code"]`. `master_values`는 `backend.prep`에서 import(이미 claims·recon_match가 그렇게 씀)
- [ ] **Step 5: 테스트 통과·결과 불변 확인** — `make test` 통과. `make data` → "LLM 캐시에 없는 건" 줄이 없음, `make verify` 통과
- [ ] **Step 6: 커밋** — `backend: 병원 값을 ax_master_values로 — 거래처 표·계정 과목·급여 나이·월 일수·치아 번호`

### Task 2: 규칙 기준선을 `backend/expense.py`로

**Files:**
- Create: `backend/expense.py`
- Modify: `evaluation/expense.py` (규칙 본문을 지우고 backend를 부름 — `DOC_WINDOW`·`rule_match`·`rule_account`·`vendors_by_label`은 backend 것)
- Test: `tests/backend/test_expense.py`(새), `tests/evaluation/test_expense_eval.py`

**Interfaces:**
- Produces (`backend/expense.py`): `DOC_WINDOW = (-3, 14)`, `vendors_by_label(vendors: list[dict]) -> dict[str, dict]`(적요·가맹점명 → 거래처), `rule_match(outflow: dict, docs: list[dict], vendors: dict) -> list[int]`, `rule_account(outflow: dict, vendors: dict) -> str | None` — 지금 `evaluation/expense.py`와 같은 입력·출력. `vendors_by_label`은 기본 인자가 없다(앱은 `mv["expense_vendors"]`, 평가는 `master.VENDORS`)

- [ ] **Step 1: 실패하는 테스트** — `tests/backend/test_expense.py`

```python
def test_rule_from_master_values_equals_master():
    """앱(ax_master_values 거래처 표)과 평가(master.VENDORS)가 같은 거래처 사전을 만든다 — 규칙 = 채점한 기준선"""
    mv = {n: json.loads(v) for n, v in master_values_rows()}
    a, b = expense.vendors_by_label(mv["expense_vendors"]), expense.vendors_by_label(VENDORS)
    assert set(a) == set(b)
    assert all(
        {k: a[x][k] for k in ("name", "pay", "account")}
        == {k: b[x][k] for k in ("name", "pay", "account")}
        for x in a
    )
```

`tests/evaluation/test_expense_eval.py`의 기존 테스트는 import 위치만 바꾸지 않고 그대로 통과해야 한다(`ex.rule_match` 등은 `evaluation.expense`가 다시 내보냄).

- [ ] **Step 2: 실행해 실패 확인** — `uv run pytest tests/backend/test_expense.py -q` → ModuleNotFoundError
- [ ] **Step 3: 옮기기** — 본문을 그대로 옮기고 `evaluation/expense.py`는 `from backend.expense import DOC_WINDOW, rule_account, rule_match, vendors_by_label`, `score`의 기본 거래처는 `vendors_by_label(MASTER_VENDORS)`. 모듈 독스트링은 "기준선 원본은 backend/expense.py, 이 파일은 채점"으로
- [ ] **Step 4: 통과·점수 불변** — `uv run pytest tests/backend/test_expense.py tests/evaluation/test_expense_eval.py -q` 통과, 그리고

Run: `uv run python -c "import sqlite3; from evaluation.expense import score; c=sqlite3.connect(':memory:'); c.execute(\"attach 'data/source.sqlite' as source\"); c.execute(\"attach 'ground_truth/truth.sqlite' as truth\"); print(score(c,'dev'))"`
Expected: `{'n': 59, 'match': {'k': 48, 'n': 59}, 'account': {'k': 48, 'n': 59}}`

- [ ] **Step 5: 커밋** — `eval: ⑧-2 규칙 기준선을 backend/expense.py로 — 평가는 같은 규칙을 부른다`

### Task 3: 지출 증빙 집계·확정·오늘 화면 줄

**Files:**
- Modify: `backend/expense.py`, `shared/app_schema.sql`, `backend/today.py`
- Test: `tests/backend/test_expense.py`, `tests/backend/test_today.py`

**Interfaces:**
- Consumes: Task 1 `master_values` 새 키, Task 2 규칙
- Produces:
  - 테이블 `ax_expense_confirmations(outflow_table TEXT CHECK IN ('fin_bank_transactions','fin_card_expenses'), outflow_id INTEGER, label TEXT NOT NULL, document_ids TEXT NOT NULL, account TEXT NOT NULL, judged_by TEXT NOT NULL, judged_at TEXT NOT NULL, PRIMARY KEY (outflow_table, outflow_id))` — `label` = 그 출금의 적요·가맹점명 글자 그대로(기본값 기억 키)
  - `sheet(conn, today: date, month: str | None = None) -> dict` — `{months, month, accounts, chips: {outflows, amount, found, none, check, open}, rows: [...]}`. 달은 기준일 이전 출금이 있는 달, 없으면 가장 최근 달, 모르는 달 ValueError. 행: `{table, id, at, label, amount, vendor, docs: [{id, kind, issuer, issued_on, amount}], diff, account, account_from: 'confirmed'|'memory'|'vendor'|None, check: bool, confirmed: {by, at}|None}`. `diff` = 출금액 − 증빙 합(증빙 없으면 None)
  - 과목 우선순위: 이 행의 확정 > 같은 `label`의 가장 최근 사람 확정(거래처가 `by_item`이면 건너뜀) > 거래처 기본값 > None. `check` = 과목이 None이거나 거래처가 `by_item`
  - `candidates(conn, table: str, outflow_id: int) -> list[dict]` — 출금일 기준 `DOC_WINDOW` 안 증빙 전부(금액 무관, 월말 이체면 그달 그 거래처 증빙도), 날짜 가까운 순
  - `confirm(conn, table, outflow_id, document_ids: list[int], account: str, by: str, at: str) -> None` — 없는 출금·모르는 과목(`mv["expense_accounts"]`)·후보 밖 증빙 id는 ValueError, 덮어씀, 커밋은 부르는 쪽
  - `open_summary(conn, today) -> {"count": int, "month": str | None}` — 기준일의 지난달 출금 중 (증빙 없음 또는 `check`)이면서 확정 안 된 수
  - `today.sheet` people에 `expense_open`·`expense_month`

- [ ] **Step 1: 실패하는 테스트** — `tests/backend/test_expense.py`에 작은 source DB 픽스처(`tests/backend/test_recon_match.py` `Books`처럼 `fin_*`·`ax_master_values`를 넣는 도우미 `Outflows`)로

```python
def test_unknown_label_needs_check_then_remembered(o):
    o.card("2026-11-03 12:00:00", "KCP*솔밭식당", 18_000)
    o.card("2026-11-20 12:00:00", "KCP*솔밭식당", 21_000)
    first, second = expense.sheet(o.conn, DAY, "2026-11")["rows"]
    assert first["account"] is None and first["check"]
    expense.confirm(o.conn, first["table"], first["id"], [], "복리후생비", by="desk", at=AT)
    second = expense.sheet(o.conn, DAY, "2026-11")["rows"][1]
    assert (second["account"], second["account_from"], second["check"]) == ("복리후생비", "memory", False)

def test_by_item_vendor_never_remembered(o):
    o.card("2026-11-03 12:00:00", "페이온*모아덴탈몰", 30_000)
    o.card("2026-11-20 12:00:00", "페이온*모아덴탈몰", 40_000)
    first = expense.sheet(o.conn, DAY, "2026-11")["rows"][0]
    expense.confirm(o.conn, first["table"], first["id"], [], "복리후생비", by="desk", at=AT)
    second = expense.sheet(o.conn, DAY, "2026-11")["rows"][1]
    assert (second["account"], second["account_from"], second["check"]) == ("소모품비", "vendor", True)

def test_month_end_mismatch_none_then_pick_many(o):  # 명세서 합 ≠ 출금 → 증빙 없음, 후보에서 여러 장 확정
    ...
    assert row["docs"] == [] and len(expense.candidates(o.conn, row["table"], row["id"])) >= 2

def test_confirm_rejects_bad_input(o):  # 없는 출금 id · '간식비' · 후보 밖 증빙 id → ValueError

def test_open_summary_is_last_month_only(o):  # 기준일 2026-12-01: 10월 증빙 없음은 안 셈, 11월 증빙 없음 1·과목 확인 1 → 2, 하나 확정 → 1, month == "2026-11"

def test_sheet_matches_rule(o):  # 행의 docs id = rule_match, 거래처 기본 과목 = rule_account (규칙과 화면이 같다)
```

`tests/backend/test_today.py`: `people`에 `expense_open`·`expense_month`가 있고 `expense.open_summary`와 같다.

- [ ] **Step 2: 실행해 실패 확인** — `uv run pytest tests/backend/test_expense.py tests/backend/test_today.py -q` → AttributeError(`sheet`)
- [ ] **Step 3: 구현** — 출금·증빙은 표마다 한 번 읽는다(행 수와 무관하게 쿼리 수 일정 — `recon_match`와 같은 꼴). 대상은 `fin_bank_transactions` `direction='out'` 전부와 `fin_card_expenses` 전부(지금 데이터엔 급여 출금이 없다). 기본값 기억은 확정 테이블을 `judged_at` 내림차순으로 한 번 읽어 label별 첫 값
- [ ] **Step 4: 통과 확인** — 같은 명령 PASS, `make test` 통과
- [ ] **Step 5: 커밋** — `backend: ⑧-2 지출 증빙 — 규칙 증빙·과목 기본값·사람 확정·오늘 화면 줄`

### Task 4: API

**Files:**
- Modify: `backend/api.py` (⑧ 절 아래)
- Test: `tests/backend/test_api_expense.py`(새, `test_api_recon.py` 꼴)

**Interfaces:**
- `GET /finance/expenses?month=` → `expense.sheet`(모르는 달 400)
- `GET /finance/expenses/{table}/{outflow_id}/candidates` → `{"docs": candidates}`(모르는 표·없는 출금 400)
- `POST /finance/expenses/{table}/{outflow_id}/confirm` body `{document_ids: list[int], account: str, part: str}` → `{"ok": true}`, ValueError는 400. 기록 시각은 실제 시계(`_stamp(now)`)

- [ ] **Step 1: 실패하는 테스트** — 모양(`set(r) == {"months", "month", "accounts", "chips", "rows"}`), 모르는 달 400, 확정 뒤 행 `confirmed`·`account`, 모르는 과목 400, 후보 밖 증빙 400, `/today`에 `expense_open`
- [ ] **Step 2: 실패 확인** — `uv run pytest tests/backend/test_api_expense.py -q` → 404
- [ ] **Step 3: 구현** — 기존 `finance_recon`·`finance_recon_confirm` 꼴
- [ ] **Step 4: 통과 확인** — PASS, `make test`
- [ ] **Step 5: 커밋** — `backend: ⑧-2 지출 증빙 API`

### Task 5: 화면 — 지출 증빙 탭·오늘 화면 줄

**Files:**
- Modify: `frontend/lib/finance-api.ts`(타입·`fetchExpenses`·`fetchCandidates`·`confirmExpense`), `frontend/components/finance/finance-view.tsx`(탭 `일계표 | 지출 증빙`, URL `?tab=expense`), `frontend/lib/today-api.ts`·`frontend/components/today/today-view.tsx`(줄 "지출 증빙 확인 n건" → `/finance?tab=expense&month=…`)
- Create: `frontend/components/finance/expense-table.tsx`
- Test: `frontend/e2e/finance.spec.ts`, `frontend/e2e/today.spec.ts`

**Interfaces:**
- Consumes: Task 4 API 응답 모양

화면 기준은 와이어프레임 `expenseView`에 설계 §2를 더한 것: 달 버튼, 칩 `출금 n · x원`·`증빙 찾음`·`증빙 못 찾음`·`과목 확인 필요`·`확정 안 된`(누르면 거름), 줄 `출금일 · 출금처 · 금액 · 찾은 증빙(여러 장이면 "명세서 n장 합계") · 금액 차이 · 계정 과목(고르기, 기본값 표시와 출처) · 확정`. 줄을 펼치면 증빙 본문과 후보 목록(체크해서 증빙 바꾸기). 와이어프레임의 "추천 계정 AI" 칸은 두지 않는다(4단계). 와이어프레임의 "지출 탭 test 달 숨김"은 옮기지 않는다 — 앱은 분할을 모른다(기능 정의 §9).

- [ ] **Step 1: 실패하는 e2e** — 지출 증빙 탭으로 가면 칩과 줄이 보인다 / `증빙 못 찾음` 칩으로 거른다 / 과목을 골라 확정하면 줄이 확정으로 바뀌고 다시 불러도 남는다 / 오늘 화면 줄을 누르면 지출 증빙 탭 지난달로 간다
- [ ] **Step 2: 실패 확인** — `make e2e` 해당 테스트 FAIL
- [ ] **Step 3: 구현** — 일계표와 같은 데이터 로딩·오류 띠(`ErrorBand`)·파트 선택 꼴
- [ ] **Step 4: 통과 확인** — `make e2e` 통과, `make lint` 통과
- [ ] **Step 5: 커밋** — `frontend: ⑧-2 지출 증빙 탭·오늘 화면 줄`

### Task 6: 문서

**Files:**
- Modify: `docs/치과 AX - 앱 설계.md` §5 ⑧(API 세 개, `ax_expense_confirmations` 칸 — 기억 키 `label`, 품목으로 정함은 기억 안 함), §8 할 일 `ax_master_values` 항목 지움
- Modify: `docs/치과 AX - 평가 설계.md` §5 ⑧-2 기준선 위치를 `backend/expense.py`로
- Modify: `docs/치과 AX - 가상 데이터 생성 설계.md` §3 `ax_master_values` 목록에 새 키
- Modify: `docs/치과 AX - 기능 정의.md` §5 ⑧-2(오늘 화면 줄), §9 "⑧ 2~4단계" → "⑧ 3~4단계", 와이어프레임 목록에서 "지출 탭 test 달 숨김" 지움
- Modify: `docs/superpowers/specs/2026-11-11-finance-recon-design.md` "반영할 문서 변경"에서 끝난 줄

- [ ] **Step 1: 고치기** — 규칙만, 날짜·경위 없이. 값은 코드 이름으로 가리킨다
- [ ] **Step 2: 확인** — `uv run pytest tests/generator/test_doc_sync.py -q` 통과
- [ ] **Step 3: 커밋** — `docs: ⑧-2 지출 증빙 2단계 — API·확정 테이블·병원 값 목록`
