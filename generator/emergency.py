"""6b단계 응급 내원 — 환자 측 응급 통화마다 원장 내원 1건 (가상 데이터 생성 설계 §2.6 (5)·§4.1 응급 내원 사실).
5단계(진료·수납·청구)·5b(차트)·6단계(통화) 뒤에 덧붙여 다른 예약·진료·통화가 밀리지 않는다 — 이 단계는 난수를 쓰지 않고
통화 번호 해시로 고른다.
- 통화 고르기·시각: emergency 플래그 통화 중 환자가 있는 것. 그날 통화 뒤 master.EMERGENCY_LEAD_MIN분이 지난 첫 빈 슬롯
  (원장 동시 진료·체어·점심·마감), 자리가 없으면 다음 진료일 개원부터. TODAY 전 진료일 늦은 통화 1건(장면)은 그 통화를 진료 끝
  무렵(scene_call_time)으로 옮겨 TODAY 오전 빈 시간 예약(scheduled)으로 — TODAY 준비표·오늘 화면에 보인다
- 처치: master.EMERGENCY_TREATMENT(상황 → 시술·치아 후보), 치아는 장부·치료계획에 없는 치아. 그날 전액 수납, 급여면 청구 1줄
  (확정 월은 paid), 차트는 응급 서술(9b가 덮어씀)
- 통화 사실: 응급 통화에 emergency_visit(그 내원), 그 환자의 뒤 통화는 장부가 바뀐 만큼 사실을 다시 계산
"""

import hashlib
import json
from datetime import date, datetime, timedelta

from generator import chart, records
from generator.calls import (
    CHILD_MAX_AGE,
    FMT,
    OPEN,
    Ledger,
    next_open_day,
    prev_open_day,
    recall_targets,
)
from generator.master import (
    CHAIRS,
    DOCTOR_CONCURRENCY,
    EMERGENCY_AFTER_VISIT_SCENE,
    EMERGENCY_CHILD_SCENES,
    EMERGENCY_DX,
    EMERGENCY_HISTORY_SCENES,
    EMERGENCY_LEAD_MIN,
    EMERGENCY_RESTORED_SCENES,
    EMERGENCY_SCENES,
    EMERGENCY_TREATMENT,
    EXTRACTION_CODES,
    FULL_MOUTH,
    HOURS,
    IMPLANT_CODE,
    PAYMENT_METHOD_WEIGHTS,
    PROCEDURE_DIAGNOSES,
    PROCEDURES,
    SLOT_MIN,
    TOOTH_TREATMENT_MIN_AGE,
    age_on,
    close_time,
    insured_on,
)
from generator.transcripts import pick

CODE = {p["name"]: p["code"] for p in PROCEDURES}
PROCEDURE = {p["code"]: p for p in PROCEDURES}
CONSULT = CODE["상담"]
RESTORED_CATEGORIES = ("restorative", "prosth")  # 때운·씌운 치아로 보는 시술군
LUNCH = tuple(datetime.strptime(t, "%H:%M").time() for t in HOURS["lunch"])
# TODAY 장면 — TODAY 전 진료일 마감 이만큼 전으로 옮긴 응급 통화(진료 끝 무렵이라 그날 자리가 없어 TODAY 오전)
SCENE_CALL_BEFORE_CLOSE_MIN = 25


def _hash(*parts) -> int:
    return int(hashlib.sha256(":".join(map(str, parts)).encode()).hexdigest(), 16)


def scene(uid: str) -> str:
    """그 통화의 응급 상황 — 7단계 caller_situation과 같은 고르기(transcripts.situation)"""
    return pick(uid, "scene", EMERGENCY_SCENES)


def emergency_calls(conn) -> list[dict]:
    """환자 측 응급 통화 (emergency 플래그 — 주 목적·부 주제 모두), 통화 시각순"""
    cur = conn.execute(
        "SELECT c.id, c.call_uid, c.patient_id, c.started_at, c.in_eval_pool, c.in_blind_pool,"
        " l.primary_purpose, l.used_fact FROM ax_calls c JOIN truth.truth_call_labels l USING (call_uid)"
        " WHERE l.emergency = 1 AND c.patient_id IS NOT NULL ORDER BY c.started_at, c.id"
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in cur]


class Board:
    """창 안 비취소 예약의 원장·체어 사용 구간 — 새 내원을 넣을 자리 찾기 (verify check_doctor_concurrency·
    check_chair_overlap과 같은 기준: 어떤 예약의 시작 시각에 진행 중인 건수)"""

    def __init__(self, conn, start: str):
        # (시작, 끝, 원장 여부, 체어) — 원장 동시 진료는 원장 전체로 센다(원장 1명 전제 — verify는 staff_id별)
        self.busy = []
        for at, minutes, chair, role in conn.execute(
            "SELECT a.scheduled_at, a.duration_min, a.chair_no, s.role FROM emr_appointments a"
            " JOIN emr_staff s ON s.id = a.staff_id WHERE a.status != 'cancelled' AND a.scheduled_at >= ?",
            (start,),
        ):
            s = datetime.strptime(at, FMT)
            self.busy.append((s, s + timedelta(minutes=minutes), role == "doctor", chair))

    def _ok(self, s: datetime, e: datetime, key, cap: int) -> bool:
        group = [b for b in self.busy if key(b)]
        points = [s] + [b[0] for b in group if s <= b[0] < e]
        for p in points:
            n = sum(1 for b in group if b[0] <= p < b[1]) + (1 if s <= p < e else 0)
            if n > cap:
                return False
        return True

    def place(self, s: datetime, minutes: int) -> int | None:
        """그 자리에 넣을 수 있으면 체어 번호, 없으면 None"""
        e = s + timedelta(minutes=minutes)
        if not self._ok(s, e, lambda b: b[2], DOCTOR_CONCURRENCY):
            return None
        for chair in range(1, CHAIRS + 1):
            if self._ok(s, e, lambda b, c=chair: b[3] == c, 1):
                return chair
        return None

    def add(self, s: datetime, minutes: int, chair: int) -> None:
        self.busy.append((s, s + timedelta(minutes=minutes), True, chair))


def _fits_hours(s: datetime, minutes: int) -> bool:
    e = s + timedelta(minutes=minutes)
    close = datetime.combine(s.date(), datetime.strptime(close_time(s.weekday()), "%H:%M").time())
    lunch = s.weekday() != 5 and s.time() < LUNCH[1] and e.time() > LUNCH[0]
    return s.time() >= OPEN and e <= close and not lunch


def _slots(d: date, after: datetime | None):
    """그날 SLOT_MIN분 격자의 시작 시각 — after가 있으면 그 뒤부터"""
    t = datetime.combine(d, OPEN)
    close = datetime.combine(d, datetime.strptime(close_time(d.weekday()), "%H:%M").time())
    while t < close:
        if after is None or t >= after:
            yield t
        t += timedelta(minutes=SLOT_MIN)


def _visit_time(board: Board, call: datetime, minutes: int, holidays, taken):
    """통화 뒤 EMERGENCY_LEAD_MIN분이 지난 첫 빈 슬롯, 그날 없으면 다음 진료일 개원부터 (그 환자의 다른 예약과 겹치지 않게)"""
    after = call + timedelta(minutes=EMERGENCY_LEAD_MIN)
    for d, lower in ((call.date(), after), (next_open_day(call.date(), holidays), None)):
        for s in _slots(d, lower):
            if not _fits_hours(s, minutes) or any(
                a < s + timedelta(minutes=minutes) and s < b for a, b in taken
            ):
                continue
            chair = board.place(s, minutes)
            if chair is not None:
                return s, chair
    raise ValueError(f"응급 내원 자리 없음: {call}")


_SIDE = {
    "1": "오른쪽 위",
    "2": "왼쪽 위",
    "3": "왼쪽 아래",
    "4": "오른쪽 아래",
}  # 치아 번호 앞자리(사분면)


def tooth_area(tooth: str) -> str:
    """치아 번호를 통화에서 쓰는 부위 말로 — '왼쪽 위 어금니'(녹취록이 최근 진료 치아로 부위를 지어내지 않게)"""
    n = int(tooth[1])
    kind = "앞니" if n <= 3 else "작은어금니" if n <= 5 else "어금니"
    return f"{_SIDE[tooth[0]]} {kind}"


def _tooth(uid: str, candidates: tuple, used: set) -> str | None:
    free = [t for t in candidates if t not in used]
    return free[_hash(uid, "tooth") % len(free)] if free else None


def _restored(uid: str, teeth: tuple, code: str, history: list, planned: set, call: dict) -> str:
    """때운 것이 빠진 상황(master.EMERGENCY_RESTORED_SCENES) — 통화 전에 그 환자가 다른 수복 시술로 때운 치아(이 시술을 같은
    치아에 한 적이 없어 재시행 요건이 아니고, 발치·임플란트·통화 때 열린 치료계획 치아가 아님). 없으면 FULL_MOUTH(장부에 없는 치아로)"""
    gone = {t for t, c, _ in history if c in EXTRACTION_CODES or c == IMPLANT_CODE}
    same = {t for t, c, _ in history if c == code}
    done = {
        t
        for t, c, at in history
        if at < call["started_at"] and PROCEDURE[c]["category"] in RESTORED_CATEGORIES
    }
    free = sorted(t for t in done & set(teeth) if t not in gone | same | planned)
    return free[_hash(uid, "restored") % len(free)] if free else FULL_MOUTH


def _method(uid: str) -> str:
    names = sorted(PAYMENT_METHOD_WEIGHTS)
    total = sum(PAYMENT_METHOD_WEIGHTS.values())
    k = _hash(uid, "method") % total
    for n in names:
        if k < PAYMENT_METHOD_WEIGHTS[n]:
            return n
        k -= PAYMENT_METHOD_WEIGHTS[n]
    raise AssertionError


def _scene_call(run, calls: list[dict]) -> dict | None:
    """TODAY 장면으로 옮길 통화 — 판정·블라인드 풀 밖 주 목적 응급 통화 중 가장 늦은 것(그 환자가 TODAY에 예약이 없고
    옮긴 시각에 이탈 전)"""
    for c in reversed(calls):
        if c["primary_purpose"] != "emergency" or c["in_eval_pool"] or c["in_blind_pool"]:
            continue
        busy = run.conn.execute(
            "SELECT 1 FROM emr_appointments WHERE patient_id = ? AND date(scheduled_at) = ?"
            " AND status != 'cancelled'",
            (c["patient_id"], run.today.isoformat()),
        ).fetchone()
        churned = run.state["churn"].get(c["patient_id"])
        if not busy and (churned is None or churned > run.today):
            return c
    return None


def scene_call_time(run) -> datetime:
    """TODAY 장면 통화 시각 — TODAY 전 진료일 마감 SCENE_CALL_BEFORE_CLOSE_MIN분 전(토요일이면 토요일 마감)"""
    day = prev_open_day(run.today, run.holidays)
    close = datetime.combine(day, datetime.strptime(close_time(day.weekday()), "%H:%M").time())
    return close - timedelta(minutes=SCENE_CALL_BEFORE_CLOSE_MIN)


def visit_scene(uid: str, age: int, visited_today: bool, done: set) -> tuple[str, bool]:
    """응급 상황과 바꿨는지 — 해시 상황(scene), 그날 먼저 치료받고 간 뒤면 EMERGENCY_AFTER_VISIT_SCENE, 그 시술군 이력이
    없으면 EMERGENCY_HISTORY_SCENES의 대신 상황, 소아면 EMERGENCY_CHILD_SCENES로. done = 통화 전 진료의 시술군.
    바꾼 상황은 emergency_visit.scene으로 7단계 caller_situation에 간다(verify도 이 함수로 확인)"""
    base = situation = scene(uid)
    if visited_today:
        return EMERGENCY_AFTER_VISIT_SCENE, True
    if situation in EMERGENCY_HISTORY_SCENES:
        need, instead = EMERGENCY_HISTORY_SCENES[situation]
        if not done & set(need):
            situation = instead
    if age <= CHILD_MAX_AGE:
        situation = EMERGENCY_CHILD_SCENES.get(situation, situation)
    return situation, situation != base


def stage(run, rng, fake) -> None:
    conn = run.conn
    calls = emergency_calls(conn)
    moved = _scene_call(run, calls)
    if moved is not None:
        moved["started_at"] = scene_call_time(run).strftime(FMT)
        conn.execute(
            "UPDATE ax_calls SET started_at = ? WHERE id = ?", (moved["started_at"], moved["id"])
        )
        _renumber_calls(conn)
        calls.sort(key=lambda c: c["started_at"])
    birth = {
        pid: date.fromisoformat(b)
        for pid, b in conn.execute("SELECT id, birth_date FROM emr_patients")
    }
    chart_no = dict(conn.execute("SELECT id, chart_no FROM emr_patients"))
    used_teeth, history = {}, {}  # 환자 → 장부·계획 치아 / [(치아, 시술, 진료 시각)]
    for pid, tooth, code, at in conn.execute(
        "SELECT patient_id, tooth_no, procedure_code, performed_at FROM emr_treatments"
    ):
        used_teeth.setdefault(pid, set()).add(tooth)
        history.setdefault(pid, []).append((tooth, code, at))
    planned = {}
    for item in run.state["plan_items"]:
        used_teeth.setdefault(item["patient_id"], set()).add(item["tooth"])
        planned.setdefault(item["patient_id"], []).append(item)
    alerts = chart._alerts(run)
    doctor = conn.execute("SELECT id FROM emr_staff WHERE role = 'doctor' ORDER BY id").fetchone()[
        0
    ]
    board = Board(conn, run.range_start.isoformat())
    failure = {d.isoformat() for d in run.state["link_failure_days"]}
    approvals = {
        r[0]
        for r in conn.execute("SELECT approval_no FROM emr_payments WHERE approval_no IS NOT NULL")
    }
    visits = {}
    for c in calls:
        uid, pid = c["call_uid"], c["patient_id"]
        call = datetime.strptime(c["started_at"], FMT)
        visited_today = conn.execute(
            "SELECT 1 FROM emr_appointments WHERE patient_id = ? AND status = 'completed'"
            " AND type = 'treatment' AND date(scheduled_at) = ? AND scheduled_at < ?",
            (pid, c["started_at"][:10], c["started_at"]),
        ).fetchone()
        done = {
            PROCEDURE[code]["category"]
            for _, code, at in history.get(pid, [])
            if at < c["started_at"]
        }
        situation, changed = visit_scene(
            uid, age_on(birth[pid], call.date()), visited_today is not None, done
        )
        name, teeth = EMERGENCY_TREATMENT[situation]
        code = CODE[name]
        tooth = FULL_MOUTH
        if teeth and age_on(birth[pid], call.date()) >= TOOTH_TREATMENT_MIN_AGE:
            if situation in EMERGENCY_RESTORED_SCENES:
                open_teeth = {
                    i["tooth"]
                    for i in planned.get(pid, [])
                    if i["closed"] is None or i["closed"] > c["started_at"]
                }
                tooth = _restored(uid, teeth, code, history.get(pid, []), open_teeth, c)
            tooth = (
                tooth if tooth != FULL_MOUTH else _tooth(uid, teeth, used_teeth.get(pid, set()))
            ) or FULL_MOUTH
        if tooth == FULL_MOUTH and PROCEDURE[code]["is_tooth_level"]:
            code = CONSULT
        p = PROCEDURE[code]
        minutes = p["duration_min"]
        taken = [
            (datetime.strptime(a, FMT), datetime.strptime(a, FMT) + timedelta(minutes=m))
            for a, m in conn.execute(
                "SELECT scheduled_at, duration_min FROM emr_appointments WHERE patient_id = ?"
                " AND status != 'cancelled'",
                (pid,),
            )
        ]
        start, chair = _visit_time(board, call, minutes, run.holidays, taken)
        board.add(start, minutes, chair)
        at = start.strftime(FMT)
        done = start.date() < run.today
        appt = conn.execute(
            "INSERT INTO emr_appointments (patient_id, staff_id, scheduled_at, duration_min, type,"
            " planned_procedure_code, status, booked_via, confirmed, created_at, chair_no)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'phone', 0, ?, ?)",
            (
                pid,
                doctor,
                at,
                minutes,
                "treatment",  # 응급 진찰(상담)도 원장이 체어에서
                code,
                "completed" if done else "scheduled",
                c["started_at"],
                chair,
            ),
        ).lastrowid
        visits[uid] = (
            {"appointment_id": appt, "date": at[:10], "time": at[11:16]}
            | ({"area": tooth_area(tooth)} if tooth != FULL_MOUTH else {})
            | ({"scene": situation} if changed else {})
        )
        if not done:
            continue
        if tooth != FULL_MOUTH:
            used_teeth.setdefault(pid, set()).add(tooth)
        age = age_on(birth[pid], start.date())
        insured = (
            round(p["fee_total"] * (1 - p["patient_share_rate"])) if insured_on(p, age) else 0
        )
        due = p["fee_total"] - insured
        tid = conn.execute(
            "INSERT INTO emr_treatments (appointment_id, patient_id, procedure_code, tooth_no, performed_at,"
            " fee_total, insured_amount, patient_amount) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (appt, pid, code, tooth, at, p["fee_total"], insured, due),
        ).lastrowid
        end = (start + timedelta(minutes=minutes)).strftime(FMT)
        method, approval = _method(uid), None  # 0원(상담)도 visit 1행 (5단계와 같다)
        if due > 0 and method == "card" and at[:10] not in failure:
            k = 0
            while (approval := f"{10**7 + _hash(uid, 'approval', k) % (9 * 10**7)}") in approvals:
                k += 1
            approvals.add(approval)
        conn.execute(
            "INSERT INTO emr_payments (patient_id, appointment_id, kind, amount_due, amount_paid, paid_at,"
            " method, approval_no) VALUES (?, ?, 'visit', ?, ?, ?, ?, ?)",
            (pid, appt, due, due, end, method, approval),
        )
        # 5단계와 같은 청구 월 규칙(records.claim_bucket) — 확정 월은 paid(오류를 심지 않는다)
        if insured:
            month = at[:7]
            bucket = records.claim_bucket(run.today, month)
            status = "paid" if bucket == "settled" else bucket
            submitted = None if status == "pending" else records.submitted_at(month)
            reviewed = records.reviewed_on(submitted) if status == "paid" else None
            conn.execute(
                "INSERT INTO emr_claims (treatment_id, patient_id, chart_no, tooth_no, claim_month, service_date,"
                " procedure_code, claim_amount, submitted_at, status, paid_amount, reviewed_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    tid,
                    pid,
                    chart_no[pid],
                    tooth,
                    month,
                    at[:10],
                    code,
                    insured,
                    submitted,
                    status,
                    insured if status == "paid" else None,
                    reviewed,
                ),
            )
        facts = chart.emergency_facts(
            uid,
            {
                "patient_id": pid,
                "code": code,
                "tooth": tooth,
                "performed_at": at,
                "appointment_id": appt,
            },
            situation,
            alerts,
            run.state["plan_items"],
        )
        allowed = sorted(PROCEDURE_DIAGNOSES[code])
        dx = EMERGENCY_DX.get(situation)
        conn.execute(
            "INSERT INTO emr_chart_notes (treatment_id, dx_code, note) VALUES (?, ?, ?)",
            (tid, dx if dx in allowed else allowed[0], chart.draft_note(facts, tid)),
        )
        conn.execute(
            "INSERT INTO truth.truth_chart_notes VALUES (?, 'narrative', 0, NULL, NULL, 0, ?)",
            (tid, json.dumps(facts, ensure_ascii=False, sort_keys=True)),
        )
    _refresh_facts(run, calls, visits)


def _renumber_calls(conn) -> None:
    """ax_calls id = 통화 시각순 (옮긴 통화가 있으면 다시 매김 — call_uid·풀 표시는 그대로)"""
    order = [r[0] for r in conn.execute("SELECT id FROM ax_calls ORDER BY started_at, id")]
    offset = max(order) + 1
    conn.executemany("UPDATE ax_calls SET id = id + ? WHERE id = ?", [(offset, i) for i in order])
    conn.executemany(
        "UPDATE ax_calls SET id = ? WHERE id = ?",
        [(k, i + offset) for k, i in enumerate(order, start=1)],
    )


def _refresh_facts(run, calls: list[dict], visits: dict) -> None:
    """응급 통화에 emergency_visit, 옮긴 통화는 통화일, 응급 환자의 통화는 바뀐 장부로 사실을 다시 계산(같으면 그대로)"""
    conn = run.conn
    led = Ledger(
        conn,
        run.state["plan_items"],
        run.state["churn"],
        run.state["dormant"],
        recall_targets(run),
        frozenset(v["appointment_id"] for v in visits.values()),
    )
    patients = {c["patient_id"] for c in calls}
    rows = conn.execute(
        "SELECT c.call_uid, c.patient_id, c.started_at, c.direction, l.primary_purpose, l.used_fact"
        " FROM ax_calls c JOIN truth.truth_call_labels l USING (call_uid) WHERE c.patient_id IS NOT NULL"
    ).fetchall()
    updates = []
    for uid, pid, t, direction, purpose, raw in rows:
        if pid not in patients:
            continue
        fact = json.loads(raw)
        # 6단계가 장부로 고른 주제는 여기서 다시 고르지 않는다 — 응급 내원으로 바뀌면 멈춘다(경과 확인 = 최근 진료, 리콜 문자)
        if (purpose, direction) == ("treatment_q", "outbound") and fact.get(
            "topic_procedure"
        ) != led.recent_treatment(pid, t):
            raise ValueError(f"{uid}: 응급 내원이 경과 확인 통화의 최근 진료를 바꾼다")
        if "recall_text" in fact and (
            fact["recall_text"] != led.recall_text(pid, t)
            or (led.next_appointment(pid, t) is not None and not fact.get("next_appointment"))
        ):
            raise ValueError(f"{uid}: 응급 내원이 리콜 문의 통화의 받은 문자·잡힌 예약을 바꾼다")
        new = fact | {"call_date": t[:10]} | led.facts(pid, t)
        if uid in visits:
            new["emergency_visit"] = visits[uid]
        if new != fact:
            updates.append((json.dumps(new, ensure_ascii=False), uid))
    conn.executemany(
        "UPDATE truth.truth_call_labels SET used_fact = ? WHERE call_uid = ?", updates
    )
