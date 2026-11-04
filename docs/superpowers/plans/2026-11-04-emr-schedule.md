# 전환 전 EMR·일별 예약표 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 일별 예약표(`/schedule`)와 전환 전 EMR(`/emr`)을 실데이터로 띄운다. 예약표는 소요 시간만큼 늘어나는 카드와 환자 요약 패널, EMR은 가상 EMR 창과 설명 카드(흩어짐 지도 → 따라 보기 → 하루 규모)다.

**Architecture:** 조회는 `backend/emr.py`의 순수 함수(`conn, today, … → dict`)로 두고, `backend/api.py`가 `source.sqlite`만 읽기 전용으로 열어 라우트 네 개로 내보낸다. 프론트는 `lib/emr-api.ts` 타입을 공유한다. 화면은 두 개(`/schedule`, `/emr`)이고, 부품은 두 개(`components/patient-summary.tsx`, `components/emr/*`)다. 날짜·선택·보기는 URL 쿼리로 둔다.

**Tech Stack:** FastAPI, sqlite3 / Next.js 16(App Router, cacheComponents), Tailwind v4 토큰, shadcn/ui.

**Spec:** `docs/superpowers/specs/2026-11-04-emr-schedule-design.md`. 화면 모습의 기준은 시안 캔버스 https://claude.ai/artifact/XpYThqzPJrNEy1NFxcpaPy("합친 안" 세 장, "일별 예약표" 한 장)다. EMR 창의 구조는 `docs/wireframe/index-v2.html` `R.emr`와 그 도우미(1187–1342행), 옛 프로그램 CSS(252–310행)를 따른다.

## Global Constraints

- backend는 `ground_truth`·`generator`·`evaluation`을 참조하지 않는다. 주석에도 그 낱말을 쓰지 않는다(`tests/test_no_ground_truth_import.py`)
- `emr_*`·`fin_*`·`ax_calls`는 읽기만 한다. `/emr/*`은 `source.sqlite`만 연다. `?mode=ro` URI로 열고 `row_factory = sqlite3.Row`를 쓴다. `app.sqlite`는 열지 않는다
- 기준일은 `shared.clinic.load_clinic().today`, 신환 기준은 `.range_start`다. 환자 사실(미수·주의·마지막 내원·앞으로의 예약·노쇼·취소)은 고른 날짜가 아니라 기준일 기준이다
- 미수는 기준일 **전** 수납분 `amount_due − amount_paid`의 합이다. 0 이하면 0으로 준다
- 주의사항은 `recorded_at ≤ 기준일`이고 `ends_at`이 없거나 `기준일 < ends_at`인 것만이다
- 노쇼·취소는 기준일 전 365일 안(`기준일 − 365일 ≤ 날짜 < 기준일`)만 센다
- 예약 미확인은 그날 = 기준일, `status = scheduled`, `confirmed = 0`일 때만이다
- 진료 종류(`treatment`) 예약의 시술 이름 `상담`은 `진찰`로 표기한다
- 예약표 열은 원장(doctor) → 위생사(hygienist) → 상담실장(consultant) 순이고, 같은 역할 안에서는 `emr_staff.id` 순이다. 데스크(desk)와 `active = 0`은 뺀다
- 격자 한 칸은 30분이다(프론트 상수 `SLOT_MIN = 30`)
- 프론트 색·모서리·그림자는 `frontend/app/globals.css` 토큰으로만 쓴다(hex·임의 값·Tailwind 기본 색 이름 금지). 부품은 `components/ui`만 쓴다. EMR 창의 옛 프로그램 색은 `--emr-*` 토큰이고 다크 모드에서도 같은 값이다
- 화면 문구는 아래 Task에 적힌 그대로 쓴다. 표기 규칙: 격자 배지 "예약 미확인", 상단 건수 "예약 미확인 N", 패널 배지 "예약 확인 문자 무응답", 응급 자리 "분류 전"
- 커밋 전 `make fmt` → `make lint` → `make test`. `git add`는 파일 이름으로 한다(괄호·대괄호가 든 경로는 `git diff --name-only -z -- frontend | xargs -0 git add`). 커밋 끝에 아래 두 줄을 붙인다
  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01G62cGwhYixc5LpBLhj8iGp
  ```

## Review Focus

1. 휴진일(목·일)과 토요일(13:30 마감)에 격자가 깨지지 않고 "휴진일입니다"·짧은 격자가 나와야 한다 → Task 1 `test_schedule_closed_day`·`test_schedule_saturday_close`, Task 5 수동 확인
2. 같은 담당자·같은 시각에 취소와 새 예약이 겹칠 때, "취소·노쇼 보기"를 켜도 두 카드가 서로 가리지 않아야 한다 → Task 1 `test_schedule_includes_cancelled_overlap`, Task 5 수동 확인
3. 데이터 범위 밖 날짜나 형식이 틀린 날짜를 URL에 직접 넣으면, 오류 띠나 범위 안 날짜로 처리되고 화면이 깨지지 않아야 한다 → Task 1 `test_schedule_bad_date_422`, Task 5 수동 확인
4. 기준 환자에게 없는 정보(미수 0, 주의사항 없음, 남은 치료 없음)가 있어도 따라 보기가 그 단계를 건너뛰지 않고 "없음"을 보여야 한다 → Task 3 `test_tour_counts_missing`, Task 7 수동 확인
5. 따라 보기 도중 사용자가 EMR 창의 탭·검색을 눌러도 화면이 꼬이지 않아야 한다. EMR 창은 잠겨 반응하지 않고, 이전·다음·그만 보기와 머리 띠 돌아가기만 움직인다 → Task 7 수동 확인

## 파일 구조

| 파일 | 하는 일 |
|---|---|
| `backend/emr.py` | 조회 함수: `schedule_day`, `search_patients`, `patient_record`, `patient_summary`, `tour_patient` |
| `backend/api.py` | `_open_source`, 라우트 `/emr/schedule`·`/emr/patients`·`/emr/patients/{chart_no}`·`/emr/tour`, `/clinic`도 `_open_source` 사용 |
| `tests/backend/test_emr.py` | 픽스처 `emr_paths`와 함수·라우트 테스트 |
| `frontend/lib/emr-api.ts` | API 타입 |
| `frontend/app/globals.css` | `--emr-*` 토큰 |
| `frontend/lib/nav.ts` | `COMING_SOON`에서 `schedule`·`emr` 제거 |
| `frontend/components/patient-summary.tsx` | 환자 요약 카드(예약표 패널, 나중에 준비표) |
| `frontend/app/(portal)/schedule/page.tsx`, `frontend/components/schedule/*` | 예약표 화면 |
| `frontend/app/(portal)/emr/page.tsx`, `frontend/components/emr/*` | 전환 전 EMR 화면, EMR 창, 설명 카드 |

---

### Task 1: 예약표 조회 `GET /emr/schedule`

**Files:**
- Create: `backend/emr.py`, `tests/backend/test_emr.py`
- Modify: `backend/api.py`

**Interfaces:**
- Produces:
  - `schedule_day(conn: sqlite3.Connection, day: date, today: date, range_start: date) -> dict`
  - 반환 모양: `{date, hours: {open, close, lunch: [s, e], closed: bool}, staff: [{id, name, role}], appointments: [Appt], range: {from, to}, counts: {total, treatment, hygiene, consult, unconfirmed}}`
    - `role`: `원장`·`위생사`·`상담실장`
    - `counts`에는 취소·노쇼를 넣지 않는다. `unconfirmed`는 그날이 기준일일 때만 센다
  - `Appt` 모양: `{id, time "HH:MM", duration, staff_id, staff_name, staff_role, type, procedure, tooth, status, unconfirmed, chart_no, name, new_patient, plan_step: {seq, total} | None, booked_via, booked_at "YYYY-MM-DD HH:MM", call: {uid, started_at} | None}`
    - `type`: `treatment`·`hygiene`·`consult`
    - `status`: `scheduled`·`completed`·`cancelled`·`noshow`
    - `tooth`: 계획 항목의 치아이고, `00`이나 계획이 없으면 None
    - 정렬은 시각, 그다음 id 순
  - `_open_source(paths) -> sqlite3.Connection` (`api.py`): 파일이 없으면 503 `NO_DATA`. `/clinic`도 이것을 쓴다
  - `GET /emr/schedule?date=` → `schedule_day(...)`. `date`를 빼면 기준일, 형식이 틀리면 422

- [ ] **Step 1: 픽스처 `emr_paths(source_paths)`를 쓴다** (`tests/backend/test_emr.py` 안)

`source_paths`의 source.sqlite에 아래를 더하고 경로를 그대로 돌려준다. NOT NULL 칸은 아무 값이나 채운다.
- `emr_clinic.hours_json`: `{"open":"09:30","weekday_close":"18:30","saturday_close":"13:30","lunch":["13:00","14:00"],"closed_weekdays":[3,6]}`
- `emr_staff`: 1 강태현 doctor, 2 임재윤 hygienist, 3 오지수 desk, 4 송민지 consultant, 5 퇴사자 hygienist `active=0`
- `emr_procedures`: `CROWN` 크라운, `CONSULT` 상담
- `emr_patients` 3의 등록일을 `2026-11-01`로 바꾼다(신환). 1·2는 그대로 둔다
- 치료계획: plan 201(환자 1)의 항목 1 `done`, 2 `open` 치아 `36`, 3 `open`. 각각 id 211·212·213, `CROWN`
- 예약
  - 101: 환자1, staff1, `2026-12-01 10:00:00`, 60분, treatment, CROWN, scheduled, `confirmed=0`, phone, 잡은 시각 `2026-11-17 14:20:00`, `plan_item_id=212`
  - 102: 환자2, staff2, `09:30`, 30분, hygiene, scaling, scheduled, `confirmed=1`, desk, `2026-06-01 10:00:00`
  - 103: 환자3, staff1, `11:00`, 30분, treatment, CONSULT, cancelled
  - 104: 환자3, staff1, `11:00`, 30분, treatment, CONSULT, scheduled, `confirmed=0`, desk, `2026-11-30 09:00:00`
  - 90: 환자1, `2026-11-17 10:00`, completed
  - 91: 환자1, `2026-03-02 10:00`, noshow
  - 92: 환자1, `2025-11-20 10:00`, noshow
  - 110: 환자2, `2026-12-15 14:00`, scheduled
- `ax_calls`: `c-0003` 환자1 inbound `2026-11-17 14:20:00`, `has_transcript=1`
- `emr_payments`: 환자1 `(100000 due, 15000 paid, 2026-11-17)`과 `(50000, 0, 2026-12-01)`, 환자2 `(0, 10000, 2026-11-01)`
- `emr_patient_alerts`
  - 환자1: `ANTICOAG` `2026-01-01`, `PREGNANT` `2026-01-01` ends `2026-12-01`
  - 환자3: `PENICILLIN` `2026-12-02`
  - `emr_alert_types`에 이 세 코드를 넣는다(항응고제 복용, 임신 중, 페니실린 알레르기)
- `emr_messages`: 환자1 out `2026-11-30 10:00:00` `appt_confirm` `related_appointment_id=101` "내일 10시 예약 확인 부탁드립니다"

- [ ] **Step 2: 실패하는 테스트를 쓴다**

```python
TODAY, START = date(2026, 12, 1), date(2024, 12, 1)


def test_schedule_staff_order_excludes_desk_and_inactive(conn):
    d = schedule_day(conn, TODAY, TODAY, START)
    assert [(s["name"], s["role"]) for s in d["staff"]] == [
        ("강태현", "원장"),
        ("임재윤", "위생사"),
        ("송민지", "상담실장"),
    ]


def test_schedule_appointment_fields(conn):
    a = {x["id"]: x for x in schedule_day(conn, TODAY, TODAY, START)["appointments"]}
    assert (a[101]["time"], a[101]["duration"], a[101]["unconfirmed"]) == ("10:00", 60, True)
    assert (a[101]["staff_name"], a[101]["staff_role"]) == ("강태현", "원장")
    assert a[101]["plan_step"] == {"seq": 2, "total": 3} and a[101]["tooth"] == "36"
    assert a[101]["call"] == {"uid": "c-0003", "started_at": "2026-11-17 14:20"}
    assert (
        a[102]["unconfirmed"] is False and a[102]["plan_step"] is None and a[102]["call"] is None
    )
    assert a[104]["procedure"] == "진찰" and a[104]["new_patient"] is True
    assert a[101]["new_patient"] is False


def test_schedule_includes_cancelled_overlap(conn):
    rows = [
        x for x in schedule_day(conn, TODAY, TODAY, START)["appointments"] if x["time"] == "11:00"
    ]
    assert sorted(x["status"] for x in rows) == ["cancelled", "scheduled"]


def test_schedule_counts_skip_cancelled(conn):
    assert schedule_day(conn, TODAY, TODAY, START)["counts"] == {
        "total": 3,
        "treatment": 2,
        "hygiene": 1,
        "consult": 0,
        "unconfirmed": 2,
    }


def test_unconfirmed_only_on_today(conn):
    d = schedule_day(conn, date(2026, 12, 15), TODAY, START)
    assert [x["unconfirmed"] for x in d["appointments"]] == [False]
    assert d["counts"]["unconfirmed"] == 0


def test_schedule_closed_day(conn):
    assert schedule_day(conn, date(2026, 12, 3), TODAY, START)["hours"]["closed"] is True


def test_schedule_saturday_close(conn):
    h = schedule_day(conn, date(2026, 12, 5), TODAY, START)["hours"]
    assert (h["close"], h["closed"]) == ("13:30", False)


def test_schedule_range(conn):
    assert schedule_day(conn, TODAY, TODAY, START)["range"] == {
        "from": "2025-11-20",
        "to": "2026-12-15",
    }


def test_schedule_route_default_and_bad_date(client):
    assert client.get("/emr/schedule").json()["date"] == "2026-12-01"
    assert client.get("/emr/schedule", params={"date": "2026-13-01"}).status_code == 422


def test_schedule_route_without_index(client_source_only):
    assert client_source_only.get("/emr/schedule").status_code == 200  # app.sqlite가 없어도 된다
```

픽스처 `conn`은 `emr_paths`의 source.sqlite를 읽기 전용으로 연 연결이다. `client`는 `get_db_paths`를 `(app 경로, source 경로)`로 바꿔 끼운 TestClient다. `client_source_only`는 app 경로가 없는 파일을 가리키게 둔다. `test_api.py`의 `client_with` 방식을 따른다.

- [ ] **Step 3: 실패를 확인한다**

Run: `uv run pytest tests/backend/test_emr.py -q`
Expected: FAIL — `ModuleNotFoundError: backend.emr`

- [ ] **Step 4: `schedule_day`, `_open_source`, 라우트를 구현한다**

`backend/emr.py` 모듈 독스트링은 이렇게 쓴다: 전환 전 EMR·예약표 조회(앱 설계 §9). `source.sqlite` 읽기 전용, 환자 사실은 기준일 기준. 신환은 `registered_at ≥ range_start`이고 그 예약 전에 `completed`가 없을 때다. 잡은 통화는 같은 환자이고 `ax_calls.started_at = created_at`이다. `/clinic`도 `_open_source`로 바꾸고 기존 `test_clinic*`이 그대로 통과하는지 본다.

- [ ] **Step 5: 통과를 확인한다**

Run: `uv run pytest tests/backend -q`
Expected: 모두 PASS

- [ ] **Step 6: 커밋한다** — `backend: 예약표 조회 GET /emr/schedule`

---

### Task 2: 환자 검색·한 환자 전체 `GET /emr/patients`, `GET /emr/patients/{chart_no}`

**Files:** Modify `backend/emr.py`, `backend/api.py`, `tests/backend/test_emr.py`

**Interfaces:**
- Consumes: Task 1의 `_open_source`, 픽스처
- Produces:
  - `search_patients(conn, q: str, today: date) -> list[dict]`: `[{chart_no, name, age, sex}]`
    - 이름이나 차트 번호(대소문자 무시)에 `q`가 들어간 환자
    - 차트 번호 순, 최대 20명
    - `sex`는 `남`·`여`
  - `patient_summary(conn, patient_id: int, today: date) -> dict`
    - `{balance: int, alerts: [{code, label, category}], last_visit: "YYYY-MM-DD" | None, upcoming: [{id, date, time, procedure, staff}], noshow_1y: int, cancelled_1y: int}`
    - `upcoming`는 기준일 포함 그 뒤의 scheduled 예약이다(`staff`는 담당자 이름). 지금 보는 예약을 빼는 일은 화면이 한다
  - `patient_record(conn, chart_no: str, today: date) -> dict | None`
    - `{patient, summary, appointments, treatments, payments, claims, plans, alerts, messages}`
    - `patient`: `{chart_no, name, age, sex, birth, phone, registered, private_insurance, family: [{chart_no, name}], guardian: {chart_no, name} | None}`
    - 나머지 목록은 `export.py`의 `emr[...]`와 같은 칸이다. 예약에는 `id`·`plan_item_id`, 진료에는 차트 서술 `note`, 메시지에는 `related_appointment_id`를 더한다
    - 모두 오래된 순이다
  - `GET /emr/patients?q=`: 빈 `q`(공백만 포함)는 422
  - `GET /emr/patients/{chart_no}`: 없으면 404 "환자를 찾을 수 없습니다", 대소문자를 무시한다

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
def test_summary_today_boundaries(conn):
    s = patient_summary(conn, 1, TODAY)
    assert s["balance"] == 85000  # 기준일 당일 수납분 제외
    assert [a["code"] for a in s["alerts"]] == ["ANTICOAG"]  # ends_at 당일부터 안 보임
    assert s["last_visit"] == "2026-11-17"
    assert (s["noshow_1y"], s["cancelled_1y"]) == (1, 0)  # 2025-11-20은 365일 밖
    assert [u["id"] for u in s["upcoming"]] == [101]


def test_summary_overpay_is_zero_and_future_alert_hidden(conn):
    assert patient_summary(conn, 2, TODAY)["balance"] == 0
    assert patient_summary(conn, 3, TODAY)["alerts"] == []


def test_search_patients(conn):
    assert [p["chart_no"] for p in search_patients(conn, "c-0000", TODAY)] == [
        "C-00001",
        "C-00002",
        "C-00003",
    ]
    assert search_patients(conn, "영희", TODAY)[0]["name"] == "박영희"


def test_patient_record_lists(conn):
    r = patient_record(conn, "c-00001", TODAY)
    assert r["patient"]["chart_no"] == "C-00001"
    assert [
        m["related_appointment_id"] for m in r["messages"] if m["trigger"] == "appt_confirm"
    ] == [101]
    assert {p["status"] for p in r["plans"]} == {"done", "open"}
    assert patient_record(conn, "C-99999", TODAY) is None


def test_patient_routes(client):
    assert client.get("/emr/patients", params={"q": " "}).status_code == 422
    assert client.get("/emr/patients/C-99999").status_code == 404
    assert client.get("/emr/patients/C-00001").json()["summary"]["balance"] == 85000
```

- [ ] **Step 2: 실패를 확인한다** — `uv run pytest tests/backend/test_emr.py -q` → 새 테스트 FAIL(ImportError)
- [ ] **Step 3: 세 함수와 라우트를 구현한다.** 가족은 같은 `family_id`의 다른 환자, 보호자는 `guardian_patient_id`다
- [ ] **Step 4: 통과를 확인한다** — `uv run pytest tests/backend -q` → 모두 PASS
- [ ] **Step 5: 커밋한다** — `backend: 환자 검색·한 환자 전체 GET /emr/patients`

---

### Task 3: 따라 보기 기준 환자 `GET /emr/tour`

**Files:** Modify `backend/emr.py`, `backend/api.py`, `tests/backend/test_emr.py`

**Interfaces:**
- Consumes: Task 2의 `patient_summary`
- Produces:
  - `tour_patient(conn, today: date) -> dict | None`
    - `{chart_no, name, time, appointment_id, has: {balance, alert, remaining}, appointments_today}`
    - `remaining`: 오늘 예약의 `plan_item_id`를 뺀, 그 환자의 열린(`open`) 계획 항목이 있는지
    - 고르는 순서: `has`의 참 개수가 큰 순 → 예약 미확인 먼저 → 시각 → 차트 번호
    - `appointments_today`: 기준일의 취소·노쇼가 아닌 예약 수
    - 기준일 예약이 없으면 None
  - `GET /emr/tour`: None이면 404 "기준일 예약이 없습니다"

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
def test_tour_picks_most_items(conn):
    t = tour_patient(conn, TODAY)
    assert (t["chart_no"], t["appointment_id"], t["time"]) == ("C-00001", 101, "10:00")
    assert t["has"] == {
        "balance": True,
        "alert": True,
        "remaining": True,
    }  # 212는 오늘 것이라 빠지고 213이 남음
    assert t["appointments_today"] == 3


def test_tour_counts_missing(emr_paths):
    # 남은 항목 213을 done으로 바꾸면 remaining만 거짓, 그래도 가장 많이 가진 환자로 뽑힌다
    rw(emr_paths, "UPDATE emr_treatment_plan_items SET status = 'done' WHERE id = 213")
    t = tour_patient(ro(emr_paths), TODAY)
    assert t["chart_no"] == "C-00001" and t["has"]["remaining"] is False


def test_tour_tie_prefers_unconfirmed_then_time(emr_paths):
    # 모두 0개면 미확인인 101(10:00)·104(11:00) 중 이른 101. 102는 확인돼 뒤로
    rw(emr_paths, "DELETE FROM emr_patient_alerts WHERE patient_id = 1")
    rw(emr_paths, "DELETE FROM emr_payments WHERE patient_id = 1")
    rw(emr_paths, "UPDATE emr_treatment_plan_items SET status = 'done' WHERE id = 213")
    assert tour_patient(ro(emr_paths), TODAY)["chart_no"] == "C-00001"
```

`rw(paths, sql)`은 source.sqlite에 쓰기 연결로 실행·커밋하고, `ro(paths)`는 읽기 전용 연결을 새로 연다. 둘 다 테스트 파일 안의 도우미이고, 픽스처 `conn`도 `ro`를 쓴다.

- [ ] **Step 2: 실패를 확인한다** → FAIL(ImportError)
- [ ] **Step 3: 구현한다**
- [ ] **Step 4: 통과를 확인한다** — `uv run pytest tests/backend tests/test_no_ground_truth_import.py -q` → 모두 PASS
- [ ] **Step 5: 커밋한다** — `backend: 따라 보기 기준 환자 GET /emr/tour`

---

### Task 4: 프론트 공통 — 타입, EMR 토큰, 환자 요약 카드

**Files:**
- Create: `frontend/lib/emr-api.ts`, `frontend/components/patient-summary.tsx`
- Modify: `frontend/app/globals.css`, `frontend/lib/nav.ts`

**Interfaces:**
- Consumes: Task 1–3 응답 모양, 앞 작업의 `getJson`·`ApiError`(`lib/api.ts`)
- Produces:
  - `lib/emr-api.ts`
    - 타입 `ScheduleDay`, `Appt`, `PatientSummary`, `PatientRecord`, `PatientHit`, `TourPatient`. 백엔드 칸 이름 그대로 쓴다
    - 함수 `fetchSchedule(date?)`, `searchPatients(q)`, `fetchPatient(chartNo)`, `fetchTour()`
    - 라벨 `TYPE_LABEL`(treatment 진료·hygiene 위생·consult 상담), `STATUS_LABEL`(scheduled 예정·completed 완료·cancelled 취소·noshow 노쇼)
  - `<PatientSummary summary={PatientSummary} patient={{chart_no, name, age, sex, phone, guardian}} appt={Appt | null} today={string} />`
  - 토큰
    - `--emr-title` `#3b4a5c`, `--emr-title-fg` `#ffffff`, `--emr-chrome` `#dce1e7`, `--emr-tab-off` `#c3cbd4`
    - `--emr-line` `#9aa4b0`, `--emr-line-soft` `#c9d0d8`, `--emr-surface` `#ffffff`, `--emr-surface-2` `#f4f6f8`
    - `--emr-fg` `#1f2933`, `--emr-fg-muted` `#6b7785`
    - `--emr-alert-bg` `#fdecec`, `--emr-alert-fg` `#8a1f1f`, `--emr-alert-line` `#e8a3a3`
    - `--emr-row-on` `#fff7cc`
    - 모두 `:root`에만 두고 다크 블록에는 넣지 않는다. `@theme inline`에 `--color-emr-*`로 잇는다

패널은 카드 하나이고, 묶음 사이는 얇은 구분선(`border-t`)이다. 상자 안에 상자를 넣지 않는다. 위에서부터 이 순서다(문구 그대로).
1. 이름(굵게), `{chart_no} · {age}세 {sex}`
2. "이 예약"(appt가 있을 때, 한 단계 큰 글자)
   - `{MM-DD (요일)} {time} · {duration}분`
   - `{procedure}{tooth ? " #" + tooth : ""} · {staff_role} {staff_name}`
   - plan_step이 있으면 `치료계획 {total}개 중 {seq}번째`
   - 배지: 상태(`muted`), `unconfirmed`면 "예약 확인 문자 무응답"(`warn`), `new_patient`면 "신환"(`info`)
3. 작은 제목 "어떻게 잡혔나"
   - `{전화 | 데스크} · {booked_at}`
   - call이 있으면 `통화 있음 · {call.started_at}` 아래에 `응급 여부` + 배지 "분류 전"(`outdated` variant)
4. 작은 제목 "이 환자의 일정"
   - `마지막 내원 · {last_visit | "—"}`
   - `앞으로 · {날짜 시각 시술}`: 지금 보는 예약을 빼고 최대 3개, 없으면 "없음"
   - `최근 1년 · 노쇼 {n} · 취소 {m}`
5. 작은 제목 "연락": `{phone}`, 보호자가 있으면 ` · 보호자 {name}({chart_no})`
6. 배지 줄: 주의사항마다 `주의 · {label}`(`danger`), 미수가 0보다 크면 `미수 {n,원}`(`warn`). 둘 다 없으면 이 줄을 두지 않는다
7. 버튼 "전자차트 열기" → `/emr?chart={chart_no}&view=chart`(`Button asChild` + `Link`)

- [ ] **Step 1: 위 파일을 만든다.** `nav.ts`의 `COMING_SOON`에서 `schedule`·`emr`을 뺀다. `SPACES.emr`은 남긴다
- [ ] **Step 2: 검사한다** — `make lint && npm --prefix frontend run build` → 성공
- [ ] **Step 3: 커밋한다** — `frontend: EMR API 타입·옛 프로그램 토큰·환자 요약 카드`

---

### Task 5: 예약표 화면 `/schedule`

**Files:**
- Create: `frontend/app/(portal)/schedule/page.tsx`, `frontend/components/schedule/schedule-view.tsx`, `frontend/components/schedule/day-grid.tsx`
- Consumes: Task 4의 `fetchSchedule`, `fetchPatient`, `<PatientSummary>`

화면 규칙(시안 "일별 예약표" 장이 모습 기준).
- URL: `/schedule?date=YYYY-MM-DD&appt={id}`
  - `date`가 없거나 `range` 밖이면 기준일로 바꾼다(`router.replace`)
  - `useSearchParams`를 쓰므로 `page.tsx`는 `<Suspense>`로 감싼다
- 머리말: 제목 "예약", 설명 "날짜별 예약을 담당자별로 봅니다. 예약을 잡거나 바꾸는 일은 EMR에서 합니다."
- 위쪽 줄
  - ◀ / 날짜 `M월 D일 (요일)` / ▶ / "오늘" / date input(min·max = range)
  - Checkbox "취소·노쇼 보기"(URL에 `all=1`)
  - 오른쪽에 `총 {total}건 · 진료 {n} · 위생 {n} · 상담 {n}`, 그날이 기준일이면 ` · 예약 미확인 {unconfirmed}`
- 격자(`day-grid.tsx`)
  - CSS grid: 첫 열 시각, 그 뒤 담당자 열. 행은 `open`부터 `close`까지 30분씩
  - 카드는 `grid-row: {시작 행} / span {duration/30}`으로 소요 시간만큼 늘린다
  - 점심 시간 행은 모든 열을 덮는 띠 "{s} – {e} 점심시간" 하나다
  - `closed`면 격자 대신 "휴진일입니다"
  - 같은 열·같은 시작 행에 카드가 둘 이상이면, 그 칸을 `display:grid; grid-auto-flow: column`인 감싸개로 나눠 나란히 놓는다
- 카드는 `<button>`이고 색은 종류별이다: treatment `bg-accent text-accent-foreground`, hygiene `bg-secondary`, consult `border bg-card`
  - 내용 줄: `{time} {name}`, `{procedure} · {duration}분`, plan_step이 있으면 `치료계획 {seq}/{total}`
  - 배지: "신환"(`info`), "예약 미확인"(`warn`)
  - 취소·노쇼는 기본으로 숨기고, 보이면 `opacity-50`에 상태를 적는다
  - 고른 카드는 `ring-2 ring-primary`
- 카드를 누르면 URL에 `appt`가 들어가고 오른쪽에 `<PatientSummary>`가 열린다(`fetchPatient(chart_no)`의 summary·patient + 고른 appt). 좁은 화면에서는 격자 아래로 내려간다
- 상태 문구: 불러오는 중 "불러오는 중…", 오류는 `danger` 안내 띠(앞 작업과 같은 모양)

- [ ] **Step 1: 만든다**
- [ ] **Step 2: 검사한다** — `make lint && npm --prefix frontend run build` → 성공
- [ ] **Step 3: 실데이터로 확인한다**(`make dev`)
  - 기준일 격자에서 60분 카드가 30분 카드의 두 배 높이다
  - "예약 미확인" 배지 수와 상단 건수가 같다
  - "취소·노쇼 보기"를 켜면 흐린 카드가 나오고, 겹치는 카드는 나란히 놓인다
  - 12-03(목)은 "휴진일입니다", 12-05(토)는 13:30까지다
  - 날짜 이동과 date input이 범위 안에서만 움직이고, `?date=2099-01-01`을 직접 넣으면 기준일로 바뀐다
  - 카드를 누르면 패널 일곱 묶음이 순서대로 나온다. 새로고침해도 날짜와 선택이 남는다
  - "전자차트 열기" 링크가 `/emr?chart=…&view=chart`다
- [ ] **Step 4: 커밋한다** — `frontend: 일별 예약표 — 소요 시간 카드·환자 요약 패널`

---

### Task 6: 전환 전 EMR 창 `/emr`

**Files:**
- Create: `frontend/app/(portal)/emr/page.tsx`, `frontend/components/emr/emr-view.tsx`, `frontend/components/emr/emr-window.tsx`, `frontend/components/emr/emr-desk.tsx`, `frontend/components/emr/emr-book.tsx`, `frontend/components/emr/emr-chart.tsx`, `frontend/components/emr/odontogram.tsx`
- Consumes: Task 4의 `fetchPatient`, `searchPatients`, `fetchSchedule`, `fetchTour`, `--emr-*` 토큰
- Produces (Task 7이 쓴다):
  ```ts
  type EmrMode = "desk" | "book" | "chart";
  type EmrNav = { mode: EmrMode; tab: string; panel: string };  // tab: 히스토리 탭, panel: 차트 옆 패널 탭
  <EmrWindow record={PatientRecord} nav={EmrNav} onNav={(next: EmrNav) => void}
             moves={number} highlight={"balance" | "alert" | "plan" | "messages" | null}
             onPickPatient={(chartNo: string) => void} locked={boolean} />
  ```
  - `EMR_TABS = ["진료기록", "수납", "청구", "치료계획", "주의사항", "예약", "문자"]`
  - `EMR_PANELS = ["기본", "메모", "계획", "접수", "예약", "수납", "가족"]`
  - 기본 nav는 `{mode: "desk", tab: "진료기록", panel: "기본"}`
  - `locked`가 참이면 창 위에 옅은 막(`bg-background/40`)을 덮어 탭·버튼·검색이 반응하지 않게 한다. 막을 누르면 창 위쪽에 "따라 보기 중에는 다음 버튼으로 넘어갑니다"를 2초 띄운다

규칙
- 구조와 칸은 와이어프레임 `emrTop`·`emrDesk`·`emrBook`·`emrChartView`·`emrPane`·`teethSvg`를 옮긴다
  - HTML 문자열을 만들지 않고 React로 짠다
  - 라벨표는 와이어프레임 466–470행을 쓴다. `PLAN_ST`에는 `replaced: "대체"`를 더한다
  - 비활성 도구 버튼은 `disabled`로 둔다
  - 나이 표시(노인·어른·아이)는 넣지 않는다
- 상단 검색은 `searchPatients`다. 300ms 디바운스 뒤 최대 10개를 보이고, 고르면 `onPickPatient`를 부른다
- 접수 목록·오늘 예약은 기준일 `fetchSchedule()`에서 가져온다
- 상태줄: `덴탈차트 Pro v3.2 · 가상 프로그램` · `파트: {담당 파트}` · `이 환자에서 화면 이동 {moves}회` · `읽기 전용` · `{기준일}`
- `highlight`가 가리키는 영역에 `ring-2 ring-primary ring-offset-2`를 친다
  - balance: 당일 수납 상자
  - alert: Alert 줄
  - plan: 차트 옆 패널
  - messages: 데스크 히스토리 상자
- 옛 프로그램 모양(각진 모서리, 회청색 띠, 12px 글자)은 `--emr-*` 토큰과 `rounded-none`으로 낸다. 앱의 shadcn 부품은 EMR 창 안에 쓰지 않는다. EMR 창 안의 버튼·탭은 순수 `<button>`이고, 이 예외는 `emr-window.tsx` 머리 주석에 적는다
- `emr-view.tsx`
  - URL `/emr?chart=…&view=chart|desk`를 읽는다. chart가 없으면 `fetchTour()`의 chart_no로 연다
  - nav·moves 상태를 갖는다
  - 이동 수: mode·tab·panel이 바뀔 때마다 +1(같은 값이면 그대로), 환자가 바뀌면 0과 기본 nav
  - `view=chart`면 처음 nav의 mode가 chart다
  - 404면 "환자를 찾을 수 없습니다"와 기준 환자로 가는 링크를 둔다

- [ ] **Step 1: 만든다**
- [ ] **Step 2: 검사한다** — `make lint && npm --prefix frontend run build` → 성공
- [ ] **Step 3: 실데이터로 확인한다**
  - 데스크·예약·전자차트가 와이어프레임과 같은 칸으로 나온다
  - 히스토리 탭 7개와 옆 패널 탭 7개가 동작한다
  - 탭을 바꿀 때마다 이동 수가 1씩 오르고, 검색으로 환자를 바꾸면 0이 된다
  - 예약표 "전자차트 열기"로 오면 전자차트가 열린다
  - 다크 모드에서도 EMR 창 색이 그대로다
- [ ] **Step 4: 커밋한다** — `frontend: 전환 전 EMR 창 — 데스크·예약·전자차트, 화면 이동 수`

---

### Task 7: 설명 카드 — 흩어짐 지도 → 따라 보기 → 하루 규모

**Files:**
- Create: `frontend/components/emr/explain-card.tsx`, `frontend/components/emr/tour.ts`
- Modify: `frontend/components/emr/emr-view.tsx`
- Consumes: Task 6의 `EmrWindow`·`EmrNav`, Task 3의 `TourPatient`

`tour.ts`: 단계 정의는 순수 데이터와 함수다.
```ts
type TourStep = { key: "balance" | "alert" | "remaining" | "confirm"; what: string; where: string; nav: EmrNav; highlight: "balance" | "alert" | "plan" | "messages" };
export const TOUR_STEPS: TourStep[]  // 아래 순서와 문구 그대로
export function stepValue(step, record: PatientRecord, apptId: number): { found: boolean; text: string }
export function stepWhy(step, record: PatientRecord, found: boolean): string
```

| key | what | where | nav | 찾았을 때 text | 없을 때 text |
|---|---|---|---|---|---|
| balance | 미수 잔액 | 데스크 › 당일 수납 | desk / 진료기록 / 기본 | `{n}원` | 미수 없음 |
| alert | 주의사항 | 전자차트 › 맨 위 Alert | chart / 진료기록 / 기본 | 첫 주의사항 label | 주의사항 없음 |
| remaining | 남은 치료 | 전자차트 › 옆 패널 계획 | chart / 진료기록 / 계획 | `{시술}{#치아}` 외 n건 | 남은 치료 없음 |
| confirm | 예약 확인 문자 응답 | 데스크 › 히스토리 문자 | desk / 문자 / 계획 | 확인됨 | 무응답 |

- remaining: 열린 계획 항목 중 오늘 예약(`apptId`)의 `plan_item_id`가 아닌 것
- confirm: 오늘 예약의 `confirmed`이거나 그 예약을 가리키는 `reply` 메시지가 있으면 "확인됨"이다. 확인 문자 자체가 없으면 "확인 문자 없음"이고, 이것은 "없음"으로 친다
- 이동 수(기준 환자, 데스크 시작): balance 0 → alert +1(mode) → remaining +1(panel) → confirm +2(mode, tab). 따라 보기 끝의 N은 실제로 센 값을 쓴다(상수로 두지 않는다)

`stepWhy` 문구:
- 찾았을 때
  - balance: "오늘 수납할 때 함께 안내해야 합니다. 이 금액은 데스크 화면에만 있습니다."
  - alert: 주의사항 문장표(아래)의 앞 항목. 표에 없으면 "진료 전에 원장님께 알려야 하는 정보입니다. 데스크 화면에는 없어 전자차트로 넘어왔습니다."
  - remaining: "오늘 진료가 끝나면 다음 회차를 잡아야 합니다. 남은 치료는 차트 옆 계획 탭에만 있습니다."
  - confirm 무응답: "확인 문자에 답이 없었습니다. 노쇼일 수 있어 진료 전에 전화를 한 번 더 겁니다."
  - confirm 확인됨: "확인 문자에 답했습니다. 문자 기록은 데스크의 다른 탭에 있어 또 옮겨 왔습니다."
- 없을 때(공통): "없어도 확인하려면 여기까지 와야 합니다."

주의사항 문장표(이 순서가 우선순위):

| 코드 | 문장 |
|---|---|
| ANTICOAG | 항응고제를 먹는 환자는 피가 잘 멈추지 않아, 진료 전에 원장님께 알려야 합니다. |
| PENICILLIN | 페니실린 알레르기가 있으면 처방 약을 바꿔야 합니다. |
| LATEX | 라텍스 알레르기가 있으면 장갑과 기구를 바꿔야 합니다. |
| EPI | 에피네프린에 민감하면 마취약을 바꿔야 합니다. |
| PROSTHESIS | 인공관절·인공판막이 있으면 시술 전 예방 항생제가 필요할 수 있습니다. |
| OSTEO | 골다공증 약을 먹으면 발치·임플란트 전에 확인이 필요합니다. |
| PREGNANT | 임신 중이면 X-ray와 약을 조심해야 합니다. |
| SYNCOPE | 마취 후 실신한 적이 있어 체어 옆에서 지켜봐야 합니다. |
| HTN | 혈압이 높으면 마취 전에 혈압을 잽니다. |
| DM | 당뇨가 있으면 상처가 늦게 낫고 감염을 조심해야 합니다. |
| UPRIGHT | 체어를 눕히지 말고 진료해야 합니다. |
| 그 밖 | 진료 전에 미리 알아 두어야 하는 주의사항입니다. |

카드(`explain-card.tsx`, 시안 "합친 안" 세 장)
- 단계는 `"map" | "tour" | "done"`이고 `tour`면 지금 단계 번호를 갖는다
- 제목: `{기준 환자} 님 진료 전에 알아야 할 것이 흩어진 곳`
- map
  - 맨 위에 "▶ 따라 보기" 버튼
  - 네 줄: 번호, what, where. 값은 아직 보이지 않는다
- tour
  - 상단(EMR 창 위)에 `accent` 띠: "따라 보기 중" · "데스크 직원이 {time} {name} 님을 맞기 전에 하는 일" · "그만 보기"
  - 지난 줄은 체크 표시와 그 값(text)
  - 지금 줄은 `bg-accent` + `ring-primary`이고, 그 아래에 값과 stepWhy
  - 이전·다음 버튼이 있다. 마지막 단계의 "다음"은 "마치기"다
  - 단계에 들어갈 때 기준 환자의 EMR을 그 nav로 바꾸고, highlight를 그 단계로 둔다
  - 이동 수는 Task 6의 규칙대로 오른다(같은 nav면 오르지 않는다)
- done
  - 네 줄 모두 체크 표시와 값
  - 계산 상자: `환자 한 명 · 화면 이동 {N}회` / `× 오늘 예약 환자 · {appointments_today}명` / `데스크의 하루 · {N×M}회`
  - 점선 상자: "준비표가 생기면 — 같은 정보가 한 줄에, 화면 이동 0회"
  - "다시 보기" 링크
- 따라 보기 중(tour)에는 `EmrWindow`에 `locked`를 넘겨 EMR 창을 잠근다. 움직일 수 있는 것은 이전·다음·그만 보기와 머리 띠 돌아가기뿐이다. 그만 보기는 map으로 돌아가고, EMR 창은 그 자리에 둔다
- 들어오는 길
  - `view=chart`(예약표에서 옴): 카드는 한 줄 띠 "왜 화면을 옮겨 다니나? 따라 보기"로 접혀 있다. 누르면 map으로 펼친다
  - 따라 보기를 시작하면 기준 환자로 바꾸고 데스크·이동 0부터 시작한다
  - 그 밖(시작 화면에서 옴): map으로 펼쳐 둔다
- `fetchTour()`가 404면 카드 자리에 "기준일 예약이 없어 따라 보기를 할 수 없습니다"를 보인다

- [ ] **Step 1: 만든다**
- [ ] **Step 2: 검사한다** — `make lint && npm --prefix frontend run build` → 성공
- [ ] **Step 3: 실데이터로 확인한다**
  - 시작 화면 → 전환 전 EMR에서 map이 보인다
  - 따라 보기 네 단계가 EMR을 움직이고 강조가 맞다. "없음" 단계도 이동한다
  - done의 숫자가 실제 이동 수 × 기준일 예약 수다
  - 따라 보기 중 EMR 창을 누르면 반응하지 않고 안내가 잠깐 뜬다. 그만 보기를 누르면 map으로 돌아간다
  - 예약표에서 오면 카드가 접혀 있고, 눌러서 따라 보기를 하면 기준 환자로 바뀐다
- [ ] **Step 4: 커밋한다** — `frontend: 전환 전 EMR 설명 카드 — 흩어짐 지도·따라 보기·하루 규모`

---

### Task 8: 문서 반영

**Files:** `docs/치과 AX - 기능 정의.md`, `docs/치과 AX - 앱 설계.md`, `docs/superpowers/specs/2026-11-03-app-skeleton-design.md`

- [ ] **Step 1: 설계안 "문서 변경"대로 고친다**
  - 기능 정의 §4 앱 구성 일별 예약표
    - 패널 일곱 묶음(신환·치료 회차·노쇼·취소·연락 추가)
    - 카드는 소요 시간만큼
    - "예약 미확인"·"예약 확인 문자 무응답" 표기
  - 기능 정의 §4 ④ 전환 전 EMR: 설명 카드, 네 단계(미수·주의사항·남은 치료·예약 확인 문자), "없음", 들어오는 길
  - 기능 정의 §9
    - 착수 순서에서 "전환 전 EMR 화면·일별 예약표 →"를 지운다
    - "화면마다 옮길 때 와이어프레임에서 고쳐 옮긴다" 항목에 이번에 고친 것을 반영한다
  - 앱 설계 §9: `GET /emr/tour`, `/emr/*`은 `source.sqlite`만, 예약 한 건의 칸과 `summary`의 기준일 규칙. 값은 `backend/emr.py` 이름으로 가리킨다
  - 앱 뼈대 설계안: "`예약` 포함 — 와이어프레임에는 없다"를 "와이어프레임에도 있다"로 고친다
- [ ] **Step 2: 확인한다** — `make fmt && make lint && make test && npm --prefix frontend run build` → 모두 성공
- [ ] **Step 3: 커밋한다** — `docs: 예약표·전환 전 EMR 반영`
