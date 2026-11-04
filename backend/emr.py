"""전환 전 EMR·일별 예약표 조회 (앱 설계 §9, 전환 전 EMR·일별 예약표 설계안). source.sqlite를 읽기만 한다.
환자에 대한 사실(미수·주의사항·마지막 내원·앞으로의 예약·노쇼·취소)은 고른 날짜가 아니라 기준일 기준이다(앱 설계 §6).
신환은 등록일이 range_start 이후이고 그 예약 전에 완료된 예약이 없을 때다. 예약을 잡은 통화는 같은 환자이고
ax_calls.started_at = emr_appointments.created_at인 통화다."""

import json
import sqlite3
from datetime import date

ROLE = {"doctor": "원장", "hygienist": "위생사", "consultant": "상담실장"}
LIVE = ("scheduled", "completed")  # 건수에 넣는 상태 — 취소·노쇼는 뺀다


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
        "  WHERE x.plan_id = i.plan_id) AS plan_total,"
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
                "chart_no": r["chart_no"],
                "name": r["name"],
                "new_patient": r["registered_at"] >= range_start.isoformat()
                and r["done_before"] == 0,
                "plan_step": {"seq": r["seq"], "total": r["plan_total"]}
                if r["seq"] is not None
                else None,
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
