# ⑤ 청구 점검·삭감 대응 1단계 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 메뉴 "청구 점검" `/claims` 한 화면 — 탭 `삭감 대응 | 청구 전 점검`. 삭감 대응은 확정 월 조정·불능 청구를 재심 신청 기한 순 5칸 목록 + 칩 6개 + 규칙 판단 제안 + 사람 판단 기록으로, 청구 전 점검은 대기 청구 줄의 상병 확인 경고(규칙)로 보인다. 오늘 화면에 줄 둘. 과금 없음.

**Architecture:** 생성기가 `source.ax_master_values`에 이름 넷(재심 기한 일수·시술별 허용 상병·상세 사유 문구 → 유형·상세 사유 문구 → 청구 기준 절, 그리고 시술 → 요건 사유)을 더해 적재한다(verify 먼저). 새 모듈 `backend/claims.py`가 **표마다 한 번 훑는 집합 쿼리**로 조정 목록·칩·제안과 청구 전 경고를 계산하고, 사람 판단은 새 앱 테이블 `ax_claim_judgments`(청구 id 단위, 덮어씀). `GET /claims`·`GET /claims/pre`·`POST /claims/{claim_id}/judgment`, 오늘 화면은 같은 함수로 센다. 화면은 `frontend/components/claims/`.

**Tech Stack:** Python 3 · FastAPI · sqlite3(SQL 그대로) · pytest / Next.js(App Router, TypeScript strict) · Tailwind 토큰 · shadcn `components/ui/` · Playwright

**Spec:** `docs/superpowers/specs/2026-11-10-claim-review-design.md` — **1단계만**("목표" 1단계, "정한 것" §1, "하면 실패인 것"의 처음 둘과 마지막, "지금 데이터"). §2~§4(AI 점검·근거 찾기·사유서)는 이 계획 밖.

## 시작 전 (브랜치)

- `feat/missed`(⑨ 미예약 환자 1단계)가 main에 머지된 뒤 main에서 `feat/claims-1`을 딴다. 이 계획의 오늘 화면·생성기 Task는 머지 뒤 구조를 전제로 한다 — 시작할 때 아래가 main에 있는지 확인하고, 없으면 멈추고 보고한다:
  - `generator/clinic.py` `master_values_rows`에 `external_adjust_notes`(그 테스트 `tests/generator/test_master_values.py`의 `len(check_master_values(...)) == 4`)
  - `backend/today.py` `sheet`에 `missed_top`이 **없어야** 한다(⑨ 리뷰 반영으로 오늘 화면은 미예약 목록을 계산하지 않음), `frontend/components/today/today-view.tsx`에 `MISSED_LINK_TEXT`와 늘 보이는 미예약 환자 링크 줄
  - `frontend/lib/nav.ts` `BUILT`에 `"/missed"`, `shared/app_schema.sql` 끝에 `ax_missed_contacts`
  - 테스트 도우미 `tests/backend/test_missed.py` `Source`(실제 스키마 + `master_values_rows` 적재, `appt`·`treatment`·`patient`·`app()`)
- ⑧ 재무 대조가 먼저 머지되면 `master_values_rows`·`check_master_values` 개수가 달라질 수 있다 — Task 1의 개수 단언을 그때 값으로 맞춘다

## Global Constraints

- backend는 `ground_truth/`·`generator/`·`evaluation/`을 import하지 않는다(`tests/test_no_ground_truth_import.py`). 정답(`truth_claim_errors`·`truth_chart_notes`·`truth_claim_qa`)을 읽지 않는다 — master 값은 `prep.master_values(conn)`(= `source.ax_master_values`)로만. 테스트는 `generator.master`를 읽어도 된다
- `emr_*`·`fin_*`에 쓰지 않는다. 데이터 상태 고정 — 기존 생성 테이블은 그대로, `ax_master_values`에 이름 추가만(verify 먼저, 절대 규칙). `make data`는 실데이터로 돌리되 LLM 작업 0건으로 끝나는지 먼저 확인
- 사유는 `adjust_note` 문구로 가린다(코드 `C`·`B`는 여러 사유가 공유). 유형은 `master.ADJUST_REASONS`의 `type` — `requirement`/`documentation`/`diagnosis`/`external`
- 규칙 제안(가상 데이터 생성 설계 §2.4, 설계 §1): `diagnosis` → `justified`(재청구 불가) · `external` → `justified` · `documentation` → `resubmit` · `requirement` → 제안 없음("차트 확인 필요" — 2단계 AI 몫). 판단 값 3개 `("justified", "contestable", "resubmit")` ↔ 화면 `정당`·`다툴 만함`·`보완 후 재청구`(`master.ADJUST_JUDGMENT_TARGET` 키와 같은 이름)
- 재심 신청 기한 = `reviewed_at`(통보일) + `appeal_days`(= `master.APPEAL_DAYS` 90), 남은 날 = 기한 − 앱 기준일(`shared/clinic.py`, 지금 2026-12-01). **기한 남음 = 남은 날 ≥ 0**(기한 당일 포함). 꼬리표: 0~30일 위험(`danger`), 31~60일 주의(`warn`)
- 판단 기록: 누가 = 파트(`frontend/lib/part.ts`), 언제 = 실제 시계(`api._stamp(now)`). 상병 착오는 `justified`만, 기한 지난 청구·조정/불능이 아닌 청구는 기록 불가(ValueError → 400)
- 집합 쿼리 — 청구 건마다 DB를 부르지 않는다. 시간 테스트 대신 **쿼리 횟수 테스트**(`conn.set_trace_callback`, ⑨와 같은 꼴). `emr_claims`에 `status` 인덱스는 없다(`ix_claims_month`·`ux_claims_match_key`만) — 원본 스키마는 고정이므로 바꾸지 않고, 느리면 마무리에서 보고만
- LLM 호출 없음. 과금 명령·`make gate`·`next build`를 돌리지 않는다
- 화면에 코드 값(`adjust_code`·`justified`·`diagnosis`·시술 코드)을 보이지 않는다. 색·모양은 `frontend/app/globals.css` 토큰만, 부품은 `components/ui/`만(배지 `danger`·`warn`·`info`·`ok`·`muted`, `tabs`, `button`)
- 주석·독스트링 한국어, 근거 문서 절을 가리킨다(모듈 첫 독스트링에 하는 일과 원본 — 설계 2026-11-10 청구 점검, 기능 정의 §5 ⑤, 가상 데이터 생성 설계 §2.4)
- 커밋 전 `make fmt` → `make lint` → `make test`. `git add`는 파일 이름으로. 커밋 `<영역>: <무엇>` + 본문 한국어 글머리 + 끝에 두 줄:
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`
  `Claude-Session: https://claude.ai/code/session_01G62cGwhYixc5LpBLhj8iGp`
- e2e는 `page.route` 가짜 응답으로 — 실데이터엔 판단 기록·기한 0일·경고 0건 같은 경우가 없다

## Review Focus

1. 상병 착오 건에 "다툴 만함"·"보완 후 재청구"가 기록되면 실패(설계 "하면 실패인 것") — 서버가 막고 화면은 정당만 보인다 → Task 2 `test_diagnosis_only_justified`, Task 5 e2e `상병 착오는 정당만`
2. 기한·남은 날이 `APPEAL_DAYS`와 어긋나거나 기한 당일·하루 지난 날의 경계가 틀림 → Task 2 `test_deadline_boundary`(남은 날 0 = 기한 남음·`soon`, −1 = `expired`), `test_deadline_from_master_values`(`ax_master_values`의 값을 바꾸면 기한이 따라 바뀜 — 상수를 박지 않았는지)
3. 칩 건수·못 받은 금액이 칩끼리 어긋남(상병 착오가 `soon`에 섞임, 기한 지난 건이 `all`에 섞임) → Task 2 `test_chip_membership_and_sums`
4. 청구가 많아지면 쿼리가 늘어남(건마다 판단·과거 조정 조회) → Task 2 `test_query_count_independent_of_claims`(조정 목록·청구 전 점검 둘 다)
5. 오늘 화면 숫자와 `/claims` 칩 숫자가 다른 함수로 세짐 → Task 4 `test_today_claim_lines_match_screen`(같은 DB에서 `today.sheet` 두 숫자 == `claims.adjusted(...)["chips"]["soon"]["count"]`·`claims.pre_claim(...)["counts"]["warning"]`)

---

### Task 1: `ax_master_values`에 ⑤ 값 적재 (생성기, verify 먼저)

**Files:**
- Modify: `generator/clinic.py` (`master_values_rows` — `APPEAL_DAYS`·`PROCEDURE_DIAGNOSES`·`ADJUST_REASONS`, `generator/doc_catalog.py` `CLAIM_HEADINGS` import)
- Test: `tests/generator/test_master_values.py`
- verify `generator/verify/common.py` `check_master_values`는 `master_values_rows`와 이름마다 비교하므로 코드 변경 없음(테스트로 확인)

**Interfaces:**
- Produces(`source.ax_master_values` 이름 → JSON, 이름은 이 계획에서 정함):
  - `appeal_days` → `90` (`master.APPEAL_DAYS`)
  - `procedure_diagnoses` → `{procedure_code: [dx_code, ...]}` (`master.PROCEDURE_DIAGNOSES`, 상병 정렬)
  - `adjust_note_types` → `{adjust_note: type}` (`ADJUST_REASONS` 11개 — `note` → `type`)
  - `adjust_note_headings` → `{adjust_note: 절 소제목}` (`ADJUST_REASONS` 키 → `CLAIM_HEADINGS[키]`, 11개 — 2단계 입력·3단계 카드가 쓴다. 병원이 아는 기준 문서 목차라 정답 누출이 아님 — 설계 §1). 1단계 화면은 "보완 후 재청구"의 채울 항목 이름으로 쓴다
  - `requirement_notes` → `{procedure_code: [adjust_note, ...]}` — `type == "requirement"`인 사유의 `procedures`를 시술별로 뒤집은 것(문구 정렬). `CHART_REQUIREMENT`(항상 요건 줄)와 `RETREATMENT_REQUIREMENT`·`R02`(조건부 요건 줄)를 함께 덮는다 — 2단계가 요건 점검 대상 시술을 거르는 데 씀
- 주석: `master_values_rows` 독스트링에 "⑤ 재심 기한·허용 상병·사유 유형·기준 절·요건 사유" 추가, 각 이름 위에 한 줄(근거 절: 가상 데이터 생성 설계 §2.4·§4.3)

- [ ] **Step 1: Write the failing tests** in `tests/generator/test_master_values.py`
  - `test_master_values_rows_match_master`에 단언 추가:
    - `got["appeal_days"] == APPEAL_DAYS`
    - `got["procedure_diagnoses"] == {p: sorted(dx) for p, dx in PROCEDURE_DIAGNOSES.items()}`
    - `got["adjust_note_types"] == {r["note"]: r["type"] for r in ADJUST_REASONS.values()}` 그리고 `len(...) == len(ADJUST_REASONS)`(문구가 겹치지 않음)
    - `got["adjust_note_headings"] == {r["note"]: CLAIM_HEADINGS[k] for k, r in ADJUST_REASONS.items()}`
  - `test_requirement_notes_cover_chart_requirement` — `CHART_REQUIREMENT`의 `(code, key)`마다 `ADJUST_REASONS[key]["note"] in got["requirement_notes"][code]`, `RETREATMENT_REQUIREMENT`의 문구가 그 사유의 시술 둘(아말감/GI·신경치료) 모두에 있음, 요건 사유가 없는 시술(예: `SCALING_CODE`)은 키 없음
  - `test_check_master_values_flags_missing_and_wrong`의 `len(check_master_values(conn, CTX)) == 4` → `9`
- [ ] **Step 2: Run** `uv run pytest tests/generator/test_master_values.py -q` — Expected: FAIL(KeyError `appeal_days`, 개수 4 ≠ 9)
- [ ] **Step 3: Implement** `master_values_rows`에 다섯 이름
- [ ] **Step 4: Run** `uv run pytest tests/generator/test_master_values.py -q` — Expected: PASS. 그다음 실데이터:
  - `make data`가 LLM 작업 0건으로 끝나는지 확인(작업이 나가면 멈추고 보고 — 이 단계는 마지막 단계 9c라 나가면 안 된다) → `make verify` 통과
  - `sqlite3 data/source.sqlite "select name from ax_master_values order by name"` — 9개(`adjust_note_headings`·`adjust_note_types`·`alert_rules`·`appeal_days`·`external_adjust_notes`·`procedure_diagnoses`·`requirement_notes`·`scaling_code`·`scaling_min_age`), `select value from ax_master_values where name='appeal_days'` = `90`
  - 다른 생성 테이블이 그대로인지: `make data` 전후 `select count(*), sum(claim_amount) from emr_claims`(5,428건)이 같음
- [ ] **Step 5: Commit** — `generator: ax_master_values에 ⑤ 재심 기한·허용 상병·사유 유형·기준 절·요건 사유`

---

### Task 2: `backend/claims.py` — 조정 목록·칩·제안, 청구 전 경고, 판단 기록

**Files:**
- Modify: `shared/app_schema.sql` (새 테이블, 파일 끝)
- Create: `backend/claims.py`
- Test: `tests/backend/test_claims.py` (새 파일 — `tests.backend.test_missed.Source`를 써서 손으로 만든 source에 청구·차트 기록을 더하는 도우미를 이 파일에 둔다)

**Interfaces:**
- Consumes: `prep.master_values(conn)`의 `appeal_days`·`procedure_diagnoses`·`adjust_note_types`·`adjust_note_headings`(Task 1). 테이블 `source.emr_claims`·`emr_patients`(이름·차트 번호)·`emr_procedures`(시술 이름)·`emr_chart_notes`(`treatment_id` → `dx_code`)·`emr_diagnoses`(상병 이름)
- Produces:
  - 테이블
    ```sql
    -- ⑤ 조정 건 사람 판단(기능 정의 §5 ⑤, backend/claims.py) — 청구 id 단위, 다시 누르면 덮어씀.
    -- 판단한 사람 = 파트(frontend/lib/part.ts), 시각은 실제 시계. 재생성 때 버림(앱 설계 §7.3)
    CREATE TABLE ax_claim_judgments (
        claim_id   INTEGER PRIMARY KEY,  -- source.emr_claims.id
        judgment   TEXT NOT NULL CHECK (judgment IN ('justified', 'contestable', 'resubmit')),
        judged_by  TEXT NOT NULL,
        judged_at  TEXT NOT NULL
    );
    ```
  - 상수: `CHIPS = ("all", "diagnosis", "soon", "denied", "adjusted", "expired")`, `JUDGMENTS = ("justified", "contestable", "resubmit")`, `SOON_DAYS = 30`(위험 꼬리표·`soon` 칩), `WATCH_DAYS = 60`(주의 꼬리표), `SUGGEST: dict[str, str | None]`(유형 → 제안, Global Constraints), `SUGGEST_REASON: dict[str, str]`(유형 → 이유 한 줄 — 화면 ②가 그대로 쓴다. 예: diagnosis "상병 착오는 재청구할 수 없습니다 — 다음 청구 때 상병을 확인하세요", external "다른 기관 이력이라 우리 청구의 잘못이 아닙니다", documentation "빠진 기재·증빙을 채워 다시 청구할 수 있습니다", requirement "차트에 요건 근거가 있으면 다툴 만함, 없으면 정당입니다")
  - `claims.adjusted(conn, today: date) -> dict` — `{"date", "appeal_days", "chips": {chip: {"count": int, "amount": int}}, "rows": [...]}`. 대상: `status IN ('adjusted', 'denied')` 전부(기한 지난 건 포함). 행:
    `{"claim_id", "chart_no", "name", "service_date", "tooth_no", "procedure_name", "status", "claim_amount", "paid_amount", "unpaid", "adjust_note", "type", "reviewed_at", "deadline", "days_left", "expired": bool, "chips": list[str], "suggestion": str | None, "reason": str, "fill_item": str | None, "judgment": {"value", "by", "at"} | None}`
    - `unpaid` = `claim_amount - paid_amount`(불능은 `paid_amount` 0 — 실데이터 확인함)
    - 행 칩: 기한 남음이면 `all` + (`type == "diagnosis"`면 `diagnosis`, 아니고 `days_left <= SOON_DAYS`면 `soon`) + `status`(`denied`/`adjusted`). 기한 지남이면 `["expired"]`만
    - `fill_item` = `suggestion == "resubmit"`일 때 `adjust_note_headings[note]`, 아니면 None
    - 정렬: 기한 남은 행은 기한 이른 순(같으면 `claim_id`), 기한 지난 행은 그 뒤에 기한 늦은 순(최근에 지난 것 먼저 — 아래 "내 판단")
    - 칩 집계는 행 칩으로 센다(`amount` = 그 칩 행의 `unpaid` 합)
  - `claims.pre_claim(conn, today: date) -> dict` — `{"counts": {"lines": int, "warning": int}, "rows": [...]}`. 대상: `status = 'pending'` 청구 줄 전부. 행: `{"claim_id", "service_date", "chart_no", "name", "procedure_name", "tooth_no", "claim_amount", "dx_code", "dx_name", "warning": {"allowed": [상병 이름, ...], "past_count": int, "past_amount": int} | None}`. 경고 = `dx_code not in procedure_diagnoses[procedure_code]`. `past_*` = 같은 `procedure_code` × 차트 상병으로 `type == "diagnosis"` 사유로 깎인(`adjusted`·`denied`, `reviewed_at < today`) 청구의 건수·`unpaid` 합 — `(procedure_code, dx_code)`로 묶은 **쿼리 한 번**. 정렬: 경고 먼저 → 진료일 → `claim_id`
  - `claims.record_judgment(conn, claim_id: int, judgment: str, by: str, at: str, today: date) -> None` — 덮어씀(`INSERT ... ON CONFLICT(claim_id) DO UPDATE`). ValueError: `judgment not in JUDGMENTS`, 청구가 없거나 `adjusted`/`denied`가 아님, 기한 지남, `type == "diagnosis"`인데 `justified`가 아님. 커밋은 부르는 쪽

- [ ] **Step 1: Write the failing tests** in `tests/backend/test_claims.py` (기준일 `DAY = date(2026, 12, 1)`, 문구는 `generator.master.ADJUST_REASONS`에서 유형별로 하나씩 꺼낸다)
  - 도우미 `add_claim(src, pid, *, day, code, status, note=None, reviewed=None, amount=10000, paid=None, dx="D06", tooth="00") -> int` — `src.appt`·`src.treatment`로 진료, `emr_diagnoses`(없으면 `master.DIAGNOSES` 전부 한 번 넣기)·`emr_chart_notes`·`emr_claims`
  - `test_deadline_and_days_left` — 통보 2026-09-09 → `deadline == "2026-12-08"`, `days_left == 7`(설계 `12/08 · 7일 남음`)
  - `test_deadline_boundary` — 통보일 = 기준일 − 90일 → `days_left == 0`, `"soon" in chips`; − 91일 → `chips == ["expired"]`, `expired is True`
  - `test_deadline_from_master_values` — `source.ax_master_values`의 `appeal_days`를 `30`으로 바꾼 DB에서 같은 청구의 `deadline`이 30일 뒤
  - `test_chip_membership_and_sums` — 상병 착오(기한 10일)·서류(기한 10일, 불능)·요건(기한 45일)·기한 지남 한 건씩: `diagnosis` 칩에만 상병 착오, `soon`은 서류 한 건, `all` 3건(기한 지남 제외), `expired` 1건, `denied`/`adjusted` 건수, 각 칩 `amount` == 그 행 `unpaid` 합
  - `test_unpaid_full_denial` — 불능 `unpaid == claim_amount`
  - `test_suggestion_by_type` — parametrize 네 유형 → `suggestion` `justified`/`justified`/`resubmit`/`None`, `reason == SUGGEST_REASON[type]`, 서류 유형만 `fill_item == CLAIM_HEADINGS[...]`(테스트에서 `generator.doc_catalog` 읽음)
  - `test_sort_by_deadline` — 기한 남은 행 기한 이른 순, 기한 지난 행은 뒤에 늦은 순
  - `test_record_and_overwrite` — 기록 → `judgment == {"value": "contestable", "by": "데스크", "at": ...}`, 다시 기록하면 덮어씀(테이블 1행)
  - `test_diagnosis_only_justified` — 상병 착오 건에 `contestable`·`resubmit` → ValueError, `justified`는 됨
  - `test_expired_rejects_judgment` — 기한 지난 건 ValueError, 그 전에 남긴 판단은 행에 그대로 보인다(직접 INSERT 후 `adjusted` 행의 `judgment`)
  - `test_record_rejects_unknown` — 모르는 값·`paid`/`pending` 청구·없는 id → ValueError
  - `test_pre_claim_warning` — 대기 줄 둘(스케일링 × D06 허용, 스케일링 × D07 밖) → `counts == {"lines": 2, "warning": 1}`, 경고 행이 먼저, `allowed == ["만성 치은염", "치아 침착물(치석)"]`(상병 코드 순 D04·D06)
  - `test_pre_claim_past_history` — 같은 스케일링 × D07 조합으로 상병 착오 불능 2건(₩10,000·₩12,000) → `past_count == 2`, `past_amount == 22000`; 다른 조합·서류 사유로 깎인 건·기준일 이후 통보는 안 셈
  - `test_query_count_independent_of_claims` — 청구 10건·200건 source에서 `adjusted`와 `pre_claim`의 실행 문장 수가 각각 같다(⑨ `_count` 꼴)
- [ ] **Step 2: Run** `uv run pytest tests/backend/test_claims.py -q` — Expected: FAIL(모듈 없음)
- [ ] **Step 3: Implement** — 조정 목록은 청구·환자·시술·`ax_claim_judgments` LEFT JOIN 한 쿼리 + master 값 한 번, 날짜 계산·칩은 파이썬. 청구 전 점검은 대기 줄 조인 한 쿼리 + 과거 상병 착오 집계 한 쿼리(`adjust_note IN (상병 유형 문구들)`)
- [ ] **Step 4: Run** `uv run pytest tests/backend/test_claims.py tests/test_no_ground_truth_import.py -q` — Expected: PASS, 그리고 `make test`
- [ ] **Step 5: Commit** — `backend: 청구 점검 — 재심 신청 기한·칩·규칙 제안, 상병 확인 경고, 판단 기록`

---

### Task 3: API

**Files:**
- Modify: `backend/api.py` (⑨ 절 뒤에 "⑤ 청구 점검" 절)
- Test: `tests/backend/test_api_claims.py` (새 파일 — `tests.backend.test_api_calls._client`, Task 2 도우미)

**Interfaces:**
- Consumes: `claims.adjusted`·`claims.pre_claim`·`claims.record_judgment`(Task 2), `ClinicDep`·`Paths`·`Now`·`_open_rw`·`_stamp`(⑨ 라우트와 같은 꼴)
- Produces:
  - `GET /claims` → `claims.adjusted(conn, clinic.today)` — **칩 인자 없이 전부**(기한 지난 건 포함, 지금 633행)를 보내고 화면이 행의 `chips`로 거른다. 이유: 칩 집계를 서버 한 함수가 내고 오늘 화면도 그 함수를 쓴다, 행 수가 작아(수백) 칩을 바꿀 때 다시 부를 필요가 없다, ⑨ `GET /missed`와 같은 꼴
  - `GET /claims/pre` → `claims.pre_claim(conn, clinic.today)`
  - `POST /claims/{claim_id}/judgment` 본문 `{"judgment": str, "part": str}` → `{"ok": True}`, ValueError면 400 — `at = _stamp(now)`

- [ ] **Step 1: Write the failing tests** — `test_get_claims_shape`(`set(r) == {"date", "appeal_days", "chips", "rows"}`, `set(r["chips"]) == set(claims.CHIPS)`), `test_get_pre_shape`(`set(r) == {"counts", "rows"}`), `test_post_judgment_then_shown`(POST 뒤 GET 행 `judgment.value`·`by == "데스크"`), `test_post_judgment_bad_400`(모르는 값, 상병 착오에 `contestable`, 기한 지난 건 — 각각 400)
- [ ] **Step 2: Run** `uv run pytest tests/backend/test_api_claims.py -q` — Expected: FAIL(404)
- [ ] **Step 3: Implement** 세 라우트(독스트링: 앱 기준일·실제 시계 구분)
- [ ] **Step 4: Run** `uv run pytest tests/backend/test_api_claims.py -q` — Expected: PASS, `make test`
- [ ] **Step 5: Commit** — `backend: GET /claims·/claims/pre, 판단 기록 API`

---

### Task 4: 오늘 화면 줄 둘 (backend)

**Files:**
- Modify: `backend/today.py` (모듈 독스트링의 "같은 함수" 목록, `sheet`의 `people`) — **머지 뒤 구조 기준**: `people`에 `missed_top`은 없다(미예약 환자는 늘 보이는 링크). 리베이스·머지 뒤 `sheet`를 다시 읽고 붙인다
- Test: `tests/backend/test_today.py`, `tests/backend/test_api_today.py`(응답 키 단언이 있으면 맞춤)

**Interfaces:**
- Consumes: `claims.adjusted`·`claims.pre_claim`(Task 2)
- Produces: `today.sheet(...)["people"]`에
  - `"appeal_soon": claims.adjusted(conn, today)["chips"]["soon"]["count"]` — 오늘 화면 `재심 신청 기한 임박 n건 → /claims?chip=soon`
  - `"preclaim_warning": claims.pre_claim(conn, today)["counts"]["warning"]` — `청구 전 상병 경고 n건 → /claims?tab=pre`

- [ ] **Step 1: Write the failing tests** in `tests/backend/test_today.py` — `test_today_claim_lines_match_screen`(Task 2 도우미로 기한 10일 서류 조정 1건·기한 10일 상병 착오 1건·경고 대기 줄 1건 → `appeal_soon == 1`(상병 착오 제외), `preclaim_warning == 1`, 둘 다 `claims` 함수 결과와 같음), `test_today_claim_lines_zero`(청구 없음 → 둘 다 0, 500 없음)
- [ ] **Step 2: Run** `uv run pytest tests/backend/test_today.py -q` — Expected: FAIL(KeyError)
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run** `uv run pytest tests/backend/test_today.py tests/backend/test_api_today.py -q` — Expected: PASS, `make test`
- [ ] **Step 5: Commit** — `backend: 오늘 화면 재심 기한 임박·청구 전 상병 경고 줄`

---

### Task 5: 청구 점검 화면과 오늘 화면 줄 (frontend)

**Files:**
- Create: `frontend/lib/claims-api.ts` (응답 타입 = Task 2 모양, `CHIP_LABEL`, `JUDGMENT_LABEL`, `fetchClaims`, `fetchPreClaim`, `postJudgment(claimId, judgment, part)` — `@/lib/api`의 `getJson`·`postJson`)
- Create: `frontend/app/(portal)/claims/page.tsx` (준비표·미예약 환자 페이지와 같은 머리말·`Suspense` 꼴 — 머리말 한 줄: 깎인 청구를 재심 신청 기한 순으로 보이고, 청구 전에 상병을 확인한다. 청구 수정·재심 제출은 사람이 한다)
- Create: `frontend/components/claims/claims-view.tsx` (탭·URL 상태), `frontend/components/claims/adjust-table.tsx`(삭감 대응), `frontend/components/claims/pre-claim-table.tsx`(청구 전 점검)
- Modify: `frontend/lib/nav.ts` (`BUILT`에 `"/claims"` — "준비 중" 대신 화면), `frontend/lib/today-api.ts` (`people.appeal_soon`·`preclaim_warning`), `frontend/components/today/today-view.tsx` (`People`에 `count(...)` 둘 — 머지 뒤 다른 숫자 줄과 같은 꼴)
- Test: `frontend/e2e/claims.spec.ts` (새 파일, `/api/claims`·`/api/claims/pre`는 `page.route` 가짜 응답), `frontend/e2e/today.spec.ts` (줄 둘)

**Interfaces:**
- Consumes: `GET /claims`·`GET /claims/pre`·`POST /claims/{claim_id}/judgment`(Task 3), `readPart`(`frontend/lib/part.ts`), 오늘 화면 `people.appeal_soon`·`preclaim_warning`(Task 4)
- Produces: 주소 상태 `/claims?tab=adjust|pre&chip=<칩>`(기본 `tab=adjust`·`chip=all`, 청구 전 점검 기본 `chip=warning`), 오늘 화면 이동 `/claims?chip=soon`·`/claims?tab=pre`

**화면 규칙(설계 §1):**
- 탭 `삭감 대응 | 청구 전 점검`, 기본 삭감 대응
- **삭감 대응 칩**(문구 그대로): `전체`(기한 남음) · `상병 착오` · `신청 기한 30일 이내` · `전액 불능` · `일부 조정` · `기한 지남`, 칩마다 건수, 오른쪽에 고른 칩의 `못 받은 돈 합계 ₩…`
- **목록 5칸**, 기한 순(서버 순서 그대로):
  - `재심 신청 기한` — `12/08 · 7일 남음` + 30일 이내 `danger` 배지 `30일 이내`, 60일 이내 `warn` 배지 `60일 이내`. 상병 착오는 날짜 대신 `재청구 불가` 배지. 기한 지난 건 `08/09 · 기한 지남`
  - `환자 · 시술` — 이름 · 시술 이름, 아래 줄에 진료일·치아(`00`이면 치아 없음)
  - `못 받은 돈` — `₩12,340`, 불능이면 뒤에 `(전액)`
  - `왜 깎였나` — `adjust_note` 문구 그대로(코드 없이)
  - `할 일` — 판단이 있으면 `다툴 만함 · 데스크 11/09`(사람이 정함 표시), 없으면 `보완 후 재청구(제안)`·`정당(제안)`·`차트 확인 필요`, 기한 지난 건에 판단이 없으면 `—`
- **행 펼침**(행을 누르면 그 아래, 하나씩): ① **왜 깎였나** — 사유, `청구 ₩a → 지급 ₩b`, `일부 조정`/`전액 불능`, 통보일, 재심 신청 기한 ② **어떻게 할까** — 제안 + `reason` 한 줄, 보완 후 재청구 제안이면 `채울 항목: {fill_item}`, 판단 버튼 `정당`·`다툴 만함`·`보완 후 재청구`(상병 착오는 `정당`만, 기한 지난 건은 버튼 없이 남긴 판단만). 누르면 POST 뒤 다시 조회, 누르는 중 버튼 꺼짐 ③ 재심 사유서 칸은 1단계에서 두지 않는다
- **청구 전 점검**: 칩 `청구 전 n줄`(전부) · `상병 확인 경고(규칙) n`(기본). AI 칩은 두지 않는다(2단계). 표 칸 `진료일 · 환자(이름 → /emr?chart=) · 시술(치아) · 청구액 · 차트 상병 · 경고`. 경고 칸: `허용 상병 밖 — 이 시술은 {allowed 이름들}`, `past_count > 0`이면 아래 줄 `지난 조정에서 이 조합 n건, ₩x 못 받음`
- 오늘 화면 줄: `재심 신청 기한 임박 n건`(→ `/claims?chip=soon`, 아래 글 "30일 이내 · 상병 착오 제외"), `청구 전 상병 경고 n건`(→ `/claims?tab=pre`), 0이면 숨김

- [ ] **Step 1: Write the failing e2e** in `frontend/e2e/claims.spec.ts` (가짜 응답: 기한 7일 서류 불능 1·기한 38일 요건 조정 1·상병 착오 1·기한 지남(판단 있음) 1, 청구 전 줄 2(경고 1, 과거 이력 있음)):
  - `메뉴에서 청구 점검이 열리고 기본은 삭감 대응` — 메뉴 링크 → `/claims`, 탭 `삭감 대응` 선택됨, "준비 중" 없음
  - `5칸과 기한 표시` — 머리 5칸 이름 그대로, `12/08 · 7일 남음`·`30일 이내`·`60일 이내`·`(전액)`, 상병 착오 행 `재청구 불가`, 코드 값(`C`·`justified`·`U0001`) 안 보임
  - `칩 건수와 합계` — `신청 기한 30일 이내`를 누르면 1행·합계가 그 행 금액, URL `?chip=soon`, `기한 지남`은 판단만 보이고 버튼 없음
  - `상병 착오는 정당만` — 펼치면 `정당` 버튼 하나, `다툴 만함`·`보완 후 재청구` 없음
  - `판단을 누르면 기록되고 할 일 칸이 바뀜` — POST `/api/claims/{id}/judgment` 가로채 본문 `{"judgment": "contestable", "part": …}` 확인, 두 번째 GET 응답에 판단을 넣어 `다툴 만함 · 데스크 11/09`
  - `보완 후 재청구 제안은 채울 항목` — 서류 행 펼침에 `채울 항목:`
  - `청구 전 점검 — 경고 칩 기본, 과거 이력 문구` — `/claims?tab=pre` → 경고 1행, `지난 조정에서 이 조합 2건, ₩22,000 못 받음`, `청구 전 2줄`을 누르면 2행, 이름 링크가 `/emr?chart=`
  - `today.spec.ts`: `재심 기한 임박·청구 전 상병 경고 줄 → /claims` — 가짜 `/api/today`에 둘 다 > 0이면 두 줄과 이동 URL, 0이면 숨김
- [ ] **Step 2: Run** `cd frontend && npx playwright test e2e/claims.spec.ts e2e/today.spec.ts` (`make dev`를 끄고) — Expected: FAIL
- [ ] **Step 3: Implement** 화면·API 래퍼·`nav.ts`·오늘 화면 줄
- [ ] **Step 4: Run** `make lint`, `make e2e` — Expected: PASS
- [ ] **Step 5: Commit** — `frontend: 청구 점검 화면 — 삭감 대응 5칸·칩·판단 기록, 청구 전 상병 경고`

---

### Task 6: 문서

**Files:**
- Modify: `docs/치과 AX - 기능 정의.md`
  - §5 ⑤ — 정한 규칙: 조정 판단은 사유 유형 규칙 제안(상병 착오·외부 이력 → 정당, 서류 → 보완 후 재청구) + 사람 기록, **요건 사유는 코드로 정해지지 않아 ⑤-2 AI(2단계)가 조정 건에도 제안**, 재심 근거 조사 **에이전트는 두지 않음**(판단은 규칙 + 요건 AI, 근거는 ⑤-3 검색·차트 문장 고르기). 조정 목록 5칸·기한 남은 건 대상·`기한 지남` 칩(판단 없이 기록만)·칩 6개. ⑤-2 상병 확인 경고 문구 "지난달 이 조합으로" → "지난 조정에서 이 조합 n건". 단계 나눔(1 코드 → 2 ⑤-2 AI → 3 ⑤-3 근거 → 4 ⑤-4 사유서)과 화면에 시스템 말 없음. §2 범위 표 ⑤ 행의 "재심 근거 조사는 에이전트"와 §3 표 "⑤ 청구 | 재심 근거 조사" 행을 이에 맞게 고친다
  - §9 — "슬롯 ⑤ 착수 시" 줄을 ⑤ 2~4단계 항목으로 바꾼다: ⑤-2 채점 범위(전 기간 급여 청구 줄, 기간별, 추가 처치 누락 실데이터 test 전용 + 평가 전용 합성 사례), `truth_claim_qa`는 AI 초안 + 사람 검토 뒤 정답 저장소, dev/test는 **사유(절) 단위**(청구 기준 문서가 5개뿐이라 — "③의 문서 단위 격리와 같은 이유" 문구를 고친다), 다음 단계 계획은 앞 단계 머지 뒤
- Modify: `docs/치과 AX - 앱 설계.md`
  - §5 ⑤ — `backend/claims.py`(집합 쿼리), 앱 테이블 `ax_claim_judgments`(청구 id·판단·파트·실제 시각, 덮어씀), API `GET /claims`·`GET /claims/pre`·`POST /claims/{claim_id}/judgment`(`{judgment, part}`, 상병 착오의 정당 외·기한 지남·모르는 값 400)
  - §4 오늘 화면 — 재심 기한 임박·청구 전 상병 경고는 `backend/claims.py` `adjusted`·`pre_claim`(그 화면과 같은 함수)
  - §7.3 "버림"에 `⑤ 판단 기록(ax_claim_judgments)` — 재생성 뒤 청구 id가 다른 청구를 가리킬 수 있다(⑧ 확정·⑨ 연락 기록과 같은 처리)
  - §8 미결 `master` 값 항목에서 ⑤ 몫(`PROCEDURE_DIAGNOSES`·`APPEAL_DAYS`) 지움 — 나머지(⑧·⑤-2 나이 규칙·박혀 있는 값)는 남김
  - §9 화면 표 `/claims` 행 — `삭감 대응(조정 목록 5칸·칩·행 펼침·판단 기록), 청구 전 점검(상병 확인 경고)` · `GET /claims`·`GET /claims/pre`·`POST /claims/{claim_id}/judgment`(`backend/claims.py`), 주소 `?tab=pre&chip=…`
- Modify: `docs/치과 AX - 가상 데이터 생성 설계.md` §3 `ax_master_values` 이름 목록에 ⑤ 다섯(`appeal_days`·`procedure_diagnoses`·`adjust_note_types`·`adjust_note_headings`·`requirement_notes`, 각 원본 이름)
- Modify(진행 상황 줄이 있으면): `docs/치과 AX - 프로젝트 소개.md`·`docs/치과 AX - 개발자 소개.md` — ⑤ 1단계
- `docs/치과 AX - AI 실험 기록.md`는 손대지 않는다(이번 브랜치는 AI 실험이 없다)

- [ ] **Step 1:** 위 문장을 고친다(규칙만 — 결정 날짜·경위 없이, 값은 코드 이름으로 가리킨다)
- [ ] **Step 2: Run** `uv run pytest tests/generator/test_doc_sync.py -q` — Expected: PASS
- [ ] **Step 3: Commit** — `docs: 청구 점검 1단계 — 판단 규칙·5칸 목록·단계 나눔, 판단 기록 테이블`

---

### 마무리

- [ ] `make fmt` → `make lint` → `make test` → `make e2e` 통과
- [ ] 실데이터(`make dev`)로 `/claims`·`/today`를 열어 숫자를 대조하고 `GET /claims`·`GET /claims/pre` 응답 시간(밀리초)을 재 보고한다. 기대값(기준일 2026-12-01, 지금 데이터):
  - 칩 `all` 101건 ₩2,595,660 · `diagnosis` 15건 ₩847,710 · `soon` 36건 ₩744,430 · `denied` 25건 ₩1,029,000 · `adjusted` 76건 ₩1,566,660 · `expired` 532건 ₩22,776,670. 남은 날 7~69일, 31~60일 꼬리표 19건
  - 청구 전 점검: 218줄, 상병 확인 경고 7줄(그중 지난 조정 이력 있는 조합 3줄)
  - 오늘 화면: 재심 신청 기한 임박 36건, 청구 전 상병 경고 7건
- [ ] PR 전 `pj2-doc-tidy`(바뀐 코드·문서 목록과 이 계획의 "내 판단"을 넘김)

## 이 계획에서 정한 것 (설계가 빌더에게 맡겼거나 비어 있던 곳)

- `ax_master_values` 이름 다섯, 앱 테이블 `ax_claim_judgments`, API 모양(칩은 화면이 거름 — Task 3 이유)
- 기한 경계: 남은 날 0(기한 당일)까지 기한 남음·`soon`. 꼬리표 0~30 위험, 31~60 주의
- 기한 지난 칩의 정렬은 기한 늦은 순(사용자 결정 — 설계의 "기한 이른 순"은 기한 남은 목록에 적용 — 지난 건을 오래된 것부터 보이면 2025년 건이 앞에 온다)
- 상병 착오 행은 기한 칸에 날짜 대신 `재청구 불가`, 판단 버튼은 `정당` 하나만 보인다(다른 둘을 꺼 둔 채 보이지 않음)
- 청구 전 점검 탭 칩에서 `서술 있는 줄 n`은 두지 않는다(사용자 결정) — 설계 §2가 서술/템플릿 구분을 앱에서 하지 않기로 정했고(정답 DB에만 있음), 1단계 앱이 셀 방법이 없다. 기본 칩은 `상병 확인 경고(규칙)`
- "지난 조정" = 상병 유형 사유로 깎이고 통보일이 기준일 전인 청구 전부(기간 제한 없음)
- `adjust_note_headings`는 절 소제목만(문서 제목은 3단계에서 `ax_documents`로 찾는다), "보완 후 재청구"의 채울 항목 이름으로도 쓴다
- `requirement_notes`는 `CHART_REQUIREMENT`가 아니라 요건 유형 사유의 시술 목록에서 만든다 — 조건부 요건(같은 부위 재시행 R01·치석제거 연결 R02)까지 덮어 2단계가 "후보 절을 모두 넣는" 데 그대로 쓴다
- 재생성 때 판단 기록은 버림(사용자 결정, 앱 설계 §7.3)
