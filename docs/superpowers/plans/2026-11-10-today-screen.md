# 오늘 화면 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 메뉴 첫 칸 `/today`를 만든다 — 요약 카드 → 사람이 볼 일(확인할 통화 포함) → AI가 한 일(접힘), 숫자는 전부 쿼리.

**Architecture:** 새 모듈 `backend/today.py`가 기존 함수(`calls.today_board`·`prep.prep_facts`·판정 큐·자동 처리)를 다시 써서 한 번에 모으고 `GET /today`로 낸다. 확인할 통화는 `backend/calls.py`에 대상 계산과 "확인함" 쓰기를 더하고 새 앱 테이블 `ax_call_checks`에 남긴다. 화면은 `frontend/components/today/` 하나와 통화 상세·물어보기의 작은 수정.

**Tech Stack:** Python 3 · FastAPI · sqlite3(SQL 그대로) · pytest / Next.js(App Router, TypeScript strict) · Tailwind 토큰 · shadcn `components/ui/` · Playwright

**Spec:** `docs/superpowers/specs/2026-11-10-today-screen-design.md`

## Global Constraints

- 숫자는 전부 쿼리, LLM 호출 없음(기능 정의 §3 원칙 2). `GET /today`의 각 숫자는 그 숫자가 가리키는 화면과 **같은 함수**로 센다
- backend는 `ground_truth/`·`generator/`·`evaluation/`을 import하지 않는다(`tests/test_no_ground_truth_import.py`). 판정 풀(`in_eval_pool = 1`) 통화는 어디에도 섞지 않는다(`calls.CALLS_CTE`가 이미 뺀다)
- 화면에 시스템 말·코드 값을 보이지 않는다 — 라벨 이름은 `GET /labels`(`frontend/lib/calls-api.ts`), 자동 처리 이유는 `autoclose.REASON` 문장
- 확인할 통화 문구(사용자 결정): `AI: 종결, 사람: 다시 열림` / `AI: 할 일 없음, 사람 판정: 응급` / 할 일 있지만 7일 창 밖이면 `AI: 할 일 있음(지난 통화), 사람 판정: …` / 여러 개는 `응급·불만`. 사람 판정 항목 순서 응급 → 급함 → 불만 → 할 일 있음. 둘 다 해당하면 사람 판정 쪽 문구 하나
- 통화 상세 자동 종결 띠 버튼 `되돌리기` → `다시 열기`. 오늘 화면 자동 처리 버튼도 `다시 열기`. 공통 개념 "되돌리기"(앱 설계 §3b·`AutoAction`)·`자동 종결` 배지·할 일 체크 해제는 그대로
- 근거 없는 질문: `ax_search_logs` `status = 'refused'`·`is_eval = 0`·`kind` 둘 다·`created_at`(실제 시계) 최근 7일. 누르면 물어보기 질문하기 탭 입력칸만 채움(실행 안 함)
- 자동 처리: 요약은 지금 닫혀 있는 통화 전부, 펼친 목록은 `created_at` 최근 20건 + "전체 보기 → 통화 기록". 유효 조건은 `calls.ROWS_SQL`의 `auto` 조인과 같다(정책 켜짐·활성 버전 쌍)
- 주석·독스트링 한국어로, 근거 문서 절을 가리킨다. 색·모양은 `frontend/app/globals.css` 토큰만, 부품은 `components/ui/`만
- 커밋 전 `make fmt` → `make lint` → `make test`. `git add`는 파일 이름으로. 커밋 메시지 `<영역>: <무엇>` + 본문 글머리 + 끝에 `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`와 `Claude-Session: https://claude.ai/code/session_01G62cGwhYixc5LpBLhj8iGp`

## Review Focus

1. 사람 판정은 있는데 활성 분류가 `ok`가 아닌 통화 — 확인할 통화가 아니라 "AI가 한 일"의 분류 실패 줄로만 세고, 두 곳에 겹치지 않는다 → Task 1 `test_review_skips_failed_classification`
2. 확인함 뒤에 다시 닫혔다 다시 열린 통화, 확인 뒤 판정을 다시 저장한 통화 — 다시 뜬다(시각 비교) → Task 1 `test_check_then_rejudge_shows_again`
3. 자동 처리 정책을 끈 뒤 남은 `ax_auto_actions` 행 — 오늘 화면 자동 처리 0, 요약에 "자동 종결 꺼짐" → Task 2 `test_auto_policy_off_hides_actions`
4. 기준일에 예약이 0건인 날 — 카드가 0으로 나오고 500이 나지 않는다 → Task 2 `test_empty_day`
5. 다시 열기 직후 오늘 화면을 다시 불러오기 전 두 번 누름 — 두 번째는 409, 화면은 오류 띠 없이 다시 조회 → Task 4 e2e `다시 열기 — AI가 한 일에서 빠지고 확인할 통화에 뜸`에서 버튼이 즉시 꺼지는지 확인

---

### Task 1: 확인할 통화 — 대상 계산과 확인함

**Files:**
- Modify: `shared/app_schema.sql` (새 테이블 `ax_call_checks`)
- Modify: `backend/calls.py` (함수 2개 + `call_detail`에 `review` 칸)
- Test: `tests/backend/test_review_calls.py` (새 파일, `tests/backend/call_db.py`의 `make_call_db`·`classify_row`·`judge_row` 사용)

**Interfaces:**
- Produces:
  - 테이블 `ax_call_checks(call_uid TEXT PRIMARY KEY, checked_by TEXT NOT NULL, checked_at TEXT NOT NULL)` — 주석: 오늘 화면 확인할 통화의 "확인함"(설계 2026-11-10 오늘 화면 §2). 재확인은 덮어씀
  - `calls.review_calls(conn, today: date) -> list[dict]` — 각 dict: `{"call_uid", "name", "at", "ai": "closed" | "none" | "old_todo", "reopened": bool, "flags": list[str]}`. `flags`는 사람 판정이 켠 것을 `["emergency", "urgent", "complaint", "has_todo"]` 순서로(`urgent` = `urgency == "urgent"`). 통화 시각 최근 순
  - `calls.check_call(conn, call_uid: str, by: str, at: str) -> bool` — 대상이 아니면 False(커밋은 부르는 쪽)
  - `calls.call_detail(...)` 반환에 `"review": {"ai", "reopened", "flags"} | None` 추가(대상일 때만)

대상 규칙(설계 §2): `CALLS_CTE` 통화 중 `today_board`에 뜨지 않는 통화(활성 분류 `ok`이고 — 할 일 목록이 비었거나, 생성 통화이고 `at < today - TODAY_WINDOW_DAYS`) 가운데 `call_autoclose` 행이 `reverted_at IS NOT NULL`이거나 `autoclose.human_flag`가 켜진 것. `ai`: 되돌린 종결 행이 있으면 `closed`, 아니면 할 일 목록이 비었으면 `none`, 아니면 `old_todo`. 확인 행이 있고 `checked_at`이 판정 시각(`ax_judgments.judged_at`)과 되돌린 시각(`reverted_at`) 둘 다보다 뒤면 뺀다(시각은 `datetime.fromisoformat`으로 비교 — 형식이 섞여도 맞게). `backend/autoclose.py`가 `backend/calls.py`를 import하므로 `human_flag`는 calls 쪽에서 같은 조건 SQL로 다시 쓰거나 지연 import — 순환 import를 만들지 않는다.

- [ ] **Step 1: Write the failing tests** in `tests/backend/test_review_calls.py`

```python
def test_judged_flag_on_empty_todo_is_review(cconn):
    classify_row(cconn, "c-0001", todo=[])  # 덮어쓰기 — 기존 행을 지우고 다시 넣는다
    judge_human(cconn, "c-0001", emergency=1)
    r = calls.review_calls(cconn, TODAY)
    assert [(x["call_uid"], x["ai"], x["flags"]) for x in r] == [("c-0001", "none", ["emergency"])]


@pytest.mark.parametrize(
    "flag, lab",
    [
        ("emergency", {"emergency": 1}),
        ("urgent", {"urgency": "urgent"}),
        ("complaint", {"complaint": 1}),
        ("has_todo", {"has_todo": 1}),
    ],
)
def test_each_human_flag(cconn, flag, lab): ...  # flags == [flag]


def test_reopened_autoclose_is_review(
    cconn,
): ...  # 되돌린 call_autoclose 행 → ai "closed", reopened True
def test_old_call_with_todo_and_flag_is_review(
    cconn,
): ...  # c-0003(7일 밖, 할 일 있음) + 판정 → ai "old_todo"
def test_on_board_call_is_not_review(cconn): ...  # c-0001 할 일 있음·7일 안 + 판정 → []
def test_pool_call_is_not_review(cconn): ...  # c-0002 판정 풀 → []
def test_review_skips_failed_classification(cconn): ...  # status 'failed' + 판정 → []
def test_check_removes_from_review(
    cconn,
): ...  # check_call True → review_calls에서 빠짐, 대상 아닌 통화는 False
def test_check_then_rejudge_shows_again(cconn): ...  # judged_at > checked_at이면 다시 뜸
def test_detail_has_review(
    cconn,
): ...  # call_detail(...)["review"]["flags"] == ["emergency"], 대상 아니면 None
```

`judge_human`은 테스트 안 도우미: `ax_judgments`에 `LABELS | 켠 값 | {"has_todo": 0}` 기본으로 `judged_by='데스크'`, `judged_at='2026-11-10T09:00:00'` 행을 넣는다(`has_todo` 경우만 1).

- [ ] **Step 2: Run** `uv run pytest tests/backend/test_review_calls.py -q` — Expected: FAIL (`review_calls` 없음)
- [ ] **Step 3: Implement** 테이블과 `review_calls`·`check_call`·`call_detail` `review` 칸
- [ ] **Step 4: Run** `uv run pytest tests/backend/test_review_calls.py tests/backend/test_call_screens.py tests/backend/test_autoclose.py -q` — Expected: PASS
- [ ] **Step 5: Commit** — `backend: 확인할 통화 — 사람이 일이 남았다고 표시했는데 오늘 할 일에 안 뜨는 통화, 확인함`

---

### Task 2: `backend/today.py` — 오늘 화면 한 장

**Files:**
- Create: `backend/today.py`
- Modify: `backend/judge_queue.py` (`pending_count`)
- Test: `tests/backend/test_today.py`

**Interfaces:**
- Consumes: `calls.today_board`, `calls.review_calls`(Task 1), `calls.active_versions`, `calls.CALLS_CTE`, `autoclose.policy_on`·`autoclose.KIND`, `prep.prep_facts`
- Produces:
  - `judge_queue.pending_count(conn) -> int` — `next_call`과 같은 조건(`pending`·`skipped`, 활성 분류 `ok`)의 수. `next_call`의 `left`와 같은 값(테스트로 고정)
  - `today.sheet(conn, today: date, now: datetime) -> dict` — 모양:

```python
{
  "date": "2026-12-01",
  "cards": {"appointments": {"total", "treatment", "hygiene", "consult"}, "new_patients": int,
            "alert_patients": int, "todo": {"items": int, "calls": int, "urgent_calls": int}},
  "people": {"urgent_calls": [{"call_uid", "name", "text", "at", "emergency": bool}],
             "review_calls": [...Task 1 dict...], "unconfirmed": int, "alert_patients": int,
             "todo_left": {"items": int, "calls": int}, "judge_pending": int,
             "refused": [{"id", "kind", "question", "at"}]},
  "ai": {"autoclose_on": bool, "auto_total": int,
         "auto_recent": [{"id", "call_uid", "name", "reason", "at"}],
         "prep": {"rows": int, "suggest": int, "failed": int},
         "classify_failed": int, "followup_failed": int},
}
```

값 출처: 예약·신환·주의·미확인은 `prep.prep_facts(conn, today, today)`의 행·`chips`(`"alert"`·`"unconfirmed"`)·`patient["new_patient"]`·`type`. 할 일은 `today_board`의 `internal + contact`(열린 것) — `items` = 줄 수, `calls` = 고유 통화 수, `urgent_calls` = 그중 `urgent or emergency`인 통화 수. `urgent_calls` 목록은 통화마다 한 줄(첫 열린 할 일 `text`), `today_board` 순서. 자동 처리는 `calls.ROWS_SQL`의 `auto` 조인과 같은 조건 — 정책 꺼짐이면 `auto_total = 0`, `auto_recent = []`, `autoclose_on = False`. `auto_recent`는 `created_at` 내림차순 20건(`AUTO_RECENT = 20` 상수). 준비표는 `ax_prep_sheets` `sheet_date = today`의 행 수·`suggestion` 액션 suggest 수·`suggestion_valid = 0` 수. 분류 실패 = `CALLS_CTE` 통화 중 활성 분류 행 `status != 'ok'`, 처리안 실패 = 활성 분류 행의 `ax_call_followups.status = 'failed'`. 거절 = `now - 7일` 이후(`REFUSED_DAYS = 7`), 최근 순.

- [ ] **Step 1: Write the failing tests** in `tests/backend/test_today.py` — 소스는 `tests/backend/test_prep.py` `make_source`(예약 101·102·103)와 `tests/backend/call_db.py`를 한 DB에 쓸 수 없으면 두 픽스처로 나눠 테스트한다(카드·준비표 = prep 소스, 통화·판정·자동 처리 = call_db)

```python
def test_cards_match_prep_chips(
    prep_conn,
): ...  # alert_patients·unconfirmed == prep_facts 칩 수, appointments 종류별
def test_todo_counts_match_board(
    cconn,
): ...  # items == len(open), calls == 고유 통화, urgent_calls == 1(c-0004)
def test_urgent_calls_one_line_per_call(cconn): ...  # c-0004 한 줄, text == 첫 할 일
def test_judge_pending_equals_next_call_left(cconn): ...
def test_refused_recent_non_eval_only(
    cconn,
): ...  # is_eval=1·8일 전·answered는 빠짐, 챗·검색 요약 둘 다
def test_auto_recent_twenty_and_total(
    cconn,
): ...  # 정책 켜고 활성 쌍으로 21건 → total 21, recent 20(최근 순)
def test_auto_policy_off_hides_actions(
    cconn,
): ...  # 행이 있어도 정책 끄면 total 0·autoclose_on False
def test_failures_counted(cconn): ...  # 분류 failed 1·처리안 failed 1
def test_prep_saved_counts(prep_conn): ...  # rows·suggest·failed
def test_empty_day(prep_conn): ...  # 예약 없는 날 cards 0, 예외 없음
```

- [ ] **Step 2: Run** `uv run pytest tests/backend/test_today.py -q` — Expected: FAIL (모듈 없음)
- [ ] **Step 3: Implement** `judge_queue.pending_count`, `backend/today.py` `sheet` (모듈 독스트링: 하는 일·원본 문서 — 설계 2026-11-10 오늘 화면, 기능 정의 §4 오늘 화면)
- [ ] **Step 4: Run** `uv run pytest tests/backend/test_today.py tests/backend/test_judge_queue.py tests/test_no_ground_truth_import.py -q` — Expected: PASS
- [ ] **Step 5: Commit** — `backend: 오늘 화면 집계 — 카드·사람이 볼 일·AI가 한 일을 같은 함수로`

---

### Task 3: API — `GET /today`, `POST /calls/{call_uid}/check`

**Files:**
- Modify: `backend/api.py`
- Test: `tests/backend/test_api_today.py`

**Interfaces:**
- Consumes: `today.sheet`(Task 2), `calls.check_call`(Task 1)
- Produces:
  - `GET /today` → `today.sheet(conn, clinic.today, now)` (의존성 `Paths`·`ClinicDep`·`Now`, `_open_rw`)
  - `POST /calls/{call_uid}/check` 본문 `{"part": str}`(기존 `Revert`와 같은 모양 — 같은 모델을 쓰거나 `Part`로 이름을 바꿔 둘이 함께 씀) → `{"ok": True}`, 대상이 아니면 404 `"확인할 통화가 아닙니다"`. `checked_at = now.isoformat(timespec="seconds")`
  - `GET /calls/{call_uid}` 응답에 Task 1 `review` 칸이 그대로 실린다

- [ ] **Step 1: Write the failing tests**: `test_get_today_shape`(키 셋·`date == "2026-12-01"`), `test_check_removes_review`(판정 넣고 → `GET /today` `review_calls` 1 → `POST check` → 0), `test_check_not_review_404`, `test_revert_then_review`(자동 종결 행 넣고 `POST /auto-actions/{id}/revert` → `GET /today` `auto_total` 줄고 `review_calls`에 `ai == "closed"`)
- [ ] **Step 2: Run** `uv run pytest tests/backend/test_api_today.py -q` — Expected: FAIL (404 라우트 없음)
- [ ] **Step 3: Implement** 두 라우트
- [ ] **Step 4: Run** `uv run pytest tests/backend/test_api_today.py tests/backend/test_api_calls.py -q` — Expected: PASS
- [ ] **Step 5: Commit** — `backend: GET /today·확인함 API`

---

### Task 4: 오늘 화면 프론트

**Files:**
- Create: `frontend/lib/today-api.ts` (응답 타입 = Task 2 모양, `fetchToday`, `checkCall(call_uid, part)`)
- Create: `frontend/app/(portal)/today/page.tsx` (준비표 페이지와 같은 머리말·`Suspense` 꼴, 머리말 한 줄: 오늘 남은 일과 AI가 한 일을 모아 보이고 누르면 그 화면으로 보낸다)
- Create: `frontend/components/today/today-view.tsx`
- Modify: `frontend/lib/nav.ts` (`BUILT`에 `"/today"`)
- Test: `frontend/e2e/today.spec.ts` (`/api/today`는 `page.route` 가짜 응답 — 실데이터는 자동 처리·판정·거절이 0건)

**Interfaces:**
- Consumes: `GET /today`, `GET /labels`(`frontend/lib/calls-api.ts`의 기존 호출), `revertAuto`(`frontend/lib/calls-api.ts`), `readPart`(`frontend/lib/part.ts`)
- Produces: 이동 경로(테스트가 고정) — 예약·신환 카드 `/prep`, 주의 `/prep?chip=alert`, 미확인 `/prep?chip=unconfirmed`, 할 일 `/calls`, 통화 줄 `/calls?call=<uid>`, 판정 대기 `/eval`, 근거 없는 질문 `/ask?tab=chat&draft=<질문>`(Task 5), 자동 처리 전체 보기 `/calls?tab=history`

화면 순서(설계 §1, 시안 B): 카드 4개 → "사람이 볼 일"(급한 통화 전부 → 확인할 통화 → 예약 미확인 → 주의 환자 → 남은 통화 할 일 → 판정 대기 → 근거 없는 질문; 목록 줄은 0건이면 숨김, 숫자 줄도 0이면 숨김 — 모두 0이면 "오늘 남은 일이 없습니다") → "AI가 한 일" 한 줄 요약(`자동 처리 N건 · 오늘 준비표 N행 · 실패 n`, 꺼져 있으면 `자동 종결 꺼짐`) + 펼치기(기본 접힘): 자동 처리 목록과 `다시 열기`, 준비표·분류 실패·처리안 실패 줄에 `끝남`/`사람에게 넘김` 꼬리표. `다시 열기`는 누르는 즉시 꺼지고 끝나면 다시 조회.

- [ ] **Step 1: Write the failing e2e** in `frontend/e2e/today.spec.ts` (`prep.spec.ts`의 `fakePrep` 꼴):
  - `카드와 줄을 누르면 그 화면으로` — 주의 카드 → URL `/prep?chip=alert`, 확인할 통화 줄 → `/calls?call=c-0450`
  - `확인할 통화 문구` — `AI: 할 일 없음, 사람 판정: 응급·불만`, `AI: 종결, 사람: 다시 열림`, `AI: 할 일 있음(지난 통화), 사람 판정: 급함`이 보이고 `emergency`·`has_todo` 같은 코드 값은 안 보임
  - `0건 줄은 숨김` — 거절 0이면 "근거 없음" 없음, 모두 0이면 "오늘 남은 일이 없습니다"
  - `AI가 한 일은 접힌 채 요약, 펼치면 다시 열기` — 요약 `자동 종결 꺼짐`, 켜진 응답에서 펼치면 `다시 열기` 버튼
  - `다시 열기 — AI가 한 일에서 빠지고 확인할 통화에 뜸` — POST `/api/auto-actions/1/revert` 가로채 두 번째 `/api/today` 응답을 바꾼다, 누른 직후 버튼 disabled
- [ ] **Step 2: Run** `npx --prefix frontend playwright test e2e/today.spec.ts` (frontend 폴더에서) — Expected: FAIL
- [ ] **Step 3: Implement** 화면·API 래퍼·`nav.ts`
- [ ] **Step 4: Run** `make lint` 그리고 `cd frontend && npx playwright test e2e/today.spec.ts` — Expected: PASS
- [ ] **Step 5: Commit** — `frontend: 오늘 화면 — 카드·사람이 볼 일·AI가 한 일`

---

### Task 5: 통화 상세 다시 열기·확인함, 물어보기 질문 채우기

**Files:**
- Modify: `frontend/components/calls/call-detail.tsx` (버튼 문구, `review` 띠와 확인함)
- Modify: `frontend/components/calls/calls-view.tsx` (확인함 처리 — `revertClose`와 같은 꼴)
- Modify: `frontend/lib/calls-api.ts` (`CallDetail`에 `review`)
- Modify: `frontend/components/ask/ask-view.tsx`·`frontend/components/ask/chat-panel.tsx` (`?draft=` → 질문하기 입력칸 초기값, 실행 안 함, 채운 뒤 주소에서 `draft`를 지움)
- Test: `frontend/e2e/calls.spec.ts`, `frontend/e2e/ask.spec.ts`

**Interfaces:**
- Consumes: `POST /calls/{uid}/check`(Task 3), `review` 칸(Task 1), `checkCall`(Task 4 `today-api.ts`)
- Produces: 통화 상세 맨 위 띠 — `review`가 있으면 확인할 통화 문구(Task 4와 같은 함수로 — `frontend/lib/today-api.ts`에 `reviewText(review, names)` 하나를 두고 두 곳이 같이 씀) + `확인함` 버튼, 누르면 띠가 사라짐

- [ ] **Step 1: Write the failing e2e**: `calls.spec.ts`에 `자동 종결 띠 버튼은 다시 열기`(상세 응답 가로채 `autoclose` 넣음 → 버튼 이름 `다시 열기`, `되돌리기` 없음)·`확인할 통화 띠의 확인함`(가로챈 상세에 `review` → 문구·버튼 → POST 가로채 → 띠 사라짐); `ask.spec.ts`에 `draft로 오면 질문하기 입력칸만 채움`(`/ask?tab=chat&draft=유치는 언제 빠져?` → 입력칸 값, `/api/chat` 요청 0번)
- [ ] **Step 2: Run** `cd frontend && npx playwright test e2e/calls.spec.ts e2e/ask.spec.ts` — Expected: 새 3개 FAIL
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run** `make lint` 그리고 `make e2e` — Expected: 전부 PASS
- [ ] **Step 5: Commit** — `frontend: 통화 상세 다시 열기·확인함, 물어보기 질문 채우기`

---

### Task 6: 문서

**Files:**
- Modify: `docs/치과 AX - 기능 정의.md` — §4 앱 구성 "오늘 화면"(확인할 통화·근거 없는 질문 7일·AI가 한 일 결과 표 세기·자동 처리 20건), §4 ① 통화 상세 "되돌리기(오늘 화면이 생기기 전까지 되돌리는 곳은 여기)" → `다시 열기`와 확인함, §9 착수 순서(오늘 화면 → ⑨ 후속 관리 → ⑤ 청구 점검 → ⑧ 재무 대조 → ⑥ 경영 리포트 → ⑦ 아침 브리핑 + 운영 에이전트, ⑧이 ⑥ 앞인 근거 한 구절), §9 "오늘 화면 착수 시" 항목 삭제, §9 "새 통화 색인"은 `make nightly` 착수 시로만
- Modify: `docs/치과 AX - 앱 설계.md` — §3b 또는 §6에 `ax_call_checks` 한 줄, §4 "오늘 화면" `GET /today` 문장, §9 화면 표에 `/today` 행(API `GET /today`·`POST /calls/{call_uid}/check`)
- Modify: `docs/치과 AX - AI 실험 기록.md` — 손대지 않음(이번 브랜치는 AI 품질 실험이 없다)

- [ ] **Step 1:** 위 문장을 고친다(규칙만, 결정 날짜·경위 없이 — 문서에는 규칙만)
- [ ] **Step 2: Run** `uv run pytest tests/generator/test_doc_sync.py -q` — Expected: PASS
- [ ] **Step 3: Commit** — `docs: 오늘 화면 — 확인할 통화·다시 열기, 착수 순서`

---

### 마무리

- [ ] `make fmt` → `make lint` → `make test` → `make e2e` 전부 통과 확인
- [ ] 실데이터로 `make dev`를 켜고 `/today`를 열어 카드 숫자가 준비표·통화 화면과 같은지 눈으로 확인(기준일 2026-12-01: 예약 26, 신환 4, 주의 6, 미확인 1, 할 일 34줄·통화 16 — 급함 통화 수는 통화 화면 오늘 할 일과 대조)
