# ⑤-4 재심 사유서 초안 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 사람이 "재심 신청"을 기록한 조정 건에 AI가 모든 문장이 인용(조정 내역·기준 절·차트)이거나 추측 목록에 묶인 재심 사유서 초안을 쓰고, 검토 확인 뒤에만 복사·제출 결정을 기록하며, 정답 다툴 만함 dev 건으로 후검증 통과율과 근거 없는 문장 비율을 잰다.

**Architecture:** `backend/claim_appeal.py`가 입력·스키마·문장 나누기·후검증(순수 함수)과 저장·초안·검토·제출을 맡는다. 저장·한 번 더는 `backend/claim_store.py`(Kind)를 그대로 쓰고, 표 `ax_claim_appeals`는 그 칸 + 검토·제출 칸. 평가는 `evaluation/claim_appeal.py`가 같은 함수로 dev 건을 돌려 `evaluation/` 기록에만 쓴다.

**Tech Stack:** Python(sqlite3, 게이트웨이 별칭 `chat`), FastAPI, Next.js, pytest, Playwright.

**Spec:** `docs/superpowers/specs/2026-11-10-claim-review-design.md` §4

## Global Constraints

- 대상: `ax_claim_judgments.judgment = 'contestable'`인 조정 건만, 기한 지난 건 제외(`claims.record_judgment`와 같은 검사)
- 입력: 조정 사유 문구, 시술·치아, 그 사유의 기준 절(`claim_check.section_cards` 제목·본문), 차트 서술. 금액·판단 기록은 넣지 않는다
- 근거 종류 `adjustment`(조정 내역) · `standard`(기준 절) · `chart`(차트) · `assumption`(추측 번호, 1부터)
- 후검증: 한 칸 = 한 문장(문장 나누기 결과 1개), 인용 구절은 원문에 있고(`claim_check.found`) 그 문장 안에도 있다, 추측 번호는 목록 안. 실패면 한 번 다시, 또 실패면 결과 없음
- 문장 나누기: `.`·`?`·`!` 뒤 공백, 그리고 줄바꿈
- 화면 문구: 꼬리표 `조정 내역 인용`·`기준 절 인용`·`차트 인용`·`추측 ①`(원 숫자), 추측 머리 `AI가 추측한 부분 — 여기만 확인하면 됩니다`, 체크 `검토했고 정확함`, 버튼 `초안 쓰기`·`다시 쓰기`·`복사`·`제출 결정`, 실패 `초안을 만들지 못했습니다`, 안내 `검토 확인 전에는 복사·제출할 수 없습니다 · AI는 제출하지 않습니다`
- 제출 결정 뒤 다시 쓰기·그 건의 판단 변경을 서버가 막는다(400). 제출 결정은 검토 확인이 있을 때만
- 평가 초안은 `app.sqlite`에 쓰지 않는다(`evaluation/claim_appeal_runs.jsonl`)
- 재생성 때 `ax_claim_appeals`는 버린다(판단 기록과 같은 이유 — 앱 설계 §7.3)
- 과금 명령은 건수·예상 금액을 보이고 승인. test는 사람만

## Review Focus

1. 초안을 쓴 뒤 판단을 "삭감 수용"으로 바꿈 — 초안 칸이 사라지고, 다시 "재심 신청"이면 같은 초안이 보인다(지우지 않음)
2. 검토 확인 뒤 [다시 쓰기] — 새 초안이 오고 검토 확인이 풀려 [복사]·[제출 결정]이 다시 꺼진다
3. 제출 결정 뒤 판단 버튼·[다시 쓰기] — 화면은 꺼지고 API는 400
4. 차트 서술이 빈 건 — 초안은 쓰되 차트 인용을 쓸 수 없다(후검증이 막음), 화면이 깨지지 않는다
5. 후검증 두 번 실패 — "초안을 만들지 못했습니다"와 [다시 쓰기]만, 복사·제출 없음

---

### Task 1: 입력·스키마·문장 나누기·후검증 (순수 함수)

**Files:** Create `backend/claim_appeal.py`; Test `tests/backend/test_claim_appeal.py`

**Interfaces — Produces:**
- `TASK, ALIAS, PROMPT_VERSION = "claim_appeal", "chat", "v1"`, `BASES = ("adjustment", "standard", "chart", "assumption")`
- `SCHEMA` — `{sentences: [{text, basis: enum BASES, quote: str|null, assumption: int|null}] (minItems 1), assumptions: [str]}`, null은 `anyOf`
- `appeal_facts(row) -> {adjust_note, procedure, tooth, standard: {heading, body}, note}`
- `split_sentences(text) -> list[str]`
- `check_result(facts, r) -> bool` — Global Constraints 후검증
- `unbacked(facts, r) -> tuple[int, int]` — (나눈 문장 수, 근거 없는 문장 수): 칸을 나눈 각 문장이 그 칸의 근거를 만족하지 않으면(인용이 그 문장·원문에 없음, 추측 번호 밖) 근거 없음
- [ ] **Step 1: Write the failing tests** — `test_facts_hide_amount_and_judgment`, `test_split_sentences`(`"가. 나? 다!\n라"` → 4, 소수점 `"5.5mm"`은 안 나눔), `test_check_ok`(네 근거 각 한 문장), `test_check_quote_must_be_in_sentence`(원문엔 있지만 문장엔 없는 인용 → 실패), `test_check_quote_not_in_source`, `test_check_two_sentences_in_one_slot`, `test_check_assumption_index`(0·목록 밖 → 실패), `test_unbacked_counts_split_pieces`(두 문장 칸 — 인용이 첫 문장에만 → (2, 1))
- [ ] **Step 2: Run** `uv run pytest tests/backend/test_claim_appeal.py -q` — Expected: FAIL(모듈 없음)
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run** 같은 명령 — Expected: PASS
- [ ] **Step 5: Commit** · `backend: ⑤-4 재심 사유서 입력·스키마·문장 나누기·후검증`

### Task 2: 저장·초안·검토·제출

**Files:** Modify `backend/claim_appeal.py`, `backend/claims.py`(`record_judgment`이 제출된 건 거부), `shared/app_schema.sql`(`ax_claim_appeals` — `ax_claim_omissions` 칸 + `reviewed_by, reviewed_at, submitted_by, submitted_at` NULL 허용); Create `prompts/claim_appeal/v1.md`; Test `tests/backend/test_claim_appeal.py`

**Interfaces — Produces:**
- `KIND`(claim_store, 표 `ax_claim_appeals`, `text_key="adjust_note"` — 조정 사유는 늘 있어 빈 줄로 건너뛰지 않음)
- `target(conn, claim_id, today) -> dict` — 판단이 `contestable`이고 기한 안이 아니면 ValueError("재심 신청을 기록한 건만…")
- `draft(conn, gw, claim_id, today) -> dict` — 제출된 건이면 ValueError, 있던 행을 지우고 `claim_store.build(…, use_batch=False)`(한 번 더 포함), 검토 칸은 비어 있다. 반환은 `appeal(conn, claim_id)`
- `appeal(conn, claim_id) -> {sentences, assumptions, valid, reviewed: {by, at}|None, submitted: {by, at}|None, at} | None`
- `review(conn, claim_id, by, at, checked: bool)` — 통과한 초안만, 제출된 건 ValueError, `checked=False`면 검토를 지운다
- `submit(conn, claim_id, by, at, today)` — 판단 `contestable` + 통과한 초안 + 검토 확인 있어야, 아니면 ValueError
- `claims.record_judgment` — 그 건에 제출 결정이 있으면 ValueError("재심 사유서를 제출 결정한 건은 판단을 바꿀 수 없습니다")
- [ ] **Step 1: Write the failing tests** — `test_draft_only_contestable`, `test_draft_saves_and_redraft_resets_review`, `test_draft_failed_twice_none`, `test_review_requires_valid_draft`, `test_submit_requires_review`, `test_submitted_locks_redraft_and_judgment`(Review Focus 3), `test_empty_chart_still_drafts`(차트 인용 없이 통과 — Review Focus 4)
- [ ] **Step 2: Run** — Expected: 새 테스트 FAIL
- [ ] **Step 3: Implement** — 프롬프트 v1 요지: 심평원 이의신청 사유서 문장을 쓴다, 한 칸에 한 문장, 사실은 조정 내역·기준 절·차트에서 글자 그대로 인용하고 그 구절을 문장 안에 그대로 넣는다, 원문에 없는 판단·결론·요청은 추측 목록에 적고 그 문장의 근거를 추측 번호로, 차트에 없는 사실을 지어내지 않는다, 금액은 쓰지 않는다
- [ ] **Step 4: Run** `uv run pytest tests/backend/test_claim_appeal.py tests/backend/test_claims.py -q` — Expected: PASS
- [ ] **Step 5: Commit** · `backend: ⑤-4 재심 사유서 저장·초안·검토·제출, 제출 뒤 판단 잠금`

### Task 3: API와 조정 목록 행

**Files:** Modify `backend/api.py`, `backend/claims.py`(`adjusted` 행 `appeal`); Test `tests/backend/test_claim_appeal.py`

**Interfaces — Produces:** `POST /claims/{id}/appeal`(초안 쓰기·다시 쓰기, 게이트웨이 `get_gateway_factory`, 일반 호출 — 사람이 기다림) → `appeal`; `POST /claims/{id}/appeal/review {part, checked}`; `POST /claims/{id}/appeal/submit {part}` — 모두 ValueError → 400. `adjusted` 행 `appeal`: 저장된 초안이 있으면 `appeal(...)`, 없으면 None(판단과 무관하게 내려 주고 화면이 판단이 `contestable`일 때만 보인다 — Review Focus 1)
- [ ] **Step 1: Write the failing tests** — `test_api_draft_review_submit_flow`(가짜 게이트웨이), `test_api_400s`(판단 없음·검토 없이 제출·제출 뒤 다시 쓰기), `test_adjusted_row_has_appeal`
- [ ] **Step 2: Run** — Expected: FAIL
- [ ] **Step 3: Implement** — 시각은 `_stamp(now)`, 기준일은 `clinic.today`(판단 API와 같은 꼴)
- [ ] **Step 4: Run** `uv run pytest tests/backend/test_claim_appeal.py tests/backend/test_claims.py -q` — Expected: PASS
- [ ] **Step 5: Commit** · `backend: ⑤-4 재심 사유서 API — 초안·검토·제출`

### Task 4: 화면

**Files:** Create `frontend/components/claims/appeal-draft.tsx`; Modify `frontend/lib/claims-api.ts`, `frontend/components/claims/adjust-table.tsx`(③ 칸 — `judgment.value === "contestable"`일 때), `frontend/e2e/claims.spec.ts`
- [ ] **Step 1: e2e 먼저**(가짜 응답) — 재심 신청 건 ③에 [초안 쓰기] → 초안 문장·꼬리표(`기준 절 인용`·`추측 ①`)·추측 머리 문구, 검토 체크 전 [복사]·[제출 결정] 꺼짐 → 체크 → 켜짐 → [제출 결정] → `제출 결정 · 데스크` 표시·[다시 쓰기] 사라짐, 실패 응답이면 `초안을 만들지 못했습니다`, 판단이 삭감 수용이면 ③ 칸에 초안 없음
- [ ] **Step 2: Run** `npm --prefix frontend run -s e2e -- e2e/claims.spec.ts` — Expected: 새 테스트 FAIL
- [ ] **Step 3: Implement** — 파트는 판단 버튼과 같은 파트 선택값, [복사]는 `navigator.clipboard.writeText(문장들을 줄바꿈으로)`, 초안 쓰는 동안 버튼 비활성·"쓰는 중…", 판단 버튼은 `appeal.submitted`면 비활성
- [ ] **Step 4: Run** 같은 명령 + `make lint` — Expected: PASS
- [ ] **Step 5: Commit** · `frontend: ⑤-4 재심 사유서 초안 — 꼬리표·추측·검토 확인·복사·제출 결정`

### Task 5: 평가

**Files:** Create `evaluation/claim_appeal.py`; Modify `evaluation/eval.py`, `Makefile`(`claim-appeal-dev`); Test `tests/evaluation/test_claim_appeal_eval.py`

**Interfaces — Produces:**
- `targets(conn, split) -> list[dict]` — 요건 사유 조정·불능 중 `truth_claim_errors`에 오류가 없는 건(정답 다툴 만함), `claim_split == split`, 행은 `claim_appeal.appeal_facts`가 읽는 칸
- `run(gw, rows, path=RUNS) -> dict` — 같은 요청·후검증·한 번 더, 기록 `{claim_id, facts_hash, prompt_version, model, first, final}`(first = 첫 답 원출력, final = 통과한 답 또는 None), 같은 (청구, 해시, 버전, 모델)이고 final이 있으면 건너뜀
- `score(rows, runs) -> {n, run, first_valid, final_valid, sentences, unbacked, unbacked_rate}` — 근거 없는 문장은 first로 `claim_appeal.unbacked`
- `main`: `run --split dev`(과금) · `run --split test`(사람, 버전마다 한 번 — `reports/claim_appeal_test_runs.jsonl`); `eval.py` dev에 기록이 있으면 ⑤-4 절
- [ ] **Step 1: Write the failing tests** — `test_targets_contestable_truth_only`, `test_run_records_first_and_final`, `test_run_skips_done`, `test_score`, `test_render`
- [ ] **Step 2: Run** — Expected: FAIL
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run** `uv run pytest tests/evaluation -q` — Expected: PASS
- [ ] **Step 5: Commit** · `eval: ⑤-4 재심 사유서 채점 — 후검증 통과율·첫 답 근거 없는 문장 비율`

### Task 6: 문서

평가 설계 §5 ⑤-4(문장 나누기 규칙·지표 정의, "⑤ 4단계 때" 지움), 앱 설계 ⑤ 문단(`ax_claim_appeals`·API·"⑤-4 앱 테이블은 그 단계 때 추가" 지움)·§7.3 버림 목록·§9 `/claims` 행, 기능 정의 §5 ⑤-4·§9(⑤ 끝 — 남은 test 채점만), CLAUDE.md 명령(`make claim-appeal-dev`), 개발자 소개 해당 줄
- [ ] **Step 1:** 고친 뒤 `uv run pytest tests/generator/test_doc_sync.py -q` — Expected: PASS
- [ ] **Step 2: Commit** 설계·계획과 함께 · `docs: ⑤-4 재심 사유서 초안`

### Task 7: 실행·확인(과금·사람)

- [ ] **Step 1:** `make claim-appeal-dev` — 정답 다툴 만함 dev 건(약 70) 일반 호출, 예상 ~$0.3 → 승인
- [ ] **Step 2:** `make dev`로 화면을 띄워 사용자가 한 건을 "재심 신청"으로 기록하고 [초안 쓰기]를 직접 눌러 본다(1건 ~$0.005)
- [ ] **Step 3:** AI 실험 기록 §30, 커밋
