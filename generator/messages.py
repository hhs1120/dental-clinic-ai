"""8단계 문자 — emr_messages (가상 데이터 생성 설계 §2.5 '8단계 구현 결정'). 전부 템플릿(LLM 없음).
트리거별 발송 비율은 config messages. out은 트리거 뒤, 답장(in)은 out 뒤. TODAY 전날까지만(TODAY는 개원 전 스냅샷).
- 예약 확정: 창 안 예약 created_at 뒤 / 리마인드: 리마인드 대상(§2.6 (3) 4)에 직전 진료일 개원 시각 일괄(confirmed는 반드시)
- 노쇼: 진료가 끝났을 시각 뒤 / 미수 안내: 미수가 생긴 내원의 다음 진료일 / 서류 준비: 환자가 건 서류 요청 통화(주 목적)의 다음 진료일
- 리콜: 그달 첫 진료일 개원 시각에 잡힌 예약이 없는 환자 중 기한(마지막 completed 위생 내원 + RECALL_CYCLE)이 다음 달이면
  다가옴, 지난달이면 지남 — 기한마다 최대 2건(RECALL_TEMPLATES)
- 답장: 이탈 환자(§2.2)는 이탈일부터 어떤 문자에도 답하지 않는다. 장부 실수(입력 누락·금액 오입력)로 생긴 미수의 안내 답장은
  이미 냈다는 문구(MESSAGE_REPLIES_PAID + 실제 결제수단의 MESSAGE_REPLIES_PAID_METHOD), 진짜 미수도 PAID_REPLY_NOISE_RATE만큼
  그 문구(착각)
"""

from datetime import date, datetime, timedelta

from generator.calls import FMT, OPEN, any_minute, next_open_day, prev_open_day, recall_targets
from generator.master import (
    CLINIC_NAME,
    MESSAGE_CONFIRM_DELAY_MIN,
    MESSAGE_DELAY_MIN,
    MESSAGE_REPLIES,
    MESSAGE_REPLIES_PAID,
    MESSAGE_REPLIES_PAID_METHOD,
    MESSAGE_REPLY_CHANGE,
    MESSAGE_REPLY_OK,
    MESSAGE_TEMPLATES,
    PAID_REPLY_NOISE_RATE,
    PROCEDURES,
    RECALL_TEMPLATES,
)

PROCEDURE_NAME = {p["code"]: p["name"] for p in PROCEDURES}
WEEKDAYS = "월화수목금토일"


def _day(d: date) -> str:
    return f"{d.month}월 {d.day}일({WEEKDAYS[d.weekday()]})"


def _minutes(rng, lo_hi: tuple) -> timedelta:
    lo, hi = lo_hi
    return timedelta(minutes=int(rng.integers(lo, hi + 1)))


def emergency_appointments(conn) -> set[int]:
    """응급 통화가 잡은 내원 (6b단계 emergency_visit)"""
    return {
        r[0]
        for r in conn.execute(
            "SELECT json_extract(used_fact, '$.emergency_visit.appointment_id') FROM truth.truth_call_labels"
            " WHERE json_extract(used_fact, '$.emergency_visit') IS NOT NULL"
        )
    }


def stage(run, rng, fake) -> None:
    conn, rates = run.conn, run.cfg["messages"]
    start = run.range_start.isoformat()
    names = dict(conn.execute("SELECT id, name FROM emr_patients"))
    out = []  # (sent_at, patient_id, trigger, related_appointment_id, body)

    def send(
        t: datetime, pid: int, trigger: str, appt: int | None, template=None, **fields
    ) -> None:
        if t.date() < run.today:
            body = (template or MESSAGE_TEMPLATES[trigger]).format(
                clinic=CLINIC_NAME, name=names[pid], **fields
            )
            out.append((t, pid, trigger, appt, body))

    def next_open_minute(d: date) -> datetime:
        return any_minute(rng, next_open_day(d, run.holidays))

    # 응급 내원(6b단계)은 통화로 바로 잡혀 예약 문자가 없다 — 난수를 쓰기 전에 건너뛰어 다른 예약의 문자가 그대로
    emergency = emergency_appointments(conn)
    for aid, pid, at, duration, code, status, confirmed, created, cancelled in conn.execute(
        "SELECT id, patient_id, scheduled_at, duration_min, planned_procedure_code, status, confirmed, created_at,"
        " cancelled_at FROM emr_appointments ORDER BY id"
    ):
        if aid in emergency:
            continue
        when = datetime.strptime(at, FMT)
        fields = {"date": _day(when.date()), "time": at[11:16], "procedure": PROCEDURE_NAME[code]}
        if created >= start and rng.random() < rates["appt_confirm"]:
            send(
                datetime.strptime(created, FMT) + _minutes(rng, MESSAGE_CONFIRM_DELAY_MIN),
                pid,
                "appt_confirm",
                aid,
                **fields,
            )
        day = prev_open_day(when.date(), run.holidays)
        target = (
            run.range_start <= day < run.today
            and created[:10] < day.isoformat()
            and (cancelled is None or cancelled[:10] > day.isoformat())
        )
        if target and (confirmed or rng.random() < rates["reminder"]):
            send(datetime.combine(day, OPEN), pid, "reminder", aid, **fields)
        if status == "noshow" and rng.random() < rates["noshow"]:
            send(
                when + timedelta(minutes=duration) + _minutes(rng, MESSAGE_DELAY_MIN),
                pid,
                "noshow",
                aid,
                **fields,
            )
    for aid, pid, paid_at, owed, at in conn.execute(
        "SELECT p.appointment_id, p.patient_id, p.paid_at, p.amount_due - p.amount_paid, a.scheduled_at"
        " FROM emr_payments p JOIN emr_appointments a ON a.id = p.appointment_id"
        " WHERE p.kind = 'visit' AND p.amount_paid < p.amount_due ORDER BY p.id"
    ):
        if rng.random() < rates["balance"]:
            send(
                next_open_minute(date.fromisoformat(paid_at[:10])),
                pid,
                "balance",
                aid,
                date=_day(date.fromisoformat(at[:10])),
                amount=f"{owed:,}",
            )
    for t, pid, kind, due in recall_targets(run):  # 대상 전원 (§2.5)
        send(t, pid, "recall", None, RECALL_TEMPLATES[kind], month=due.month)
    for pid, started in conn.execute(
        "SELECT c.patient_id, c.started_at FROM ax_calls c JOIN truth.truth_call_labels l USING (call_uid)"
        " WHERE l.primary_purpose = 'document_req' AND c.direction = 'inbound' AND c.patient_id IS NOT NULL"
        " ORDER BY c.id"
    ):
        send(next_open_minute(date.fromisoformat(started[:10])), pid, "doc_ready", None)
    churn = run.state["churn"]
    ledger_debt = dict(  # 장부 실수 미수 내원 → 실제 결제수단
        conn.execute(
            "SELECT p.appointment_id, e.true_method FROM truth.truth_ledger_errors e JOIN emr_payments p ON p.id = e.payment_id"
            " WHERE e.kind IN ('entry_missing', 'amount_typo') AND p.kind = 'visit'"
        )
    )
    visit_method = dict(
        conn.execute("SELECT appointment_id, method FROM emr_payments WHERE kind = 'visit'")
    )
    # 예약 확정·리마인드 답장은 예약 상태에 맞춘다(§2.5) — 바뀐 예약(변경 쌍의 원래 예약)과 그 취소 시각, confirmed,
    # 예약 확인 통화
    moved = dict(
        conn.execute(
            "SELECT o.id, o.cancelled_at FROM emr_appointments n JOIN emr_appointments o ON o.id = n.rescheduled_from_id"
        )
    )
    confirmed = {r[0] for r in conn.execute("SELECT id FROM emr_appointments WHERE confirmed = 1")}
    called = {
        r[0]
        for r in conn.execute(
            "SELECT json_extract(used_fact, '$.appointment_id') FROM truth.truth_call_labels"
            " WHERE primary_purpose = 'confirm'"
        )
    }

    def appt_reply(trigger: str, appt: int, drawn: str, at: datetime) -> str | None:
        """뽑은 답장을 예약 상태에 맞춘다(난수를 더 쓰지 않는다) — 시간 변경은 그 뒤 실제로 바뀐 예약에만, 리마인드
        확인 답장은 confirmed 예약에만(아니면 답장 없음)"""
        change_ok = appt in moved and at.strftime(FMT) <= moved[appt]
        if drawn == MESSAGE_REPLY_CHANGE and not change_ok:
            drawn = MESSAGE_REPLY_OK[appt % len(MESSAGE_REPLY_OK)]
        if trigger == "reminder" and drawn in MESSAGE_REPLY_OK and appt not in confirmed:
            return MESSAGE_REPLY_CHANGE if change_ok else None
        return drawn

    traced = set()  # 리마인드 답장이 있는 예약
    rows = [(t, pid, "out", trigger, appt, body) for t, pid, trigger, appt, body in out]
    for t, pid, trigger, appt, _ in out:
        if rng.random() < rates["reply_rate"]:
            reply = t + _minutes(rng, MESSAGE_DELAY_MIN)
            if pid in churn and reply.date() >= churn[pid]:
                continue  # 이탈 — 이탈일부터 어떤 문자에도 답하지 않는다
            if reply.date() < run.today:
                options = MESSAGE_REPLIES[trigger]
                if trigger == "balance" and (
                    appt in ledger_debt or rng.random() < PAID_REPLY_NOISE_RATE
                ):
                    method = ledger_debt.get(appt) or visit_method[appt]
                    options = (*MESSAGE_REPLIES_PAID, MESSAGE_REPLIES_PAID_METHOD[method])
                body = options[int(rng.integers(len(options)))]
                if trigger in ("appt_confirm", "reminder"):
                    body = appt_reply(trigger, appt, body, reply)
                    if body is None:
                        continue
                rows.append((reply, pid, "in", "reply", appt, body))
                if trigger == "reminder":
                    traced.add(appt)
    # confirmed(리마인드 응답)인데 답장도 확인 통화도 없으면 확인 답장을 남긴다 — 확인의 흔적. 진료 번호로 문구·지연을 정해
    # 난수를 쓰지 않는다(위 답장 추첨이 그대로)
    lo, hi = MESSAGE_DELAY_MIN
    for t, pid, trigger, appt, _ in out:
        if trigger != "reminder" or appt not in confirmed or appt in traced or appt in called:
            continue
        reply = t + timedelta(minutes=lo + appt % (hi - lo + 1))
        if reply.date() < run.today and not (pid in churn and reply.date() >= churn[pid]):
            rows.append(
                (reply, pid, "in", "reply", appt, MESSAGE_REPLY_OK[appt % len(MESSAGE_REPLY_OK)])
            )
    rows.sort(key=lambda r: r[0])  # 안정 정렬 — 같은 시각은 생성 순서
    conn.executemany(
        'INSERT INTO emr_messages (id, patient_id, direction, sent_at, body, "trigger", related_appointment_id)'
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (i, pid, d, t.strftime(FMT), body, trig, appt)
            for i, (t, pid, d, trig, appt, body) in enumerate(rows, 1)
        ],
    )
