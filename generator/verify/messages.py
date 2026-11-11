"""문자·가상 PII 검사 (8단계, 가상 데이터 생성 설계 §2.5·§5.3)"""

from collections import defaultdict
from datetime import date, datetime, timedelta

from generator.master import (
    CLINIC_NAME,
    HOURS,
    MESSAGE_REPLIES_PAID,
    MESSAGE_REPLIES_PAID_METHOD,
    MESSAGE_REPLY_CANCEL,
    MESSAGE_REPLY_CHANGE,
    MESSAGE_REPLY_OK,
    RECALL_CYCLE,
    RECALL_TEMPLATES,
    open_slots,
)
from generator.verify.common import C_ONLY, _dt, _rows
from generator.verify.schedule import _prev_open_day

# ── 문자 (8단계, §2.5) ────────────────────────────────────────


APPOINTMENT_TRIGGERS = ("appt_confirm", "reminder", "noshow")  # 예약 하나에 최대 1건


def _first_open_day(year: int, month: int, holidays) -> date:
    d = date(year, month, 1)
    while not open_slots(d.weekday()) or d in holidays:
        d += timedelta(days=1)
    return d


def _month_offset(d: date, months: int) -> str:
    y, m = divmod(d.year * 12 + d.month - 1 + months, 12)
    return f"{y}-{m + 1:02d}"


def _recall_body(conn, pid: int, t: str) -> str | None:
    """시각 t(그달 첫 진료일 개원)의 리콜 문자 본문 — 기한(마지막 completed 위생 내원 + RECALL_CYCLE)이 다음 달이면
    다가옴, 지난달이면 지남(RECALL_TEMPLATES). 잡힌 예약이 있거나 그 밖이면 None (§2.5)"""
    last = conn.execute(
        "SELECT max(scheduled_at) FROM emr_appointments WHERE patient_id = ? AND type = 'hygiene'"
        " AND status = 'completed' AND scheduled_at < ?",
        (pid, t),
    ).fetchone()[0]
    live = conn.execute(
        "SELECT 1 FROM emr_appointments WHERE patient_id = ? AND created_at <= ? AND scheduled_at > ?"
        " AND (cancelled_at IS NULL OR cancelled_at > ?)",
        (pid, t, t, t),
    ).fetchone()
    if last is None or live is not None:
        return None
    due, day = _dt(last).date() + RECALL_CYCLE, _dt(t).date()
    name = conn.execute("SELECT name FROM emr_patients WHERE id = ?", (pid,)).fetchone()[0]
    if due.strftime("%Y-%m") == _month_offset(day, 1):
        return RECALL_TEMPLATES["before"].format(clinic=CLINIC_NAME, name=name, month=due.month)
    if due.strftime("%Y-%m") == _month_offset(day, -1):
        return RECALL_TEMPLATES["after"].format(clinic=CLINIC_NAME, name=name)
    return None


def _message_problem(m, appts, unpaid, doc_calls, ctx, conn) -> str | None:
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
    if trig == "recall":
        sent = _dt(t)
        first = _first_open_day(sent.year, sent.month, ctx.holidays)
        if sent != datetime.combine(first, datetime.strptime(HOURS["open"], "%H:%M").time()):
            return "리콜 = 그달 첫 진료일 개원 시각 일괄"
        if m["related_appointment_id"] is not None or m["body"] != _recall_body(
            conn, m["patient_id"], t
        ):
            return "리콜 = 기한이 다음 달(다가옴)·지난달(지남)이고 잡힌 예약이 없는 환자, 연결 예약 없음, 본문이 맞음"
        return None
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
            or (a["cancelled_at"] and a["cancelled_at"] <= t)
        ):
            return "리마인드 = 리마인드 대상 예약(보낼 때 아직 취소 전)의 직전 진료일"
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
    노쇼는 노쇼 예약 진료 끝 뒤, 미수 안내는 미수가 생긴 내원 수납 뒤, 서류 준비는 같은 환자가 건 서류 요청 통화 뒤,
    리콜은 그달 첫 진료일 개원 시각에 기한이 다음 달(다가옴)·지난달(지남)이고 잡힌 예약이 없는 환자(연결 예약 없음)"""
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
        " WHERE l.primary_purpose = 'document_req' AND c.direction = 'inbound' AND c.patient_id IS NOT NULL"
        f" AND c.{C_ONLY}",  # hc- 서류 요청은 준비 문자를 약속하지 않는다(어려운 통화 설계 §3.3)
    ):
        doc_calls[r["patient_id"]].append(r["started_at"])
    return [
        {"id": m["id"], "trigger": m["trigger"], "problem": p}
        for m in _rows(conn, "SELECT * FROM emr_messages ORDER BY id")
        if (p := _message_problem(m, appts, unpaid, doc_calls, ctx, conn))
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


def check_recall_once_a_month(conn, ctx):
    """리콜 문자는 환자당 한 달에 최대 1건"""
    return _rows(
        conn,
        "SELECT patient_id, substr(sent_at, 1, 7) AS month, count(*) AS n FROM emr_messages"
        " WHERE \"trigger\" = 'recall' GROUP BY patient_id, month HAVING n > 1",
    )


def _replies_to(conn) -> list[dict]:
    """답장(in)마다 바로 앞의 같은 환자·같은 예약 out 문자"""
    return _rows(
        conn,
        'SELECT i.id, i.patient_id, i.sent_at, i.body, o.id AS out_id, o."trigger" AS out_trigger,'
        " o.related_appointment_id AS out_appt FROM emr_messages i JOIN emr_messages o"
        " ON o.id = (SELECT o2.id FROM emr_messages o2 WHERE o2.direction = 'out'"
        " AND o2.patient_id = i.patient_id AND o2.related_appointment_id IS i.related_appointment_id"
        " AND o2.sent_at < i.sent_at ORDER BY o2.sent_at DESC, o2.id DESC LIMIT 1)"
        " WHERE i.direction = 'in'",
    )


def check_churn_no_reply(conn, ctx):
    """이탈 환자는 이탈일부터 어떤 문자에도 답장하지 않는다(⑨ 연락 무응답 흔적, §2.5)"""
    churn = dict(
        conn.execute(
            "SELECT patient_id, churned_on FROM truth.truth_patient_churn WHERE kind = 'churn'"
        )
    )
    return [
        r
        for r in _rows(
            conn, "SELECT id, patient_id, sent_at FROM emr_messages WHERE direction = 'in'"
        )
        if r["patient_id"] in churn and r["sent_at"][:10] >= churn[r["patient_id"]]
    ]


def check_paid_replies(conn, ctx):
    """이미 냈다는 답장(master.MESSAGE_REPLIES_PAID·MESSAGE_REPLIES_PAID_METHOD)은 미수 안내 답장에만, 장부 실수(입력 누락·
    금액 오입력)로 생긴 미수의 안내 답장은 전부 그 문구, 결제수단을 말하면 그 내원의 실제 결제수단(장부 실수면 정답의 실제 값)
    (§2.5 reply)"""
    errors = dict(
        conn.execute(
            "SELECT p.appointment_id, e.true_method FROM truth.truth_ledger_errors e JOIN emr_payments p ON p.id = e.payment_id"
            " WHERE e.kind IN ('entry_missing', 'amount_typo') AND p.kind = 'visit'"
        )
    )
    visit_method = dict(
        conn.execute("SELECT appointment_id, method FROM emr_payments WHERE kind = 'visit'")
    )
    by_text = {text: m for m, text in MESSAGE_REPLIES_PAID_METHOD.items()}
    bad = []
    for r in _replies_to(conn):
        paid = r["body"] in MESSAGE_REPLIES_PAID or r["body"] in by_text
        said = by_text.get(r["body"])
        if said and said != (errors.get(r["out_appt"]) or visit_method.get(r["out_appt"])):
            bad.append({**r, "problem": "이미 냈다는 답장의 결제수단이 실제와 다름"})
        if paid and r["out_trigger"] != "balance":
            bad.append({**r, "problem": "미수 안내가 아닌 문자에 이미 냈다는 답장"})
        elif r["out_trigger"] == "balance" and r["out_appt"] in errors and not paid:
            bad.append({**r, "problem": "장부 실수 미수 안내에 이미 냈다는 답장이 아님"})
    return bad


def check_confirmed_has_reminder(conn, ctx):
    """confirmed(리마인드 응답) 예약에는 리마인드 문자가 있다"""
    return _rows(
        conn,
        "SELECT id, scheduled_at FROM emr_appointments a WHERE confirmed = 1 AND NOT EXISTS ("
        "SELECT 1 FROM emr_messages m WHERE m.related_appointment_id = a.id AND m.\"trigger\" = 'reminder')",
    )


def check_appt_reply_state(conn, ctx):
    """예약 확정·리마인드 답장이 예약 상태와 맞음 — 리마인드 확인 답장(master.MESSAGE_REPLY_OK)은 confirmed 예약에만,
    시간 변경(master.MESSAGE_REPLY_CHANGE)은 그 뒤 실제로 바뀐 예약(변경 쌍의 원래 예약, 답장 ≤ 취소 시각)에만, confirmed
    (리마인드 응답) 예약은 리마인드 답장이나 예약 확인 통화가 있다(확인의 흔적, §2.5)"""
    appts = {
        r["id"]: r for r in _rows(conn, "SELECT id, confirmed, cancelled_at FROM emr_appointments")
    }
    moved = {
        r[0]
        for r in conn.execute(
            "SELECT rescheduled_from_id FROM emr_appointments WHERE rescheduled_from_id IS NOT NULL"
        )
    }
    called = {
        r[0]
        for r in conn.execute(
            "SELECT json_extract(used_fact, '$.appointment_id') FROM truth.truth_call_labels"
            " WHERE primary_purpose = 'confirm'"
        )
    }
    bad, traced = [], set()
    for r in _replies_to(conn):
        if r["out_trigger"] not in ("appt_confirm", "reminder"):
            continue
        a = appts.get(r["out_appt"])
        if r["body"] == MESSAGE_REPLY_CHANGE:
            if r["out_appt"] not in moved or r["sent_at"] > (a["cancelled_at"] or ""):
                bad.append({**r, "problem": "시간 변경 답장인데 그 뒤 바뀐 예약이 아님"})
        elif (
            r["out_trigger"] == "reminder" and r["body"] in MESSAGE_REPLY_OK and not a["confirmed"]
        ):
            bad.append({**r, "problem": "리마인드 확인 답장인데 confirmed 아님"})
        if r["out_trigger"] == "reminder":
            traced.add(r["out_appt"])
    bad += [
        {"appointment_id": aid, "problem": "confirmed인데 리마인드 답장·확인 통화 없음"}
        for aid, a in appts.items()
        if a["confirmed"] and aid not in traced and aid not in called
    ]
    return bad


def check_cancel_replies(conn, ctx):
    """취소 답장(master.MESSAGE_REPLY_CANCEL)은 취소된 예약(변경 쌍의 원래 예약 아님)에만, 취소 시각 이전 — 개원 전 당일 취소와
    그날 전에 받은 취소는 통화가 없는 시각이라 반드시 취소 답장이 있다(§2.5) — 이탈 환자의 이탈일부터의 취소는 연락 없이"""
    appts = {
        r["id"]: r
        for r in _rows(
            conn, "SELECT id, patient_id, scheduled_at, status, cancelled_at FROM emr_appointments"
        )
    }
    origins = {
        r[0]
        for r in conn.execute(
            "SELECT rescheduled_from_id FROM emr_appointments WHERE rescheduled_from_id IS NOT NULL"
        )
    }
    bad, replied = [], set()
    for m in _rows(
        conn,
        "SELECT id, related_appointment_id AS appt, sent_at FROM emr_messages"
        " WHERE direction = 'in' AND body = ?",
        MESSAGE_REPLY_CANCEL,
    ):
        a = appts.get(m["appt"])
        if (
            a is None
            or a["status"] != "cancelled"
            or m["appt"] in origins
            or m["sent_at"] > a["cancelled_at"]
        ):
            bad.append({**m, "problem": "취소 답장인데 그 뒤 취소된 예약이 아님"})
        replied.add(m["appt"])
    opening = HOURS["open"] + ":00"
    churned = dict(
        conn.execute(
            "SELECT patient_id, churned_on FROM truth.truth_patient_churn WHERE kind = 'churn'"
        )
    )
    for aid, a in appts.items():
        if a["status"] != "cancelled" or aid in origins or aid in replied:
            continue
        if (on := churned.get(a["patient_id"])) is not None and a["cancelled_at"][:10] >= on:
            continue  # 이탈 환자는 이탈일부터 연락 없이 취소(§2.2)
        day, at = a["cancelled_at"][:10], a["cancelled_at"][11:]
        if day < a["scheduled_at"][:10] or at < opening:
            bad.append(
                {"appointment_id": aid, "problem": "통화 없는 시각의 취소인데 취소 답장 없음"}
            )
    return bad


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
