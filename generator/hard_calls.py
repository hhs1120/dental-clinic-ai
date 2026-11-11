"""어려운 통화 평가셋 hc- 슬롯 (어려운 통화 설계 docs/superpowers/specs/2026-11-11-hard-calls-design.md §3).
생성기 맨 끝 단계라 앞 단계 결과·캐시를 바꾸지 않는다. 기존 데이터는 읽기만 한다 — 예약류는 녹음 없는 기존 예약 이벤트에,
응급 통화는 그 환자의 통화 뒤 그날·다음 진료일 기존 예약에 붙여 전자차트를 고치지 않는다(§3.3).
행: ax_calls에 hc- 빈 행(녹취는 다음 단계), truth_call_labels에 생성 라벨(split hard), truth_hard_calls에 종류·절반"""

import json
import re
from datetime import date, timedelta

from generator.calls import (
    FMT,
    MAX_DATE_TRIES,
    Ledger,
    _appt_fact,
    _booking_events,
    _side,
    _used_fact,
    _weighted,
    any_minute,
    is_open,
    next_open_day,
    quota,
    recall_targets,
)
from generator.master import (
    BOOKING_PURPOSES,
    CALL_COUNTERPART_WEIGHTS,
    CALL_DURATION_SEC,
    CALL_FLAGS,
    CALL_PATIENT_RULES,
    CALL_PURPOSE_SHARE,
    CALL_URGENCY_WEIGHTS,
    EVAL_RARE_PURPOSES,
    HARD_CALL_TYPES,
    HARD_CALLS_PER_TYPE,
    HARD_HALF_PURPOSES,
    HARD_NOT_URGENT_CUES,
    HARD_RARE_MIN,
    HARD_TYPE_PURPOSES,
    HARD_URGENCY_HALVES,
    HARD_URGENT_SIGNS,
    HARD_WINDOW_DAYS,
    HAS_TODO_DEFAULT,
    HAS_TODO_SHARE,
    IMPLIED_FLAG,
    MESSAGE_REPLY_CANCEL,
)
from generator.paths import ROOT

PREFIX = "hc-"
RAG_QA = ROOT / "ground_truth" / "rag_qa.json"
CHART_NO = re.compile(r"C-\d{5}")
CALL_ID = re.compile(r"call:(c-\d+)")


def excluded_patients(conn) -> set[int]:
    """새 통화 대상에서 뺄 환자 (§3.3) — "그 뒤 통화 없음"을 전제로 만든 장면의 환자와 RAG 평가셋에 나오는 환자.
    - 정산 장부 실수(truth_ledger_errors — 8b 앞 단계가 "그날 뒤 통화 없음"으로 고름)
    - 취소 답장 문자(8a 노쇼 → 취소·오늘 취소 장면은 "그 뒤 통화 없음", 개원 전 취소 답장 전부를 넉넉히 뺀다)
    - ground_truth/rag_qa.json 질문·정답에 나오는 차트 번호·통화·이름(동명이인은 함께 뺀다)"""
    out = {
        r[0]
        for r in conn.execute(
            "SELECT p.patient_id FROM truth.truth_ledger_errors e JOIN emr_payments p ON p.id = e.payment_id"
        )
    }
    out |= {
        r[0]
        for r in conn.execute(
            "SELECT patient_id FROM emr_messages WHERE direction = 'in' AND \"trigger\" = 'reply'"
            " AND body = ?",
            (MESSAGE_REPLY_CANCEL,),
        )
    }
    if RAG_QA.exists():
        text = RAG_QA.read_text(encoding="utf-8")
        charts = set(CHART_NO.findall(text))
        calls = set(CALL_ID.findall(text))
        for pid, chart, name in conn.execute("SELECT id, chart_no, name FROM emr_patients"):
            if chart in charts or name in text:
                out.add(pid)
        out |= {
            pid
            for uid, pid in conn.execute("SELECT call_uid, patient_id FROM ax_calls")
            if uid in calls and pid is not None
        }
    return out


def allocation() -> list[tuple[str, str | None, str, str]]:
    """(종류, 절반, 목적, 방향) 할당 — 종류 안은 CALL_PURPOSE_SHARE 비율(최대 잔여), 희귀 목적은 HARD_RARE_MIN 이상
    (모자라면 가장 많은 비희귀 목적에서 뺀다). urgency_inverted는 절반마다 HARD_HALF_PURPOSES 안에서 같은 방식"""
    out = []
    for t in HARD_CALL_TYPES:
        if t == "urgency_inverted":
            groups = [
                (h, [(p, "inbound") for p in HARD_HALF_PURPOSES[h]]) for h in HARD_URGENCY_HALVES
            ]
            n = HARD_CALLS_PER_TYPE // len(HARD_URGENCY_HALVES)
        else:
            groups, n = [(None, list(HARD_TYPE_PURPOSES[t]))], HARD_CALLS_PER_TYPE
        for half, pairs in groups:
            total = sum(CALL_PURPOSE_SHARE[p] for p, _ in pairs)
            counts = quota({k: CALL_PURPOSE_SHARE[k[0]] / total for k in pairs}, n)
            if len(pairs) > 1:
                for k in pairs:
                    while k[0] in EVAL_RARE_PURPOSES and counts[k] < HARD_RARE_MIN:
                        big = max(
                            (x for x in pairs if x[0] not in EVAL_RARE_PURPOSES),
                            key=lambda x: counts[x],
                        )
                        counts[big] -= 1
                        counts[k] += 1
            out += [(t, half, p, d) for (p, d), c in counts.items() for _ in range(c)]
    return out


def _appointments_soon(led: Ledger, t: str, holidays) -> dict[int, dict]:
    """통화 시각 t 뒤 그날 또는 다음 진료일의 기존 예약(취소 아님, t 전에 잡힘) — 환자 → 가장 이른 예약 (§3.3 응급)"""
    last = next_open_day(date.fromisoformat(t[:10]), holidays).isoformat()
    out = {}
    for a in sorted(led.appts.values(), key=lambda a: a["at"]):
        if (
            a["status"] != "cancelled"
            and t < a["at"]
            and a["at"][:10] <= last
            and a["created"] < t
        ):
            out.setdefault(a["patient"], a)
    return out


class _Picker:
    def __init__(self, run, rng, led: Ledger, excluded: set[int]):
        self.run, self.rng, self.led, self.excluded = run, rng, led, excluded
        lo = run.today - timedelta(days=HARD_WINDOW_DAYS)
        self.lo = lo.isoformat()
        self.days = [
            lo + timedelta(days=i)
            for i in range(HARD_WINDOW_DAYS)
            if is_open(lo + timedelta(days=i), run.holidays)
        ]
        self.used = {
            (r[0], r[1])
            for r in run.conn.execute(
                "SELECT primary_purpose, json_extract(used_fact, '$.appointment_id')"
                " FROM truth.truth_call_labels WHERE json_extract(used_fact, '$.appointment_id') IS NOT NULL"
            )
        }
        self.events = {}

    def ok_patient(self, pid: int, t: str) -> bool:
        return pid not in self.excluded and not self.led.gone(pid, t)

    def free_events(self, purpose: str) -> list[tuple[dict, str]]:
        """녹음 없는(c-가 안 붙은) 창 안 예약 이벤트 — 시각순 (spec §3.3 표)"""
        if purpose not in self.events:
            self.events[purpose] = sorted(
                (
                    (a, t)
                    for a, t in _booking_events(self.led, purpose, self.run, self.rng)
                    if self.lo <= t < self.run.today.isoformat()
                    and (purpose, a["id"]) not in self.used
                    and self.ok_patient(a["patient"], t)
                ),
                key=lambda x: (x[1], x[0]["id"]),
            )
        return [e for e in self.events[purpose] if (purpose, e[0]["id"]) not in self.used]

    def take_event(self, purpose: str) -> tuple[dict, str] | None:
        free = self.free_events(purpose)
        if not free:
            return None
        a, t = free[int(self.rng.integers(len(free)))]
        self.used.add((purpose, a["id"]))
        return a, t

    def free_call(self, purpose: str, direction: str, emergency: bool) -> dict:
        """예약 이벤트가 없는 통화 — 날짜·상대·환자(기존 6단계 규칙 + 제외 환자, 응급은 곧 있을 기존 예약)"""
        led, rng = self.led, self.rng
        who = (
            "side"
            if direction == "outbound" or emergency
            else _weighted(rng, CALL_COUNTERPART_WEIGHTS.get(purpose, {"side": 100}))
        )
        # 리콜 안내 발신은 리콜 문의와 같은 조건 — 문자를 받았는데 예약이 없는 환자에게 건다
        rule = CALL_PATIENT_RULES.get(
            (purpose, "inbound" if purpose == "recall_q" else direction), ("visited", None)
        )
        for _ in range(MAX_DATE_TRIES):
            d = self.days[int(rng.integers(len(self.days)))]
            t = any_minute(rng, d).strftime(FMT)
            if who not in ("side", "insurer"):
                return {"t": t, "patient": None, "about": None, "counterpart": who}
            soon = _appointments_soon(led, t, self.run.holidays) if emergency else None
            cands = [
                pid
                for pid in led.patients
                if self.ok_patient(pid, t)
                and led.matches(pid, rule, t)
                and (who == "side" or led.patients[pid]["private"])
                and (soon is None or pid in soon)
            ]
            if cands:
                pid = cands[int(rng.integers(len(cands)))]
                side = who == "side"
                return {
                    "t": t,
                    "patient": pid if side else None,
                    "about": pid,
                    "counterpart": _side(led, pid, t[:10]) if side else who,
                    "visit": soon[pid] if soon else None,
                }
        raise ValueError(
            f"hc {purpose} {direction}: {MAX_DATE_TRIES}번 동안 조건에 맞는 환자 없음"
        )


def _event_slot(led: Ledger, purpose: str, a: dict, t: str) -> dict:
    pid = a["patient"]
    new = purpose == "book" and led.patients[pid]["registered"] == t[:10]
    return {
        "t": t,
        "patient": pid,
        "about": pid,
        "appointment": a,
        "counterpart": "new_caller"
        if new and not led.is_child(pid, t[:10])
        else _side(led, pid, t[:10]),
    }


def stage_slots(run, rng, fake) -> None:
    conn = run.conn
    emergency_visits = frozenset(
        r[0]
        for r in conn.execute(
            "SELECT json_extract(used_fact, '$.emergency_visit.appointment_id') FROM truth.truth_call_labels"
            " WHERE json_extract(used_fact, '$.emergency_visit') IS NOT NULL"
        )
    )
    led = Ledger(
        conn,
        run.state["plan_items"],
        run.state["churn"],
        run.state["dormant"],
        recall_targets(run),
        emergency_visits,
    )
    pick = _Picker(run, rng, led, excluded_patients(conn))
    slots = []
    for hard_type, half, purpose, direction in allocation():
        s = {"hard_type": hard_type, "half": half, "direction": direction, "appointment": None}
        postponed = hard_type == "reversal" and purpose == "book"
        event = pick.take_event(purpose) if purpose in BOOKING_PURPOSES and not postponed else None
        if purpose in BOOKING_PURPOSES and not postponed and event is None:
            # 붙일 이벤트가 모자라면(변경은 창 안 빈 이벤트가 적다) 같은 종류·방향의 다른 예약류(취소 → 예약)로,
            # 그래도 없으면 이벤트 없는 목적으로 (§3.3) — reversal은 "예약하려다 미룸"
            for alt in _alternates(hard_type, half, purpose, direction):
                if (event := pick.take_event(alt)) is not None:
                    purpose = alt
                    break
            else:
                purpose, postponed = _fallback(hard_type, half)
        s["purpose"] = purpose
        if event is not None:
            s |= _event_slot(led, purpose, *event)
        else:
            s |= {"appointment": None, "visit": None} | pick.free_call(
                purpose, direction, emergency=purpose == "emergency"
            )
        s["postponed"] = postponed
        slots.append(s)
    _labels(rng, led, slots)
    _write(run, rng, fake, slots)
    run.state["hard_slots"] = slots


def _alternates(hard_type: str, half: str | None, purpose: str, direction: str) -> list[str]:
    """이벤트가 모자란 예약류 대신 붙일 예약류 — 빈 이벤트가 많은 순(취소 → 예약), 종류의 허용 목록·같은 방향 안.
    reversal의 예약은 이벤트에 붙으면 "다른 날 하려다 결국 그날로 잡음"(예약 확정)"""
    allowed = (
        {(p, "inbound") for p in HARD_HALF_PURPOSES[half]}
        if half
        else set(HARD_TYPE_PURPOSES[hard_type])
    )
    return [p for p in ("cancel", "book") if p != purpose and (p, direction) in allowed]


def _fallback(hard_type: str, half: str | None) -> tuple[str, bool]:
    if hard_type == "reversal":
        return "book", True
    pairs = (
        [(p, "inbound") for p in HARD_HALF_PURPOSES[half]]
        if half
        else HARD_TYPE_PURPOSES[hard_type]
    )
    free = [p for p, _ in pairs if p not in BOOKING_PURPOSES and p not in EVAL_RARE_PURPOSES]
    return max(free, key=lambda p: CALL_PURPOSE_SHARE[p]), False


def _labels(rng, led: Ledger, slots: list[dict]) -> None:
    """생성 라벨 (§3.4) — 플래그는 목적 함의 + 예약 확정(이벤트에 붙은 book·reschedule) + 할 일 추첨, 급함은 응급이면
    urgent·아니면 CALL_URGENCY_WEIGHTS, 급함 뒤집기 절반은 정의대로. used_fact는 6단계 모양 + 기존 예약·신호 문구"""
    for s in slots:
        purpose = s["purpose"]
        flags = dict.fromkeys(CALL_FLAGS, 0)
        if purpose in IMPLIED_FLAG:
            flags[IMPLIED_FLAG[purpose]] = 1
        if purpose in ("book", "reschedule") and s["appointment"] is not None:
            flags["appt_confirmed"] = 1  # 통화 시각 = 이벤트 시각(예약을 그 통화에서 잡음)
        flags["has_todo"] = int(rng.random() < HAS_TODO_SHARE.get(purpose, HAS_TODO_DEFAULT))
        urgency = "urgent" if flags["emergency"] else _weighted(rng, CALL_URGENCY_WEIGHTS)
        if s["half"] == "pain_not_urgent":
            urgency = "normal"
        accepted = [purpose] + (["treatment_q"] if s["half"] == "calm_urgent" else [])
        s |= {"flags": flags, "urgency": urgency, "accepted": accepted}
        fact = _used_fact(rng, led, s)
        if s.get("visit"):
            fact["existing_visit"] = {"appointment_id": s["visit"]["id"]} | _appt_fact(s["visit"])
        if s["half"] == "calm_urgent":
            fact["hard_sign"] = HARD_URGENT_SIGNS[int(rng.integers(len(HARD_URGENT_SIGNS)))]
        elif s["half"] == "pain_not_urgent":
            fact["hard_cue"] = HARD_NOT_URGENT_CUES[int(rng.integers(len(HARD_NOT_URGENT_CUES)))]
        s["fact"] = fact


def _write(run, rng, fake, slots: list[dict]) -> None:
    """id는 기존 행 뒤에 통화 시각순, call_uid는 셔플 순번(종류·목적 순서가 번호에 드러나지 않게)"""
    conn = run.conn
    led_patients = {
        r[0]: (r[1], r[2]) for r in conn.execute("SELECT id, phone, name FROM emr_patients")
    }
    lo, hi = CALL_DURATION_SEC
    uids = (rng.permutation(len(slots)) + 1).tolist()
    durations = rng.integers(lo, hi + 1, size=len(slots)).tolist()
    base = conn.execute("SELECT coalesce(max(id), 0) FROM ax_calls").fetchone()[0]
    guardians = dict(conn.execute("SELECT id, guardian_patient_id FROM emr_patients"))
    calls, labels, hard = [], [], []
    for k, i in enumerate(sorted(range(len(slots)), key=lambda i: slots[i]["t"])):
        s = slots[i]
        s["uid"] = f"{PREFIX}{uids[k]:04d}"
        if s["patient"] is not None:
            speaker = guardians[s["patient"]] if s["counterpart"] == "guardian" else s["patient"]
            phone, name = led_patients[s["patient"]][0], led_patients[speaker][1]
        else:
            phone, name = next(run.state["phones"]), fake.name()
        calls.append(
            (
                base + k + 1,
                s["uid"],
                s["patient"],
                phone,
                name,
                s["direction"],
                s["t"],
                durations[k],
            )
        )
        labels.append(
            (
                s["uid"],
                s["purpose"],
                json.dumps(s["accepted"]),
                s["counterpart"],
                s["urgency"],
                *(s["flags"][f] for f in CALL_FLAGS),
                None,
                json.dumps(s["fact"], ensure_ascii=False),
            )
        )
        hard.append((s["uid"], s["hard_type"], s["half"]))
    conn.executemany(
        "INSERT INTO ax_calls (id, call_uid, source, patient_id, caller_phone, caller_name, direction,"
        " started_at, duration_sec) VALUES (?, ?, 'generated', ?, ?, ?, ?, ?, ?)",
        calls,
    )
    conn.executemany(
        f"INSERT INTO truth.truth_call_labels VALUES ({', '.join('?' * 13)}, 'hard')", labels
    )
    conn.executemany("INSERT INTO truth.truth_hard_calls VALUES (?, ?, ?, 'generated')", hard)
