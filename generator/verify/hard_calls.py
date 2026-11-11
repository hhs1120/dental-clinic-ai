"""어려운 통화 hc- 검사 (어려운 통화 설계 docs/superpowers/specs/2026-11-11-hard-calls-design.md §3·§5) — 종류별 건수·번호·
날짜 창·목적·기존 예약 이벤트에 붙이기·응급 통화의 기존 예약·제외 환자·풀 밖·급함 뒤집기 라벨.
c- 통화 규칙(verify/calls.py)은 hc-를 세지 않으므로 hc-는 여기서 본다"""

import json
from collections import Counter, defaultdict
from datetime import timedelta

from generator.calls import next_open_day
from generator.hard_calls import excluded_patients
from generator.master import (
    BOOKING_PURPOSES,
    HARD_CALL_TYPES,
    HARD_CALLS_PER_TYPE,
    HARD_HALF_PURPOSES,
    HARD_TYPE_PURPOSES,
    HARD_URGENCY_HALVES,
    HARD_WINDOW_DAYS,
)
from generator.verify.calls import _booking_problem
from generator.verify.common import HARD_PREFIX, _dt, _rows


def _hard(conn) -> list[dict]:
    rows = _rows(
        conn,
        "SELECT c.*, l.primary_purpose, l.accepted_purposes, l.urgency, l.emergency, l.appt_confirmed,"
        " l.used_fact, l.split, h.hard_type, h.half FROM ax_calls c"
        " JOIN truth.truth_call_labels l USING (call_uid)"
        " LEFT JOIN truth.truth_hard_calls h USING (call_uid)"
        f" WHERE c.call_uid LIKE '{HARD_PREFIX}%' ORDER BY c.id",
    )
    for r in rows:
        r["fact"] = json.loads(r["used_fact"])
    return rows


def check_hard_counts(conn, ctx):
    """hc-는 종류(master.HARD_CALL_TYPES)마다 HARD_CALLS_PER_TYPE건, urgency_inverted는 절반(HARD_URGENCY_HALVES)마다 반씩,
    그 밖 종류는 절반 없음. 모든 hc-가 truth_hard_calls에 있고 split = hard"""
    rows = _hard(conn)
    bad = [
        {"call_uid": r["call_uid"], "problem": "truth_hard_calls 없음 또는 split ≠ hard"}
        for r in rows
        if r["hard_type"] is None or r["split"] != "hard"
    ]
    by_type = Counter(r["hard_type"] for r in rows if r["hard_type"])
    for t in HARD_CALL_TYPES:
        if by_type[t] != HARD_CALLS_PER_TYPE:
            bad.append({"hard_type": t, "n": by_type[t], "expected": HARD_CALLS_PER_TYPE})
    bad += [
        {"hard_type": t, "problem": "알 수 없는 종류"} for t in set(by_type) - set(HARD_CALL_TYPES)
    ]
    halves = Counter(r["half"] for r in rows if r["hard_type"] == "urgency_inverted")
    for h in HARD_URGENCY_HALVES:
        if halves[h] != HARD_CALLS_PER_TYPE // len(HARD_URGENCY_HALVES):
            bad.append({"half": h, "n": halves[h]})
    bad += [
        {"call_uid": r["call_uid"], "problem": "절반은 urgency_inverted만"}
        for r in rows
        if r["half"] is not None and r["hard_type"] != "urgency_inverted"
    ]
    return bad


def check_hard_uids(conn, ctx):
    """hc- 번호는 hc-0001부터 빈틈없이(종류 수 × HARD_CALLS_PER_TYPE개), truth_hard_calls는 hc-만"""
    n = len(HARD_CALL_TYPES) * HARD_CALLS_PER_TYPE
    have = {r["call_uid"] for r in _hard(conn)}
    want = {f"{HARD_PREFIX}{i:04d}" for i in range(1, n + 1)}
    stray = {
        r[0]
        for r in conn.execute("SELECT call_uid FROM truth.truth_hard_calls")
        if not r[0].startswith(HARD_PREFIX)
    }
    return [{"call_uid": u, "problem": "빠짐"} for u in sorted(want - have)] + [
        {"call_uid": u, "problem": "남음"} for u in sorted((have - want) | stray)
    ]


def check_hard_window(conn, ctx):
    """hc- 통화일은 TODAY 전 HARD_WINDOW_DAYS일 안(영업시간·진료일은 check_call_day가 모든 통화에)"""
    lo = ctx.today - timedelta(days=HARD_WINDOW_DAYS)
    return [
        {"call_uid": r["call_uid"], "started_at": r["started_at"]}
        for r in _hard(conn)
        if not lo <= _dt(r["started_at"]).date() < ctx.today
    ]


def check_hard_purposes(conn, ctx):
    """(주 목적, 방향)이 종류의 허용 목록(HARD_TYPE_PURPOSES), urgency_inverted는 절반의 목적(HARD_HALF_PURPOSES)"""
    bad = []
    for r in _hard(conn):
        ok = (r["primary_purpose"], r["direction"]) in HARD_TYPE_PURPOSES.get(r["hard_type"], ())
        if r["half"] is not None:
            ok = ok and r["primary_purpose"] in HARD_HALF_PURPOSES[r["half"]]
        if not ok:
            bad.append(
                {
                    "call_uid": r["call_uid"],
                    "hard_type": r["hard_type"],
                    "purpose": r["primary_purpose"],
                    "direction": r["direction"],
                }
            )
    return bad


def check_hard_booking_events(conn, ctx):
    """예약류 hc-는 기존 예약 이벤트에 붙고 c-와 같은 시각 규칙(verify/calls.py _booking_problem). 예외는 reversal의
    "예약하려다 미룸"(book·appt_confirmed 0 — 이벤트 없음). 이벤트 하나에 통화 최대 1건 — c-와 hc-를 합쳐 센다"""
    appts = {a["id"]: a for a in _rows(conn, "SELECT * FROM emr_appointments")}
    origins = {a["rescheduled_from_id"] for a in appts.values() if a["rescheduled_from_id"]}
    reminders = defaultdict(list)
    for m in _rows(
        conn,
        "SELECT related_appointment_id AS a, sent_at FROM emr_messages WHERE \"trigger\" = 'reminder'",
    ):
        reminders[m["a"]].append(m["sent_at"])
    bad = []
    for r in _hard(conn):
        if r["primary_purpose"] not in BOOKING_PURPOSES:
            continue
        aid = r["fact"].get("appointment_id")
        postponed = (
            r["hard_type"] == "reversal"
            and r["primary_purpose"] == "book"
            and not r["appt_confirmed"]
        )
        if postponed:
            if aid is not None:
                bad.append({"call_uid": r["call_uid"], "problem": "미룬 예약인데 이벤트가 붙음"})
            continue
        if p := _booking_problem(r, appts.get(aid), origins, reminders, ctx):
            bad.append({"call_uid": r["call_uid"], "appointment_id": aid, "problem": p})
    used = defaultdict(list)
    for r in _rows(
        conn,
        "SELECT l.call_uid, l.primary_purpose AS p, json_extract(l.used_fact, '$.appointment_id') AS a"
        " FROM truth.truth_call_labels l WHERE json_extract(l.used_fact, '$.appointment_id') IS NOT NULL",
    ):
        if r["p"] in BOOKING_PURPOSES:
            used[(r["p"], r["a"])].append(r["call_uid"])
    return bad + [
        {"event": k, "calls": v, "problem": "이벤트 하나에 통화 여러 건"}
        for k, v in used.items()
        if len(v) > 1 and any(u.startswith(HARD_PREFIX) for u in v)
    ]


def check_hard_emergency_appt(conn, ctx):
    """emergency 플래그 hc-는 used_fact existing_visit = 같은 환자의 기존 예약(취소 아님), 통화 뒤 그날 또는 다음 진료일 —
    새 응급 내원 예약은 만들지 않는다(설계 §3.3)"""
    appts = {a["id"]: a for a in _rows(conn, "SELECT * FROM emr_appointments")}
    bad = []
    for r in _hard(conn):
        if not r["emergency"]:
            continue
        a = appts.get((r["fact"].get("existing_visit") or {}).get("appointment_id"))
        call = _dt(r["started_at"])
        ok = (
            a is not None
            and a["patient_id"] == r["patient_id"]
            and a["status"] != "cancelled"
            and a["scheduled_at"] > r["started_at"]
            and _dt(a["scheduled_at"]).date() <= next_open_day(call.date(), ctx.holidays)
            and a["created_at"] < r["started_at"]
        )
        if not ok:
            bad.append({"call_uid": r["call_uid"], "appointment": a and a["id"]})
    return bad


def check_hard_excluded(conn, ctx):
    """hc- 환자는 제외 환자(hard_calls.excluded_patients — 통화 없음 전제 장면·RAG 평가셋 환자)가 아님"""
    out = excluded_patients(conn)
    return [
        {"call_uid": r["call_uid"], "patient_id": r["patient_id"]}
        for r in _hard(conn)
        if r["patient_id"] in out
    ]


def check_hard_not_pooled(conn, ctx):
    """hc-는 판정 풀·블라인드 풀 밖(순번 없음) — 채점 전용(설계 §2)"""
    return [
        {"call_uid": r["call_uid"]}
        for r in _hard(conn)
        if r["in_eval_pool"]
        or r["in_blind_pool"]
        or r["eval_sort_order"] is not None
        or r["blind_sort_order"] is not None
    ]


def check_hard_urgency_labels(conn, ctx):
    """calm_urgent = urgent·emergency 1(허용 목적에 treatment_q), pain_not_urgent = urgent 아님·emergency 0"""
    bad = []
    for r in _hard(conn):
        if r["half"] == "calm_urgent":
            ok = (
                r["urgency"] == "urgent"
                and r["emergency"] == 1
                and "treatment_q" in json.loads(r["accepted_purposes"])
            )
        elif r["half"] == "pain_not_urgent":
            ok = r["urgency"] != "urgent" and r["emergency"] == 0
        else:
            continue
        if not ok:
            bad.append({"call_uid": r["call_uid"], "half": r["half"], "urgency": r["urgency"]})
    return bad
