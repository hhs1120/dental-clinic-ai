# ⑧ 재무 대조 1단계 (⑧-1 코드) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 메뉴 "재무 대조" `/finance` — 일계표(날짜당 한 줄, 카드·현금·이체 장부 vs 금융)와 행 단위 짝짓기로 찾은 불일치 건, 건마다 규칙 추정 원인과 사람 확정. 규칙 원인 = ⑧-1 규칙 기준선(이 단계에서 고정), dev 채점. 오늘 화면 줄 하나. 과금 없음.

**Architecture:** 생성기가 `source.ax_master_values`에 ⑧-1 값(원인 묶음·카드사 율과 정산 지연·현금 입금 요일과 적요·공단 적요·구강용품 가격표·은행 공휴일)을 더한다(verify 먼저). `backend/recon_match.py`가 장부·금융 행을 표마다 한 번 읽어 짝짓고 남은 것을 "건"으로 묶는다. `backend/recon.py`가 건의 규칙 원인(규칙 11개, 순서 고정)·일계표 응답·사람 확정(`ax_recon_confirmations`)을 맡는다. `evaluation/recon.py`가 정답과 맞춰 탐지 재현율·규칙 기준선 원인 정확도를 낸다(dev). 화면은 `frontend/components/finance/`.

**Tech Stack:** Python 3 · FastAPI · sqlite3(SQL 그대로) · pytest / Next.js(App Router, TypeScript strict) · Tailwind 토큰 · shadcn `components/ui/` · Playwright

**Spec:** `docs/superpowers/specs/2026-11-11-finance-recon-design.md` — **1단계만**("목표" 1단계, "정한 것" §1, "하면 실패인 것" 중 탐지·자동 마감 밖 항목, "한계", "지금 데이터"). §2~§4(지출 증빙·조사 에이전트·⑧-2 AI)는 이 계획 밖.

## 시작 전 (브랜치)

- main(`ec2cb1c`)에서 딴 `feat/recon-1`에서 한다(⑤ 2단계는 자기 worktree `feat/claims-2`). 첫 커밋은 spec·이 계획 파일(`docs: ⑧ 재무 대조 설계·1단계 계획`)
- ⑤ 2단계가 먼저 머지되면 `master_values_rows` 이름 수·`check_master_values` 개수 단언이 달라진다 — Task 1의 개수를 그때 값으로 맞춘다
- 시작할 때 main에 있는지 확인(없으면 멈추고 보고): `generator/clinic.py` `master_values_rows`(인자 없음, 이름 9개), `backend/claims.py`, `backend/today.py` `sheet`의 `people`(`appeal_soon`·`preclaim_warning`), `frontend/lib/nav.ts` `BUILT`에 `"/claims"`, `tests/backend/test_missed.py` `Source`

## Global Constraints

- backend는 `ground_truth/`·`generator/`·`evaluation/`을 import하지 않는다(`tests/test_no_ground_truth_import.py`). master 값은 `prep.master_values(conn)`(= `source.ax_master_values`)로만. 정답(`truth_recon_mismatches`·`truth_ledger_errors`)은 `evaluation/`과 `tests/`만 읽는다
- `emr_*`·`fin_*`에 쓰지 않는다. 데이터 상태 고정 — `ax_master_values`에 이름 추가만(verify 먼저). `make data`는 LLM 작업 0건으로 끝나는지 먼저 확인(9c 단계만 바뀜)
- 원인 키 9개 = `truth_recon_mismatches.cause` CHECK 그대로: `split_payment`·`cancel_reapprove`·`fee_mismatch`·`deposit_delay`(묶음 `rule`) / `non_clinical_sale`·`entry_missing`·`amount_typo`·`date_shift`·`method_wrong`(묶음 `ambiguous`). 화면 이름: 분할결제·취소 후 재승인·수수료 차이·입금 지연·진료 외 매출·입력 누락·금액 오입력·날짜 밀림·결제수단 오입력
- 규칙 원인은 `unknown`을 내지 않는다. 규칙 11개와 그 순서는 spec §1 그대로, 이 단계 머지 뒤 test 실행 전까지 바꾸지 않는다
- 수수료 = `int(total * fee_rate)`(파이썬 float, 생성기와 같은 계산). 은행 영업일 = 평일·공휴일 아님. 현금 입금일 = 다음 날부터 첫 입금 요일, 은행 영업일이 아니면 다음 영업일(`master.cash_deposit_day`와 같은 결과 — 테스트로 대조)
- 진료일 = 장부(`emr_payments`)에 행이 있는 날(0원 행 포함). 날짜 밀림의 "이웃"은 진료일 기준(은행 영업일 아님)
- 대조 대상: 그날 카드 매출의 예상 정산일과 그날 현금의 입금일이 모두 앱 기준일(`shared/clinic.py`, 2026-12-01) 전인 날만. 아니면 상태 `pending`, 탐지 안 함
- 이체 짝: 같은 금액, 시각 차 30분 이하에서 가장 가까운 것(`TRANSFER_WINDOW_MIN = 30`). 입금자명(적요)은 보지 않는다. 통장 입금 중 카드 정산(적요 = 카드사)·현금 입금(적요 `cash_deposit.memo`)·공단(`nhis_memo`)은 이체 후보가 아니다
- 받은 금액 0원 진료 행(`kind='visit' AND amount_paid = 0 AND amount_due > 0`)은 짝짓기에서 빼고 규칙 8에서만 쓴다
- 집합 쿼리 — 행마다 DB를 부르지 않는다. 쿼리 횟수 테스트(`conn.set_trace_callback`, ⑤·⑨와 같은 꼴)
- LLM 호출 없음. 과금 명령·`make gate`·`next build`를 돌리지 않는다. `--split test`는 돌리지 않는다(사람만, 평가 설계 §2.7)
- 화면에 코드 값(원인 키·테이블 이름·`fin_` id)을 보이지 않는다. 색·모양은 `frontend/app/globals.css` 토큰만, 부품은 `components/ui/`만(`badge`·`button`·`select`·`card`)
- 주석·독스트링 한국어, 근거 문서 절을 가리킨다(모듈 첫 독스트링에 하는 일과 원본 — 설계 2026-11-11 재무 대조, 기능 정의 §5 ⑧, 가상 데이터 생성 설계 §2.7)
- 커밋 전 `make fmt` → `make lint` → `make test`. `git add`는 파일 이름으로. 커밋 `<영역>: <무엇>` + 본문 한국어 글머리 + 끝에 `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`

## Review Focus

1. 하루 합계가 0인데 불일치가 있는 날(취소 후 재승인, 분할결제)을 놓침 → Task 2 `test_cancel_reapprove_detected_with_zero_diff`, Task 4 `test_every_truth_mismatch_detected`
2. 디코이를 불일치로 냄(보호자 이름 이체, 주 단위 현금 합산 입금, 기준일 직전 정산 대기) → Task 2 `test_guardian_name_transfer_not_item`·`test_weekly_cash_deposit_not_item`·`test_pending_day_not_detected`, Task 4 `test_no_false_positives`(정답과 겹치지 않는 건 0, 평가 달 밖 0)
3. 수수료를 다른 방식으로 계산해 1원 오탐 → Task 2 `test_fee_rounding_matches_generator`(율 × 합계가 x.999…가 되는 금액)
4. 사람 확정이 다시 열 때 사라지거나, 모르는 원인·없는 건 키가 저장됨 → Task 3 `test_confirm_overwrite_and_shown`·`test_confirm_rejects_unknown`
5. 일계표 숫자와 오늘 화면 숫자가 다른 함수로 세짐 → Task 6 `test_today_recon_line_matches_screen`

---

### Task 1: master 값과 `ax_master_values` (생성기, verify 먼저)

**Files:**
- Modify: `generator/master.py` (⑧-1 블록 끝, `RECON_MISMATCH_COUNTS` 옆) — `RECON_CAUSE_GROUPS: dict[str, str]`(원인 9개 → `"rule"`/`"ambiguous"`, 위 Global Constraints 순서. 평가 설계 §5 ⑧-1 두 묶음)
- Modify: `generator/clinic.py` (`master_values_rows(holidays: frozenset = frozenset())`, `stage_master_values`가 `run.holidays`를 넘김)
- Modify: `generator/verify/common.py` (`check_master_values`가 `master_values_rows(ctx.holidays)`)
- Test: `tests/generator/test_master_values.py`

**Interfaces:**
- Produces(`source.ax_master_values` 이름 → JSON):
  - `recon_causes` → `RECON_CAUSE_GROUPS`
  - `card_issuers` → `{issuer: {"fee_rate": float, "settle_days": int}}` (`CARD_ISSUERS`에서 `weight` 뺌 — 생성 비중은 병원이 모름)
  - `cash_deposit` → `{"weekdays": list(CASH_DEPOSIT_WEEKDAYS), "memo": CASH_DEPOSIT_MEMO}`
  - `nhis_memo` → `NHIS_MEMO`
  - `oral_care_prices` → `ORAL_CARE_PRICES`
  - `bank_holidays` → 정렬한 ISO 날짜 목록(인자 `holidays`)
  - 넣지 않는 것: `LINK_FAILURE_DAYS`·연동 실패일·`RECON_MISMATCH_COUNTS`·`FEE_MISMATCH_RATE_DIFF`·`DEPOSIT_DELAY_DAYS`(생성 메타 — spec §1)

- [ ] **Step 1: Write the failing tests** in `tests/generator/test_master_values.py`
  - `test_master_values_rows_match_master`에 단언: `got["recon_causes"] == RECON_CAUSE_GROUPS`, `set(RECON_CAUSE_GROUPS) == set(RECON_MISMATCH_COUNTS)`, `got["card_issuers"] == {k: {"fee_rate": v["fee_rate"], "settle_days": v["settle_days"]} for k, v in CARD_ISSUERS.items()}`, `got["cash_deposit"]`·`got["nhis_memo"]`·`got["oral_care_prices"]`
  - `test_bank_holidays_from_argument` — `master_values_rows(frozenset({date(2026, 10, 3)}))`의 `bank_holidays == ["2026-10-03"]`, 인자 없으면 `[]`
  - `test_no_generation_meta` — 이름에 `link_failure`·`mismatch_counts`가 없고 `card_issuers` 값에 `weight` 없음
  - `test_check_master_values_flags_missing_and_wrong`의 개수 9 → 15
- [ ] **Step 2: Run** `uv run pytest tests/generator/test_master_values.py -q` — Expected: FAIL(ImportError `RECON_CAUSE_GROUPS`)
- [ ] **Step 3: Implement** master 상수 + 여섯 이름 + 인자(독스트링에 "⑧-1 원인 묶음·정산·현금 입금·구강용품·은행 공휴일", 각 이름 위 한 줄 근거 §2.7)
- [ ] **Step 4: Run** 테스트 PASS → `make data`(LLM 작업 0건 확인, 나가면 멈추고 보고) → `make verify` 통과 → `sqlite3 data/source.sqlite "select count(*) from ax_master_values"` = 15, `select count(*) from fin_card_sales` 7,493 그대로
- [ ] **Step 5: Commit** — `generator: ax_master_values에 ⑧-1 원인 묶음·카드사 율·현금 입금·구강용품·은행 공휴일`

---

### Task 2: `backend/recon_match.py` — 짝짓기와 불일치 건

**Files:**
- Create: `backend/recon_match.py`
- Test: `tests/backend/test_recon_match.py` (새 파일 — `tests.backend.test_missed.Source`에 수납·금융 행을 더하는 도우미 `pay(...)`·`sale(...)`·`deposit(...)`·`bank(...)`를 이 파일에)

**Interfaces:**
- Consumes: `prep.master_values(conn)`의 Task 1 이름들. 테이블 `source.emr_payments`·`emr_patients`·`fin_card_sales`·`fin_card_deposits`·`fin_bank_transactions`
- Produces:
  - 상수 `TRANSFER_WINDOW_MIN = 30`
  - `bank_day(d: date, holidays: set[date]) -> bool`, `add_bank_days(d, n, holidays) -> date`, `cash_deposit_day(d, weekdays, holidays) -> date` — master와 같은 결과
  - `load(conn: sqlite3.Connection, today: date) -> dict` — 장부(0원 행 분리)·승인·정산·통장 입금·master 값을 **쿼리 고정 횟수**로 읽은 묶음(`"payments"`, `"zero_visits"`, `"sales"`, `"deposits"`, `"bank_in"`, `"mv"`, `"open_days"`, `"today"`)
  - `day_status(books, d: date) -> str` — `"ok"`(대조 대상) / `"pending"`(정산·현금 입금이 기준일 이후)
  - `detect(books) -> list[dict]` — 건, 날짜·키 순:
    ```
    {"key": str,            # 관련 fin 참조·수납 id를 정렬한 JSON의 sha1 앞 12자
     "date": "YYYY-MM-DD",  # 건의 대표 날짜(아래)
     "dates": [str],        # 걸친 날짜(날짜 밀림은 둘)
     "channel": "card" | "settlement" | "cash" | "transfer",
     "payment_ids": [int], "fin": [{"table": str, "id": int}],
     "ledger_total": int, "fin_total": int}
    ```
- **짝짓기·묶기 순서**(spec §1, 이 순서로):
  1. 카드: 취소 행(`cancel_of`)과 그 원승인(같은 날·같은 번호·취소 아님)은 "취소 쌍"으로 짝 후보에서 뺀다. 장부 카드 행에 승인번호가 있으면 같은 날·같은 번호·같은 금액 승인과 짝, 없으면(손 입력) 같은 날·같은 금액·시각이 가장 가까운 승인과 짝
  2. 정산: (카드사, 매출일) 승인 합계(취소 차감)와 정산 `sales_total` 비교, `fee != int(total * fee_rate)`면 건, `deposited_on > add_bank_days(sales_date, settle_days)`면 건. 정산 건은 정산 입금 행 + 같은 날 같은 금액 통장 입금(적요 = 카드사) 하나, 날짜 = 매출일
  3. 현금: 장부 현금 행을 `cash_deposit_day`로 입금일에 모으고 그 입금(적요 `cash_deposit.memo`)과 합계 비교. 다르면 입금 1건 + 그 기간 장부 현금 행 전부가 한 건, 날짜 = 그 기간 마지막 진료일
  4. 이체: 위 Global Constraints대로
  5. 남은 것 묶기: 같은 날 남은 장부·금융 행 중 같은 금액·합(둘의 합 = 하나)·10배로 엮이는 것끼리, 취소 쌍은 원승인 번호가 같은 장부 행이 남은 건에 붙인다. 이웃 진료일의 남은 행과 같은 금액이면 두 날을 잇는 한 건(날짜 = 이른 날). 현금 건은 그 기간의 남은 카드 승인·장부 이체와 같은 금액이면 합친다. 엮이지 않는 남은 행은 각각 한 건
- 묶기 세부(여러 남은 행이 금액 관계로 얽힐 때 나누는 법)는 구현자 판단 — 기준은 Task 4 테스트

- [ ] **Step 1: Write the failing tests** (기준일 `DAY = date(2026, 12, 1)`, 날짜는 11월 첫째 주, 카드사 `가온카드` 율 0.012·지연 2)
  - `test_bank_helpers_match_master` — 2026-08~11 모든 날에 `add_bank_days`·`cash_deposit_day`가 `generator.master`의 같은 함수와 같음(공휴일은 `config/holidays.json`)
  - `test_clean_day_no_items` — 카드(번호 일치)·현금·이체·정산이 다 맞는 날 → `detect == []`, 일계표용 합계는 Task 3
  - `test_split_payment_one_item` — 장부 카드 50,000(번호 X) ↔ 승인 X 20,000 + Y 30,000 같은 시각 → 건 1개, `payment_ids == [장부]`, fin 승인 둘
  - `test_cancel_reapprove_detected_with_zero_diff` — 장부 X 30,000 ↔ 승인 X·취소 X·새 승인 Z 30,000 → 하루 카드 합계 차이 0인데 건 1개, fin 셋
  - `test_fee_mismatch_item` / `test_deposit_delay_item` — 정산 행만 바꾼 날 → `channel == "settlement"`, 날짜 = 매출일
  - `test_fee_rounding_matches_generator` — 합계 × 율이 정수에 아주 가까운 금액(예: 승인 합계 25,000 × 0.0120)에서 `int(total*rate)` 그대로면 건 없음
  - `test_amount_typo_card_and_cash` — 손 입력 카드 장부 3,500 ↔ 승인 35,000 → 건 1개(두 행). 현금 장부 1,800인데 입금이 18,000 많음 → 현금 건 1개
  - `test_date_shift_transfer_neighbor_open_day` — 이체 장부 11/05, 통장 11/03, 11/04는 휴진(장부 행 없음) → 두 날을 잇는 건 1개, `dates == ["2026-11-03", "2026-11-05"]`
  - `test_method_wrong_cash_vs_card` — 장부 현금 18,000, 실제 카드 승인 18,000(같은 날), 그 주 현금 입금이 18,000 적음 → 건 1개(장부 행·승인·현금 입금)
  - `test_entry_missing_zero_row_not_paired` — 0원 진료 행(낼 돈 36,000) + 손 입력일 승인 36,000 → 승인이 0원 행과 짝지어지지 않고 건 1개(`payment_ids == []`)
  - `test_guardian_name_transfer_not_item` — 이체 적요가 보호자 이름, 금액·시각 같음 → 건 없음
  - `test_weekly_cash_deposit_not_item` — 월·화·수 현금이 다음 주 월요일 한 번에 입금, 합계 같음 → 건 없음
  - `test_transfer_window` — 같은 금액, 시각 차 29분 → 짝, 31분 → 건 2개
  - `test_pending_day_not_detected` — 11/28 카드 매출(예상 정산 12/02 ≥ 기준일) → `day_status == "pending"`, 승인이 장부와 안 맞아도 건 없음
  - `test_nhis_and_card_deposit_not_transfer` — 공단·카드사 적요 통장 입금은 이체 후보가 아님
  - `test_key_stable` — 같은 DB에서 두 번 `detect` → 키 같음, 관련 행이 다르면 키 다름
  - `test_query_count_independent_of_rows` — 수납 10건·300건 source에서 `load`의 실행 문장 수 같음
- [ ] **Step 2: Run** `uv run pytest tests/backend/test_recon_match.py -q` — Expected: FAIL(모듈 없음)
- [ ] **Step 3: Implement** — `load`는 표마다 한 쿼리, 짝짓기·묶기는 파이썬(날짜별 사전)
- [ ] **Step 4: Run** `uv run pytest tests/backend/test_recon_match.py tests/test_no_ground_truth_import.py -q` — PASS, `make test`
- [ ] **Step 5: Commit** — `backend: 재무 대조 짝짓기 — 카드·정산·현금·이체, 남은 행을 불일치 건으로`

---

### Task 3: `backend/recon.py` — 규칙 원인, 일계표, 사람 확정

**Files:**
- Modify: `shared/app_schema.sql` (파일 끝)
- Create: `backend/recon.py`
- Test: `tests/backend/test_recon.py` (Task 2 도우미 import)

**Interfaces:**
- Consumes: `recon_match.load`·`detect`·`day_status`(Task 2)
- Produces:
  - 테이블
    ```sql
    -- ⑧-1 불일치 건 사람 확정(기능 정의 §5 ⑧, backend/recon.py) — 건 키 단위, 다시 누르면 덮어씀.
    -- proposed_* = 확정할 때 보이던 제안(1단계는 규칙, 3단계부터 에이전트). 재생성 때 버림(앱 설계 §7.3)
    CREATE TABLE ax_recon_confirmations (
        item_key       TEXT PRIMARY KEY,
        occurred_on    TEXT NOT NULL,
        fin_ids        TEXT NOT NULL,  -- JSON [{"table": …, "id": …}]
        payment_ids    TEXT NOT NULL,  -- JSON
        proposed_by    TEXT NOT NULL CHECK (proposed_by IN ('rule', 'agent')),
        proposed_cause TEXT NOT NULL,
        cause          TEXT NOT NULL CHECK (cause IN ('split_payment', 'cancel_reapprove', 'fee_mismatch',
                           'deposit_delay', 'non_clinical_sale', 'entry_missing', 'amount_typo', 'date_shift',
                           'method_wrong')),
        judged_by      TEXT NOT NULL,
        judged_at      TEXT NOT NULL
    );
    ```
  - `rule_cause(item: dict, books: dict) -> str` — spec §1 규칙 1~11을 그 순서로, 처음 맞는 원인(규칙 5의 현금 9배·0.9배, 규칙 7의 카드 ↔ 현금 기간, 규칙 8 = 같은 날 0원 진료 행의 `amount_due` = 짝 없는 금융 금액, 규칙 9 = `oral_care_prices` 1~2개 합(같은 품목 두 번 포함))
  - `ledger(conn, today: date, month: str | None = None) -> dict` — 화면 응답:
    ```
    {"months": [str],  # 장부 행이 있는 달, 오래된 순
     "month": str,     # 인자 없으면 가장 최근 달
     "causes": {cause: group},
     "chips": {"days": int, "mismatch_days": int, "open_items": int},  # 그 달 기준
     "days": [{"date", "status": "ok" | "pending",
               "card": {"ledger", "fin", "diff"}, "transfer": {"ledger", "fin", "diff"},
               "cash": {"ledger", "fin": int | None, "diff": int | None, "deposit": {"on", "days": int} | None},
               "items": [item + {"rule_cause", "ledger_rows": [...], "fin_rows": [...],
                                  "confirmation": {"cause", "proposed_cause", "by", "at"} | None}]}]}
    ```
    `card.fin` = 그날 승인 합계(취소 차감), `transfer.fin` = 그날 이체 후보 입금 합계, `cash.fin`·`diff`·`deposit`은 현금 입금이 덮는 기간의 마지막 진료일 줄에만(다른 날 `None`). `ledger_rows` = `{"id", "paid_at", "name", "amount_due", "amount_paid", "method", "approval_no"}`, `fin_rows` = `{"table", "at", "amount", "label"}`(`label` = 카드사·적요·`취소` 표시). 건은 대표 날짜 줄에 둔다
  - `confirm(conn, key: str, cause: str, by: str, at: str, today: date) -> None` — 그 키의 건이 지금 탐지에 있어야 하고 `cause`가 9개 중 하나, 아니면 ValueError. `proposed_by = "rule"`, `proposed_cause = rule_cause(...)`. 덮어씀. 커밋은 부르는 쪽
  - `open_count(conn, today: date) -> int` — 전 기간 확정 안 된 건 수(오늘 화면)
- [ ] **Step 1: Write the failing tests**
  - `test_rule_cause_each` — parametrize: Task 2 시나리오마다 기대 원인(분할결제·취소 후 재승인·수수료 차이·입금 지연·금액 오입력(카드·현금)·날짜 밀림·결제수단 오입력·입력 누락(0원 행 있음)·진료 외 매출(8,500 + 4,500 = 13,000, 0원 행 없음))
  - `test_rule_order_entry_missing_beats_oral_care` — 짝 없는 승인 13,000이 구강용품 합과 같고 같은 날 0원 진료 행 낼 돈도 13,000 → `entry_missing`(규칙 8이 9보다 먼저)
  - `test_rule_fallbacks` — 금융에만 있는 돈(어떤 규칙에도 안 걸림) → `entry_missing`, 장부에만 있는 돈 → `amount_typo`
  - `test_ledger_day_columns` — 깨끗한 날 `card.diff == 0`, 현금은 입금 기간 마지막 진료일 줄에만 `fin`·`deposit.days`
  - `test_ledger_zero_diff_day_has_badge_items` — 취소 후 재승인 날: `card.diff == 0`인데 `items` 1개, `chips.mismatch_days == 1`
  - `test_ledger_pending_days` — 기준일 직전 날 `status == "pending"`, `items == []`
  - `test_ledger_month_default_and_list` — 인자 없으면 가장 최근 달, `months` 오래된 순
  - `test_confirm_overwrite_and_shown` — 확정 → `confirmation == {"cause", "proposed_cause", "by": "데스크", "at"}`, 다른 원인으로 다시 → 덮어씀(1행), `open_count` 1 줄어듦
  - `test_confirm_rejects_unknown` — 모르는 원인·`unknown`·없는 키 → ValueError
  - `test_query_count_independent_of_rows` — `ledger`·`open_count` 실행 문장 수가 수납 10건·300건에서 같음
- [ ] **Step 2: Run** `uv run pytest tests/backend/test_recon.py -q` — FAIL
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run** `uv run pytest tests/backend/test_recon.py tests/backend/test_recon_match.py -q` — PASS, `make test`
- [ ] **Step 5: Commit** — `backend: 재무 대조 규칙 원인(기준선)·일계표·사람 확정`

---

### Task 4: 생성 데이터로 탐지·규칙 확인

**Files:**
- Create: `tests/generator/test_recon_generated.py` (`tests/generator/conftest.py` `generated` 픽스처 — 실제 설정 생성 1회, 정답 DB 포함. backend를 부르는 테스트지만 생성 결과가 여기 있어 이 폴더에 둔다)
- Modify(테스트가 실패하면): `backend/recon_match.py`·`backend/recon.py`

**Interfaces:**
- Consumes: `recon_match.load`·`detect`, `recon.rule_cause`, `truth.truth_recon_mismatches`
- Produces: 없음(이 테스트가 탐지 재현율 = 테스트 — 평가 설계 §5 ⑧-1)

- [ ] **Step 1: Write the tests** — 정답 건마다 겹침 = 관련 `fin_ids`·`payment_ids` 교집합이 있음
  - `test_every_truth_mismatch_detected` — 정답 65건 모두 겹치는 탐지 건이 있다(실패하면 놓친 건의 원인·날짜를 메시지에)
  - `test_no_false_positives` — 정답과 겹치지 않는 탐지 건 0, 평가 달(`master.recon_months`) 밖 날짜의 건 0(디코이 — 보호자 이름 이체·주 단위 현금·기준일 직전 정산 포함)
  - `test_rule_group_exact` — 정답 원인이 `rule` 묶음인 건은 대응 탐지 건의 `rule_cause`가 정답과 같다(대응 = spec "정답 건 ↔ 탐지 건 대응")
- [ ] **Step 2: Run** `uv run pytest tests/generator/test_recon_generated.py -q` — 실패하면 Task 2·3 코드를 고친다(규칙 순서는 spec 그대로, 묶기·짝짓기 세부만). 알려진 어려운 날: 08-28(연동 실패일, 입력 누락 카드 18,000 17:00 + 결제수단 오입력 같은 금액·시각 — 한 건으로 묶여 둘 다 그 원인으로 채점되는 것은 허용), 08-12 → 08-14 → 08-18(날짜 밀림 사슬 — 이체 30분 창 덕에 08-14 두 행이 짝지어지지 않아야 함)
- [ ] **Step 3: Run** `make test` — PASS
- [ ] **Step 4: Commit** — `backend: 재무 대조 탐지를 생성 데이터 정답으로 확인 — 재현율 100%, 오탐 0`

---

### Task 5: 규칙 기준선 dev 채점

**Files:**
- Create: `evaluation/recon.py`
- Modify: `evaluation/eval.py` (dev 결과에 `result["recon"]`, 리포트 절 `_render_recon`, 모듈 독스트링)
- Test: `tests/evaluation/test_recon_eval.py` (Task 2 도우미로 만든 source + 손으로 쓴 truth 행)

**Interfaces:**
- Consumes: `recon_match.load`·`detect`, `recon.rule_cause`, `truth.truth_recon_mismatches`(`split`)
- Produces: `evaluation.recon.score(conn, split: str, today: date) -> dict`:
  ```
  {"n": int, "detected": {"k", "n"},
   "rule": {"k", "n"},  # 규칙 기준선 원인 정확도 top-1 — 미탐지 정답은 오답
   "by_group": {"rule": {"k", "n"}, "ambiguous": {"k", "n"}},
   "by_cause": {cause: {"k", "n"}} | None,   # dev만
   "wrong": [{"date", "cause", "rule_cause"}] | None}  # dev만, 건별
  ```
- 대응 규칙(spec): 정답 건마다 겹침이 가장 큰 탐지 건(같으면 키 순 첫 건), 한 탐지 건이 정답 둘과 겹치면 둘 다 그 원인
- test 묶음(`--section recon`)은 만들지 않는다 — 3단계에서 에이전트와 짝 비교로 한 번에(이 계획에서 정함)

- [ ] **Step 1: Write the failing tests** — `test_score_counts`(정답 3건·탐지 2건·하나 미탐지 → `detected == {"k": 2, "n": 3}`, 미탐지는 `rule`에서 오답), `test_one_item_two_truths`(한 탐지 건 ↔ 정답 2건 → 둘 다 같은 원인으로 채점), `test_groups_from_master`(묶음은 `RECON_CAUSE_GROUPS`), `test_test_split_hides_lists`(`split="test"`면 `by_cause`·`wrong`이 None), `test_eval_dev_includes_recon`(`eval.run` dev 결과·리포트에 "⑧-1 규칙 기준선" 절)
- [ ] **Step 2: Run** `uv run pytest tests/evaluation/test_recon_eval.py -q` — FAIL
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run** PASS, 그리고 실데이터는 `evaluation.recon.score`만 직접 부른다(`eval.py --split dev` 전체는 ③ 검색 절이 질의 임베딩을 불러 과금될 수 있어 돌리지 않는다) — `uv run python -c` 로 app·source·truth를 붙인 연결에서 `score(conn, "dev", date(2026, 12, 1))`를 출력해 마무리 보고에 옮긴다
- [ ] **Step 5: Commit** — `eval: ⑧-1 규칙 기준선 dev 채점 — 탐지·원인 정확도·두 묶음`

---

### Task 6: API와 오늘 화면 줄 (backend)

**Files:**
- Modify: `backend/api.py` ("⑤ 청구 점검" 절 뒤에 "⑧ 재무 대조" 절), `backend/today.py` (독스트링 "같은 함수" 목록, `people`)
- Test: `tests/backend/test_api_recon.py` (새 파일, `tests.backend.test_api_calls._client`), `tests/backend/test_today.py`

**Interfaces:**
- Consumes: `recon.ledger`·`recon.confirm`·`recon.open_count`(Task 3)
- Produces:
  - `GET /finance/recon?month=YYYY-MM` → `recon.ledger(conn, clinic.today, month)`, 모르는 달 400
  - `POST /finance/recon/{key}/confirm` 본문 `{"cause": str, "part": str}` → `{"ok": True}`, ValueError 400, `at = _stamp(now)`
  - `today.sheet(...)["people"]["recon_open"] = recon.open_count(conn, today)` — `일계표 불일치 n건 → /finance`
- [ ] **Step 1: Write the failing tests** — `test_get_recon_shape`(키 집합), `test_get_recon_bad_month_400`, `test_post_confirm_then_shown`, `test_post_confirm_bad_400`(모르는 원인·없는 키), `test_today_recon_line_matches_screen`(`people.recon_open == recon.open_count(...)`, 확정하면 줄어듦), `test_today_recon_zero`
- [ ] **Step 2: Run** `uv run pytest tests/backend/test_api_recon.py tests/backend/test_today.py -q` — FAIL
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run** PASS, `make test`
- [ ] **Step 5: Commit** — `backend: GET /finance/recon·확정 API, 오늘 화면 일계표 불일치 줄`

---

### Task 7: 재무 대조 화면 — 일계표 (frontend)

**Files:**
- Create: `frontend/lib/finance-api.ts` (응답 타입 = Task 3 모양, `CAUSE_LABEL`(Global Constraints 화면 이름), `fetchRecon(month?)`, `postConfirm(key, cause, part)`)
- Create: `frontend/app/(portal)/finance/page.tsx` (청구 점검 페이지와 같은 머리말·`Suspense` 꼴, 머리말: "장부 수납과 카드·통장 입금을 맞춰 봅니다. 장부 수정은 전자차트에서 합니다.")
- Create: `frontend/components/finance/finance-view.tsx`(달·펼친 날 URL 상태), `frontend/components/finance/ledger-table.tsx`(일계표), `frontend/components/finance/recon-item.tsx`(펼친 건 하나)
- Modify: `frontend/lib/nav.ts` (`BUILT`에 `"/finance"`), `frontend/lib/today-api.ts`·`frontend/components/today/today-view.tsx` (`people.recon_open` 줄)
- Test: `frontend/e2e/finance.spec.ts` (새 파일, `page.route` 가짜 응답), `frontend/e2e/today.spec.ts`

**Interfaces:**
- Consumes: `GET /finance/recon`·`POST /finance/recon/{key}/confirm`(Task 6), `readPart`(`frontend/lib/part.ts`)
- Produces: 주소 상태 `/finance?month=YYYY-MM&day=YYYY-MM-DD`

**화면 규칙(spec §1):**
- 탭 바는 두지 않는다(지출 증빙 탭은 2단계 — 그때 `일계표 | 지출 증빙`)
- 달 선택(버튼 묶음) + 칩 `영업일 n` · `불일치 있는 날 n` · `확정 안 된 건 n`
- 표 칸: `날짜` · `카드 장부 / 승인 / 차이` · `현금 장부 / 입금 / 차이` · `이체 장부 / 입금 / 차이` · `상태`. 현금 입금 칸은 입금 기간 마지막 진료일에만 `n일 치 합산 ₩x`, 다른 날 `—`
- 상태: `차이 없음` / `불일치 n건`(`danger` 배지 — 합계 차이가 0이어도) / 모두 확정이면 `확인함`(`ok`) / `정산 대기`(`muted`, 펼침 없음)
- 불일치 날을 누르면 그 아래 건 목록, 건마다: 관련 행 표(장부: 시각·환자·낼 돈·받은 돈·결제수단·승인번호 / 금융: 종류·시각·금액·카드사나 적요, 취소는 `취소` 표시), `규칙 추정: {원인}`, `맞음` 버튼과 `아님 → 원인 고르기`(select, 9개), 확정되면 `{원인} · 데스크 12/01`, 장부 쪽 원인(입력 누락·금액 오입력·날짜 밀림·결제수단 오입력)으로 확정된 건에 "장부 수정은 전자차트에서" 한 줄
- 오늘 화면 줄: `일계표 불일치 n건`(→ `/finance`), 0이면 숨김
- [ ] **Step 1: Write the failing e2e** in `frontend/e2e/finance.spec.ts` (가짜 응답: 달 둘, 깨끗한 날 1·취소 후 재승인 날(차이 0) 1·현금 입금 기간 끝 날 1·정산 대기 날 1, 건 하나는 확정됨)
  - `메뉴에서 재무 대조가 열림` — 메뉴 → `/finance`, "준비 중" 없음, 탭 바 없음
  - `차이 0인 날도 불일치 배지` — 취소 후 재승인 날 카드 차이 `0`·`불일치 1건`
  - `현금 입금은 기간 끝 날에만` — `5일 치 합산`이 한 줄에만
  - `정산 대기 날은 펼치지 않음`
  - `펼치면 관련 행과 규칙 추정` — `규칙 추정: 취소 후 재승인`, 코드 값(`cancel_reapprove`·`fin_card_sales`) 안 보임
  - `맞음을 누르면 확정` — POST 본문 `{"cause": "cancel_reapprove", "part": …}`, 두 번째 GET 응답으로 `취소 후 재승인 · 데스크`
  - `아님에서 원인을 고르면 그 원인으로 확정`, 장부 쪽 원인이면 `장부 수정은 전자차트에서`
  - `달을 바꾸면 URL과 표가 바뀜` — `?month=`
  - `today.spec.ts`: `일계표 불일치 줄 → /finance`, 0이면 숨김
- [ ] **Step 2: Run** `cd frontend && npx playwright test e2e/finance.spec.ts e2e/today.spec.ts`(`make dev`를 끄고) — FAIL
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run** `make lint`, `make e2e` — PASS
- [ ] **Step 5: Commit** — `frontend: 재무 대조 일계표 — 날짜별 대조·불일치 건·규칙 추정·확정`

---

### Task 8: 문서

**Files (spec "반영할 문서 변경"의 1단계 몫):**
- `docs/치과 AX - 기능 정의.md` — §5 ⑧: 단계 나눔(1 ⑧-1 코드 → 2 ⑧-2 코드 → 3 ⑧-1 에이전트 → 4 ⑧-2 AI), 탭 `일계표 | 지출 증빙`, 조사 과정은 누르면 한 줄씩(+ 일괄), 불일치는 건 단위. §9: "⑧ 착수 시 — 거래처 첫 기본값"·"슬롯 ⑧ 착수 시 — 규칙 기준선 정의"를 ⑧ 2~4단계 항목으로 바꾸고, "다음 데이터 재설계 때 — 미수 정산 수납 누락(행 없음)" 추가
- `docs/치과 AX - 평가 설계.md` §5 ⑧-1: 규칙 기준선 = `backend/recon.py` `rule_cause`(코드 이름으로 가리킴, 1단계 머지 때 고정), 정답 ↔ 탐지 대응 규칙, 탐지 재현율 = `tests/generator/test_recon_generated.py`
- `docs/치과 AX - 앱 설계.md` — §3b 자동 처리 키 표 ⑧-1 "날짜·결제수단·차이 금액" → 건 키. §5 ⑧: `backend/recon_match.py`·`backend/recon.py`, 건 키, `ax_recon_confirmations` 칸(제안 출처·제안 원인), API `GET /finance/recon`·`POST /finance/recon/{key}/confirm`. §4 오늘 화면: 일계표 불일치 = `recon.open_count`. §8 할 일: ⑧-1 `ax_master_values` 처리(⑧-2 거래처 표는 남김). §9 화면 표 `/finance` 행
- `docs/치과 AX - 가상 데이터 생성 설계.md` §3: `ax_master_values` 쓰는 곳에 ⑧-1 여섯 이름
- `docs/wireframe/index-v2.html`은 고치지 않는다(기능 정의 §9 "화면마다 옮길 때 와이어프레임에서 고쳐 옮긴다" — 이번 화면이 기준)
- [ ] **Step 1:** 고친다(규칙만 — 결정 날짜·경위 없이, 값은 코드 이름으로)
- [ ] **Step 2: Run** `uv run pytest tests/generator/test_doc_sync.py -q` — PASS
- [ ] **Step 3: Commit** — `docs: 재무 대조 1단계 — 단계 나눔·건 단위·규칙 기준선·확정 테이블`

---

### 마무리

- [ ] `make fmt` → `make lint` → `make test` → `make e2e` 통과
- [ ] 실데이터(`make dev`)로 `/finance`·`/today`를 열고 `GET /finance/recon` 응답 시간(밀리초)을 재 보고. 확인값: 2026-08~11에만 불일치 건, 전체 건 수 ≤ 65(묶여 줄 수 있음), 평가 달 밖 0, 오늘 화면 줄 = 확정 안 된 건 수
- [ ] Task 5 리포트의 ⑧-1 규칙 기준선 숫자(탐지·전체·두 묶음·원인별)를 보고 — 이 숫자가 3단계 에이전트가 넘어야 할 기준
- [ ] PR 전 `pj2-doc-tidy`(바뀐 코드·문서 목록과 이 계획의 "정한 것"을 넘김)

## 이 계획에서 정한 것 (spec이 빌더에게 맡겼거나 비어 있던 곳)

- `ax_master_values` 이름 여섯과 `master_values_rows(holidays)` 인자, `master.RECON_CAUSE_GROUPS`. 원인 화면 이름은 ⑤처럼 프론트(`finance-api.ts`)에 둔다(사용자 결정)
- 진료일 = 장부에 행이 있는 날(휴진 요일·공휴일 표를 따로 싣지 않는다)
- 모듈 둘(`recon_match` 짝짓기·묶기 / `recon` 규칙·화면·확정), API 모양, 확정 테이블의 제안 출처·제안 원인 칸(맞음/아님은 `cause = proposed_cause`로 안다)
- 탐지 재현율·오탐 0 테스트는 `tests/generator/`(생성 픽스처가 거기 있음)
- ⑧-1 test 묶음은 3단계에서 에이전트와 함께 한 번 — 1단계는 dev만
- 1단계 화면은 탭 바 없이 일계표만
