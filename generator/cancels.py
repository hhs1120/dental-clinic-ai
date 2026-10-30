"""8a단계 취소 연락 — 통화가 없는 시각의 취소에 환자 문자를 남긴다 (가상 데이터 생성 설계 §2.5 취소 답장·§2.6 (3) 장면).
환자가 그 예약의 문자(리마인드·예약 확정)에 답장하고 직원이 그 뒤 취소한다. 8단계 뒤라 다른 문자의 난수가 그대로이고, 이 단계는
난수를 쓰지 않는다(예약 번호 해시).
- 개원 전 당일 취소(4단계): 취소 시각 몇 분 전 답장
- 장면 1: TODAY 전 달 노쇼 master.NOSHOW_TO_CANCEL건을 개원 전 당일 취소로 — 노쇼 문자와 그 답장은 지운다
- 장면 2: TODAY 예약 1건 — 전날 리마인드에 취소 답장, 그 뒤 취소(TODAY 준비표·오늘 화면에 빈 시간)
문자 id는 시각순으로 다시 매긴다(8단계와 같은 규칙)
"""

import hashlib
from datetime import date, datetime, timedelta

from generator.calls import FMT, OPEN
from generator.master import (
    MESSAGE_DELAY_MIN,
    MESSAGE_REPLY_CANCEL,
    NOSHOW_TO_CANCEL,
    SAMEDAY_CANCEL_LEAD_MIN,
)
from generator.messages import emergency_appointments

PROCESS_MIN = 5  # 장면 2 — 답장을 보고 직원이 취소하기까지


def _hash(*parts) -> int:
    return int(hashlib.sha256(":".join(map(str, parts)).encode()).hexdigest(), 16)


def _prev_month(today: date) -> str:
    return (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")


def _untouched(conn, run) -> set[int]:
    """장면에 쓰지 않는 예약 — 통화 사실이 가리키는 예약, 응급 내원, 이탈 환자의 예약"""
    used = {
        r[0]
        for r in conn.execute(
            "SELECT json_extract(used_fact, '$.appointment_id') FROM truth.truth_call_labels"
            " WHERE json_extract(used_fact, '$.appointment_id') IS NOT NULL"
        )
    } | emergency_appointments(conn)
    churn = set(run.state["churn"])
    return used | {
        r[0] for r in conn.execute("SELECT id, patient_id FROM emr_appointments") if r[1] in churn
    }


def _noshow_scenes(conn, run, skip: set) -> list[int]:
    """TODAY 전 달 노쇼 중 개원 전에 당일 취소할 수 있는 시각(개원 뒤 SAMEDAY_CANCEL_LEAD_MIN분 안 — 4단계 당일 취소 규칙)이고
    그날 이후 그 환자의 통화가 없는 것 — 예약 번호 해시순 NOSHOW_TO_CANCEL건"""
    latest = (
        datetime.combine(run.today, OPEN) + timedelta(minutes=SAMEDAY_CANCEL_LEAD_MIN - 1)
    ).time()
    rows = conn.execute(
        "SELECT a.id FROM emr_appointments a WHERE a.status = 'noshow' AND substr(a.scheduled_at, 1, 7) = ?"
        f" AND time(a.scheduled_at) <= '{latest.strftime('%H:%M:%S')}'"
        " AND NOT EXISTS (SELECT 1 FROM ax_calls c WHERE c.patient_id = a.patient_id"
        " AND c.started_at >= date(a.scheduled_at))",
        (_prev_month(run.today),),
    ).fetchall()
    pool = sorted((r[0] for r in rows if r[0] not in skip), key=lambda a: _hash(a, "noshow"))
    if len(pool) < NOSHOW_TO_CANCEL:
        raise ValueError(f"노쇼 → 취소 장면 후보 {len(pool)}건 < {NOSHOW_TO_CANCEL}건")
    return pool[:NOSHOW_TO_CANCEL]


def _today_scene(conn, run, skip: set) -> tuple[int, str] | None:
    """TODAY 예약 중 리마인드 뒤 답장이 없고 confirmed가 아닌 것 — 해시 첫 번째와 그 리마인드 시각"""
    rows = conn.execute(
        "SELECT a.id, m.sent_at FROM emr_appointments a JOIN emr_messages m"
        " ON m.related_appointment_id = a.id AND m.\"trigger\" = 'reminder'"
        " WHERE date(a.scheduled_at) = ? AND a.status = 'scheduled' AND a.confirmed = 0"
        " AND NOT EXISTS (SELECT 1 FROM emr_messages r WHERE r.related_appointment_id = a.id"
        " AND r.direction = 'in' AND r.sent_at > m.sent_at)",
        (run.today.isoformat(),),
    ).fetchall()
    pool = sorted((r for r in rows if r[0] not in skip), key=lambda r: _hash(r[0], "today"))
    return pool[0] if pool else None


def stage(run, rng, fake) -> None:
    conn = run.conn
    skip = _untouched(conn, run)
    for aid in _noshow_scenes(conn, run, skip):
        start = datetime.strptime(
            conn.execute(
                "SELECT scheduled_at FROM emr_appointments WHERE id = ?", (aid,)
            ).fetchone()[0],
            FMT,
        )
        # 예약 1~SAMEDAY_CANCEL_LEAD_MIN분 전이면서 개원 전
        early = int((start - datetime.combine(start.date(), OPEN)).total_seconds() // 60) + 1
        at = start - timedelta(
            minutes=early + _hash(aid, "cancel") % (SAMEDAY_CANCEL_LEAD_MIN - early + 1)
        )
        conn.execute(
            "UPDATE emr_appointments SET status = 'cancelled', cancelled_at = ? WHERE id = ?",
            (at.strftime(FMT), aid),
        )
        conn.execute(
            "DELETE FROM emr_messages WHERE related_appointment_id = ? AND sent_at >= ("
            "SELECT min(sent_at) FROM emr_messages WHERE related_appointment_id = ? AND \"trigger\" = 'noshow')",
            (aid, aid),
        )
    replies = []
    if (scene := _today_scene(conn, run, skip)) is not None:
        aid, reminder = scene
        lo, hi = MESSAGE_DELAY_MIN
        reply = datetime.strptime(reminder, FMT) + timedelta(minutes=lo + aid % (hi - lo + 1))
        conn.execute(
            "UPDATE emr_appointments SET status = 'cancelled', cancelled_at = ? WHERE id = ?",
            ((reply + timedelta(minutes=PROCESS_MIN)).strftime(FMT), aid),
        )
        replies.append((aid, reply))
    origins = {
        r[0]
        for r in conn.execute(
            "SELECT rescheduled_from_id FROM emr_appointments WHERE rescheduled_from_id IS NOT NULL"
        )
    }
    for aid, cancelled in conn.execute(
        "SELECT id, cancelled_at FROM emr_appointments WHERE status = 'cancelled'"
        " AND date(cancelled_at) = date(scheduled_at) AND time(cancelled_at) < ? ORDER BY id",
        (OPEN.strftime("%H:%M:%S"),),
    ).fetchall():
        if aid in origins:
            continue
        last = conn.execute(
            "SELECT max(sent_at) FROM emr_messages WHERE related_appointment_id = ? AND direction = 'out'"
            " AND sent_at < ?",
            (aid, cancelled),
        ).fetchone()[0]
        if last is None:
            raise ValueError(f"개원 전 취소 {aid}: 답장할 문자 없음")
        at = datetime.strptime(cancelled, FMT) - timedelta(minutes=1 + aid % 10)
        replies.append((aid, max(at, datetime.strptime(last, FMT) + timedelta(minutes=1))))
    patient = dict(conn.execute("SELECT id, patient_id FROM emr_appointments"))
    conn.executemany(
        'INSERT INTO emr_messages (patient_id, direction, sent_at, body, "trigger", related_appointment_id)'
        " VALUES (?, 'in', ?, ?, 'reply', ?)",
        [(patient[a], t.strftime(FMT), MESSAGE_REPLY_CANCEL, a) for a, t in replies],
    )
    _renumber_messages(conn)


def _renumber_messages(conn) -> None:
    order = [r[0] for r in conn.execute("SELECT id FROM emr_messages ORDER BY sent_at, id")]
    offset = max(order) + 1
    conn.executemany(
        "UPDATE emr_messages SET id = id + ? WHERE id = ?", [(offset, i) for i in order]
    )
    conn.executemany(
        "UPDATE emr_messages SET id = ? WHERE id = ?",
        [(k, i + offset) for k, i in enumerate(order, start=1)],
    )
