"""6단계 통화 슬롯 — ax_calls 빈 행(has_transcript=0)·truth_call_labels (가상 데이터 생성 설계 §4.1 '6단계 구현 결정').
세 단계, 각자 시드:
- call_slots: 목적·목적 안 방향 할당량 → 예약류는 이벤트 비복원 추출(통화 시각 = 이벤트 시각), 나머지는 날짜·상대·환자·발신자
- call_labels: 복합 주제·노이즈 할당량·급함·플래그·used_fact → 행 쓰기(id = 통화 시각순, call_uid = 셔플 순번)
- call_pools: 판정 풀 층화·dev/test·순번, 블라인드 풀 (평가 설계 §2.1)
"""
import json
from collections import defaultdict
from datetime import date, datetime, timedelta

from generator.master import (AGE_BANDS, APPT_CONFIRMED_SHARE, BOOKING_PURPOSES, CALL_COUNTERPART_WEIGHTS,
                              CALL_DURATION_SEC, CALL_FLAGS, CALL_NOISE_SHARE, CALL_OUTBOUND_SHARE, CALL_PAIN_PURPOSES,
                              CALL_PATIENT_RULES, CALL_PURPOSE_SHARE, CALL_SUBTOPICS, CALL_URGENCY_WEIGHTS,
                              EVAL_MIN_PER_SPLIT, EVAL_RARE_PURPOSES, EVAL_RARE_SHARE, HAS_TODO_DEFAULT, HAS_TODO_SHARE,
                              HOURS, IMPLIED_FLAG, OUTBOUND_MULTI_SHARE, PAIN_URGENT_SHARE, PROCEDURES, RECALL_CYCLE,
                              SUBTOPIC_PURPOSE, age_on, open_slots)

FMT = "%Y-%m-%d %H:%M:%S"
CHILD_MAX_AGE = next(hi for name, _, hi, _ in AGE_BANDS if name == "child")
PROCEDURE_NAME = {p["code"]: p["name"] for p in PROCEDURES}
PRICED = sorted(p["name"] for p in PROCEDURES if p["fee_total"] > 0)  # 처음 연락의 주제 시술 후보
TOPIC_PURPOSES = ("price_q", "insurance_q", "treatment_q")
OPEN = datetime.strptime(HOURS["open"], "%H:%M").time()
CLOSE = datetime.strptime(HOURS["weekday_close"], "%H:%M").time()
DAY_MINUTES = (datetime.combine(date.min, CLOSE) - datetime.combine(date.min, OPEN)).seconds // 60
MAX_DATE_TRIES = 1000


def quota(shares: dict, n: int) -> dict:
    """비중 × n을 최대 잔여 방식으로 반올림한 건수 (합 = n). 비중 합은 1. 잔여가 같으면 dict 순서"""
    raw = {k: v * n for k, v in shares.items()}
    out = {k: int(x) for k, x in raw.items()}
    rest = sorted(raw, key=lambda k: -(raw[k] - out[k]))
    for k in rest[:n - sum(out.values())]:
        out[k] += 1
    return out


def _weighted(rng, weights: dict) -> str:
    names = list(weights)
    total = sum(weights.values())
    return names[int(rng.choice(len(names), p=[weights[k] / total for k in names]))]


def _is_open(d: date, holidays) -> bool:
    return open_slots(d.weekday()) > 0 and d not in holidays


def _prev_open_day(d: date, holidays) -> date:
    d -= timedelta(days=1)
    while not _is_open(d, holidays):
        d -= timedelta(days=1)
    return d


def _in_hours(t: str) -> bool:
    return OPEN <= datetime.strptime(t, FMT).time() < CLOSE


class Ledger:
    """통화 시점 장부 — 환자별 내원·예약·수납을 메모리에. 시각 비교는 ISO 문자열"""

    def __init__(self, conn):
        self.patients = {r[0]: {"id": r[0], "name": r[1], "birth": date.fromisoformat(r[2]), "sex": r[3], "phone": r[4],
                                "family": r[5], "guardian": r[6], "registered": r[7], "private": r[8]}
                         for r in conn.execute("SELECT id, name, birth_date, sex, phone, family_id, guardian_patient_id,"
                                               " registered_at, has_private_insurance FROM emr_patients ORDER BY id")}
        self.appts = {r[0]: {"id": r[0], "patient": r[1], "at": r[2], "type": r[3], "code": r[4], "status": r[5],
                             "from": r[6], "via": r[7], "confirmed": r[8], "created": r[9], "cancelled": r[10]}
                      for r in conn.execute("SELECT id, patient_id, scheduled_at, type, planned_procedure_code, status,"
                                            " rescheduled_from_id, booked_via, confirmed, created_at, cancelled_at"
                                            " FROM emr_appointments ORDER BY id")}
        self.by_patient = defaultdict(list)
        for a in self.appts.values():
            self.by_patient[a["patient"]].append(a)
        self.treatments = defaultdict(list)
        for r in conn.execute("SELECT patient_id, performed_at, procedure_code, tooth_no FROM emr_treatments"
                              " ORDER BY performed_at, id"):
            self.treatments[r[0]].append({"at": r[1], "procedure": PROCEDURE_NAME[r[2]], "tooth": r[3]})
        self.payments = defaultdict(list)
        for r in conn.execute("SELECT patient_id, paid_at, amount_due - amount_paid FROM emr_payments ORDER BY paid_at, id"):
            self.payments[r[0]].append((r[1], r[2]))
        self.alerts = defaultdict(list)
        for r in conn.execute("SELECT a.patient_id, a.recorded_at, a.ends_at, t.label FROM emr_patient_alerts a"
                              " JOIN emr_alert_types t USING (code) ORDER BY a.recorded_at, a.code"):
            self.alerts[r[0]].append((r[1], r[2], r[3]))
        self.family = defaultdict(list)
        for p in self.patients.values():
            if p["family"] is not None:
                self.family[p["family"]].append(p)

    def completed_before(self, pid: int, day: str) -> list[dict]:
        return [a for a in self.by_patient[pid] if a["status"] == "completed" and a["at"][:10] < day]

    def balance(self, pid: int, t: str) -> int:
        return sum(owed for at, owed in self.payments[pid] if at <= t)

    def next_appointment(self, pid: int, t: str) -> dict | None:
        live = [a for a in self.by_patient[pid]
                if a["created"] <= t < a["at"] and (a["cancelled"] is None or a["cancelled"] > t)]
        return min(live, key=lambda a: a["at"]) if live else None

    def matches(self, pid: int, rule: tuple, t: str) -> bool:
        """CALL_PATIENT_RULES의 환자 조건 (규칙 없으면 visited)"""
        day = t[:10]
        visits = self.completed_before(pid, day)
        if not visits or self.patients[pid]["registered"] > day:
            return False
        kind, value = rule
        d = date.fromisoformat(day)
        if kind == "recall_due":
            hygiene = [a for a in visits if a["type"] == "hygiene"]
            return bool(hygiene) and date.fromisoformat(hygiene[-1]["at"][:10]) + RECALL_CYCLE < d \
                and self.next_appointment(pid, t) is None
        if kind == "balance":
            return self.balance(pid, t) > 0
        if kind == "recent_treatment":
            lo, hi = value
            return any(a["type"] == "treatment" and (d - timedelta(days=hi)).isoformat() <= a["at"][:10]
                       <= (d - timedelta(days=lo)).isoformat() for a in visits)
        if kind == "recent_visit":
            return any(a["at"][:10] >= (d - timedelta(days=value)).isoformat() for a in visits)
        return True

    def facts(self, pid: int, t: str) -> dict:
        """통화 시각 t 시점 사실 (§4.1 절차 2). 청구·status 컬럼은 읽지 않는다"""
        p, day = self.patients[pid], t[:10]
        nxt = self.next_appointment(pid, t)
        visits = [v for v in self.treatments[pid] if v["at"] <= t][-3:][::-1]
        return {
            "patient": {"id": pid, "name": p["name"], "age": age_on(p["birth"], date.fromisoformat(day)), "sex": p["sex"]},
            "recent_visits": [{"date": v["at"][:10], "procedure": v["procedure"], "tooth": v["tooth"]} for v in visits],
            "balance": self.balance(pid, t),
            "next_appointment": {"appointment_id": nxt["id"]} | _appt_fact(nxt) if nxt else None,
            "family": [m["name"] for m in self.family[p["family"]] if m["id"] != pid and m["registered"] <= day]
            if p["family"] is not None else [],
            "alerts": [label for rec, ends, label in self.alerts[pid] if rec <= day and (ends is None or day < ends)],
        }

    def is_child(self, pid: int, day: str) -> bool:
        """보호자가 거는 환자 — 통화일에 소아이고 보호자 기록이 있음(TODAY에 청소년인 환자는 보호자 기록이 없다)"""
        p = self.patients[pid]
        return p["guardian"] is not None and age_on(p["birth"], date.fromisoformat(day)) <= CHILD_MAX_AGE

    def speaker(self, pid: int, day: str) -> dict:
        """실제로 말하는 사람 — 소아면 보호자"""
        p = self.patients[pid]
        return self.patients[p["guardian"]] if self.is_child(pid, day) else p


def _appt_fact(a: dict) -> dict:
    return {"date": a["at"][:10], "time": a["at"][11:16], "procedure": PROCEDURE_NAME[a["code"]]}


def _booking_events(led: Ledger, purpose: str, run, rng) -> list[tuple[dict, str]]:
    """목적별 예약 이벤트 (예약, 통화 시각) — 창 안 예약만, 통화 시각이 영업시간 안인 것만 (§4.1 예약류 역산)"""
    start = run.range_start.isoformat()
    appts = [a for a in led.appts.values() if a["created"] >= start]
    if purpose == "book":
        return [(a, a["created"]) for a in appts if a["via"] == "phone" and a["from"] is None]
    if purpose == "reschedule":
        return [(a, a["created"]) for a in appts if a["from"] is not None]
    if purpose == "cancel":
        origins = {a["from"] for a in led.appts.values() if a["from"] is not None}
        return [(a, a["cancelled"]) for a in appts
                if a["status"] == "cancelled" and a["id"] not in origins and _in_hours(a["cancelled"])]
    # confirm: 리마인드 날 개원 뒤 임의 분 — 8단계가 리마인드 문자를 그 전에 보낸다
    out = []
    for a in appts:
        if a["confirmed"]:
            day = _prev_open_day(date.fromisoformat(a["at"][:10]), run.holidays)
            t = datetime.combine(day, OPEN) + timedelta(minutes=int(rng.integers(1, DAY_MINUTES)))
            out.append((a, t.strftime(FMT)))
    return out


def _side(led: Ledger, pid: int, day: str) -> str:
    return "guardian" if led.is_child(pid, day) else "patient"


def stage_slots(run, rng, fake) -> None:
    led = Ledger(run.conn)
    n = run.cfg["calls_with_transcript"]
    days = [run.range_start + timedelta(days=i) for i in range((run.today - run.range_start).days)]
    days = [d for d in days if _is_open(d, run.holidays)]
    slots = []
    for purpose, k in quota(CALL_PURPOSE_SHARE, n).items():
        out_share = CALL_OUTBOUND_SHARE.get(purpose, 0) / CALL_PURPOSE_SHARE[purpose]
        out = quota({"outbound": out_share, "inbound": 1 - out_share}, k)["outbound"]
        multi = quota({"multi": OUTBOUND_MULTI_SHARE, "single": 1 - OUTBOUND_MULTI_SHARE}, out)["multi"]
        shape = [("outbound", i < multi) for i in range(out)] + [("inbound", False)] * (k - out)
        if purpose in BOOKING_PURPOSES:
            events = _booking_events(led, purpose, run, rng)
            if len(events) < k:
                raise ValueError(f"{purpose} 이벤트 {len(events)}건 < 통화 {k}건")
            for (direction, structural), i in zip(shape, rng.choice(len(events), size=k, replace=False).tolist()):
                a, t = events[i]
                pid = a["patient"]
                new = purpose == "book" and led.patients[pid]["registered"] == t[:10]
                slots.append({"purpose": purpose, "direction": direction, "structural": structural, "t": t,
                              "patient": pid, "about": pid, "appointment": a,
                              "counterpart": "new_caller" if new else _side(led, pid, t[:10])})
            continue
        for direction, structural in shape:
            who = "side" if direction == "outbound" else _weighted(rng, CALL_COUNTERPART_WEIGHTS[purpose])
            rule = CALL_PATIENT_RULES.get((purpose, direction), ("visited", None))
            for _ in range(MAX_DATE_TRIES):
                d = days[int(rng.integers(len(days)))]
                t = (datetime.combine(d, OPEN) + timedelta(minutes=int(rng.integers(DAY_MINUTES)))).strftime(FMT)
                if who not in ("side", "insurer"):
                    about = None
                    break
                cands = [pid for pid in led.patients if led.matches(pid, rule, t)
                         and (who == "side" or led.patients[pid]["private"])]
                if cands:
                    about = cands[int(rng.integers(len(cands)))]
                    break
            else:
                raise ValueError(f"{purpose} {direction} {who}: {MAX_DATE_TRIES}일 동안 조건에 맞는 환자 없음")
            side = who == "side"
            slots.append({"purpose": purpose, "direction": direction, "structural": structural, "t": t,
                          "patient": about if side else None, "about": about, "appointment": None,
                          "counterpart": _side(led, about, t[:10]) if side else who})
    for s in slots:
        if s["patient"] is not None:
            speaker = led.speaker(s["patient"], s["t"][:10])
            s["caller"] = (led.patients[s["patient"]]["phone"], speaker["name"])
        else:
            s["caller"] = (next(run.state["phones"]), fake.name())
    run.state["call_slots"], run.state["call_ledger"] = slots, led


def _noise_eligible(s: dict, noise: str) -> bool:
    if noise == "two_topics":
        return s["direction"] == "inbound" and bool(CALL_SUBTOPICS[s["purpose"]])
    if noise == "ambiguous_pain":
        return s["purpose"] in CALL_PAIN_PURPOSES
    if noise == "guardian_confused":
        return s["counterpart"] == "guardian"
    return True


def _assign_noise(rng, slots: list[dict]) -> None:
    """노이즈 할당량 — 대상이 좁은 노이즈부터 비복원 추출"""
    counts = quota({**CALL_NOISE_SHARE, None: 1 - sum(CALL_NOISE_SHARE.values())}, len(slots))
    for s in slots:
        s["noise"] = None
    for noise in sorted(CALL_NOISE_SHARE, key=lambda k: sum(_noise_eligible(s, k) for s in slots)):
        free = [i for i, s in enumerate(slots) if s["noise"] is None and _noise_eligible(s, noise)]
        if len(free) < counts[noise]:
            raise ValueError(f"노이즈 {noise} 대상 {len(free)}건 < {counts[noise]}건")
        for i in rng.choice(len(free), size=counts[noise], replace=False).tolist():
            slots[free[i]]["noise"] = noise


def _used_fact(rng, led: Ledger, s: dict) -> dict:
    fact = {"call_date": s["t"][:10]}
    a = s["appointment"]
    if a is not None:
        fact["appointment_id"] = a["id"]
        fact["appointment"] = _appt_fact(a)
        if a["from"] is not None:
            orig = led.appts[a["from"]]
            fact["appointment"] |= {"from_date": orig["at"][:10], "from_time": orig["at"][11:16]}
    if s["about"] is not None:
        fact |= led.facts(s["about"], s["t"])
    if s["purpose"] in TOPIC_PURPOSES:
        if s["about"] is not None:
            nxt = fact["next_appointment"]
            known = sorted({v["procedure"] for v in fact["recent_visits"]} | ({nxt["procedure"]} if nxt else set()))
        else:
            known = []
        pool = known or PRICED
        fact["topic_procedure"] = pool[int(rng.integers(len(pool)))]
    return fact


def stage_labels(run, rng, fake) -> None:
    slots, led = run.state["call_slots"], run.state["call_ledger"]
    _assign_noise(rng, slots)
    for s in slots:
        purpose = s["purpose"]
        flags = dict.fromkeys(CALL_FLAGS, 0)
        if purpose in IMPLIED_FLAG:
            flags[IMPLIED_FLAG[purpose]] = 1
        sub = None
        if s["structural"] or s["noise"] == "two_topics":
            options = CALL_SUBTOPICS[purpose]
            sub = options[int(rng.integers(len(options)))]
            flags[sub] = 1
        if purpose in ("book", "reschedule"):
            flags["appt_confirmed"] = int(rng.random() < APPT_CONFIRMED_SHARE)
        flags["has_todo"] = int(rng.random() < HAS_TODO_SHARE.get(purpose, HAS_TODO_DEFAULT))
        if flags["emergency"]:
            urgency = "urgent"
        elif s["noise"] == "ambiguous_pain":
            urgency = "urgent" if rng.random() < PAIN_URGENT_SHARE else "normal"
        else:
            urgency = _weighted(rng, CALL_URGENCY_WEIGHTS)
        s.update(flags=flags, urgency=urgency, accepted=[purpose] + ([SUBTOPIC_PURPOSE[sub]] if sub else []),
                 fact=_used_fact(rng, led, s))
    lo, hi = CALL_DURATION_SEC
    order = sorted(range(len(slots)), key=lambda i: slots[i]["t"])
    width = max(4, len(str(len(slots))))
    uids = (rng.permutation(len(slots)) + 1).tolist()
    calls, labels = [], []
    for row_id, i in enumerate(order, start=1):
        s = slots[i]
        uid = f"c-{uids[row_id - 1]:0{width}d}"
        calls.append((row_id, uid, s["patient"], *s["caller"], s["direction"], s["t"], int(rng.integers(lo, hi + 1))))
        labels.append((uid, s["purpose"], json.dumps(s["accepted"]), s["counterpart"], s["urgency"],
                       *(s["flags"][f] for f in CALL_FLAGS), s["noise"], json.dumps(s["fact"], ensure_ascii=False)))
    run.conn.executemany("INSERT INTO ax_calls (id, call_uid, source, patient_id, caller_phone, caller_name, direction,"
                         " started_at, duration_sec) VALUES (?, ?, 'generated', ?, ?, ?, ?, ?, ?)", calls)
    run.conn.executemany(f"INSERT INTO truth.truth_call_labels VALUES ({', '.join('?' * 13)}, 'silver')", labels)


def _allocate(by: dict, size: int) -> dict:
    """판정 풀 카테고리별 건수 (평가 설계 §2.1): 희귀는 전사의 EVAL_RARE_SHARE, 나머지는 비례(최대 잔여) 후
    split당 EVAL_MIN_PER_SPLIT 미만이면 그만큼 올리고 가장 큰 카테고리에서 뺀다"""
    n = {c: round(EVAL_RARE_SHARE * len(by[c])) for c in by if c in EVAL_RARE_PURPOSES}
    common = [c for c in by if c not in EVAL_RARE_PURPOSES]
    rest = size - sum(n.values())
    total = sum(len(by[c]) for c in common)
    n |= quota({c: len(by[c]) / total for c in common}, rest)
    floor = 2 * EVAL_MIN_PER_SPLIT
    for c in common:
        while n[c] < min(floor, len(by[c])):
            donor = max((d for d in common if n[d] > floor), key=lambda d: n[d])
            n[donor] -= 1
            n[c] += 1
    return n


def stage_pools(run, rng, fake) -> None:
    conn = run.conn
    by = defaultdict(list)
    for uid, purpose in conn.execute("SELECT c.call_uid, l.primary_purpose FROM ax_calls c"
                                     " JOIN truth.truth_call_labels l USING (call_uid) ORDER BY c.id"):
        by[purpose].append(uid)
    by = {c: by[c] for c in CALL_PURPOSE_SHARE if by[c]}
    size = run.cfg["eval_pool_size"]
    dev, test, odd = [], [], 0
    for c, k in _allocate(by, size).items():
        picked = [by[c][i] for i in rng.choice(len(by[c]), size=k, replace=False).tolist()]
        half = k // 2 + (k % 2 if odd % 2 == 0 else 0)  # 홀수 잔여는 카테고리를 돌며 dev/test 번갈아
        odd += k % 2
        dev += picked[:half]
        test += picked[half:]
    pool = [dev[i] for i in rng.permutation(len(dev)).tolist()] + [test[i] for i in rng.permutation(len(test)).tolist()]
    split = {uid: "dev" for uid in dev} | {uid: "test" for uid in test}
    conn.executemany("UPDATE ax_calls SET in_eval_pool = 1, eval_sort_order = ?, judge_status = 'pending' WHERE call_uid = ?",
                     [(i, uid) for i, uid in enumerate(pool, start=1)])
    conn.executemany("UPDATE truth.truth_call_labels SET split = ? WHERE call_uid = ?",
                     [(s, uid) for uid, s in split.items()])
    blind = run.cfg["blind_pool_size"]
    conn.executemany("UPDATE ax_calls SET in_blind_pool = 1, blind_sort_order = ? WHERE call_uid = ?",
                     [(i, pool[j]) for i, j in enumerate(rng.choice(len(pool), size=blind, replace=False).tolist(), start=1)])
