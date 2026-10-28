"""통화 검사 — ax_calls 컬럼(누출)·통화 슬롯·전사·판정·블라인드 풀, 통화 분포 경고·리포트
(6·7단계, 가상 데이터 생성 설계 §4.1·§5.3)"""

import json
import re
from collections import Counter, defaultdict
from datetime import date, datetime

from generator import llm_jobs, transcripts
from generator.master import (
    BOOKING_PURPOSES,
    CALL_COUNTERPARTS,
    CALL_DURATION_SEC,
    CALL_FLAGS,
    CALL_LABEL_WORDS,
    CALL_NOISE_SHARE,
    CALL_OUTBOUND_SHARE,
    CALL_PAIN_PURPOSES,
    CALL_PURPOSE_SHARE,
    CALL_SUBTOPICS,
    EVAL_MIN_PER_SPLIT,
    EVAL_RARE_PURPOSES,
    EVAL_RARE_SHARE,
    HOURS,
    IMPLIED_FLAG,
    SUBTOPIC_PURPOSE,
    URGENCIES,
    age_on,
    open_slots,
)
from generator.verify.common import CHILD_MAX_AGE, EXAMPLES, PROCEDURE, _dt, _rows
from generator.verify.records import _open_items_at, _plan_items
from generator.verify.schedule import _prev_open_day

# ── 누출 ────────────────────────────────────────────────────


# shared/source_schema.sql의 ax_calls 컬럼(의미는 가상 데이터 생성 설계 §3). 이 밖의 컬럼(라벨·노이즈·카테고리 등)은 정답 누출
AX_CALLS_COLUMNS = {
    "id",
    "call_uid",
    "source",
    "patient_id",
    "caller_phone",
    "caller_name",
    "direction",
    "started_at",
    "duration_sec",
    "has_transcript",
    "audio_path",
    "transcript",
    "in_eval_pool",
    "eval_sort_order",
    "in_blind_pool",
    "blind_sort_order",
}


CALL_UID = re.compile(r"c-\d{4,}")


def check_ax_calls_columns(conn, ctx):
    """ax_calls에 정해진 컬럼 밖(라벨·노이즈·카테고리) 없음"""
    return [
        {"column": r["name"]}
        for r in conn.execute("PRAGMA main.table_info(ax_calls)")
        if r["name"] not in AX_CALLS_COLUMNS
    ]


def check_call_uid_format(conn, ctx):
    """call_uid는 'c-' + 순번 숫자만 (목적 enum 등 의미 있는 문자열 누출 방지)"""
    return [
        r
        for r in _rows(conn, "SELECT id, call_uid FROM ax_calls")
        if not CALL_UID.fullmatch(r["call_uid"])
    ]


# ── 통화 슬롯 (6단계, §4.1) ─────────────────────────────────────


CALL_MIX_TOLERANCE = 0.02  # 목적·노이즈 분포 경고 허용치 (§5.3 경고 표)


OPEN_TIME = datetime.strptime(HOURS["open"], "%H:%M").time()


CLOSE_TIME = datetime.strptime(HOURS["weekday_close"], "%H:%M").time()


_SIDE = ("patient", "guardian")


def _calls(conn) -> list[dict]:
    """생성 통화 + 골드 라벨 (accepted·fact는 JSON 풀어서)"""
    rows = _rows(
        conn,
        "SELECT c.*, l.primary_purpose, l.accepted_purposes, l.counterpart, l.urgency,"
        f" {', '.join('l.' + f for f in CALL_FLAGS)}, l.noise_type, l.used_fact, l.split"
        " FROM ax_calls c JOIN truth.truth_call_labels l USING (call_uid) WHERE c.source = 'generated'"
        " ORDER BY c.id",
    )
    for r in rows:
        r["accepted"], r["fact"] = json.loads(r["accepted_purposes"]), json.loads(r["used_fact"])
    return rows


def check_call_labels_match(conn, ctx):
    """생성 통화 ↔ truth_call_labels 1:1 (call_uid)"""
    calls = {
        r[0] for r in conn.execute("SELECT call_uid FROM ax_calls WHERE source = 'generated'")
    }
    labels = {r[0] for r in conn.execute("SELECT call_uid FROM truth.truth_call_labels")}
    return [{"call_uid": u, "problem": "라벨 없음"} for u in sorted(calls - labels)] + [
        {"call_uid": u, "problem": "통화 없음"} for u in sorted(labels - calls)
    ]


def _label_problem(r) -> str | None:
    purpose = r["primary_purpose"]
    if (
        purpose not in CALL_PURPOSE_SHARE
        or r["counterpart"] not in CALL_COUNTERPARTS
        or r["urgency"] not in URGENCIES
        or (r["noise_type"] is not None and r["noise_type"] not in CALL_NOISE_SHARE)
    ):
        return "enum 밖"
    implied = IMPLIED_FLAG.get(purpose)
    if implied and not r[implied]:
        return "주 목적이 함의하는 플래그 꺼짐"
    subs = [f for f in SUBTOPIC_PURPOSE if r[f] and f != implied]
    if r["accepted"][:1] != [purpose] or sorted(r["accepted"][1:]) != sorted(
        SUBTOPIC_PURPOSE[f] for f in subs
    ):
        return "accepted_purposes ≠ [주 목적] + 부 주제 플래그"
    if len(subs) > 1 or not set(subs) <= set(CALL_SUBTOPICS[purpose]):
        return "부 주제가 CALL_SUBTOPICS 밖"
    if r["emergency"] and r["urgency"] != "urgent":
        return "응급인데 urgent 아님"
    if (
        purpose in ("book", "reschedule")
        and r["urgency"] == "urgent"
        and r["fact"].get("appointment", {}).get("date") != r["started_at"][:10]
    ):
        return "예약·변경 통화가 urgent인데 예약이 그날이 아님"
    if r["appt_confirmed"] and purpose not in ("book", "reschedule"):
        return "appt_confirmed는 book·reschedule만"
    if r["direction"] == "outbound" and purpose not in CALL_OUTBOUND_SHARE:
        return "outbound가 아닌 목적"
    noise = r["noise_type"]
    if (noise == "two_topics") != (r["direction"] == "inbound" and bool(subs)):
        return "inbound 복합 주제 ⇔ 노이즈 주제 2개"
    if noise == "guardian_confused" and r["counterpart"] != "guardian":
        return "보호자 노이즈인데 보호자 아님"
    if noise == "ambiguous_pain" and purpose not in CALL_PAIN_PURPOSES:
        return "애매한 통증 노이즈 대상 목적 아님"
    return None


def check_call_label_values(conn, ctx):
    """골드 라벨: enum, 함의 플래그, accepted = 주 목적 + 부 주제(CALL_SUBTOPICS 안 1개), 응급 → urgent,
    예약·변경 통화의 urgent는 예약이 그날일 때만,
    outbound 목적, inbound 복합 ⇔ 노이즈 '주제 2개', 노이즈 대상(보호자·통증 목적)"""
    return [
        {"call_uid": r["call_uid"], "problem": p} for r in _calls(conn) if (p := _label_problem(r))
    ]


def check_call_slot_count(conn, ctx):
    """생성 통화 수 = calls_with_transcript"""
    n = conn.execute("SELECT count(*) FROM ax_calls WHERE source = 'generated'").fetchone()[0]
    return [] if n == ctx.call_slots else [{"calls": n, "expected": ctx.call_slots}]


def check_call_day(conn, ctx):
    """통화는 range_start ~ TODAY 전날의 진료일, 개원 ~ 평일 마감 (§2.6 사건 시각)"""
    bad = []
    for r in _rows(conn, "SELECT id, started_at FROM ax_calls WHERE source = 'generated'"):
        t = _dt(r["started_at"])
        if not (
            ctx.range_start <= t.date() < ctx.today
            and open_slots(t.weekday())
            and t.date() not in ctx.holidays
            and OPEN_TIME <= t.time() < CLOSE_TIME
        ):
            bad.append(r)
    return bad


def check_call_duration(conn, ctx):
    """duration_sec은 CALL_DURATION_SEC 안"""
    lo, hi = CALL_DURATION_SEC
    return _rows(
        conn,
        "SELECT id, duration_sec FROM ax_calls WHERE source = 'generated'"
        " AND duration_sec NOT BETWEEN ? AND ?",
        lo,
        hi,
    )


def check_call_transcripts(conn, ctx):
    """생성 통화는 전사 있음 (7단계)"""
    return _rows(
        conn,
        "SELECT id, call_uid FROM ax_calls WHERE source = 'generated'"
        " AND (has_transcript != 1 OR transcript IS NULL OR trim(transcript) = '')",
    )


def _transcripts(conn) -> list[dict]:
    return _rows(
        conn,
        "SELECT c.id, c.transcript, l.used_fact FROM ax_calls c JOIN truth.truth_call_labels l"
        " USING (call_uid) WHERE c.source = 'generated' AND c.transcript IS NOT NULL ORDER BY c.id",
    )


def check_transcript_speakers(conn, ctx):
    """전사 화자 형식 — 줄마다 '환자:'/'직원:', 둘 다 나옴 (경고)"""
    bad = []
    for r in _transcripts(conn):
        lines = [ln for ln in r["transcript"].splitlines() if ln.strip()]
        if not all(ln.startswith(tuple(transcripts.SPEAKERS)) for ln in lines) or not all(
            any(ln.startswith(s) for ln in lines) for s in transcripts.SPEAKERS
        ):
            bad.append({"id": r["id"], "transcript": r["transcript"][:40]})
    return bad


def check_transcript_facts(conn, ctx):
    """꼭 넣을 사실(예약 날짜·주제 시술 별칭)이 전사에 있음 (경고)"""
    names = llm_jobs.procedure_names(conn)
    bad = []
    for r in _transcripts(conn):
        missing = [
            alts[0]
            for alts in transcripts.must_include(json.loads(r["used_fact"]), names)
            if not any(llm_jobs.contains(r["transcript"], a) for a in alts)
        ]
        if missing:
            bad.append({"id": r["id"], "missing": missing})
    return bad


TRANSCRIPT_LABEL_WORD_MAX = 0.2  # 라벨 어휘가 그대로 나오는 전사 비율 상한 (§5.3 경고)


def check_transcript_label_words(conn, ctx):
    """라벨 어휘(master.CALL_LABEL_WORDS)가 그대로 나오는 전사 < 20% (경고). 채우기 검증과 같은 llm_jobs.has_word('예약변경'도 잡음)"""
    rows = _transcripts(conn)
    hit = [
        r["id"]
        for r in rows
        if any(llm_jobs.has_word(r["transcript"], w) for w in CALL_LABEL_WORDS)
    ]
    share = len(hit) / len(rows) if rows else 0.0
    return (
        [{"share": round(share, 3), "ids": hit[:EXAMPLES]}]
        if share >= TRANSCRIPT_LABEL_WORD_MAX
        else []
    )


_LABEL_TOKENS = sorted(
    {*CALL_PURPOSE_SHARE, *CALL_NOISE_SHARE, *CALL_COUNTERPARTS, *URGENCIES, *CALL_FLAGS}
)


def check_label_tokens(conn, ctx):
    """저장 텍스트(전사·차트 본문)에 영문 라벨 값·요건 키(R01 등) 없음 (경고) — LLM이 입력 라벨을 옮겨 적은 누출"""
    token = re.compile(
        r"(?<![A-Za-z_])(" + "|".join(map(re.escape, _LABEL_TOKENS)) + r"|R0\d)(?![A-Za-z_\d])"
    )
    bad = []
    for table, key, col in (
        ("ax_calls", "id", "transcript"),
        ("emr_chart_notes", "treatment_id", "note"),
    ):
        for r in _rows(
            conn, f"SELECT {key} AS id, {col} AS text FROM {table} WHERE {col} IS NOT NULL"
        ):
            bad += [
                {"table": table, "id": r["id"], "token": t}
                for t in sorted(set(token.findall(r["text"])))
            ]
    return bad


def check_call_row_order(conn, ctx):
    """행 순서(id) = 통화 시각순 — 생성 순서(목적별)가 id에 드러나지 않게"""
    rows = _rows(
        conn, "SELECT id, started_at FROM ax_calls WHERE source = 'generated' ORDER BY id"
    )
    return [b for a, b in zip(rows, rows[1:], strict=False) if b["started_at"] < a["started_at"]]


def _booking_problem(r, a, origins, reminders, ctx) -> str | None:
    if a is None:
        return "appointment_id 없음"
    if a["patient_id"] != r["patient_id"]:
        return "예약 환자와 통화 환자 다름"
    if a["created_at"] < ctx.range_start.isoformat():
        return "창 이전 예약"
    t, purpose = r["started_at"], r["primary_purpose"]
    # 확정 통화는 예약을 잡은 그 시각, 미확정은 그날 그보다 앞(통화 뒤 회신해서 확정)
    at_booking = (
        t == a["created_at"]
        if r["appt_confirmed"]
        else t[:10] == a["created_at"][:10] and _dt(t).time() > OPEN_TIME and t < a["created_at"]
    )
    if purpose == "book" and not (
        a["booked_via"] == "phone" and a["rescheduled_from_id"] is None and at_booking
    ):
        return "예약 잡기 = 전화 예약(변경 새 예약 아님)의 created_at (미확정이면 그날 그 전)"
    if purpose == "reschedule" and not (a["rescheduled_from_id"] is not None and at_booking):
        return "변경 = 변경 쌍 새 예약의 created_at (미확정이면 그날 그 전)"
    if purpose == "cancel" and not (
        a["status"] == "cancelled" and a["id"] not in origins and t == a["cancelled_at"]
    ):
        return "취소 = 당일 취소의 cancelled_at"
    if purpose == "confirm":
        call = _dt(t)
        if (
            not a["confirmed"]
            or call.date() != _prev_open_day(_dt(a["scheduled_at"]).date(), ctx.holidays)
            or call.time() <= OPEN_TIME
            or any(s >= t for s in reminders.get(a["id"], []))
        ):
            return "확인 = confirmed 예약의 리마인드 날 개원 뒤, 리마인드 문자 뒤"
    return None


def check_booking_call_event(conn, ctx):
    """예약류 통화 ↔ 예약 이벤트: 통화 시각 == 이벤트 시각(미확정 예약·변경은 그날 그 전, 확인은 리마인드 날·문자 뒤), 같은 환자, 창 안 예약,
    이벤트 하나에 통화 최대 1건(= 목적별 이벤트 수 ≥ 통화 수)"""
    appts = {a["id"]: a for a in _rows(conn, "SELECT * FROM emr_appointments")}
    origins = {a["rescheduled_from_id"] for a in appts.values() if a["rescheduled_from_id"]}
    reminders = defaultdict(list)
    for m in _rows(
        conn,
        "SELECT related_appointment_id AS a, sent_at FROM emr_messages WHERE \"trigger\" = 'reminder'",
    ):
        reminders[m["a"]].append(m["sent_at"])
    bad, used = [], defaultdict(list)
    for r in _calls(conn):
        if r["primary_purpose"] not in BOOKING_PURPOSES:
            continue
        aid = r["fact"].get("appointment_id")
        used[(r["primary_purpose"], aid)].append(r["call_uid"])
        if p := _booking_problem(r, appts.get(aid), origins, reminders, ctx):
            bad.append({"call_uid": r["call_uid"], "appointment_id": aid, "problem": p})
    return bad + [
        {"event": k, "calls": v, "problem": "이벤트 하나에 통화 여러 건"}
        for k, v in used.items()
        if len(v) > 1
    ]


def _is_child(p: dict, day: date) -> bool:
    """보호자가 거는 환자 — 통화일에 소아이고 보호자 기록이 있음"""
    return (
        p["guardian_patient_id"] is not None
        and age_on(date.fromisoformat(p["birth_date"]), day) <= CHILD_MAX_AGE
    )


def check_call_counterpart(conn, ctx):
    """상대 ↔ 환자·발신자: 환자 측은 환자 연결·통화일 소아 ⇔ 보호자·번호/이름 = 말하는 사람(소아면 보호자), 처음 연락 예약은
    그날 등록한 소아 아닌 환자(소아는 기존 환자인 보호자가 건다), 그 밖의 처음 연락·보험사·업체·기타는 환자 없음·환자 번호 아님, outbound는 환자 측"""
    patients = {p["id"]: p for p in _rows(conn, "SELECT * FROM emr_patients")}
    phones = {p["phone"] for p in patients.values()}
    bad = []
    for r in _calls(conn):
        who, p = r["counterpart"], patients.get(r["patient_id"])
        day = _dt(r["started_at"]).date()
        if who in _SIDE or (who == "new_caller" and r["primary_purpose"] in BOOKING_PURPOSES):
            if p is None:
                problem = "환자 측인데 환자 없음"
            elif who == "new_caller" and (
                r["primary_purpose"] != "book"
                or p["registered_at"] != day.isoformat()
                or _is_child(p, day)
            ):
                problem = "처음 연락 예약은 그날 등록한 소아 아닌 환자의 예약 잡기만"
            elif (
                r["primary_purpose"] == "book"
                and who != "new_caller"
                and p["registered_at"] == day.isoformat()
                and not _is_child(p, day)
            ):
                problem = "그날 등록한 소아 아닌 환자의 예약 잡기는 처음 연락"
            elif who != "new_caller" and (who == "guardian") != _is_child(p, day):
                problem = "통화일 소아 ⇔ 보호자"
            else:
                speaker = patients.get(p["guardian_patient_id"]) if _is_child(p, day) else p
                problem = (
                    None
                    if speaker
                    and (r["caller_phone"], r["caller_name"]) == (p["phone"], speaker["name"])
                    else "발신 번호·이름 ≠ 말하는 사람"
                )
        elif r["patient_id"] is not None or r["caller_phone"] in phones:
            problem = "환자 측이 아닌데 환자 연결·환자 번호"
        else:
            problem = None
        if problem is None and r["direction"] == "outbound" and who not in _SIDE:
            problem = "outbound는 환자 측만"
        if problem:
            bad.append({"call_uid": r["call_uid"], "counterpart": who, "problem": problem})
    return bad


def _facts_at(conn, pid: int, t: str, birth: str, items: list[dict]) -> dict:
    """통화 시각 t 시점의 사실 — 생성기와 따로 계산 (§4.1 절차 2: created_at·performed_at·paid_at ≤ t).
    items = _plan_items (열린 치료계획 항목)"""
    day = t[:10]
    visits = [
        {
            "date": v["performed_at"][:10],
            "procedure": PROCEDURE[v["procedure_code"]]["name"],
            "tooth": v["tooth_no"],
        }
        for v in _rows(
            conn,
            "SELECT performed_at, procedure_code, tooth_no FROM emr_treatments"
            " WHERE patient_id = ? AND performed_at <= ? ORDER BY performed_at DESC LIMIT 3",
            pid,
            t,
        )
    ]
    balance = conn.execute(
        "SELECT coalesce(sum(amount_due - amount_paid), 0) FROM emr_payments"
        " WHERE patient_id = ? AND paid_at <= ?",
        (pid, t),
    ).fetchone()[0]
    nxt = conn.execute(
        "SELECT id FROM emr_appointments WHERE patient_id = ? AND created_at <= ? AND scheduled_at > ?"
        " AND (cancelled_at IS NULL OR cancelled_at > ?) ORDER BY scheduled_at LIMIT 1",
        (pid, t, t, t),
    ).fetchone()
    alerts = {
        r[0]
        for r in conn.execute(
            "SELECT t.label FROM emr_patient_alerts a JOIN emr_alert_types t USING (code) WHERE a.patient_id = ?"
            " AND a.recorded_at <= ? AND (a.ends_at IS NULL OR ? < a.ends_at)",
            (pid, day, day),
        )
    }
    return {
        "age": age_on(date.fromisoformat(birth), date.fromisoformat(day)),
        "recent_visits": visits,
        "balance": balance,
        "next": nxt[0] if nxt else None,
        "alerts": alerts,
        "open_plan_items": _open_items_at(items, pid, t),
    }


def _appt_detail(a: dict, appts: dict) -> dict:
    """예약류 통화의 예약 상세(날짜·시각·시술, 변경이면 원래 날짜·시각)"""
    out = {
        "date": a["scheduled_at"][:10],
        "time": a["scheduled_at"][11:16],
        "procedure": PROCEDURE[a["planned_procedure_code"]]["name"],
    }
    if a["rescheduled_from_id"] is not None:
        orig = appts[a["rescheduled_from_id"]]["scheduled_at"]
        out |= {"from_date": orig[:10], "from_time": orig[11:16]}
    return out


def check_call_facts_as_of(conn, ctx):
    """used_fact는 통화 시점 사실: 통화 날짜, 예약류의 예약 상세, 환자(통화 환자와 같음 — 환자가 연결됐거나 보험사면
    필수), 최근 진료 3건·잔액·다음 예약·보이는 주의사항·열린 치료계획 항목이 장부를 통화 시각까지만 읽은 값과 같음"""
    patients = {p["id"]: p for p in _rows(conn, "SELECT id, name, birth_date FROM emr_patients")}
    appts = {
        a["id"]: a
        for a in _rows(
            conn,
            "SELECT id, scheduled_at, planned_procedure_code, rescheduled_from_id"
            " FROM emr_appointments",
        )
    }
    items = _plan_items(conn)
    bad = []
    for r in _calls(conn):
        f, t = r["fact"], r["started_at"]
        if f.get("call_date") != t[:10]:
            bad.append({"call_uid": r["call_uid"], "problem": "call_date ≠ 통화 날짜"})
            continue
        if (a := appts.get(f.get("appointment_id"))) is not None and f.get(
            "appointment"
        ) != _appt_detail(a, appts):
            bad.append({"call_uid": r["call_uid"], "problem": "예약 상세 ≠ 장부"})
            continue
        if "patient" not in f:
            if r["patient_id"] is not None or r["counterpart"] == "insurer":
                bad.append(
                    {"call_uid": r["call_uid"], "problem": "환자가 연결된 통화인데 환자 사실 없음"}
                )
            continue
        pid = f["patient"]["id"]
        if r["patient_id"] is not None and pid != r["patient_id"] or pid not in patients:
            bad.append({"call_uid": r["call_uid"], "problem": "사실의 환자 ≠ 통화 환자"})
            continue
        want = _facts_at(conn, pid, t, patients[pid]["birth_date"], items)
        nxt = f.get("next_appointment")
        got = {
            "age": f["patient"]["age"],
            "recent_visits": f["recent_visits"],
            "balance": f["balance"],
            "next": nxt["appointment_id"] if nxt else None,
            "alerts": set(f["alerts"]),
            "open_plan_items": f.get("open_plan_items"),
        }
        if f["patient"]["name"] != patients[pid]["name"]:
            got["name"] = f["patient"]["name"]
        diff = sorted(k for k in got if got[k] != want.get(k))
        if diff:
            bad.append({"call_uid": r["call_uid"], "problem": f"통화 시점 장부와 다름: {diff}"})
    return bad


def check_eval_pool(conn, ctx):
    """판정 풀: 크기 = eval_pool_size, 순번 1..P, 앞 ⌈P/2⌉ = dev·나머지 test(라벨 split과 일치),
    비풀은 순번 없음·silver(판정 상태는 앱 DB — 앱 설계 §1)"""
    rows = _calls(conn)
    pool = [r for r in rows if r["in_eval_pool"]]
    bad = [] if len(pool) == ctx.eval_pool else [{"pool": len(pool), "expected": ctx.eval_pool}]
    if sorted(r["eval_sort_order"] or 0 for r in pool) != list(range(1, len(pool) + 1)):
        bad.append({"problem": "eval_sort_order가 1..P가 아님"})
    dev_n = (len(pool) + 1) // 2
    for r in rows:
        if r["in_eval_pool"]:
            want = "dev" if (r["eval_sort_order"] or 0) <= dev_n else "test"
        else:
            want = "silver"
        if r["split"] != want or (not r["in_eval_pool"] and r["eval_sort_order"] is not None):
            bad.append(
                {
                    "call_uid": r["call_uid"],
                    "split": r["split"],
                    "expected": want,
                }
            )
    return bad


def check_blind_pool(conn, ctx):
    """블라인드 풀: 판정 풀의 부분집합, 크기 = blind_pool_size, 순번 1..B, 비블라인드는 순번 없음"""
    rows = _rows(
        conn,
        "SELECT id, in_eval_pool, in_blind_pool, blind_sort_order FROM ax_calls WHERE source = 'generated'",
    )
    blind = [r for r in rows if r["in_blind_pool"]]
    bad = [r for r in blind if not r["in_eval_pool"]] + [
        r for r in rows if not r["in_blind_pool"] and r["blind_sort_order"]
    ]
    if len(blind) != ctx.blind_pool:
        bad.append({"blind": len(blind), "expected": ctx.blind_pool})
    if sorted(r["blind_sort_order"] or 0 for r in blind) != list(range(1, len(blind) + 1)):
        bad.append({"problem": "blind_sort_order가 1..B가 아님"})
    return bad


def check_eval_pool_strata(conn, ctx):
    """판정 풀 층화(평가 설계 §2.1, 카테고리 = 주 목적): 희귀 카테고리는 전사의 EVAL_RARE_SHARE(반올림),
    카테고리마다 dev·test 차이 ≤ 1, 전사가 충분한(≥ split당 최소 × 2) 일반 카테고리는 split마다 최소 건수 이상"""
    by = defaultdict(lambda: {"n": 0, "dev": 0, "test": 0})
    for r in _calls(conn):
        c = by[r["primary_purpose"]]
        c["n"] += 1
        if r["split"] in ("dev", "test"):
            c[r["split"]] += 1
    floor = 2 * EVAL_MIN_PER_SPLIT
    bad = []
    for purpose, c in sorted(by.items()):
        if abs(c["dev"] - c["test"]) > 1:
            bad.append({"purpose": purpose, **c, "problem": "dev·test 차이 > 1"})
        if purpose in EVAL_RARE_PURPOSES and c["dev"] + c["test"] != round(
            EVAL_RARE_SHARE * c["n"]
        ):
            bad.append({"purpose": purpose, **c, "problem": "희귀 카테고리 과표집 비율"})
        if (
            purpose not in EVAL_RARE_PURPOSES
            and c["n"] >= floor
            and min(c["dev"], c["test"]) < EVAL_MIN_PER_SPLIT
        ):
            bad.append({"purpose": purpose, **c, "problem": "split당 최소 건수 미달"})
    return bad


# ── 분포 ────────────────────────────────────────────────────


def _share(rows, key) -> dict:
    counts = defaultdict(int)
    for r in rows:
        counts[r[key]] += 1
    return {k: v / len(rows) for k, v in counts.items()} if rows else {}


def check_call_purpose_mix(conn, ctx):
    """통화 목적 분포가 §4.1 목표(CALL_PURPOSE_SHARE) ±CALL_MIX_TOLERANCE"""
    got = _share(_calls(conn), "primary_purpose")
    return [
        {"purpose": k, "share": round(got.get(k, 0), 3), "target": v}
        for k, v in CALL_PURPOSE_SHARE.items()
        if abs(got.get(k, 0) - v) > CALL_MIX_TOLERANCE
    ]


def check_call_noise_mix(conn, ctx):
    """노이즈 분포가 §4.1 목표(CALL_NOISE_SHARE) ±CALL_MIX_TOLERANCE"""
    got = _share(_calls(conn), "noise_type")
    return [
        {"noise": k, "share": round(got.get(k, 0), 3), "target": v}
        for k, v in CALL_NOISE_SHARE.items()
        if abs(got.get(k, 0) - v) > CALL_MIX_TOLERANCE
    ]


def report_call_mix(conn, ctx):
    """상대 분포(§4.1 근사 — 목표 아님)·outbound 비중·복합 주제 비중·카테고리별 dev n(평가 설계 §2.1)"""
    rows = _calls(conn)
    if not rows:
        return {}
    return {
        "counterpart": {
            k: round(v, 3)
            for k, v in sorted(_share(rows, "counterpart").items(), key=lambda kv: -kv[1])
        },
        "outbound": round(sum(r["direction"] == "outbound" for r in rows) / len(rows), 3),
        "multi_topic": round(sum(len(r["accepted"]) > 1 for r in rows) / len(rows), 3),
        "dev_n": dict(
            sorted(Counter(r["primary_purpose"] for r in rows if r["split"] == "dev").items())
        ),
    }
