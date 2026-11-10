# ⑨ 미예약 환자 1단계 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `/missed` 화면 — 다시 올 이유가 있는데 다음 예약이 없는 환자를 칩 6개와 원칙 점수 순으로(처음 10명, 더보기 10명씩) 보이고, 연락 결과 5개를 남기며, 오늘 화면에 줄 하나를 더한다. 과금 없음.

**Architecture:** 새 모듈 `backend/missed.py`가 그날 아침 기준 사실·칩·점수를 **표마다 한 번 훑는 집합 쿼리**(예약·청구·수납·문자·치료계획 — 쿼리 5개 안팎, 환자 수와 무관)로 계산하고(④ `backend/prep.py` 도우미는 예약 한 건 단위라 쓰지 않는다 — 규칙은 같게, 테스트로 대조), 연락 기록은 새 앱 테이블. 다른 치과 추정 칩의 외부 이력 사유는 생성기가 `source.ax_master_values`에 이름 하나를 더해 적재한다. 화면은 `frontend/components/missed/` 하나.

**Tech Stack:** Python 3 · FastAPI · sqlite3 · pytest / Next.js(TypeScript strict) · Tailwind 토큰 · shadcn `components/ui/` · Playwright

**Spec:** `docs/superpowers/specs/2026-11-10-missed-patients-design.md` (1단계 절만 — §1~§7, §12의 1단계 몫. §8~§11은 보류)

## Global Constraints

- 이름은 **미예약 환자**(메뉴·화면·문서). 기능 번호 ⑨ 그대로. 주소 `/missed`(기존 메뉴 `/followup` 자리를 바꾼다, `frontend/lib/nav.ts`)
- backend는 `ground_truth/`·`generator/`·`evaluation/`을 import하지 않는다. `truth_patient_churn`을 보지 않는다 — 칩 기준도 정답을 보고 맞추지 않는다(2단계 채점의 기준선이 된다)
- `emr_*`·`fin_*`에 쓰지 않는다. 데이터 상태 고정 — 기존 생성 테이블은 그대로, `ax_master_values`에 이름 하나 추가만(verify 먼저, 절대 규칙)
- 점수 꼴(설계 §3): 가중 덧셈 + 마지막 방문 최근성 감쇠 + 미수(금액 비례, **상한**). 올림: 최근 방문·진료계획 미예약·올해 스케일링 급여 미사용·리콜 연체·미수. 내림: 노쇼 뒤 재예약 없음·문자 무응답·다른 치과 흔적·최근 연락했는데 결과 없음(부재중 7일·나중에 다시 14일). 기본값은 `backend/missed.py` 상수, 바꾼 값만 `ax_settings`(키 이름 빌더 판단). **값마다 이유 한 구절**을 상수 옆에 두고 화면 설명이 같은 문구를 쓴다. "최근 연락"은 앱 연락 기록만(생성 문자는 읽지 않음)
- 연락 결과 5개(사용자 결정, 문구 그대로): `예약함`(목록에서 빠짐, 기간 없음) · `부재중·안 받음`(남음, 7일 감점) · `나중에 다시`(남음, 14일 감점) · `안 오겠다고 함`(180일 빠짐) · `다른 치과 다님`(빠짐). 수단 `전화`/`문자`. 누가 = 파트(`frontend/lib/part.ts`), 언제 = 실제 시계
- 칩 6개(문구 그대로): 리콜 연체 · 올해 스케일링 급여 미사용 · 노쇼 뒤 재예약 없음 · 미수 잔액 · 진료계획 미예약 · 다른 치과 추정
- 사실은 기준일 아침 기준(그날 전 사건만), 기준일 = `shared/clinic.py` 앱 기준일
- 화면: 처음 10명, "더보기"마다 10명(`PAGE = 10`). "1순위" 꼬리표·"1순위만" 필터 없음. 칩 필터·"연락함 숨기기"·"순위는 어떻게 정해지나요?"
- 주석·독스트링 한국어, 근거 절을 가리킨다. 색·모양은 토큰만, 부품은 `components/ui/`만
- 커밋 전 `make fmt` → `make lint` → `make test`. `git add` 파일 이름으로. 커밋 `<영역>: <무엇>` + 본문 + 끝에 `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`와 `Claude-Session: https://claude.ai/code/session_01G62cGwhYixc5LpBLhj8iGp`
- 과금 명령을 돌리지 않는다. `make data`는 과금 없음(가짜가 아니라 실데이터, LLM 단계는 캐시 그대로 — 나갈 작업 0건인지 먼저 확인)

## Review Focus

1. 환자 약 2,800명 — 처음부터 표마다 한 번 훑는 집합 쿼리로 짠다. 시간을 재는 테스트는 두지 않고(기계 속도에 흔들림) **쿼리 횟수 테스트**로 고정 → Task 2 `test_query_count_independent_of_patients`. 규칙이 ④와 어긋나지 않게 → Task 2 `test_same_as_prep_rules`. 실데이터 응답 시간은 마무리에서 한 번 재서 보고
2. 같은 환자에게 연락 기록이 여럿 — 가장 최근 것이 목록·점수를 정한다 → Task 3 `test_latest_contact_wins`
3. 칩이 0개가 된 환자(예: 연락 뒤 미수 정산)는 목록에서 빠진다, 다음 예약이 기준일 당일인 환자도 "예약 있음" → Task 2 `test_today_appointment_counts_as_booked`
4. 미수 상한 — 미수만 아주 큰 환자가 맨 위를 차지하지 않는다 → Task 2 `test_balance_capped`
5. 더보기 중 연락 결과를 남기면 순위가 바뀌어 같은 사람이 두 번 보이거나 빠진다 → Task 5 e2e `더보기 뒤 연락함 — 중복 없이 다시 조회`

---

### Task 1: 외부 이력 사유 적재 (생성기)

**Files:** Modify `generator/clinic.py` (`master_values_rows`), `tests/generator/test_master_values.py`, (verify `check_master_values`는 `master_values_rows`와 비교하므로 그대로)
**Interfaces:** Produces `ax_master_values` 이름 `external_adjust_notes` = `master.ADJUST_REASONS`에서 `category == "external"`인 사유의 `note` 목록(정렬). backend는 `prep.master_values(conn)["external_adjust_notes"]`로 읽는다(지금 `master_values`가 JSON을 그대로 읽는지 확인, `alert_rules`처럼 후처리가 필요 없으면 그대로)

- [ ] Step 1: 테스트 — `test_master_values_rows_match_master`에 `got["external_adjust_notes"] == sorted(r["note"] for r in ADJUST_REASONS.values() if r["category"] == "external")`, `test_check_master_values_flags_missing_and_wrong`의 `len(...) == 3` → `4`
- [ ] Step 2: `uv run pytest tests/generator/test_master_values.py -q` — FAIL
- [ ] Step 3: 구현
- [ ] Step 4: 같은 테스트 PASS, `make data`가 LLM 작업 0건으로 끝나는지 확인한 뒤 실데이터에 적재 → `make verify` 통과 → `sqlite3 data/source.sqlite "select value from ax_master_values where name='external_adjust_notes'"` 가 `["연 1회 산정 횟수 초과(타 기관 이력)"]`
- [ ] Step 5: 커밋 — `generator: ax_master_values에 외부 이력 사유 — ⑨ 다른 치과 추정 칩`

### Task 2: `backend/missed.py` — 사실·칩·점수

**Files:** Create `backend/missed.py`, `tests/backend/test_missed.py`
**Interfaces:**
- Consumes: `prep.master_values`(값만), `emr_patients.recall_cycle_months`. ④ 도우미 `prep._scaling_unused`·`prep._open_plan`·잔액은 **규칙의 원본으로만** 읽고 호출하지 않는다 — 같은 규칙을 **표마다 한 번** 훑는 쿼리로 다시 쓴다 — 예약 표 한 번으로 마지막 내원·마지막 위생·마지막 예약 상태·그날 이후 예약 여부, 청구 표 한 번으로 올해 스케일링 급여·외부 이력 거절, 수납·진료 한 번으로 잔액, 문자 한 번으로 무응답 연속, 치료계획 한 번(CTE)으로 열린 항목. 환자별 루프 안에서 DB를 부르지 않는다. 쓰는 칸에 인덱스가 있는지 `shared/source_schema.sql`에서 확인하고, 없어서 느리면 보고만 한다(원본 스키마는 고정 — 바꾸지 않는다). 캐시는 두지 않는다(재 보고 느릴 때만 — 사용자 결정)
- Produces:
  - `missed.facts(conn, day: date) -> list[dict]` — 다음 예약 없는 우리 환자마다 `{"patient_id", "chart_no", "name", "age", "phone", "last_visit", "last_hygiene", "balance", "chips": list[str], "signals": dict}`(`signals`는 점수 입력 — 최근성 개월 수·무응답 횟수 등, 키는 빌더 판단). 칩 0개인 환자는 넣지 않는다
  - `missed.score(row: dict, contacts: dict | None, day: date, weights: dict) -> int`
  - `missed.WEIGHTS: dict` + `missed.REASONS: dict[str, str]`(값마다 이유 한 구절 — 화면 설명이 이것을 쓴다)
  - 칩 규칙(설계 §2): 리콜 연체 = 마지막 위생 completed + `recall_cycle_months`×`DAYS_PER_MONTH` < 그날(④ 가족 리콜과 같은 계산), 스케일링 = `prep._scaling_unused`(예정 시술 없음), 노쇼 = 마지막 예약이 `noshow`이고 그 뒤 예약 없음, 미수 = 잔액 > 0, 진료계획 = `prep._open_plan`(예정 항목 없음) 비지 않음, 다른 치과 추정 = (외부 이력 사유로 `denied`·`adjusted`된 청구가 그날 전에 있음) 또는 (그날 전 out 문자 중 `recall`·`reminder`·`appt_confirm`에 그 뒤 `reply` 없음이 연달아 `NO_REPLY_RUN`번 이상 — 값은 빌더가 신호 분포로, 정답 안 봄) 또는 노쇼 칩
  - "다음 예약 없음" = 그날(포함) 이후 `scheduled` 예약 없음, 그날 아침 기준(그날 전에 만든 예약)

- [ ] Step 1: 테스트(합성 소스 — `tests/backend/test_prep.py` `make_source`를 늘리거나 새 도우미):
  `test_same_as_prep_rules`(합성 소스의 몇 환자·날짜에서 스케일링 급여 미사용·남은 치료계획·잔액이 ④ 도우미 결과와 같다 — 테스트에서만 ④ 도우미를 부른다), `test_recall_overdue_chip`, `test_scaling_unused_chip`, `test_noshow_without_rebook_chip`, `test_balance_chip`, `test_open_plan_chip`, `test_other_clinic_by_external_claim`, `test_other_clinic_by_no_reply_run`, `test_future_appointment_excludes`, `test_today_appointment_counts_as_booked`, `test_no_chip_excluded`, `test_facts_before_day_only`(그날 뒤 사건은 안 봄), `test_score_recent_visit_higher`, `test_score_other_clinic_lower`, `test_balance_capped`(미수만 큰 환자 < 최근 방문 + 진료계획 환자), `test_weights_override_from_settings`, `test_query_count_independent_of_patients`(`conn.set_trace_callback`으로 실행 문장 수를 세어 환자 10명·200명일 때 `facts` 쿼리 수가 같다)
- [ ] Step 2: `uv run pytest tests/backend/test_missed.py -q` — FAIL(모듈 없음)
- [ ] Step 3: 구현 — 칩마다 집합 쿼리(모듈 독스트링: 하는 일·원본 — 설계 2026-11-10 미예약 환자, 기능 정의 §5 ⑨, 규칙은 ④ 도우미와 같음)
- [ ] Step 4: `uv run pytest tests/backend/test_missed.py tests/backend/test_prep.py tests/test_no_ground_truth_import.py -q` — PASS
- [ ] Step 5: 커밋 — `backend: 미예약 환자 — 그날 아침 기준 칩 6개와 원칙 점수`

### Task 3: 연락 기록과 목록

**Files:** Modify `shared/app_schema.sql`(새 테이블, 이름 빌더 판단 — 예 `ax_missed_contacts(id, patient_id, result, channel, by, at)`), `backend/missed.py`, `tests/backend/test_missed.py`
**Interfaces:**
- `missed.RESULTS = ("booked", "no_answer", "later", "declined", "other_clinic")` ↔ 화면 문구(Global Constraints), `missed.CHANNELS = ("call", "text")`
- `missed.record_contact(conn, patient_id: int, result: str, channel: str, by: str, at: str) -> None`(목록에 없는 환자·모르는 결과면 ValueError, 커밋은 부르는 쪽)
- `missed.sheet(conn, day: date, now: datetime) -> dict` — `{"date", "chip_counts": {chip: n}, "rows": [...facts + "score", "rank", "contact": {"result","channel","by","at"} | None]}` 점수 내림차순(같으면 마지막 방문 최근 순 → chart_no). 빠지는 규칙: 최근 연락 결과 `booked`(언제든)·`other_clinic`(언제든)·`declined`(`now - 180일` 이후). 감점: `no_answer` 7일·`later` 14일 이내. 같은 환자 기록이 여럿이면 가장 최근 것
- `missed.top(conn, day, now, n=10) -> int` — 오늘 화면 줄용(목록 앞 n명이 있는지 — 수만)

- [ ] Step 1: 테스트 `test_booked_contact_removes`, `test_declined_removes_for_180_days`, `test_other_clinic_contact_removes`, `test_no_answer_lowers_for_7_days`, `test_latest_contact_wins`, `test_record_contact_rejects_unknown`, `test_chip_counts_before_hiding`
- [ ] Step 2: FAIL 확인
- [ ] Step 3: 구현
- [ ] Step 4: PASS, `make test`
- [ ] Step 5: 커밋 — `backend: 미예약 환자 연락 기록 — 결과 5개, 빠짐·감점`

### Task 4: API와 오늘 화면 줄

**Files:** Modify `backend/api.py`, `backend/today.py`, `tests/backend/test_api_missed.py`(새), `tests/backend/test_today.py`
**Interfaces:**
- `GET /missed` → `missed.sheet(conn, clinic.today, now)`; `POST /missed/{patient_id}/contacts` 본문 `{"result", "channel", "part"}` → `{"ok": True}`, ValueError면 400
- `today.sheet` `people`에 `"missed_top": int`(= `missed.top(...)`, 0이면 화면에서 숨김) — 오늘 화면 "사람이 볼 일"의 `미예약 환자 · 상위 10명 → /missed`

- [ ] Step 1: 테스트 `test_get_missed_shape`, `test_post_contact_then_hidden`, `test_post_contact_bad_400`, `test_today_has_missed_top`
- [ ] Step 2: FAIL → Step 3: 구현 → Step 4: PASS(+ `make test`)
- [ ] Step 5: 커밋 — `backend: GET /missed·연락 기록 API, 오늘 화면 미예약 환자 줄`

### Task 5: 화면

**Files:** Create `frontend/lib/missed-api.ts`, `frontend/app/(portal)/missed/page.tsx`(준비표 페이지 꼴 — 머리말: 다시 올 이유가 있는데 다음 예약이 없는 환자를 연락하면 결과가 날 가능성 순으로 보인다), `frontend/components/missed/missed-view.tsx`; Modify `frontend/lib/nav.ts`(`/followup` → `/missed`, 이름 "미예약 환자", `BUILT`에), `frontend/components/today/today-view.tsx`(줄 하나); Test `frontend/e2e/missed.spec.ts`(`/api/missed`는 `page.route` 가짜 응답), `frontend/e2e/today.spec.ts`(줄)
**화면 규칙:** 칩 필터(칩별 인원수 — `chip_counts`), "연락함 숨기기"(연락 기록이 있는 행 숨김), "순위는 어떻게 정해지나요?"(펼치면 `REASONS` 문구 — 응답에 실어 보내거나 API 하나 더, 빌더 판단), 표: 순위·환자(이름 → EMR 열기 `/emr?chart=`, 차트번호·나이·전화)·칩·마지막 내원(몇 개월 전)·미수·연락(결과 5개 고르기 + 전화/문자, 남긴 뒤 결과·시각), 처음 10명 + "더보기"(10명씩, 필터·숨기기 적용 뒤). URL에 칩·숨기기를 둔다(`/missed?chip=…&hide=1`)

- [ ] Step 1: e2e — `처음 10명, 더보기로 10명씩`, `칩 필터와 인원수`, `연락함 숨기기`, `연락 결과 남기면 다시 조회`(POST 가로채 본문 확인), `더보기 뒤 연락함 — 중복 없이 다시 조회`, `순위 설명 펼치기`, 오늘 화면 `미예약 환자 줄 → /missed`, 메뉴 이름 "미예약 환자"
- [ ] Step 2: `cd frontend && npx playwright test e2e/missed.spec.ts e2e/today.spec.ts` — FAIL
- [ ] Step 3: 구현
- [ ] Step 4: `make lint`, `make e2e` — PASS
- [ ] Step 5: 커밋 — `frontend: 미예약 환자 화면 — 칩·점수 순·더보기·연락 기록`

### Task 6: 실험 기록과 문서

**Files:** `docs/치과 AX - AI 실험 기록.md`(새 절), `docs/치과 AX - 기능 정의.md`, `docs/치과 AX - 앱 설계.md`, `docs/치과 AX - 가상 데이터 생성 설계.md`, `docs/치과 AX - 개발자 소개.md`·`프로젝트 소개.md`(진행 상황)
- [ ] Step 1: **과거 데이터 분석 한 번**(설계 §5) — scratchpad 스크립트로(저장소에 남기지 않음) 기준일 전 몇 지점에서 리콜 상태별·칩별 30일 안 새 예약 비율과, 원칙 점수 상위 10명 vs 칩 개수 순 vs 무작위의 같은 비율을 센다. 실험 기록 새 절: 문제(값을 데이터로 맞추려 함) → 원인(가상 데이터엔 연락 효과가 없음 — 예약은 수요 기한에 저절로) → 시도·결과 숫자 → 한계 → 원칙으로 정함. 정답(`truth_*`)은 보지 않는다
- [ ] Step 2: 기능 정의 — 표·§5 ⑨·§9의 "후속 관리" → "미예약 환자", §5 ⑨ 규칙(처음 10명·점수 뜻·미수 +·연락 결과 5개·"1순위" 없음·2단계 보류와 다시 열 때 정할 것), §9 "슬롯 ⑨ 착수 시 — 점수 규칙과 값" 닫기, 착수 순서에 "⑨는 1단계만, 2단계는 ⑤·⑧ 뒤"
- [ ] Step 3: 앱 설계 — §3b·§4 연락 기록 테이블(새 이름, `ax_followup_contacts` 자리), §7.3 "⑨ 연락 기록" 이름, §8 "⑨ 점수 규칙과 값" 닫기(`backend/missed.py` 상수 + `ax_settings`), §9 화면 표 `/followup` → `/missed`
- [ ] Step 4: 가상 데이터 생성 설계 — `ax_master_values` 이름 목록에 `external_adjust_notes`
- [ ] Step 5: `uv run pytest tests/generator/test_doc_sync.py -q` PASS, 커밋 — `docs: 미예약 환자 1단계 — 이름·점수 원칙·연락 기록, 실험 기록`

### 마무리

- [ ] `make fmt` → `make lint` → `make test` → `make e2e` 통과
- [ ] 실데이터 사본에서 `today.sheet`·`missed.sheet`를 돌려 칩별 인원·상위 10명·응답 시간(밀리초)을 적어 보고한다
