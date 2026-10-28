"""문자·가상 PII 검사 (8단계, 가상 데이터 생성 설계 §2.5·§5.3)"""

from collections import defaultdict
from datetime import timedelta

from generator.verify.common import _dt, _rows
from generator.verify.schedule import _prev_open_day

# ── 문자 (8단계, §2.5) ────────────────────────────────────────


APPOINTMENT_TRIGGERS = ("appt_confirm", "reminder", "noshow")  # 예약 하나에 최대 1건


def _message_problem(m, appts, unpaid, doc_calls, ctx) -> str | None:
    if _dt(m["sent_at"]).date() >= ctx.today:
        return "TODAY 이후 문자(TODAY는 개원 전 스냅샷)"
    trig, t = m["trigger"], m["sent_at"]
    if (m["direction"] == "in") != (trig == "reply"):
        return "in ⇔ reply"
    if trig == "doc_ready":
        return (
            None
            if any(c < t for c in doc_calls.get(m["patient_id"], []))
            else "서류 요청 통화 뒤가 아님"
        )
    if trig not in APPOINTMENT_TRIGGERS + ("balance",):
        return None
    a = appts.get(m["related_appointment_id"])
    if a is None or a["patient_id"] != m["patient_id"]:
        return "연결 예약 없음·다른 환자"
    if trig == "appt_confirm" and not (
        a["created_at"] >= ctx.range_start.isoformat() and a["created_at"] < t
    ):
        return "예약 확정 = 창 안 예약의 created_at 뒤"
    if trig == "reminder":
        day = _prev_open_day(_dt(a["scheduled_at"]).date(), ctx.holidays)
        if (
            _dt(t).date() != day
            or _dt(a["created_at"]).date() >= day
            or (a["cancelled_at"] and _dt(a["cancelled_at"]).date() <= day)
        ):
            return "리마인드 = 리마인드 대상 예약의 직전 진료일"
    if trig == "noshow" and not (
        a["status"] == "noshow"
        and _dt(t) > _dt(a["scheduled_at"]) + timedelta(minutes=a["duration_min"])
    ):
        return "노쇼 = 노쇼 예약의 진료가 끝났을 시각 뒤"
    if trig == "balance" and not any(paid < t for paid in unpaid.get(a["id"], [])):
        return "미수 안내 = 미수가 생긴 내원 수납 뒤"
    return None


def check_message_triggers(conn, ctx):
    """문자 ↔ 트리거: TODAY 전, in ⇔ reply, 예약 확정은 창 안 예약 created_at 뒤, 리마인드는 리마인드 대상의 직전 진료일,
    노쇼는 노쇼 예약 진료 끝 뒤, 미수 안내는 미수가 생긴 내원 수납 뒤, 서류 준비는 같은 환자가 건 서류 요청 통화 뒤"""
    appts = {a["id"]: a for a in _rows(conn, "SELECT * FROM emr_appointments")}
    unpaid = defaultdict(list)
    for r in _rows(
        conn,
        "SELECT appointment_id, paid_at FROM emr_payments WHERE kind = 'visit' AND amount_paid < amount_due",
    ):
        unpaid[r["appointment_id"]].append(r["paid_at"])
    doc_calls = defaultdict(list)
    for r in _rows(
        conn,
        "SELECT c.patient_id, c.started_at FROM ax_calls c JOIN truth.truth_call_labels l USING (call_uid)"
        " WHERE l.primary_purpose = 'document_req' AND c.direction = 'inbound' AND c.patient_id IS NOT NULL",
    ):
        doc_calls[r["patient_id"]].append(r["started_at"])
    return [
        {"id": m["id"], "trigger": m["trigger"], "problem": p}
        for m in _rows(conn, "SELECT * FROM emr_messages ORDER BY id")
        if (p := _message_problem(m, appts, unpaid, doc_calls, ctx))
    ]


def check_message_once(conn, ctx):
    """예약 확정·리마인드·노쇼 문자는 예약 하나에 최대 1건"""
    return _rows(
        conn,
        'SELECT related_appointment_id, "trigger", count(*) AS n FROM emr_messages'
        f' WHERE "trigger" IN ({", ".join("?" * len(APPOINTMENT_TRIGGERS))})'
        ' GROUP BY related_appointment_id, "trigger" HAVING n > 1',
        *APPOINTMENT_TRIGGERS,
    )


def check_confirmed_has_reminder(conn, ctx):
    """confirmed(리마인드 응답) 예약에는 리마인드 문자가 있다"""
    return _rows(
        conn,
        "SELECT id, scheduled_at FROM emr_appointments a WHERE confirmed = 1 AND NOT EXISTS ("
        "SELECT 1 FROM emr_messages m WHERE m.related_appointment_id = a.id AND m.\"trigger\" = 'reminder')",
    )


def check_reply_after_out(conn, ctx):
    """답장(in)은 같은 환자·같은 예약(둘 다 없음 포함)의 앞선 out 문자 뒤"""
    return _rows(
        conn,
        "SELECT id, patient_id, sent_at FROM emr_messages i WHERE direction = 'in' AND NOT EXISTS ("
        "SELECT 1 FROM emr_messages o WHERE o.direction = 'out' AND o.patient_id = i.patient_id"
        " AND o.related_appointment_id IS i.related_appointment_id AND o.sent_at < i.sent_at)",
    )


def report_message_counts(conn, ctx):
    """트리거별 문자 수"""
    return dict(
        conn.execute(
            'SELECT "trigger", count(*) FROM emr_messages GROUP BY 1 ORDER BY 1'
        ).fetchall()
    )


# ── 가상 PII ────────────────────────────────────────────────


def check_fake_phone(conn, ctx):
    """환자 전화·통화 상대 번호는 010-0000-xxxx만"""
    fake = "'010-0000-[0-9][0-9][0-9][0-9]'"
    return _rows(
        conn,
        f"SELECT 'emr_patients' AS tbl, id, phone FROM emr_patients WHERE phone NOT GLOB {fake}",
    ) + _rows(
        conn,
        "SELECT 'ax_calls' AS tbl, id, caller_phone AS phone FROM ax_calls"
        f" WHERE caller_phone IS NOT NULL AND caller_phone NOT GLOB {fake}",
    )
