"""전환 전 EMR·일별 예약표 조회 (앱 설계 §9, 전환 전 EMR·일별 예약표 설계안). source.sqlite를 읽기만 한다.
환자에 대한 사실(미수·주의사항·마지막 내원·앞으로의 예약·노쇼·취소)은 고른 날짜가 아니라 기준일 기준이다(앱 설계 §6).
신환은 등록일이 range_start 이후이고 그 예약 전에 완료된 예약이 없을 때다. 예약을 잡은 통화는 같은 환자이고
ax_calls.started_at = emr_appointments.created_at인 통화다."""

import json
import sqlite3
from datetime import date

ROLE = {"doctor": "원장", "hygienist": "위생사", "consultant": "상담실장"}
LIVE = ("scheduled", "completed")  # 건수에 넣는 상태 — 취소·노쇼는 뺀다
# 예약 확인은 전날 리마인드에 대한 응답이다(appt_confirm은 예약을 잡을 때의 확정 알림)
CONFIRM_SQL = (
    " (SELECT COUNT(*) FROM emr_messages m WHERE m.related_appointment_id = a.id"
    "  AND m.\"trigger\" = 'reminder') AS reminders,"
    " (SELECT COUNT(*) FROM emr_messages m WHERE m.related_appointment_id = a.id"
    "  AND m.\"trigger\" = 'reply') AS replies"
)


def _confirm(r: sqlite3.Row) -> str:
    """replied(확인됨) / no_reply(리마인드에 답 없음) / no_message(리마인드 자체가 없음 — 전날 잡은 응급 등)"""
    if r["confirmed"] or r["replies"]:
        return "replied"
    return "no_reply" if r["reminders"] else "no_message"


def _hours(conn: sqlite3.Connection, day: date) -> dict:
    h = json.loads(conn.execute("SELECT hours_json FROM emr_clinic").fetchone()[0])
    saturday = day.weekday() == 5
    return {
        "open": h["open"],
        "close": h["saturday_close"] if saturday else h["weekday_close"],
        "lunch": None if saturday else h["lunch"],  # 토요일은 점심 전에 마감
        "closed": day.weekday() in h["closed_weekdays"],
    }


def _staff(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, name, role FROM emr_staff WHERE active = 1 AND role IN (?, ?, ?) ORDER BY id",
        tuple(ROLE),
    ).fetchall()
    order = list(ROLE)
    return [
        {"id": r["id"], "name": r["name"], "role": ROLE[r["role"]]}
        for r in sorted(rows, key=lambda r: order.index(r["role"]))
    ]


def _procedure(name: str, kind: str) -> str:
    """진료 예약의 '상담'은 응급 내원의 원장 진찰 — 상담실장 상담과 헷갈리지 않게(기능 정의 §4)"""
    return "진찰" if kind == "treatment" and name == "상담" else name


def schedule_day(conn: sqlite3.Connection, day: date, today: date, range_start: date) -> dict:
    staff = _staff(conn)
    names = {s["id"]: s for s in staff}
    rows = conn.execute(
        "SELECT a.*, p.chart_no, p.name, p.registered_at, r.name AS procedure_name,"
        " i.seq, i.tooth_no, (SELECT COUNT(*) FROM emr_treatment_plan_items x"
        "  WHERE x.plan_id = i.plan_id AND x.status != 'replaced') AS plan_total,"
        + CONFIRM_SQL
        + ","
        " (SELECT COUNT(*) FROM emr_appointments b WHERE b.patient_id = a.patient_id"
        "  AND b.status = 'completed' AND b.scheduled_at < a.scheduled_at) AS done_before,"
        " (SELECT c.call_uid FROM ax_calls c WHERE c.patient_id = a.patient_id"
        "  AND c.started_at = a.created_at) AS call_uid"
        " FROM emr_appointments a JOIN emr_patients p ON p.id = a.patient_id"
        " JOIN emr_procedures r ON r.code = a.planned_procedure_code"
        " LEFT JOIN emr_treatment_plan_items i ON i.id = a.plan_item_id"
        " WHERE date(a.scheduled_at) = ? ORDER BY a.scheduled_at, a.id",
        (day.isoformat(),),
    ).fetchall()
    is_today = day == today
    appointments = []
    for r in rows:
        member = names.get(r["staff_id"], {})
        appointments.append(
            {
                "id": r["id"],
                "time": r["scheduled_at"][11:16],
                "duration": r["duration_min"],
                "staff_id": r["staff_id"],
                "staff_name": member.get("name"),
                "staff_role": member.get("role"),
                "type": r["type"],
                "procedure": _procedure(r["procedure_name"], r["type"]),
                "tooth": r["tooth_no"] if r["tooth_no"] not in (None, "00") else None,
                "status": r["status"],
                "unconfirmed": is_today and r["status"] == "scheduled" and not r["confirmed"],
                "confirm": _confirm(r),
                "chart_no": r["chart_no"],
                "name": r["name"],
                "new_patient": r["registered_at"] >= range_start.isoformat()
                and r["done_before"] == 0,
                "plan_step": {"seq": r["seq"], "total": r["plan_total"]}
                if r["seq"] is not None
                else None,
                "plan_item_id": r["plan_item_id"],
                "booked_via": r["booked_via"],
                "booked_at": r["created_at"][:16],
                "call": {"uid": r["call_uid"], "started_at": r["created_at"][:16]}
                if r["call_uid"]
                else None,
            }
        )
    live = [a for a in appointments if a["status"] in LIVE]
    first, last = conn.execute(
        "SELECT MIN(date(scheduled_at)), MAX(date(scheduled_at)) FROM emr_appointments"
    ).fetchone()
    return {
        "date": day.isoformat(),
        "hours": _hours(conn, day),
        "staff": staff,
        "appointments": appointments,
        "range": {"from": first, "to": last},
        "counts": {
            "total": len(live),
            **{k: sum(a["type"] == k for a in live) for k in ("treatment", "hygiene", "consult")},
            "unconfirmed": sum(a["unconfirmed"] for a in live),
        },
    }


def _age(birth: str, on: date) -> int:
    b = date.fromisoformat(birth[:10])
    return on.year - b.year - ((on.month, on.day) < (b.month, b.day))


def _person(r: sqlite3.Row, today: date) -> dict:
    return {
        "chart_no": r["chart_no"],
        "name": r["name"],
        "age": _age(r["birth_date"], today),
        "sex": "남" if r["sex"] == "M" else "여",
    }


def search_patients(conn: sqlite3.Connection, q: str, today: date) -> list[dict]:
    """이름이나 차트 번호(대소문자 무시)에 q가 든 환자, 차트 번호 순 최대 20명"""
    like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    rows = conn.execute(  # SQLite LIKE는 ASCII 대소문자를 무시한다. % _는 글자 그대로
        "SELECT * FROM emr_patients WHERE name LIKE ? ESCAPE '\\' OR chart_no LIKE ? ESCAPE '\\'"
        " ORDER BY chart_no LIMIT 20",
        (like, like),
    )
    return [_person(r, today) for r in rows]


def patient_summary(conn: sqlite3.Connection, patient_id: int, today: date) -> dict:
    """예약표 패널·EMR이 보는 '지금 이 환자' — 기준일 기준(설계안 '데이터 규칙')"""
    day = today.isoformat()
    year_ago = date.fromordinal(today.toordinal() - 365).isoformat()
    balance = conn.execute(
        "SELECT COALESCE(SUM(amount_due - amount_paid), 0) FROM emr_payments"
        " WHERE patient_id = ? AND date(paid_at) < ?",
        (patient_id, day),
    ).fetchone()[0]
    alerts = conn.execute(
        "SELECT t.code, t.label, t.category FROM emr_patient_alerts a"
        " JOIN emr_alert_types t ON t.code = a.code WHERE a.patient_id = ?"
        " AND a.recorded_at <= ? AND (a.ends_at IS NULL OR ? < a.ends_at) ORDER BY a.id",
        (patient_id, day, day),
    )
    last = conn.execute(
        "SELECT MAX(date(scheduled_at)) FROM emr_appointments"
        " WHERE patient_id = ? AND status = 'completed' AND date(scheduled_at) < ?",
        (patient_id, day),
    ).fetchone()[0]
    upcoming = conn.execute(
        "SELECT a.id, a.scheduled_at, a.type, r.name AS procedure, s.name AS staff"
        " FROM emr_appointments a JOIN emr_procedures r ON r.code = a.planned_procedure_code"
        " JOIN emr_staff s ON s.id = a.staff_id WHERE a.patient_id = ? AND a.status = 'scheduled'"
        " AND date(a.scheduled_at) >= ? ORDER BY a.scheduled_at",
        (patient_id, day),
    )
    missed = dict(
        conn.execute(
            "SELECT status, COUNT(*) FROM emr_appointments WHERE patient_id = ?"
            " AND status IN ('noshow', 'cancelled') AND date(scheduled_at) >= ?"
            " AND date(scheduled_at) < ? GROUP BY status",
            (patient_id, year_ago, day),
        ).fetchall()
    )
    return {
        "balance": max(balance, 0),  # 더 낸 금액은 미수가 아니다
        "alerts": [dict(a) for a in alerts],
        "last_visit": last,
        "upcoming": [
            {
                "id": u["id"],
                "date": u["scheduled_at"][:10],
                "time": u["scheduled_at"][11:16],
                "procedure": _procedure(u["procedure"], u["type"]),
                "staff": u["staff"],
            }
            for u in upcoming
        ],
        "noshow_1y": missed.get("noshow", 0),
        "cancelled_1y": missed.get("cancelled", 0),
        "open_items": [
            dict(i)
            for i in conn.execute(
                "SELECT i.id, r.name AS procedure, i.tooth_no AS tooth FROM emr_treatment_plan_items i"
                " JOIN emr_treatment_plans pl ON pl.id = i.plan_id"
                " JOIN emr_procedures r ON r.code = i.procedure_code"
                " WHERE pl.patient_id = ? AND i.status = 'open' ORDER BY pl.created_at, i.plan_id, i.seq",
                (patient_id,),
            )
        ],
    }


def patient_record(conn: sqlite3.Connection, chart_no: str, today: date) -> dict | None:
    """전환 전 EMR 한 환자 전체. 칸은 와이어프레임 export.py의 emr[...]와 같고 목록은 오래된 순"""
    p = conn.execute(
        "SELECT * FROM emr_patients WHERE upper(chart_no) = upper(?)", (chart_no,)
    ).fetchone()
    if not p:
        return None
    pid = p["id"]

    def q(sql: str) -> list[sqlite3.Row]:
        return conn.execute(sql, (pid,)).fetchall()

    family = conn.execute(
        "SELECT chart_no, name FROM emr_patients WHERE family_id = ? AND id != ? ORDER BY chart_no",
        (p["family_id"], pid),
    ).fetchall()
    guardian = conn.execute(
        "SELECT chart_no, name FROM emr_patients WHERE id = ?", (p["guardian_patient_id"],)
    ).fetchone()
    return {
        "patient": _person(p, today)
        | {
            "birth": p["birth_date"],
            "phone": p["phone"],
            "registered": p["registered_at"],
            "private_insurance": bool(p["has_private_insurance"]),
            "family": [dict(f) for f in family],
            "guardian": dict(guardian) if guardian else None,
        },
        "summary": patient_summary(conn, pid, today),
        "alerts": [
            {
                "code": a["code"],
                "label": a["label"],
                "category": a["category"],
                "recorded": a["recorded_at"],
                "ends": a["ends_at"],
                "source": a["source"],
            }
            for a in q(
                "SELECT a.*, t.label, t.category FROM emr_patient_alerts a"
                " JOIN emr_alert_types t ON t.code = a.code WHERE a.patient_id = ? ORDER BY a.id"
            )
        ],
        "appointments": [
            {
                "id": a["id"],
                "at": a["scheduled_at"][:16],
                "type": a["type"],
                "procedure": _procedure(a["procedure"], a["type"]),
                "status": a["status"],
                "staff": a["staff"],
                "chair": a["chair_no"],
                "confirmed": bool(a["confirmed"]),
                "confirm": _confirm(a),
                "plan_item_id": a["plan_item_id"],
            }
            for a in q(
                "SELECT a.*, r.name AS procedure, s.name AS staff,"
                + CONFIRM_SQL
                + " FROM emr_appointments a"
                " JOIN emr_procedures r ON r.code = a.planned_procedure_code"
                " JOIN emr_staff s ON s.id = a.staff_id WHERE a.patient_id = ?"
                " ORDER BY a.scheduled_at, a.id"
            )
        ],
        "treatments": [
            {
                "at": t["performed_at"][:10],
                "procedure": t["procedure"],
                "tooth": t["tooth_no"],
                "fee": t["fee_total"],
                "insured": t["insured_amount"],
                "patient_amount": t["patient_amount"],
                "dx": t["dx_code"],
                "note": t["note"],
            }
            for t in q(
                "SELECT t.*, r.name AS procedure, n.dx_code, n.note FROM emr_treatments t"
                " JOIN emr_procedures r ON r.code = t.procedure_code"
                " LEFT JOIN emr_chart_notes n ON n.treatment_id = t.id"
                " WHERE t.patient_id = ? ORDER BY t.performed_at, t.id"
            )
        ],
        "payments": [
            {
                "at": x["paid_at"][:16],
                "kind": x["kind"],
                "due": x["amount_due"],
                "paid": x["amount_paid"],
                "method": x["method"],
            }
            for x in q("SELECT * FROM emr_payments WHERE patient_id = ? ORDER BY paid_at, id")
        ],
        "claims": [
            {
                "month": c["claim_month"],
                "service": c["service_date"][:10],
                "procedure": c["procedure"],
                "amount": c["claim_amount"],
                "status": c["status"],
                "paid": c["paid_amount"],
                "adjust": c["adjust_note"],
            }
            for c in q(
                "SELECT c.*, r.name AS procedure FROM emr_claims c"
                " JOIN emr_procedures r ON r.code = c.procedure_code"
                " WHERE c.patient_id = ? ORDER BY c.service_date, c.id"
            )
        ],
        "plans": [
            {
                "id": i["id"],
                "plan_id": i["plan_id"],
                "seq": i["seq"],
                "created": i["created_at"][:10],
                "procedure": i["procedure"],
                "tooth": i["tooth_no"],
                "est_fee": i["est_fee"],
                "status": i["status"],
            }
            for i in q(
                "SELECT i.*, pl.created_at, r.name AS procedure FROM emr_treatment_plan_items i"
                " JOIN emr_treatment_plans pl ON pl.id = i.plan_id"
                " JOIN emr_procedures r ON r.code = i.procedure_code"
                " WHERE pl.patient_id = ? ORDER BY pl.created_at, i.plan_id, i.seq"
            )
        ],
        "messages": [
            {
                "at": m["sent_at"][:16],
                "direction": m["direction"],
                "trigger": m["trigger"],
                "body": m["body"],
                "related_appointment_id": m["related_appointment_id"],
            }
            for m in q("SELECT * FROM emr_messages WHERE patient_id = ? ORDER BY sent_at, id")
        ],
    }


def tour_patient(conn: sqlite3.Connection, today: date) -> dict | None:
    """따라 보기 기준 환자 — 기준일 예약 환자 중 미수·주의사항·남은 치료(오늘 예약이 이행하는 항목 제외)를
    가장 많이 가진 사람. 같으면 예약 미확인, 시각, 차트 번호 순(설계안 '백엔드')"""
    rows = conn.execute(
        "SELECT a.id, a.patient_id, a.scheduled_at, a.confirmed, a.plan_item_id, p.chart_no, p.name"
        " FROM emr_appointments a JOIN emr_patients p ON p.id = a.patient_id"
        " WHERE date(a.scheduled_at) = ? AND a.status = 'scheduled'",
        (today.isoformat(),),
    ).fetchall()
    if not rows:
        return None
    picks = []
    for r in rows:
        s = patient_summary(conn, r["patient_id"], today)
        remaining = conn.execute(
            "SELECT 1 FROM emr_treatment_plan_items i JOIN emr_treatment_plans pl"
            " ON pl.id = i.plan_id WHERE pl.patient_id = ? AND i.status = 'open'"
            " AND i.id IS NOT ? LIMIT 1",
            (r["patient_id"], r["plan_item_id"]),
        ).fetchone()
        has = {
            "balance": s["balance"] > 0,
            "alert": bool(s["alerts"]),
            "remaining": bool(remaining),
        }
        key = (-sum(has.values()), bool(r["confirmed"]), r["scheduled_at"], r["chart_no"])
        picks.append((key, r, has))
    _, r, has = min(picks, key=lambda x: x[0])
    live = conn.execute(
        "SELECT COUNT(*) FROM emr_appointments WHERE date(scheduled_at) = ?"
        " AND status NOT IN ('cancelled', 'noshow')",
        (today.isoformat(),),
    ).fetchone()[0]
    return {
        "chart_no": r["chart_no"],
        "name": r["name"],
        "time": r["scheduled_at"][11:16],
        "appointment_id": r["id"],
        "has": has,
        "appointments_today": live,
    }
