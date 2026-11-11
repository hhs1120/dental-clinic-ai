# 데이터 재설계 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** ⑧-1(8b)·⑧-2(8c)·⑤-2 차트 서술(5b·9b)에 "규칙은 속지만 글·행을 읽으면 풀리는" 사례를 섞어 데이터를 한 번 다시 만들고, ③ 평가셋을 보강해, dev 규칙 기준선을 목표 범위로 낮춘다.

**Architecture:** 기준선(⑤-2 키워드 규칙·⑧-2 매칭 규칙)을 `evaluation/`에 먼저 고정한다. 생성기는 새 상수·새 이름의 자식 난수로만 더해 기존 정답(장부 실수 33건·청구·추가 처치 누락 11건)을 지킨다. 장부(`emr_payments`)는 바꾸지 않는다. 코드 단계는 가짜 게이트웨이로 먼저 재고, LLM 단계(9b)는 표본 → 전체 순으로 채운다.

**Tech Stack:** Python 3 · sqlite3 · numpy Generator · pytest · `make data/verify/index` · LLM 게이트웨이(Batch)

**Spec:** `docs/superpowers/specs/2026-11-11-data-redesign-design.md`

## Global Constraints

- 목표(dev 규칙 기준선): ⑧-1 애매한 원인 묶음 55~65%(규칙으로 되는 원인 묶음은 100% 근처 유지), ⑧-2 매칭 75~85%·계정 과목 80~85%, ⑤-2 키워드 규칙 정밀도·재현율 각 60~75%. **test로 재지 않는다**(평가 설계 §2.7) — 측정 함수에 `split="dev"`만 넘긴다
- 헷갈리는 사례는 글·행을 읽으면 풀려야 한다 — dev 표본 20~30건을 Claude 서브에이전트(또는 사람)가 읽고 거의 다 맞혀야 통과
- 장부(`emr_payments`)·통화·문자·청구는 바꾸지 않는다. 기존 `truth_ledger_errors` 33건·`truth_claim_errors`·추가 처치 누락 11건은 그대로여야 한다
- 새 생성은 새 상수 + 새 이름의 자식 난수(`rng.spawn` — 기존 자식 뒤에 더해야 앞 자식이 그대로). 8b·8c는 통째로 바뀌어도 된다(뒤 LLM 단계 입력 아님)
- 헷갈리는 모양마다 그 기준선을 실제로 속이는지 단위 테스트로 확인한다(⑧-1은 `backend/recon.py` `rule_cause`, ⑧-2·⑤-2는 이 계획 Task 1·2의 규칙)
- 화면 배치: `/finance` 기본 달(장부 행이 있는 가장 최근 달, 정산 대기 약 1주 앞쪽)에 ⑧-1 원인·모양과 ⑧-2 모양마다 최소 1건, 청구 전 점검 대상 달(대기 청구)에 요건 사유별 헷갈리는 근거 표현이 각각 — verify 검사
- verify 검사를 생성 코드보다 먼저(CLAUDE.md TDD). 생성 값은 `generator/master.py` 상수로 — 문서에는 이름으로
- **과금 명령은 실행 전에 사람에게 건수·비용(`make data`가 알려 줌)을 보이고 승인받는다**: `make llm-fill-api`, `make index`, ⑤-2 점검 재실행. 가짜 게이트웨이(`make data FAKE_LLM=1`)는 자유
- 커밋 전 `make fmt` → `make lint` → `make test`. `git add`는 파일 이름으로. 커밋 끝에 두 줄: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` / `Claude-Session: https://claude.ai/code/session_01G62cGvo27FUVuEN22FEXou`
- 실데이터 `make data` 전에는 지금 `data/`·`ground_truth/`를 `.superpowers/sdd/data-redesign/before/`에 복사해 둔다(전후 비교용, 커밋 안 함)

## Review Focus

1. 새 자식 난수를 기존 자식 앞에 끼워 기존 정답이 조용히 다시 뽑힘 → Task 3·6 `test_existing_truth_unchanged`(생성 전후 `truth_ledger_errors`·`truth_claim_errors`·`omitted_extra` 행 해시 비교, `tests/generator/conftest.py` 생성 픽스처 2벌)
2. `payment_id`가 빈 정답 행이 조인에서 조용히 빠짐(통화·문자의 장부 실수 미수, `finance._true_payments`, verify) → Task 3 `test_unrecorded_settlement_rows_survive_joins`
3. 디코이 증빙·표현 풀 문구가 정답 칸·생성 메타를 앱 쪽 테이블(`fin_expense_documents` 본문 등)에 흘림(예: "디코이" 같은 낱말) → Task 5·6 verify `check_no_generation_meta_text`
4. ⑤-2 부정 표현("파절 소견 없음")이 LLM 검증에서 금지어로 막혀 서술이 영영 실패 → Task 6 `test_negated_evidence_passes_checks`
5. ③ 새 test 문항의 정답 문서가 dev 문항과 겹침 → Task 10 verify `check_qa_split_isolation` 통과 + 새 문서는 test 전용

---

### Task 1: 데이터 상태 "재설계" + ⑤-2 키워드 기준선

**Files:**
- Modify: `docs/치과 AX - 가상 데이터 생성 설계.md` §0 데이터 상태 줄 → "재설계(데이터 재설계 묶음 — 설계 docs/superpowers/specs/2026-11-11-data-redesign-design.md)"
- Modify: `evaluation/claim_check.py` (`keyword_rule`, `_requirement`에 `keyword` 열)
- Modify: `evaluation/eval.py` `_render_claim_check`(키워드 열)
- Test: `tests/evaluation/test_claim_check_eval.py`(있으면 그 파일, 없으면 새로)

**Interfaces:**
- Produces: `keyword_rule(note: str, reasons: list[str], perio_min_mm: int) -> bool` — 근거 없음(오류 예측)이면 True. 규칙(점검 때와 같음, 부정은 가리지 않음): 요건 사유별 `generator.chart.EVIDENCE_WORDS` 낱말이 하나도 없으면 True, R03은 본문의 "치주낭 Nmm"이 `perio_min_mm` 미만이면 True. 낱말 표는 `evaluation/`이 `generator.chart`를 import해 읽는다(backend는 안 됨, evaluation은 됨)
- Produces: `score(...)["requirement"]["keyword"]` = `{"precision": k/n, "recall": k/n}`(기존 `_pr` 모양)

- [ ] **Step 1: Write the failing tests** — `test_keyword_rule_words`(R01 "파절" 있으면 False, 없으면 True; "파절 소견 없음"도 False — 부정을 가리지 않음), `test_keyword_rule_r03_depth`(치주낭 3mm·`perio_min_mm=4` → True, 5mm → False), `test_score_has_keyword_column`
- [ ] **Step 2: Run** `uv run pytest tests/evaluation -q -k keyword` — FAIL
- [ ] **Step 3: Implement**. R03 기준값은 Task 6이 만들 `master.PERIO_POCKET_MIN_MM`을 쓴다 — 이 Task에서 상수를 먼저 만들고(값 4, 지금 생성 규칙 "근거 없음 2~3mm"와 같은 경계) `chart.py`는 Task 6에서 읽게 한다
- [ ] **Step 4: Run** 테스트 PASS → 실데이터 dev 지금 값 기록: `keyword` 정밀도·재현율이 점검 때(138/140·138/138) 근처인지(진행표에 적기)
- [ ] **Step 5: Commit** — `eval: ⑤-2 키워드 기준선 — 근거 낱말·치주낭 깊이 규칙을 데이터보다 먼저 고정`(데이터 상태 문서 줄 포함)

### Task 2: ⑧-2 매칭·과목 규칙 기준선

**Files:**
- Create: `evaluation/expense.py`
- Test: `tests/evaluation/test_expense_eval.py`

**Interfaces:**
- Produces: `rule_match(outflow: dict, docs: list[dict], vendors: dict) -> list[int]` — 재무 대조 설계 §2 규칙: 1:1은 금액이 같고 날짜 창(카드 = 같은 날, 자동이체 = 출금 전 `master.EXPENSE_DOC_LEAD_DAYS`) 안의 증빙 하나(여럿이면 날짜 가까운 것), 월말 이체는 적요 = 거래처이고 그달 명세서 합계 = 출금액이면 명세서 전부 + 그달 세금계산서, 없으면 []
- Produces: `rule_account(outflow: dict, vendors: dict) -> str | None` — 거래처(적요·가맹점명 `card_name`) 기본 과목
- Produces: `score(conn, split: str) -> {"n", "match": {k,n}, "account": {k,n}}` — `truth_expense_matches` 해당 split

- [ ] **Step 1: Write the failing tests** — 손으로 만든 fin 행으로 `test_one_to_one`, `test_month_end_sum_with_tax_invoice`, `test_no_doc_returns_empty`, `test_account_from_card_name`, `test_score_dev_only_rows`
- [ ] **Step 2: Run** — FAIL
- [ ] **Step 3: Implement** (⑧ 2단계가 같은 규칙을 앱으로 옮길 수 있게 순수 함수로)
- [ ] **Step 4: Run** PASS → 실데이터 dev 지금 값 기록(점검 때 매칭 56/59·과목 57/59 근처)
- [ ] **Step 5: Commit** — `eval: ⑧-2 매칭·과목 규칙 기준선을 데이터보다 먼저`

### Task 3: ⑧-1 미수 정산 수납 누락 (8b, 장부 그대로)

**Files:**
- Modify: `ground_truth/schema.sql` (`truth_ledger_errors.payment_id` NULL 허용 + `patient_id INTEGER`)
- Modify: `generator/master.py` (`UNRECORDED_SETTLEMENT_COUNT` — dev·test에 고르게, 카드·구강용품 합 금액 몫 `UNRECORDED_ORAL_SHARE`)
- Modify: `generator/finance.py` `stage_income`(새 자식 난수로 금융에만 돈), `generator/records.py`·`generator/calls.py`·`generator/messages.py`의 장부 실수 조인(빈 `payment_id` 행을 빠뜨리지도, 미수 계산에 섞지도 않게 — 장부는 그대로라 미수 계산엔 영향 없어야)
- Modify: `generator/verify/records.py` `check_ledger_errors`, `generator/verify/fin.py` `check_recon_mismatches`
- Test: `tests/generator/test_verify_fin.py`, `tests/generator/test_finance_redesign.py`(새), `tests/backend/test_recon.py`(규칙 속이기)

**Interfaces:**
- Produces: 정답 행 `truth_ledger_errors(kind='entry_missing', payment_id=NULL, patient_id, true_amount, true_paid_at, true_method)` + `truth_recon_mismatches(cause='entry_missing', payment_ids='[]', ledger_error_id=그 행)`
- 모양: 장부에 미수(잔액 > 0)가 남은 환자가 평가 달 진료일에 일부·전부를 냈는데 장부에 안 적음. 금액 ≤ 그 시점 잔액. `UNRECORDED_ORAL_SHARE`만큼은 카드이고 금액이 `ORAL_CARE_PRICES` 1~2개 합(규칙 9가 진료 외 매출로 틀림), 나머지는 현금·이체·카드 임의 금액

- [ ] **Step 1: Write the failing tests**
  - verify: `test_unrecorded_settlement_amount_within_balance`, `test_unrecorded_settlement_has_patient`(깨뜨린 DB에서 위반이 잡힘)
  - `test_unrecorded_settlement_fools_rule`(Books 도우미로 카드·13,000·0원 행 없음 → `rule_cause` = `non_clinical_sale`, 정답 `entry_missing`)
  - `test_unrecorded_settlement_rows_survive_joins`(빈 `payment_id` 행이 있는 DB에서 통화·문자 생성의 장부 실수 조회가 예외 없이 그 행을 건너뜀, `_true_payments`가 금융 쪽 돈에 넣음)
  - `test_existing_truth_unchanged`(생성 픽스처: 이 Task 전후 `truth_ledger_errors` 중 `payment_id` 있는 33행·`emr_payments`·`emr_claims` 해시 같음 — 전 값은 `main`에서 뽑아 테스트 상수로)
- [ ] **Step 2: Run** — FAIL
- [ ] **Step 3: Implement** — 8b의 기존 주 난수 흐름 뒤에 `rng.spawn`으로 새 자식 하나
- [ ] **Step 4: Run** PASS → `make data FAKE_LLM=1` → `make verify`. 실데이터 `make data`로 LLM 작업이 0건인지도 확인(8b만 바뀌었으니 0이어야 — 나가면 번짐이 있다는 뜻이라 멈추고 보고)
- [ ] **Step 5: Commit** — `generator: ⑧-1 미수 정산 수납 누락 — 장부는 그대로, 금융에만 돈`

### Task 4: ⑧-1 우연 겹침 모양 + 건수·화면 배치

**Files:**
- Modify: `generator/master.py`(새 모양 상수, 최근 달 하한 `RECON_LATEST_MONTH_MIN`), `generator/finance.py`
- Modify: `generator/verify/fin.py`(최근 달 하한·모양별 건수)
- Test: `tests/backend/test_recon.py`(모양마다 규칙 속이기), `tests/generator/test_verify_fin.py`

**Interfaces:**
- 모양은 빌더 판단(spec §1) — 조건: (a) 각 모양이 `rule_cause`를 속이는 단위 테스트가 있다 (b) 장부를 바꾸지 않는다(기존 정상 행 중에서 금액·날짜가 우연히 겹치는 것을 고르거나 금융 쪽에만 행을 더함) (c) 원인 enum 9개 안
- 예(시작점): 진료 외 매출 금액을 이웃 진료일 장부 행과 같게(규칙 6 날짜 밀림으로 틀림), 입금 지연 정산과 같은 날 같은 금액 이체를 겹치기 — 실제로 속는지 테스트로 확인하고 안 속으면 버린다
- 건수: dev·test 각 50건 안팎(애매한 원인 위주), 새 상수로 기존 `RECON_MISMATCH_COUNTS`에 더함

- [ ] **Step 1: Write the failing tests** — 모양마다 `test_<shape>_fools_rule`, verify `test_latest_month_has_each_cause`(최근 달·정산 대기 앞쪽에 원인마다 1건 이상)
- [ ] **Step 2: Run** — FAIL
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run** PASS → `make data FAKE_LLM=1` → `uv run python -c` 로 `evaluation.recon.score(conn, "dev", today)`: 애매한 묶음 55~65%·규칙 묶음 100% 근처인지. 벗어나면 상수만 고쳐 반복(진행표에 매번 숫자)
- [ ] **Step 5: Commit** — `generator: ⑧-1 우연 겹침 모양·건수·최근 달 배치`

### Task 5: ⑧-2 지출 디코이·표기 변형·짝 모양·과목 (8c)

**Files:**
- Modify: `generator/master.py`(`VENDORS` 표기 변형 칸, 범용 가맹점 거래처, `EXPENSE_ITEMS` 혼합, 디코이·부분 결제 비율 상수), `generator/finance.py` `stage_expenses`
- Modify: `generator/verify/fin.py` `check_expense_matches`(디코이는 어느 정답에도 없음, 새 짝 모양 합계 일치, 최근 달 하한), 새 `check_no_generation_meta_text`
- Modify: `docs/치과 AX - 가상 데이터 생성 설계.md` §2.7(재료상 명세서 소모품 품목도 월말 이체 정답은 거래처 과목 — 규칙 명시, 한 주문 두 과목이면 금액 큰 과목)
- Test: `tests/generator/test_verify_fin.py`, `tests/evaluation/test_expense_eval.py`(모양마다 Task 2 규칙을 속이는지)

**Interfaces:**
- Consumes: Task 2 `rule_match`·`rule_account`
- 모양(spec §2): 디코이 증빙(같은 거래처 견적서·취소 주문, 지난달 명세서 재발송, 비슷한 금액 다른 거래처 영수증), 발행처·적요·가맹점명 표기 변형, 증빙 하나 → 출금 둘(부분 결제), 증빙 둘 → 출금 하나(카드), 품목으로 정해지는 과목(쇼핑몰 혼합 주문 — 금액 큰 과목, 범용 가맹점)
- 정답 `truth_expense_matches` 모양 그대로

- [ ] **Step 1: Write the failing tests** — 모양마다 `test_<shape>_fools_rule`, verify `test_decoy_not_in_truth`, `test_partial_payment_sums`, `test_meta_words_absent`(본문에 "디코이"·"decoy"·정답 표시 없음)
- [ ] **Step 2: Run** — FAIL
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run** PASS → `make data FAKE_LLM=1` → `evaluation.expense.score(conn, "dev")` 매칭 75~85%·과목 80~85%인지, 벗어나면 상수 조정 반복
- [ ] **Step 5: Commit** — `generator: ⑧-2 지출 디코이·표기 변형·부분 결제·품목 과목`

### Task 6: ⑤-2 근거 표현 풀·R03 복합 기준·추가 처치 누락 (5b + 기준 문서)

**Files:**
- Modify: `generator/master.py`(`PERIO_POCKET_MIN_MM` — Task 1에서 만든 것, 표현 풀 비율 상수 `EVIDENCE_STYLE_SHARES`, `SAME_DAY_EXTRA` 추가분 상수)
- Modify: `generator/chart.py` — 표현 풀(`EVIDENCE_PHRASES: {사유: {"explicit": (...), "paraphrase": (...), "state": (...)}}`, `NO_EVIDENCE_PHRASES: {사유: {"negated": (...), "near_miss": (...)}}`), `CC_NO_EVIDENCE` 없애고 공통 C.C 풀, `_findings`가 `PERIO_POCKET_MIN_MM`과 R03 복합 기준(깊이 + 출혈·골소실)으로 부분만 맞는 사례 생성, 고른 문구를 사실(facts)에 남겨 9b 초안·검증이 쓰게, `narrative_checks`·`evidence_forbidden`을 "고른 문구 포함 + 다른 사유 근거 문구 없음"으로, 추가 처치 누락은 새 자식 난수로 결과 없는 달에 더함 + "같은 날 ○번 상담·예정" 디코이
- Modify: `generator/doc_catalog.py` 치주치료 산정 절 — `PERIO_POCKET_MIN_MM`을 읽는 복합 기준 문장
- Modify: `prompts/gen_chart.md` — 고른 근거 문구를 그대로 넣으라는 지시(문구 자체는 사실에서)
- Modify: `generator/verify/chart.py` `check_chart_evidence_words`·`check_chart_narrative_facts`·`check_omitted_extra`·`check_chart_decoy_ratio` + 대기 청구 달 하한, 키워드 규칙 정확도 경고 리포트
- Test: `tests/generator/test_chart.py`, `tests/generator/test_verify_chart.py`, `tests/evaluation/test_claim_check_eval.py`

**Interfaces:**
- Consumes: Task 1 `keyword_rule`, `master.PERIO_POCKET_MIN_MM`
- 라벨 규칙(spec §3): 기준 문장을 다 채우면 근거 있음, 일부만이면 근거 없음 — 사유마다 기준 문장과 풀 문구 짝 테스트
- 요건 수치(R01 기간·R02 일수)는 지금처럼 필수, R03 깊이만 일부 줄에서 정성 표현 허용

- [ ] **Step 1: Write the failing tests** — `test_phrase_pools_fool_keyword_rule`(paraphrase·state 문구는 `keyword_rule`이 오류로, negated 문구는 근거 있음으로 잘못 읽음), `test_partial_findings_labeled_no_evidence`, `test_negated_evidence_passes_checks`, `test_perio_threshold_single_source`(`chart`·`doc_catalog`가 같은 상수), `test_existing_truth_unchanged`(`truth_claim_errors`·기존 추가 처치 누락 11건 해시), verify `test_pending_month_has_each_style`
- [ ] **Step 2: Run** — FAIL
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run** PASS → `make data FAKE_LLM=1`(가짜 게이트웨이 서술로 코드 단계·검증만 확인) → `make verify`
- [ ] **Step 5: Commit** — `generator: ⑤-2 근거 표현 풀·R03 복합 기준·추가 처치 누락 더하기`

### Task 7: ⑤-2 9b 표본 채우기와 읽어서 풀리는지 확인 (과금 — 승인 먼저)

- [ ] **Step 1:** `make data`로 9b·문서 LLM 작업 건수 확인 → 표본만(요건 사유별 헷갈리는 줄 각 10줄 안팎, dev) 작업 파일을 잘라 사람에게 건수·비용을 보이고 승인 → `make llm-fill-api TASK=…` → `make llm-import`
- [ ] **Step 2:** 표본에서 `keyword_rule` 정밀도·재현율 측정, Claude 서브에이전트에 요건 사유별 기준 문장 + 서술만 주고(정답 없이) 판정 → 정답과 비교. 거의 다 맞혀야 통과, 아니면 풀 문구를 고쳐 Task 6 반복
- [ ] **Step 3:** 같은 방식으로 ⑧-1·⑧-2 dev 헷갈리는 사례 각 20~30건을 서브에이전트가 행만 보고 판정(코드 단계라 과금 없음)
- [ ] **Step 4:** 결과를 진행표에 기록, 커밋 없음(데이터는 커밋 안 함)

### Task 8: 실데이터 재생성과 직후 확인 (과금 — 승인 먼저)

- [ ] **Step 1:** 지금 `data/`·`ground_truth/`를 `.superpowers/sdd/data-redesign/before/`에 복사
- [ ] **Step 2:** `make data` → 건수·비용 보고 → 승인 → `make llm-fill-api` → `make llm-import` → `make data` → `make verify`
- [ ] **Step 3:** 직후 확인(spec §5) — 스크립트로 전후 비교: `emr_claims`·`emr_payments`·`truth_ledger_errors`(payment_id 있는 33행)·`truth_claim_errors`·추가 처치 누락 11건 같음, 바뀐 `call_uid` 0건(test 판정 풀 포함), `LEDGER_DEBT_CALLS` 배정 같음. 하나라도 다르면 멈추고 보고
- [ ] **Step 4:** dev 기준선 세 개를 다시 재 목표 안인지 확인
- [ ] **Step 5:** `ground_truth/*.json` diff 확인(바뀐 건 rag_qa 정답 정도여야), 생성 코드 커밋이 이미 끝났으면 커밋 없음

### Task 9: 색인 (과금 — 승인 먼저)

- [ ] `make index EMBEDDING=<지금 모델>` 건수·비용 보고 → 승인 → 실행(바뀐 청크만, 이어 돎)

### Task 10: ③ 질문 보강

**Files:**
- Modify: `ground_truth/rag_qa.json`, 필요하면 `generator/documents.py` `APPENDED`(test 전용 새 문서)·`generator/doc_catalog.py`, `generator/verify/rag_qa.py`(`QA_TYPE_COUNTS` 새 수)
- Test: `tests/generator/test_verify_rag_qa.py`

- [ ] **Step 1:** 재생성으로 바뀐 차트형·사례형·잔액 문항 정답을 `make verify`(answer_sql·answer_ids)로 확인하고 고침
- [ ] **Step 2:** 새 문항(spec §4: 여러 문서·외부 문서 틀린 값·과거 값·시간 계산·동명이인·같은 환자 비슷한 통화) — 유형별 dev = test 수, 방해 문서 짝 문항은 test에, 짝 없는 문항은 dev에. 베끼기 검사 통과. 사람 검토(초안을 보이고 승인)
- [ ] **Step 3:** `QA_TYPE_COUNTS` 갱신 → `make data`(문항 적재) → `make verify` → 커밋 `generator: ③ RAG 평가셋 보강 — 여러 문서·충돌·시간·동명이인, 방해 문서 비율 맞춤`

### Task 11: 앱 재실행과 숫자 갱신 (과금 — 승인 먼저)

- [ ] **Step 1:** ⑤-2 점검 재실행(약 $4.3, Batch) — 명령은 Makefile·`backend/claim_check.py` 독스트링, 승인 후
- [ ] **Step 2:** 판정기 `make judge V=<활성 판정기>`(로컬 Qwen, 바뀐 통화만 — Task 8에서 0건이면 생략)
- [ ] **Step 3:** dev 평가: `evaluation.recon.score`·`evaluation.expense.score`·`evaluation.claim_check` dev(키워드·AI·서술 전부 기준선), ③ `eval.py --split dev --retrieval-only`(검색만 — 챗은 과금이라 승인 시)

### Task 12: 문서·고정

- [ ] **Step 1:** 가상 데이터 생성 설계 §0 데이터 상태 "고정"으로, §2.4·§2.7·§4.3에 새 생성 규칙(값은 master 이름으로), 평가 설계 §5 ⑤-2 키워드 기준선·⑧-2 규칙 기준선
- [ ] **Step 2:** AI 실험 기록 §23에 재설계 전후 dev 숫자 표(⑧-1·⑧-2·⑤-2 기준선, ⑤-2 AI), 비용
- [ ] **Step 3:** 기능 정의 §9 "데이터 재설계" 항목 지우기(끝난 할 일), `tests/generator/test_doc_sync.py` 통과
- [ ] **Step 4:** 커밋 `docs: 데이터 재설계 — 새 생성 규칙·기준선·전후 숫자, 데이터 고정`

## 이 계획에서 정한 것

- 기준선 둘(Task 1·2)을 첫 커밋으로, 측정 함수는 `evaluation/`(키워드 낱말 표는 `generator.chart`에서 읽음 — evaluation은 generator import 가능)
- `PERIO_POCKET_MIN_MM`은 Task 1에서 만든다(키워드 규칙이 먼저 써야 해서) — 값 4는 지금 생성 경계와 같음
- 표본 확인은 Claude 서브에이전트(정답 없이 기준 문장·서술·행만)로 — Opus API 과금 없이
- 과금 명령 네 가지는 실행마다 승인
