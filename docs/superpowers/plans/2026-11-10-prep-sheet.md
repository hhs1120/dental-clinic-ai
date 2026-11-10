# ④ 진료 준비표 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 그날 예약 환자마다 코드가 모은 사실·칩 7개와 후검증한 AI 제안을 `ax_prep_sheets`에 만들고, 준비표·체어 보기 두 화면과 `eval.py` 채점으로 보인다.

**Architecture:** 생성기가 `master` 기준표를 `source.sqlite` `ax_master_values`에 적재 → `backend/prep.py`가 사실·칩(`prep_facts`)과 제안(`suggest` + 후검증)을 만들어 저장 → `GET /prep`을 Next.js `/prep`이 준비표·체어 보기로 그린다 → `evaluation/prep.py`가 네 지표를 낸다.

**Tech Stack:** Python 3 + sqlite3(SQL 그대로) + FastAPI, 게이트웨이 `shared/llm.py`, Next.js(Tailwind + shadcn `components/ui`), pytest·Playwright

**Spec:** `docs/superpowers/specs/2026-11-10-prep-sheet-design.md`

## Global Constraints

- `backend/`는 `generator/`·`evaluation/`·`ground_truth/`를 import하지 않는다(`tests/test_no_ground_truth_import.py`)
- 병원 상수의 원본은 `generator/master.py` — backend는 `source.ax_master_values`로만 읽는다
- 데이터 상태 고정: 기존 `emr_*`·`fin_*`·기존 `ax_*` 내용은 바뀌면 안 된다. 새 생성 단계는 `STAGES` 맨 끝에(단계별 독립 시드)
- verify 검사를 생성 코드보다 먼저
- 제안 모델 별칭 `chat`(`config/llm.yaml` "③ 챗·④ 제안"), task `prep`, 프롬프트 `prompts/prep/v1.md`
- 칩은 코드만, LLM 입력에서 예약 미확인 제외, `none`은 `used_facts`가 비어야 통과
- 화면은 `globals.css` 토큰·`components/ui`만, 회의 보기 없음
- 커밋: `<영역>: <무엇>`, `git add`는 파일 이름으로, 코드 고치면 `make fmt` → `make lint` → `make test`
- 주석·독스트링 한국어, 근거 문서 절을 가리킨다

## Review Focus

1. 휴진일·예약 없는 날짜 — `prep_facts`가 빈 목록, `make prep`이 호출 없이 지나가고 화면은 "예약 없음"(Task 2·3·5에 테스트)
2. 제안 본문의 금액 표기 "3만 원"·"30,000원" — 단위 정규화 뒤 사실 값 30000과 같으면 통과, 사실에 없으면 실패(Task 3)
3. 신환(마지막 내원 없음)·가족 없음·주의사항 없음 환자 — 사실이 비어도 행이 깨지지 않고 칩이 꺼진다(Task 2)
4. "다시 만들기"를 작업이 도는 중에 또 누름 — 같은 날짜의 돌고 있는 작업을 돌려주고 두 번 돌지 않는다(Task 4)
5. 체어 보기에서 "지금"이 첫 예약 전·마지막 예약 뒤 — 첫 예약 전엔 첫 환자를 "다음", 끝난 뒤엔 마지막 환자를 선택(표시 없음)(Task 6)

---

### Task 1: 기준표 `ax_master_values` 적재

**Files:**
- Modify: `shared/source_schema.sql` (테이블 추가), `generator/clinic.py` (`stage_master_values`), `generator/build.py` (`STAGES` 끝에 `("master_values", clinic.stage_master_values)`), `generator/verify/common.py` (`check_master_values`), `generator/verify/__init__.py` (`REQUIRED`에 추가), `docs/치과 AX - 가상 데이터 생성 설계.md` (테이블·검사 목록)
- Test: `tests/generator/test_master_values.py`

**Interfaces:**
- Produces: `source.ax_master_values(name TEXT PRIMARY KEY, value TEXT NOT NULL)` — 행 3개
  - `alert_rules`: `{code: {"action": str, "procedures": [procedure_code, …정렬]}}` (`ALERT_TYPES`)
  - `scaling_min_age`: `19` (`SCALING_MIN_AGE`)
  - `scaling_code`: `"U0001"` (`SCALING_CODE`)
- Produces: `master_values_rows() -> list[tuple[str, str]]` in `generator/clinic.py` — 적재할 (name, JSON) 목록, verify와 생성이 같이 쓴다

- [ ] **Step 1: 실패하는 검사 테스트** — `test_check_master_values_flags_missing_and_wrong`: 빈 테이블이면 위반 행 3개, `scaling_min_age`를 `20`으로 넣으면 그 이름 1행. `test_master_values_rows_match_master`: `json.loads` 한 값이 `ALERT_TYPES`의 action·정렬된 procedures, `SCALING_MIN_AGE`, `SCALING_CODE`와 같다
- [ ] **Step 2: 실행해 실패 확인** — `uv run pytest tests/generator/test_master_values.py -v` → FAIL(이름 없음)
- [ ] **Step 3: 검사 구현** — `check_master_values(conn, ctx) -> list` (독스트링 = 실패 때 출력되는 규칙: "ax_master_values = generator/master.py"), 스키마 테이블, `master_values_rows`
- [ ] **Step 4: 생성 단계 구현** — `stage_master_values(run, rng, fake)`가 `master_values_rows()`를 넣는다. 테스트 통과 확인
- [ ] **Step 5: 결정성·기존 테이블 불변** — `uv run pytest tests/generator -q` → 통과(기존 결정성 테스트가 다른 테이블 해시를 지킨다)
- [ ] **Step 6: 실데이터** — `make data` → LLM 작업 0건으로 끝남(과금 없음) → `make verify` 통과. `git status`에 `generator/llm/cache/` 변경 없음 확인
- [ ] **Step 7: 문서** — 가상 데이터 생성 설계에 `ax_master_values`(용도: backend가 읽는 master 값, 원본은 master)와 검사 이름
- [ ] **Step 8: Commit** — `generator: master 기준표를 ax_master_values로 적재(④ 주의 칩)`

### Task 2: 사실·칩 `prep_facts`

**Files:**
- Create: `backend/prep.py`
- Test: `tests/backend/test_prep.py`, 스냅샷 `tests/backend/snapshots/prep_facts.json`

**Interfaces:**
- Consumes: `source.ax_master_values`(Task 1)
- Produces (in `backend/prep.py`):
  - `master_values(conn) -> dict` — `{"alert_rules": {code: {"action", "procedures": set}}, "scaling_min_age": int, "scaling_code": str}`
  - `prep_facts(conn, day: date) -> list[dict]` — 그날 예약(취소 제외) 시각순. 행 모양(키 고정 — 스냅샷):
    ```
    {"appointment_id", "patient_id", "time": "HH:MM", "duration_min", "chair_no": int|None,
     "type": "treatment|hygiene|consult", "procedure": 이름, "procedure_code", "staff", "status", "confirmed": bool,
     "patient": {"name", "chart_no", "age", "sex", "new_patient": bool},
     "last_visit": "YYYY-MM-DD"|None, "balance": int,
     "alerts": [{"code", "label", "related": bool, "action"}],
     "scaling_unused": bool, "family_recall": [{"chart_no", "name", "last_hygiene"}],
     "open_plan": [{"procedure", "tooth", "est_fee"}],
     "chips": [CHIPS 중 켜진 것, CHIPS 순서]}
    ```
  - `CHIPS = ("appointment", "insurance", "balance", "family_recall", "alert", "open_plan", "unconfirmed")`
- 규칙(스펙 §2 — 모두 "그날 개원 전"): 미수 = 그날 전 수납 합(음수는 0), 마지막 내원 = 그날 전 `completed`, 신환 = 그날 전 `completed` 없음, 주의사항 = 기록일 ≤ 그날 < 끝 / `related` = 예정 시술이 `procedures`에 있음, `scaling_unused` = 나이 ≥ `scaling_min_age` and 그해 그날 전 스케일링 급여 청구 없음 **and 그날 예정 시술 ≠ `scaling_code`**, `family_recall` = 가족 구성원 중 (그날 전 마지막 `completed` 위생 + `recall_cycle_months`×(365.25/12)일, 반올림) < 그날 **이고 그날 이후(그날 포함) `scheduled`·`completed` 위생 예약이 없는** 사람, `open_plan` = 그날 전에 만든 계획의 항목 중 그날 기준 열린 것(끝남 = 그날 전 `completed` 연결 예약, 대체됨 = 그날 전 다른 계획의 `completed` 진료가 같은 치아 — 전체 구강 시술(`tooth_no`='00')은 같은 시술코드) **에서 그날 예정 시술 항목(`plan_item_id`) 제외**
- 칩: `appointment` 항상, `insurance` = `scaling_unused`, `balance` = >0, `family_recall` = 목록 있음, `alert` = `related` 하나 이상, `open_plan` = 목록 있음, `unconfirmed` = not `confirmed`(지난 날짜는 끔 — 기준일과 그 뒤만)

- [ ] **Step 1: 실패하는 테스트** — `tests/backend/test_emr.py`의 `emr_paths`처럼 손으로 만든 source(실제 스키마)로:
  - `test_alert_related_only_for_planned_procedure` — 같은 주의사항이 예정 시술에 따라 `related` 참/거짓, `alert` 칩
  - `test_alert_window` — 끝나는 날 당일 행에서 빠짐, 기록일 당일엔 보임
  - `test_scaling_chip_off_when_scaling_today` / `test_scaling_chip_adult_only` / `test_scaling_chip_off_after_claim_this_year`
  - `test_family_recall_excludes_booked_member` — 리콜 지난 가족 둘 중 다음 주 위생 예약 있는 사람만 빠짐
  - `test_open_plan_as_of_day` — 그날 이후 완료·대체된 항목은 지난 날짜에 열림으로, 그날 예정 항목은 빠짐, 그날 만든 계획은 없음
  - `test_new_patient_and_empty_facts` — 첫 내원 환자: `last_visit` None, `new_patient` True, 칩은 `appointment`(+미확인)만
  - `test_no_appointments_day_is_empty` — `[]`
  - `test_snapshot` — 고정 날짜 출력 = 스냅샷 JSON(처음 한 번 써서 커밋)
- [ ] **Step 2: 실패 확인** — `uv run pytest tests/backend/test_prep.py -v` → FAIL(모듈 없음)
- [ ] **Step 3: 구현** — 위 시그니처. 쿼리 기준은 `backend/emr.py` `patient_summary`·`backend/query_tools.py` `_family_recall`과 같게(날짜 제한만 더함)
- [ ] **Step 4: 통과 확인** — 같은 명령 → PASS, `uv run pytest tests/test_no_ground_truth_import.py -q` 통과
- [ ] **Step 5: Commit** — `backend: ④ 준비표 사실·칩 prep_facts`

### Task 3: 제안·후검증·저장, `make prep`

**Files:**
- Create: `prompts/prep/v1.md`
- Modify: `backend/prep.py`, `shared/app_schema.sql` (`ax_prep_sheets`), `Makefile` (`prep` 타깃, `.PHONY`)
- Test: `tests/backend/test_prep.py`

**Interfaces:**
- Consumes: `prep_facts`, `CHIPS`(Task 2)
- Produces:
  - `ax_prep_sheets(id PK, sheet_date TEXT, patient_id INT, appointment_id INT, facts TEXT, facts_hash TEXT, suggestion TEXT NULL, suggestion_valid INT NOT NULL DEFAULT 0, prompt_version TEXT, model TEXT, created_at TEXT, UNIQUE(sheet_date, patient_id, appointment_id))`
  - `llm_facts(row) -> dict[str, str | int]` — 평평한 키: `procedure`, `type`, `last_visit`, `balance`(>0일 때), `scaling_unused`, `alert.<CODE>`(관련·무관 모두 — 값은 `"<label> — <action>"`, 무관은 `"<label>"`), `plan.<n>`(`"<시술> <치아> <예상 금액>원"`), `family.<chart_no>`(`"<이름> 마지막 스케일링 <날짜>"`). **`confirmed`는 넣지 않는다**
  - `SCHEMA` — `{"action": "suggest"|"none", "text": str, "used_facts": [{"key": str, "value": str}]}`
  - `check_suggestion(facts: dict, s: dict) -> bool` — `none`: `text`·`used_facts` 비어야 참. `suggest`: 모든 `used_facts` (key, value)가 `facts`에 있고(`str(facts[key]) == value`) `text`의 숫자(단위 정규화: `3만`→30000, `5천`→5000, 쉼표 제거)가 모두 `used_facts` 값의 숫자에 속함
  - `build(conn, gw, days: list[date], *, use_batch: bool) -> dict` — 그날들 행을 만들고 `{"rows", "called", "skipped", "failed"}`. 건너뜀 = `facts_hash`·`prompt_version`·`model` 같고 `suggestion_valid` 1. 첫 답은 `use_batch`면 Batch API 한 번(게이트웨이 `submit_batch`·`batch_status`·`batch_results`, 모양은 `backend/classify_batch.py` `_submit`·`_collect`), 아니면 일반 호출(병렬). 첫 답이 후검증에 실패한 행만 일반 호출로 한 번 더, 또 실패면 `suggestion` NULL·`suggestion_valid` 0. Batch에서 빠진 행은 일반 호출로. 예약이 없어진 행은 지운다
  - CLI `python -m backend.prep run [--date YYYY-MM-DD] [--no-batch]` — 기본은 기준일 포함 최근 8일(`shared/clinic.py` 기준일), Batch
  - 화면의 "다시 만들기"(Task 4)는 `use_batch=False` — 사람이 기다린다
- [ ] **Step 1: 실패하는 테스트** (`tests/backend/script.py` 대본 게이트웨이):
  - `test_llm_facts_has_no_confirmed`
  - `test_check_suggestion_rejects_unknown_key_and_number` / `test_check_suggestion_units` ("미수 3만 원" + `balance`=30000 → 참, "4만 원" → 거짓, "30,000원" → 참) / `test_none_requires_empty`
  - `test_build_retries_once_then_null` (무효 답 2번 → NULL·0, 호출 2번)
  - `test_build_batch_first_then_plain_retry` (가짜 게이트웨이의 batch 메서드: 첫 답 batch 1번, 실패 행만 일반 호출) / `test_build_batch_missing_falls_back`
  - `test_build_skips_same_hash_valid_row` (두 번째 `build` 호출 0) / `test_build_recalls_failed_row` / `test_build_recalls_on_prompt_version_change`
  - `test_build_empty_day_no_calls`
- [ ] **Step 2: 실패 확인** — `uv run pytest tests/backend/test_prep.py -v` → 새 테스트 FAIL
- [ ] **Step 3: 구현** — 프롬프트(규칙: 관련 주의사항은 행동으로, 권할 진료는 칩 사실에서만, 없으면 `none`, 쓴 사실은 전부 `used_facts`에, 숫자는 사실 값 그대로. 예시 문장은 가상 데이터에서 옮기지 않는다), 스키마, `build`, CLI. 쓰기는 행마다 커밋(다시 부르는 동안 잠금을 쥐지 않게 — `backend/search_summary.py`와 같다). 병렬 수는 빌더 판단
- [ ] **Step 4: 통과 확인** — 같은 명령 → PASS, `uv run pytest tests/backend/test_db.py -q`(새 테이블 생성)
- [ ] **Step 5: Makefile** — `prep:` → `uv run $(if $(wildcard .env),--env-file .env) python -m backend.prep run $(if $(DATE),--date $(DATE)) $(if $(NO_BATCH),--no-batch)`, 주석 "(과금) ④ 준비표 — 기준일 포함 최근 8일, Batch(NO_BATCH=1이면 일반 호출), 사실이 같고 통과한 행은 건너뜀"
- [ ] **Step 6: Commit** — `backend: ④ 준비표 제안·후검증·저장, make prep`

### Task 4: API — `GET /prep`, 다시 만들기 작업

**Files:**
- Modify: `backend/api.py`, `shared/app_schema.sql` (`ax_jobs`), `backend/prep.py` (`sheet(conn, day)`)
- Test: `tests/backend/test_api_prep.py`

**Interfaces:**
- Consumes: `build`, 저장 행(Task 3)
- Produces:
  - `ax_jobs(id PK, kind TEXT, arg TEXT, status TEXT CHECK (running|done|failed), done INT DEFAULT 0, total INT DEFAULT 0, error TEXT, started_at TEXT, finished_at TEXT)`
  - `sheet(conn, day) -> dict` — `{"date", "generated_at": 최근 created_at|None, "days": [{"date", "count"}](기준일 포함 최근 8일, 예약 수), "rows": [저장 행: facts 펼침 + "suggestion": {...}|null + "suggestion_valid": bool]}`. 저장 행이 없는 예약도 사실만으로 행을 낸다(`suggestion` null, `"generated": false`)
  - `GET /prep?date=YYYY-MM-DD`(기본 기준일, 날짜 탭 밖이면 400), `POST /jobs/prep-regenerate?date=` → `{"job_id"}`(같은 날짜 `running` 작업이 있으면 그 id), `GET /jobs/{id}` → 작업 행. 작업은 스레드에서 `build(conn, gw, [day], use_batch=False)`(쓰기 연결은 따로)
- [ ] **Step 1: 실패하는 테스트** — `test_get_prep_rows_and_days`, `test_get_prep_out_of_range_400`, `test_regenerate_returns_running_job` (두 번 POST → 같은 id), `test_job_done_after_build` (대본 게이트웨이, 끝나면 `done`·`rows` 채워짐)
- [ ] **Step 2: 실패 확인** — `uv run pytest tests/backend/test_api_prep.py -v` → FAIL
- [ ] **Step 3: 구현** — 기존 `backend/api.py` 의존성(`Depends`) 방식 그대로
- [ ] **Step 4: 통과 확인** — 같은 명령 → PASS
- [ ] **Step 5: Commit** — `backend: ④ GET /prep, 다시 만들기 작업(ax_jobs)`

### Task 5: 화면 — 준비표

**Files:**
- Create: `frontend/lib/prep-api.ts`, `frontend/app/(portal)/prep/page.tsx`, `frontend/components/prep/prep-view.tsx`, `frontend/components/prep/prep-table.tsx`, `frontend/e2e/prep.spec.ts`
- Modify: `frontend/lib/nav.ts` (`BUILT`에 `/prep`)

**Interfaces:**
- Consumes: `GET /prep`, `POST /jobs/prep-regenerate`, `GET /jobs/{id}`(Task 4)
- Produces: `PrepView`(URL에 `date`·`view=table|chair`·`chip`·`type`·`todo`·`open`), `PrepRow` 타입(Task 6이 씀)
- 화면(스펙 §5, 와이어프레임 `prepTable`·`prepExpand`): 날짜 탭(인원수)·기준 시각·"다시 만들기"(작업 폴링, 끝나면 다시 조회) / 보기 전환 준비표|체어 보기 / 칩 필터(칩별 수) / 종류 필터 / 할 일 있는 환자만(= `suggestion.action == "suggest"`) / 몰리는 시간대 막대(시작 시각 시 단위) / 인쇄 버튼(`window.print`, 인쇄 때 필터·버튼 숨김) / 표: 시각·체어 · 환자(신환 딱지, 이름 → `/emr?chart=`) · 예정 시술 · 칩 · 제안(`none` → "그대로 진행", null → "제안 없음 — 후검증 실패", 안 만듦 → "아직 만들지 않음") · 지난 날짜 결과 / 펼침: 사실 목록 · 제안 · 근거 · 후검증
- [ ] **Step 1: 실패하는 e2e** — API를 `page.route`로 가짜(고정 JSON 2~3행, `frontend/e2e/ask.spec.ts` `fakeSearch` 방식): `준비표 — 칩 필터로 줄이 준다`, `행을 펼치면 근거와 후검증이 보인다`, `제안 없음·그대로 진행 표시`, `예약 없는 날짜는 "예약 없음"`
- [ ] **Step 2: 실패 확인** — `cd frontend && npx playwright test e2e/prep.spec.ts` → FAIL
- [ ] **Step 3: 구현** — 기존 `components/schedule/schedule-view.tsx`의 URL 상태·`Loaded` 패턴, 부품은 `components/ui`
- [ ] **Step 4: 통과 확인** — 같은 명령 → PASS, `make lint` 통과
- [ ] **Step 5: Commit** — `frontend: ④ 준비표 화면`

### Task 6: 화면 — 체어 보기

**Files:**
- Create: `frontend/components/prep/prep-chair.tsx`
- Modify: `frontend/components/prep/prep-view.tsx`, `frontend/e2e/prep.spec.ts`

**Interfaces:**
- Consumes: `PrepRow`(Task 5), `useClinic`(기준일)
- 화면(와이어프레임 `prepChair`·`chairCard`): 체어 고르기(1~5 + 상담실, 체어별 수, URL `chair`) / 왼쪽 순서(지금·다음 표시) / 오른쪽 한 장: 주의사항(관련만, 크게 + 행동 문구, 없으면 "주의사항 없음")·예정 시술·마지막 내원·남은 계획·제안. "지금" = 기준일이면 기준일 + 실제 시:분, 지난 날짜면 첫 예약. 1분마다 다시 계산
- [ ] **Step 1: 실패하는 e2e** — `체어 보기 — 체어를 바꾸면 그 체어 환자만`, `주의사항이 큰 칸에 행동과 함께`, `지금 전에는 첫 환자가 "다음"`(`page.clock`으로 시각 고정)
- [ ] **Step 2: 실패 확인** — 같은 명령 → FAIL
- [ ] **Step 3: 구현**
- [ ] **Step 4: 통과 확인** — PASS, `make lint`
- [ ] **Step 5: Commit** — `frontend: ④ 체어 보기`

### Task 7: 채점 — `evaluation/prep.py`, `eval.py`

**Files:**
- Create: `evaluation/prep.py`
- Modify: `evaluation/eval.py` (dev에 `result["prep"]`, 리포트 렌더)
- Test: `tests/evaluation/test_prep_eval.py`

**Interfaces:**
- Consumes: `ax_prep_sheets`(Task 3), `generator.master.ALERT_TYPES`(관련 여부 원본)
- Produces: `score(conn, days: list[date]) -> dict` — 각 `{"k", "n"}`: `valid`(후검증 통과 / 저장 행), `reflect`(관련 주의사항이 있는 행 중 그 키 `alert.<CODE>`가 모두 `used_facts`에), `over_mention`(무관 주의사항 키가 `used_facts`에 있는 행 / 저장 행), `over_suggest`(예약·미확인 밖 칩이 모두 꺼진 행 중 `suggest`). 그리고 `by_prompt_version` 행 수, 안 만든 예약 수
- [ ] **Step 1: 실패하는 테스트** — 손으로 만든 저장 행 몇 개로 네 지표 k/n 정확값, `null` 제안은 `valid`의 실패이면서 `reflect`에서도 실패로 셈, 관련 여부를 앱 `related`가 아니라 `master`로 다시 계산하는지(행의 `related`를 틀리게 넣어도 채점은 master 기준)
- [ ] **Step 2: 실패 확인** — `uv run pytest tests/evaluation/test_prep_eval.py -v` → FAIL
- [ ] **Step 3: 구현** — 범위는 기준일 포함 최근 8일. `eval.py`는 dev에서만(정답 분할 없음), 리포트에 분수 병기
- [ ] **Step 4: 통과 확인** — PASS
- [ ] **Step 5: Commit** — `eval: ④ 준비표 채점(후검증·반영률·과잉 언급·과잉 제안)`

### Task 8: 문서·실데이터 실행·야간 에이전트 판단

**Files:**
- Modify: `docs/치과 AX - 기능 정의.md`(§2 표, §4 ④ 화면·완료 기준, §9), `docs/치과 AX - 앱 설계.md`(§4 `ax_prep_sheets`·`ax_jobs` 칸, §8 미결 두 줄 닫기, §9 `/prep` 행·와이어프레임 예외 "회의 보기 없음"), `docs/치과 AX - 평가 설계.md`(§4 `none` 후검증), `docs/치과 AX - AI 실험 기록.md`(새 절 — 준비표 첫 측정), `CLAUDE.md`(명령에 `make prep`(과금))
- [ ] **Step 1: 실데이터** — `make prep`(과금, 약 8일 × 하루 최대 `visits_per_day`행) → 끝 요약의 `failed` 확인
- [ ] **Step 2: 채점** — `uv run --env-file .env python -m evaluation.eval --split dev` → 리포트의 ④ 네 지표
- [ ] **Step 3: 판단 기록** — 주의 칩 행의 반영률·과잉 언급률로 야간 조사 에이전트의 여지를 판단해 §9에 한 줄(만들면 다음 브랜치 착수 항목, 아니면 근거와 함께 운영 에이전트 때 다시 봄), 수치·경위는 AI 실험 기록에
- [ ] **Step 4: 문서 반영** — 위 파일들(규칙만, 경위는 실험 기록)
- [ ] **Step 5: 전체 확인** — `make fmt` → `make lint` → `make test`, 그리고 `make e2e`(`make dev` 끄고)
- [ ] **Step 6: Commit** — `docs: ④ 준비표 규칙·회의 보기 제외·첫 측정`
