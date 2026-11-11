# ⑤-2 AI 점검 (2a) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 요건이 있는 시술의 급여 청구 줄을 AI가 차트로 읽어 임상 요건 근거 유무를 판단·저장·후검증하고, 청구 전 점검 AI 칩과 조정 건 판단 추천에 보이며, 요건 오류·조정 건 판단을 기준선과 나란히 채점한다.

**Architecture:** ④ 준비표 틀을 그대로 옮긴다 — 입력(사실) → 해시 → 배치/일반 호출 → 후검증(인용 글자 대조) → 한 번 더 → 저장(`ax_claim_checks`). 앱은 저장된 결과만 읽고, 평가는 `evaluation/claim_check.py`가 정답 DB를 붙여 채점한다. 추가 처치 누락은 출력 칸만 만들고 채점은 2b.

**Tech Stack:** Python(sqlite3, `shared/llm.py` 게이트웨이, 별칭 `chat`), FastAPI, Next.js, pytest, Playwright.

**Spec:** `docs/superpowers/specs/2026-11-10-claim-review-design.md` §2 (2단계). 2b(추가 처치 누락 실데이터 11건 + 합성 사례 30~50건)는 이 계획 밖.

## Global Constraints

- 입력에 **청구 상태·조정 결과·사유·코드를 넣지 않는다**. 차트는 `emr_chart_notes`의 `dx_code`·`note`만(`generator/verify/chart.py` `CHART_NOTE_COLUMNS`)
- `backend/`는 `ground_truth/`·`generator/`·`evaluation/`을 import하지 않는다. master 값은 `backend.prep.master_values(conn)`(`requirement_notes`·`adjust_note_headings`·`procedure_diagnoses`·`adjust_note_types`)
- 대상 줄 = 시술이 `requirement_notes` 키에 있는 청구(전 기간 2,592줄, 그중 대기 99). 앱 범위는 대기 줄 + 기한 남은 요건 사유 조정 건, 평가 범위는 전부
- 같은 줄은 `(facts_hash, prompt_version, model)`이 같고 통과했으면 다시 부르지 않는다(`backend/prep.py` `_reusable`과 같은 규칙)
- 과금 명령은 실행 전에 줄 수·예상 금액을 사용자에게 보이고 승인받는다. 프롬프트를 고칠 때는 dev 고정 표본만, 전체 dev는 마지막 한 번, test는 사람만(평가 설계 §2.7, guard 훅이 `--split test`에 확인 창)
- 화면 판단 이름: `justified` 삭감 수용, `contestable` 재심 신청(`frontend/lib/claims-api.ts` `JUDGMENT_LABEL`)
- AI 문구는 사실 문장(사용자 결정): 청구 전 딱지 `차트에 요건 근거 못 찾음(AI)`, 조정 건 할 일 칸 `AI가 차트에서 근거 찾음 → 재심 신청` / `AI가 차트에서 근거 못 찾음 → 삭감 수용`
- AI 점검은 명령과 화면 버튼 둘 다로 돌린다(사용자 결정) — 버튼은 준비표 갱신 흐름(`POST /jobs/prep-regenerate`·`ax_jobs`·폴링) 재사용
- 주석·독스트링 한국어, 근거 문서 절을 가리킨다. 색은 `globals.css` 토큰, 부품은 `components/ui/`

## Review Focus

1. 인용이 공백·줄바꿈만 다른 경우 — 통과해야 한다(공백 접기 정규화). 따옴표·마침표가 붙은 인용도 앞뒤 문장부호를 떼고 대조
2. 차트 서술이 없는(`note`가 NULL 또는 빈) 줄 — AI를 부르지 않고 `none`(판단 없음)으로 저장, "근거 있음"을 만들 수 없다
3. 대기 줄이 새로 생기거나 차트가 바뀐 줄 — 해시가 달라져 다음 실행에 다시 부른다. 바뀌지 않은 줄은 호출 0
4. 한 시술에 요건 사유가 둘인 경우(`U0018` 2개) — 두 절을 모두 입력에 넣고, 판단은 줄 하나에 하나
5. 조정 건에서 AI 결과가 없거나 후검증 실패 — 할 일 칸은 지금처럼 "차트 확인 후 결정", 화면이 깨지지 않는다

---

### Task 1: 입력·출력·후검증 (순수 함수)

**Files:**
- Create: `backend/claim_check.py`
- Test: `tests/backend/test_claim_check.py`

**Interfaces:**
- Produces:
  - `TASK = "claim_check"`, `ALIAS = "chat"`, `PROMPT_VERSION = "v1"`
  - `SCHEMA` — `{requirement: enum["met","missing","na"], omissions: [{procedure: str, quote: str}], evidence: [{source: enum["chart","standard"], quote: str}]}`, `additionalProperties: False`
  - `sections(conn) -> dict[str, str]` — 소제목 → 절 본문(`source.ax_documents` `type='claim_standard'`을 `## ` 단위로 자름, `rag_index._sections` 재사용)
  - `claim_facts(row: dict, mv: dict, secs: dict[str,str]) -> dict` — `{procedure, tooth, diagnosis, note, standards: [{heading, body}]}`. row는 `procedure_code, procedure_name, tooth_no, dx_name, note`
  - `found(quote: str, text: str) -> bool` — 공백 접기(`\s+`→한 칸)·앞뒤 따옴표/문장부호 제거 뒤 부분 문자열, 빈 인용은 거짓
  - `check_result(facts: dict, r: dict) -> bool`

- [ ] **Step 1: 실패 테스트**
  - `test_facts_hide_claim_status` — `claim_facts`의 키가 정확히 `{"procedure","tooth","diagnosis","note","standards"}`, 값 어디에도 `adjusted`·`adjust_note` 문구가 없다
  - `test_facts_two_requirements` — `U0018`은 `standards` 2개(`requirement_notes["U0018"]` 각 문구의 `adjust_note_headings` 소제목)
  - `test_found_whitespace` — `found("치주낭  깊이\n5mm", "…치주낭 깊이 5mm 측정…")` 참, `found("“치주낭 깊이 5mm”.", …)` 참, `found("", …)` 거짓, `found("6mm", …)` 거짓
  - `test_check_met_needs_chart_quote` — `met`에 chart 인용 0개면 거짓, chart 인용이 `note`에 없으면 거짓
  - `test_check_missing_needs_standard_quote` — `missing`에 standard 인용이 `standards` 본문에 없으면 거짓, 있으면 참
  - `test_check_omission_quote_in_chart` — `omissions[].quote`가 `note`에 없으면 거짓
  - `test_check_na_no_evidence_ok` — `na`·인용 0개 참
- [ ] **Step 2:** `uv run pytest tests/backend/test_claim_check.py -q` → 실패(모듈 없음)
- [ ] **Step 3:** 구현. `check_result`: 모든 인용이 출처 본문(`chart`→`note`, `standard`→`standards[*].body` 중 하나)에 `found`, `met`는 chart 인용 ≥1, `missing`은 standard 인용 ≥1
- [ ] **Step 4:** 같은 명령 → 통과
- [ ] **Step 5:** 커밋 `backend: 청구 AI 점검 입력·출력·인용 후검증`

### Task 2: 저장·실행·명령

**Files:**
- Modify: `shared/app_schema.sql`(새 테이블), `backend/claim_check.py`, `Makefile`
- Create: `prompts/claim_check/v1.md`
- Test: `tests/backend/test_claim_check.py`(`tests/backend/test_prep.py` `FakeGw` 꼴로)

**Interfaces:**
- Consumes: Task 1 전부
- Produces:
  - 테이블 `ax_claim_checks(claim_id INTEGER PRIMARY KEY, facts TEXT, facts_hash TEXT, result TEXT NULL, valid INTEGER, prompt_version TEXT, model TEXT, created_at TEXT)`
  - `targets(conn, today: date, *, scope: str, claim_ids: list[int] | None = None) -> list[dict]` — `scope="app"`: 대기 줄 + `reviewed_at <= today`이고 기한 남은 요건 사유 조정·불능 건, `"all"`: 대상 줄 전부. `claim_ids`가 있으면 그 줄만
  - `build(conn, gw, rows: list[dict], *, use_batch: bool, poll_seconds: int = 30) -> dict` → `{rows, called, skipped, failed, empty}`
  - `results(conn, claim_ids: list[int]) -> dict[int, dict]` — `{claim_id: {requirement, evidence, omissions, valid}}`(저장값, 후검증 실패는 `valid: False`·나머지 None)
  - CLI `python -m backend.claim_check run [--scope app|all] [--no-batch]`, Makefile `claim-check` (과금, 이어 돎): `make claim-check [SCOPE=all] [NO_BATCH=1]`
  - `pending(conn, today) -> int` — 앱 범위 중 다시 불러야 할 줄 수(`_reusable` 아님, 서술 없는 줄 제외)
  - API `POST /jobs/claim-check`(`ax_jobs` `kind='claim_check'`, 같은 종류 running이면 그 id, 스레드에서 `build(..., use_batch=False)`) — `backend/api.py` `/jobs/prep-regenerate`와 같은 꼴. `shared/app_schema.sql` `ax_jobs.kind` CHECK가 있으면 값 추가

- [ ] **Step 1: 실패 테스트**
  - `test_build_saves_and_reuses` — FakeGw가 통과하는 답을 줌 → `called == n`, 두 번째 `build`는 `called == 0, skipped == n`
  - `test_build_retries_once_then_null` — 첫 답이 후검증 실패, 다시 부른 답도 실패 → `failed == 1`, 저장 `result IS NULL, valid = 0`
  - `test_empty_note_not_called` — `note`가 NULL·빈 줄은 호출하지 않고 `requirement="na"`로 저장, `empty == 1`
  - `test_changed_note_recalled` — 저장 뒤 차트 문구를 바꾸면 그 줄만 다시 부름
  - `test_targets_app_scope` — 대기 줄과 기한 남은 요건 사유 조정 건만, 기한 지난 건·서류 사유 조정 건은 없음
  - `test_ref_logged` — `ax_llm_calls.ref_type = 'claim'`, `ref_id = claim_id`
  - `test_job_api_reuses_running` — `POST /jobs/claim-check` 두 번 → 같은 job id(FakeGw 주입은 `tests/backend/test_api*`의 prep 작업 테스트 꼴)
- [ ] **Step 2:** 실행 → 실패
- [ ] **Step 3:** 구현. 흐름·재시도·배치 판단(`_batchable`)·`_reusable` 규칙은 `backend/prep.py` `build`와 같게, `gw.complete(..., ref=("claim", claim_id))`. 프롬프트 `v1.md`: 차트 서술과 기준 절만 보고 요건 근거를 판단, 인용은 원문 그대로 짧게, 추측 금지, 판단할 수 없으면 `na`
- [ ] **Step 4:** 실행 → 통과, `make lint`
- [ ] **Step 5:** 커밋 `backend: 청구 AI 점검 저장·재사용·make claim-check`

### Task 3: 화면용 반환 — 청구 전 AI 칩과 조정 건 추천

**Files:**
- Modify: `backend/claims.py`(`pre_claim`, `adjusted`, `SUGGEST_REASON`)
- Test: `tests/backend/test_claims.py`

**Interfaces:**
- Consumes: `claim_check.results(conn, claim_ids)`
- Produces:
  - `pre_claim` 행에 `ai: {requirement, evidence, omissions} | None`, `counts.ai`(`requirement == "missing"` 또는 `omissions` 있는 줄 수), `counts.ai_checked`(저장된 줄 수 — 0이면 화면이 칩을 숨김), `ai_at`(가장 늦은 `created_at` | None), `ai_pending`(`claim_check.pending`)
  - `backend/today.py` "사람이 볼 일"에 `preclaim_ai`(= `counts.ai`, 0이면 숨김) → 화면 줄 `청구 전 차트에 요건 근거 못 찾음 n건(AI)` → `/claims?tab=pre&chip=ai`
  - `adjusted` 요건 사유 행: AI가 `met` → `suggestion="contestable"`, `missing` → `"justified"`, 그 밖 → `None`(지금처럼). 행에 `ai_evidence: [{source, quote}] | None`, `suggest_by: "rule" | "ai" | None`

- [ ] **Step 1: 실패 테스트**
  - `test_pre_claim_ai_counts` — 저장 2줄(missing 1, met 1) → `counts.ai == 1`, `counts.ai_checked == 2`, 해당 행 `ai.requirement == "missing"`
  - `test_pre_claim_without_checks` — 저장 0 → `counts.ai_checked == 0`, 모든 행 `ai is None`
  - `test_adjusted_requirement_ai_suggestion` — 요건 사유 조정 건에 `met` 저장 → `suggestion == "contestable"`, `suggest_by == "ai"`, `ai_evidence` 1개 이상
  - `test_adjusted_invalid_check_keeps_none` — `valid = 0` → `suggestion is None`, `suggest_by is None`
  - `test_api_claims` 키 집합 갱신
  - `test_today_preclaim_ai` — `counts.ai == 1`이면 today 응답 `preclaim_ai == 1`
- [ ] **Step 2:** 실패 확인 → **Step 3:** 구현 → **Step 4:** `uv run pytest tests/backend/test_claims.py tests/backend/test_today.py -q` 통과
- [ ] **Step 5:** 커밋 `backend: 청구 전 AI 칩·조정 건 요건 판단 추천`

### Task 4: 화면

**Files:**
- Modify: `frontend/lib/claims-api.ts`, `frontend/components/claims/pre-claim-table.tsx`, `frontend/components/claims/adjust-table.tsx`, `frontend/components/today/today-view.tsx`
- Test: `frontend/e2e/claims.spec.ts`

**Interfaces:**
- Consumes: Task 3 반환 모양
- Produces: `PreRow.ai`, `PreSheet.counts.ai/ai_checked`, `AdjustRow.ai_evidence/suggest_by`

- [ ] **Step 1: 실패 e2e**
  - `청구 전 AI 칩 — 점검 결과가 있을 때만` — `ai_checked: 0`이면 칩 "임상 요건·차트 기반 누락(AI)" 없음, 있으면 `임상 요건·차트 기반 누락(AI) 1` 칩, 누르면 1줄, 경고 칸에 `차트에 요건 근거 못 찾음(AI)` 딱지와 인용 문장
  - `AI 점검 갱신 버튼` — `ai_pending: 3`이면 `AI 점검 갱신하기 (3줄)`, 누르면 `POST /api/jobs/claim-check`, 작업 끝나면 다시 조회. 0이면 `AI 점검 최신`(눌리지 않음). `AI 점검: 11/11 09:30 기준` 표시
  - `조정 건 AI 추천` — 요건 사유 행 할 일 칸 `AI가 차트에서 근거 찾음 → 재심 신청`(missing이면 `AI가 차트에서 근거 못 찾음 → 삭감 수용`), 펼친 곳에 차트 인용 문장(따옴표) 표시
  - `오늘 화면 AI 줄` — `preclaim_ai: 2`면 `청구 전 차트에 요건 근거 못 찾음 2건(AI)` 줄, 누르면 `/claims?tab=pre&chip=ai`
  - `AI 결과 없는 요건 사유는 차트 확인 후 결정` — 지금 동작 유지
- [ ] **Step 2:** `npx playwright test e2e/claims.spec.ts` → 실패
- [ ] **Step 3:** 구현. 칩 키 `"ai"`, 주소 `?tab=pre&chip=ai`. 조정 건 문구는 `suggest_by === "ai"`일 때 위 사실 문장, 규칙 추천은 지금처럼 `추천: …`. 갱신 버튼·폴링은 `frontend/components/prep/prep-view.tsx` 흐름 재사용
- [ ] **Step 4:** e2e 통과, `make lint`
- [ ] **Step 5:** 커밋 `frontend: 청구 전 AI 칩·갱신 버튼, 조정 건 AI 근거, 오늘 화면 줄`

### Task 5: 평가 — 분할·고정 표본·채점

**Files:**
- Create: `evaluation/claim_check.py`
- Modify: `evaluation/splits.py`, `evaluation/eval.py`(`SECTIONS`에 `"claim_check"`), `Makefile`
- Test: `tests/evaluation/test_claim_check.py`

**Interfaces:**
- Consumes: `backend.claim_check.targets/build/results`
- Produces:
  - `claim_split(claim_id: int) -> str` — `sha256(f"claim:{claim_id}")` 첫 바이트 짝수 `"dev"`, 홀수 `"test"`
  - `dev_sample(conn, n: int = 60) -> list[int]` — dev 안에서 요건 오류 줄 n/2 + 오류 없는 줄 n/2, 각 층 `sha256` 순 앞에서(고정)
  - `score(conn, split: str) -> dict`:
    - `(a)`: 대상 줄의 요건 오류(`truth_claim_errors.kind='requirement'`) 정밀도·재현율(AI `missing` = 양성), 기준선 "서술 있는 줄 전부"(`truth_chart_notes.kind='narrative'`), 기간별(`emr_claims.status` 확정: paid·adjusted·denied / 제출·대기: submitted·pending), 후검증 통과율, 미점검 수
    - `(b)`: 요건 사유 조정·불능 건 정확도 — 정답 `justified` = 그 줄이 요건 오류, 아니면 `contestable`(가상 데이터 생성 설계 §2.4), 기준선 "모두 삭감 수용(정당)"·"모두 재심 신청(다툴 만함)", 판단 없음 수
  - CLI `python -m evaluation.claim_check sample` (dev 표본만 점검 — 과금), `make claim-check-sample`
- [ ] **Step 1: 실패 테스트** — 작은 truth·source 고정 데이터로
  - `test_split_stable_half` — 같은 id 같은 분할, 1..2000에서 dev 비율 0.45~0.55
  - `test_sample_fixed_and_dev_only` — 두 번 부르면 같은 목록, 전부 `claim_split == "dev"`, 오류 줄 절반
  - `test_score_requirement` — AI 결과 4줄(TP·FP·FN·TN) → precision 0.5, recall 0.5, 기준선 recall 1.0
  - `test_score_adjust_baselines` — 정답 justified 3·contestable 1 → "모두 삭감 수용" 0.75
  - `test_test_split_not_in_sample` — 표본에 test 줄 없음
- [ ] **Step 2:** 실패 → **Step 3:** 구현(eval.py 연결은 `prep`과 같은 자리, test 묶음 `claim_check`는 버전 단위 1회) → **Step 4:** 통과
- [ ] **Step 5:** 커밋 `eval: 청구 AI 점검 분할·고정 표본·채점`

### Task 6: 문서

**Files:**
- Modify: `docs/치과 AX - 기능 정의.md` §5 ⑤-2·§9, `docs/치과 AX - 앱 설계.md`(⑤ 줄 — `ax_claim_checks`, 명령), `docs/치과 AX - 평가 설계.md` §5 ⑤(분할·표본·지표 이름), `CLAUDE.md` 명령 목록(`make claim-check`(과금, 이어 돎)·`claim-check-sample`(과금))
- [ ] 규칙만, 값은 코드 이름으로. 커밋 `docs: ⑤-2 AI 점검 2a`

### Task 7: 과금 실행과 기록 (사용자 승인 단계마다)

- [ ] `make claim-check-sample` — 60줄 일반 호출, 예상 1달러 안팎 → 채점(`--section`은 dev) → 프롬프트 고침 반복은 **3회 상한**(넘으면 사용자에게 물음)
- [ ] 전체 dev(약 1,300줄, 배치, 예상 5달러 안팎) 한 번 → 채점 리포트
- [ ] 앱 범위(`make claim-check`, 대기 99줄 + 조정 건, 예상 0.5달러 안팎)
- [ ] 결과를 `docs/치과 AX - AI 실험 기록.md`에 문제→원인→시도→결과→한계로. test는 사용자가 돌린다
