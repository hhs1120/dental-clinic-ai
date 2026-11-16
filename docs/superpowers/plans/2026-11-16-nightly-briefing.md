# 야간 실행 + ⑦ 아침 브리핑(템플릿) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `make nightly [PLAN=1] [NO_BATCH=1]` 한 번으로 모든 화면의 AI 결과를 고정 순서로 최신화하고, 오늘 화면에 코드 템플릿 아침 브리핑 카드를 붙인다.

**Architecture:** `backend/nightly.py`가 단계 목록(이름·실행·대상 세기)을 차례로 돌리며 `ax_jobs` 한 행(`kind='nightly'`, 단계별 `detail`)에 기록한다. 각 단계는 기존 모듈 함수를 프로세스 안에서 부른다. `backend/briefing.py`는 준비표 사실과 마지막 nightly 행으로 네 줄을 만드는 순수 규칙이고 `today.sheet`가 붙인다.

**Tech Stack:** Python 3 + sqlite3(ORM 없음), pytest, FastAPI, Next.js(Tailwind + shadcn/ui), Playwright.

**Spec:** `docs/superpowers/specs/2026-11-16-nightly-briefing-design.md`

## Global Constraints

- 브랜치 `feat/nightly` (main에서). 커밋은 `<영역>: <무엇>` + 본문 글머리, `git add`는 파일 이름으로
- `backend/`는 `evaluation/`·`generator/`·`ground_truth/`를 import하지 않는다. `source.sqlite`에 쓰지 않는다
- 주석·독스트링 한국어, 근거 문서 절을 가리킨다. 모듈 첫 독스트링에 하는 일과 원본 문서
- 값은 이름 있는 상수로: `briefing.py` `BUSY_TOP = 3`, `BIG_CATEGORIES = ("surgery", "implant")`, `BIG_MAX = 3`, `CARE_MAX = 3`, `NIGHT_HOURS = 18`; `nightly.py` `NIGHTLY_STALE = timedelta(hours=8)`
- 프론트: 색·모서리는 `globals.css` 토큰만, 부품은 `components/ui`만. 브리핑 카드에 `AI` 배지 없음(코드 계산)
- 각 Task 끝에 `make fmt` → `make lint` → 해당 테스트. 마지막 Task에서 `make test`

## Review Focus

1. 예약 0건인 날 — 브리핑 "오늘"·"큰 시술"·"챙길 환자" 줄이 없고 예외 없음 → Task 1 `test_empty_day`
2. nightly를 한 번도 안 돌린 앱 — 브리핑 "밤사이" 줄 없음, "AI가 한 일"에 끝난 시각 없음, `GET /today` 정상 → Task 1 `test_night_line_none`, Task 5 `test_sheet_without_nightly`
3. 색인이 없는 앱(`ax_chunks` 비어 있음)에서 nightly — 색인 단계 "건너뜀: 색인 없음", 나머지 진행 → Task 4 `test_index_step_skips_without_index`
4. 같은 날 nightly 두 번(첫 실행 실패 없음) — 두 번째 `new` 전부 0, 과금 0 → Task 4 `test_second_run_new_zero`
5. 판정기 모델 불일치로 `SystemExit` — 그 단계만 failed, 다음 단계 진행 → Task 3 `test_systemexit_step_continues`

---

### Task 1: ⑦ 브리핑 규칙 (`backend/briefing.py`)

**Files:**
- Create: `backend/briefing.py`
- Test: `tests/backend/test_briefing.py`

**Interfaces:**
- Produces:
  - `day_lines(rows: list[dict], categories: dict[str, str]) -> list[dict]` — `rows`는 `prep.prep_facts` 행, `categories`는 시술 코드 → `emr_procedures.category`. 반환 `[{"head": "오늘"|"큰 시술"|"챙길 환자", "text": str}]`(챙길 환자는 한 명에 한 원소, head 같음)
  - `night_line(job: dict | None, now: datetime) -> dict | None` — `job`은 Task 3 `nightly.last_run` 반환
  - `lines(conn, today: date, now: datetime) -> list[dict]` — 위 둘을 합침(`prep_facts(conn, today, today)`, `SELECT code, category FROM source.emr_procedures`, `nightly.last_run(conn)`)

- [ ] **Step 1: 실패하는 테스트** — 행은 `prep_facts` 모양 최소 dict를 만드는 `_row(time, code, procedure, name, new=False, alerts=())` 도우미로

```python
CATS = {
    "U0011": "surgery",
    "U0012": "surgery",
    "U0013": "implant",
    "U0009": "prosth",
    "U0001": "preventive",
}


def test_today_line_busy_hours_ties():
    rows = [
        _row("10:00", "U0001", "스케일링", "가"),
        _row("10:30", "U0001", "스케일링", "나"),
        _row("14:00", "U0009", "크라운", "다"),
        _row("14:30", "U0009", "크라운", "라", new=True),
        _row("17:00", "U0001", "스케일링", "마"),
    ]
    assert day_lines(rows, CATS)[0] == {
        "head": "오늘",
        "text": "예약 5건(신환 1) · 10시·14시대에 몰림",
    }


def test_big_procedures_by_category_and_overflow():
    rows = [
        _row(t, "U0011", "단순발치", n)
        for t, n in [("09:00", "가"), ("10:00", "나"), ("11:00", "다"), ("12:00", "라")]
    ]
    rows.append(_row("15:00", "U0009", "크라운", "마"))  # 60분이어도 prosth는 아님
    big = [l for l in day_lines(rows, CATS) if l["head"] == "큰 시술"][0]
    assert big["text"] == "단순발치 9:00 · 단순발치 10:00 · 단순발치 11:00 외 1건"


def test_care_patients_big_and_related_alert_only():
    rows = [
        _row(
            "10:30",
            "U0011",
            "단순발치",
            "배정웅",
            alerts=[("항응고제 복용", True), ("치과 공포", False)],
        ),
        _row("11:00", "U0001", "스케일링", "김재호", alerts=[("고혈압", True)]),  # 큰 시술 아님
        _row("15:30", "U0013", "임플란트 1차", "안서준", alerts=[("당뇨", True)]),
    ]
    care = [l["text"] for l in day_lines(rows, CATS) if l["head"] == "챙길 환자"]
    assert care == ["10:30 배정웅 단순발치 — 항응고제 복용", "15:30 안서준 임플란트 1차 — 당뇨"]


def test_empty_day():
    assert day_lines([], CATS) == []


def test_night_line_counts_and_window():
    job = {
        "status": "done",
        "finished_at": "2026-11-16T03:12:00",
        "detail": [
            {"step": "classify", "new": 5, "failed": 0, "note": None},
            {"step": "autoclose", "new": 3, "failed": 0, "note": None},
            {"step": "prep", "new": 2, "failed": 1, "note": None},
        ],
    }
    assert night_line(job, datetime(2026, 11, 16, 9, 0)) == {
        "head": "밤사이",
        "text": "통화 5건 분류 · 자동 종결 3 · 실패 1 (03:12 끝남)",
    }
    assert night_line(job, datetime(2026, 11, 16, 21, 13)) is None  # NIGHT_HOURS 18 지남


def test_night_line_budget_stop():
    job = {
        "status": "failed",
        "finished_at": "2026-11-16T03:12:00",
        "detail": [
            {"step": "classify", "new": 1, "failed": 0, "note": None},
            {"step": "judge", "new": 0, "failed": 0, "note": "상한으로 중단"},
        ],
    }
    assert night_line(job, datetime(2026, 11, 16, 9, 0))["text"].endswith(
        "(03:12 끝남 · 상한으로 중단)"
    )


def test_night_line_none():
    assert night_line(None, datetime(2026, 11, 16, 9, 0)) is None
```

- [ ] **Step 2:** `uv run pytest tests/backend/test_briefing.py -v` → FAIL(모듈 없음)
- [ ] **Step 3: 구현** — 시각은 앞 0을 뗀 `H:MM`. 몰림은 `time[:2]`별 수가 최댓값인 시(오름차순, 최대 `BUSY_TOP`), 예약 1건 이상일 때만. 큰 시술은 시각순 `BIG_MAX`개 + `외 n건`. 챙길 환자 주의사항은 `related`인 `label`을 `, `로. `lines`는 `day_lines(...) + [night]`(None 제외)
- [ ] **Step 4:** 같은 명령 → PASS
- [ ] **Step 5: 커밋** `backend: ⑦ 아침 브리핑 템플릿 규칙`

### Task 2: 붙여넣은 통화 색인

**Files:**
- Modify: `backend/rag_index.py` (`_calls`)
- Test: `tests/backend/test_rag_index.py`

**Interfaces:**
- Produces: `collect_chunks`가 `ax_pasted_calls` 통화도 `call:p-0001:n` 청크로 낸다. `build_index(conn, embedder)`는 그대로(Task 4가 부름)

- [ ] **Step 1: 실패하는 테스트** `test_pasted_calls_chunked` — `source_paths` 픽스처 DB에 `ax_pasted_calls` 한 행(`p-0001`, patient_id 1, `started_at` `2026-12-01 10:00:00`, 녹취 `"직원: 네\n환자: 틀니 문의"`) 넣고 `collect_chunks` → id `call:p-0001:0`, `source_type == "call"`, `chart_no == "C-00001"`, `event_date == "2026-12-01"`, prefix가 생성 통화와 같은 꼴
- [ ] **Step 2:** `uv run pytest tests/backend/test_rag_index.py -v` → 새 테스트 FAIL
- [ ] **Step 3: 구현** — `_calls`가 `source.ax_calls`(has_transcript=1)와 `ax_pasted_calls`(앱 DB, `caller_name` 없음 → 환자 없으면 `상대: 미상`)를 합쳐 같은 dict를 낸다
- [ ] **Step 4:** 같은 명령 → PASS
- [ ] **Step 5: 커밋** `backend: 붙여넣은 통화도 색인`

### Task 3: nightly 뼈대 — 잠금·단계 루프·기록

**Files:**
- Create: `backend/nightly.py`
- Modify: `shared/app_schema.sql` (`ax_jobs`에 `detail TEXT`, 주석에 `nightly` 추가)
- Test: `tests/backend/test_nightly.py`

**Interfaces:**
- Produces:
  - `@dataclass Ctx: conn, gw, today: date, use_batch: bool`
  - `@dataclass Step: name: str; run: Callable[[Ctx], dict]; plan: Callable[[Ctx], int | None]` — `run` 반환 `{"new": int, "failed": int, "note": str | None}`, `plan` None이면 "셀 수 없음"
  - `run(ctx: Ctx, steps: list[Step], clock=datetime.now) -> dict | None` — 잠금에 걸리면 None, 아니면 `ax_jobs` 행 dict
  - `plan(ctx: Ctx, steps: list[Step]) -> list[tuple[str, int | None]]` — `ax_jobs`에 쓰지 않음
  - `last_run(conn) -> dict | None` — 가장 최근 `kind='nightly'` 행, `detail`은 JSON을 풀어 list
  - `over_budget(gw) -> bool` — 게이트웨이의 `_check_budget()`이 `BudgetExceeded`를 내면 True(가짜 게이트웨이는 False)

- [ ] **Step 1: 실패하는 테스트** — 가짜 단계(호출 순서를 리스트에 적는 람다)로, `conn` 픽스처 사용

```python
def test_runs_steps_in_order_and_records(conn):
    ...
    # job["status"] == "done", job["done"] == job["total"] == 3, [d["step"] for d in job["detail"]] == ["a","b","c"]


def test_lock_alive_running_skips(conn, clock):
    ...
    # running 행 started_at = now - 1시간 → run(...) is None, 단계 호출 0


def test_lock_stale_running_ignored(conn, clock):
    ...
    # running 행 started_at = now - 9시간 → 실행됨


def test_step_exception_continues(conn):
    ...
    # b가 RuntimeError → detail b {"failed": 1, "note": "RuntimeError: ..."}, c 실행됨, status "done"


def test_systemexit_step_continues(conn):
    ...
    # b가 SystemExit("판정기 …") → 위와 같음


def test_budget_stops_remaining(conn, monkeypatch):
    ...
    # over_budget이 a 뒤 True → b·c 호출 0, detail b note "상한으로 중단"(c는 detail에 없음), status "failed"


def test_plan_writes_no_job(conn):
    ...
    # plan(...) == [("a", 2), ("b", None)], ax_jobs 행 0
```

- [ ] **Step 2:** `uv run pytest tests/backend/test_nightly.py -v` → FAIL
- [ ] **Step 3: 구현** — 시작 때 `running` 행 insert(`total=len(steps)`), 단계마다 `detail`·`done` 갱신 커밋, 예외는 `except BaseException`에서 `KeyboardInterrupt`만 다시 올림, 단계 예외 뒤 `conn.rollback()`. 단계 뒤 `over_budget(ctx.gw)`면 다음 단계 이름으로 note `상한으로 중단` 한 줄 넣고 끝. 끝 시각 `finished_at`
- [ ] **Step 4:** 같은 명령 → PASS. `tests/backend/test_db.py`도 통과(새 칸 추가 경로)
- [ ] **Step 5: 커밋** `backend: 야간 실행 뼈대 — 잠금·단계 기록`

### Task 4: nightly 실제 단계와 PLAN

**Files:**
- Modify: `backend/nightly.py` (`STEPS`, `main`), `backend/prep.py`, `backend/report.py`
- Modify: `Makefile` (`nightly` — `uv run --group finetune …python -m backend.nightly run $(if $(PLAN),--plan) $(if $(NO_BATCH),--no-batch)`, 주석 "과금, 이어 돎")
- Test: `tests/backend/test_nightly.py`, `tests/backend/test_prep.py`, `tests/backend/test_report.py`

**Interfaces:**
- Consumes: Task 3 `Ctx`·`Step`·`run`·`plan`, Task 2 색인
- Produces:
  - `prep.todo_count(conn, days: list[date], today: date, model: str) -> int` — `build`와 같은 `_reusable` 판정, 쓰기 없음
  - `report.todo_count(conn, today: date, model: str) -> int` — `build`의 재사용 판정을 `_fresh(conn, scope, label, m, model) -> bool`로 꺼내 둘이 같이 씀
  - `nightly.STEPS: list[Step]` — 이름 순서 정확히 `["source", "classify", "judge", "disagreements", "autoclose", "followup", "index", "prep", "claim_check", "claim_omission", "claim_evidence", "recon", "expense", "report"]`

단계별 `run`/`plan`(인자는 각 모듈 `main`이 하는 그대로, 범위 기본값):

| 단계 | run → new | plan |
|---|---|---|
| source | `policy.check_source(conn, SOURCE)` → 바뀌면 note "자동 처리 정책 끔", new 0 | None |
| classify | `classify_batch.run(… "classify", a, use_batch)`의 done + 붙여넣은 통화 중 활성 분류 ok 없는 것에 `calls.classify_one` | `len(targets(conn,"classify",a,None))` + 붙여넣은 대상 수 |
| judge | `classify_batch.run(… "judge", b, use_batch)` done | `len(targets(conn,"judge",b,None))` |
| disagreements | `disagreements.compute(conn,a,b)` + `judge_queue.refresh_if_active` + `prune` | None |
| autoclose | `autoclose.run(conn)`의 closed (꺼짐이면 note "정책 꺼짐") | None |
| followup | `followup.targets` 각각 `run_for_call`(drop_stale 안 부름) | `len(followup.targets(conn, today))` |
| index | `ax_chunks` 비면 note "건너뜀: 색인 없음"; 아니면 `build_index(conn, make_embedder(api.DEFAULT_EMBEDDING))` | 비면 0, 아니면 None |
| prep | `prep.build(conn, gw, tab_days(today), today, use_batch)` called | `prep.todo_count` |
| claim_check / omission / evidence | 각 `build(conn, gw, targets(conn, today, scope="app"), use_batch)` | 각 `len(todo_ids(conn, today))` |
| recon / expense | `run_batch(conn, gw, today, None, batch=use_batch)`; 대상 없으면 `recon_auto.refresh`/`expense_auto.refresh` | `len(targets(conn, today))` |
| report | `report.build(conn, gw, today, use_batch)` called | `report.todo_count` |

`a, b = calls.active_versions(conn)`. `main`: `--plan`이면 단계별 `이름: n건`(None이면 `—`, 판정·엇갈림·자동 종결·후속 처리안은 `(앞 단계 뒤 늘 수 있음)`)과 끝에 "Batch는 하루 상한 밖 — 이 건수로 확인" 한 줄. 실행이면 단계마다 한 줄(new·failed·초)과 끝에 이번 실행 구간 `ax_llm_calls.cost_usd` 합.

- [ ] **Step 1: 실패하는 테스트**
  - `test_prep.py::test_todo_count_matches_build_called` — FakeGateway로 첫 `todo_count == build()["called"]`, 다시 `todo_count == 0`
  - `test_report.py::test_todo_count_matches_build_called` — 같은 꼴
  - `test_nightly.py::test_step_names_order` — `[s.name for s in STEPS]`가 위 목록
  - `test_nightly.py::test_second_run_new_zero` — `cconn`(통화 DB) + FakeGateway, 단계는 `STEPS` 중 classify·autoclose·followup만 골라 두 번 → 두 번째 `new` 전부 0
  - `test_nightly.py::test_index_step_skips_without_index` — 빈 `ax_chunks`에서 index 단계 note "건너뜀: 색인 없음"
- [ ] **Step 2:** `uv run pytest tests/backend/test_nightly.py tests/backend/test_prep.py tests/backend/test_report.py -v` → 새 테스트 FAIL
- [ ] **Step 3: 구현** — 위 표대로. `main`은 `make_gateway(conn)`, `load_clinic().today`
- [ ] **Step 4:** 같은 명령 → PASS. `make nightly PLAN=1`을 실제 `data/`에서 돌려 단계 14줄이 나오고 `ax_jobs`에 nightly 행이 없음을 확인(과금 0)
- [ ] **Step 5: 커밋** `backend: make nightly — 고정 순서 단계·PLAN`

### Task 5: 오늘 화면 — 브리핑 카드·끝난 시각

**Files:**
- Modify: `backend/today.py` (`sheet`에 `"briefing": briefing.lines(conn, today, now)`, `ai`에 `"nightly": {"finished_at", "status", "failed"} | None`)
- Modify: `frontend/components/today/today-view.tsx`, 그 타입 정의 파일
- Test: `tests/backend/test_today.py`, `tests/backend/test_api_today.py`, `frontend/e2e/` 오늘 화면 스펙(없으면 `today.spec.ts` 새로)

**Interfaces:**
- Consumes: Task 1 `briefing.lines`, Task 3 `nightly.last_run`

- [ ] **Step 1: 실패하는 테스트**
  - `test_today.py::test_sheet_briefing_from_prep_rows(pconn)` — `sheet["briefing"][0]["head"] == "오늘"`, 예약 수가 `cards.appointments.total`과 같음
  - `test_today.py::test_sheet_without_nightly(pconn)` — `"밤사이"` 줄 없음, `sheet["ai"]["nightly"] is None`
  - `test_today.py::test_sheet_nightly_finished(pconn)` — nightly 행(`done`, 실패 1) 넣으면 `ai.nightly == {"finished_at": …, "status": "done", "failed": 1}`
- [ ] **Step 2:** `uv run pytest tests/backend/test_today.py -v` → FAIL
- [ ] **Step 3: 구현** — 백엔드 위대로. 화면: 요약 카드 아래 카드 한 칸, 제목 "아침 브리핑", 2열 그리드(머리말 고정 폭 · 내용), 같은 head가 이어지면 머리말은 첫 줄에만, `briefing`이 비면 카드 숨김. "AI가 한 일" 요약에 `ai.nightly`가 있으면 `HH:MM 끝남`, `failed > 0`이면 기존 실패 꼬리표 모양으로 `야간 실패 n`
- [ ] **Step 4:** `uv run pytest tests/backend/test_today.py tests/backend/test_api_today.py -v` → PASS, `make lint`, `make e2e`(브리핑 카드에 "오늘" 머리말이 보임)
- [ ] **Step 5: 커밋** `frontend: 오늘 화면 아침 브리핑 카드·야간 실행 끝난 시각`

### Task 6: 문서

**Files:**
- Modify: `docs/치과 AX - 기능 정의.md` §5 ⑦·§9, `docs/치과 AX - 앱 설계.md` §3b 스케줄러·§4 오늘 화면, `CLAUDE.md` 명령

- [ ] **Step 1:** 설계 문서 §5 "문서 반영" 다섯 줄을 그대로 반영(순서표는 `backend/nightly.py` `STEPS`를 가리킴). §9에서 "새 통화 색인"·"착수 순서" 줄 정리, "⑦ 착수 시" 줄은 "⑦ LLM 단계 착수 시"로
- [ ] **Step 2:** `uv run pytest tests/generator/test_doc_sync.py -v` → PASS, `make test` 전체 PASS
- [ ] **Step 3: 커밋** `docs: 야간 실행·⑦ 브리핑 1단계 반영`
