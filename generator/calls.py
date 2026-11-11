"""6단계 통화 슬롯 — ax_calls 빈 행(has_transcript=0)·truth_call_labels (가상 데이터 생성 설계 §4.1 '6단계 구현 결정').
세 단계, 각자 시드:
- call_slots: 목적·목적 안 방향 할당량 → 예약류는 이벤트 비복원 추출(통화 시각 = 이벤트 시각), 나머지는 날짜·상대·환자·발신자.
  환자 측이 아닌 상대의 번호는 run.state["phones"](4단계 신환 후보가 쓰고 남은 번호)를 이어 쓴다 — 뒤 단계가 같은
  이터레이터를 쓰면 실행 순서에 묶인다
- call_labels: 복합 주제·노이즈 할당량·급함·플래그·used_fact → 행 쓰기(id = 통화 시각순, call_uid = 셔플 순번)
- call_pools: 판정 풀 층화·dev/test·순번, 블라인드 풀 (평가 설계 §2.1)
"""

import json
import zlib
from collections import defaultdict
from datetime import date, datetime, timedelta

from generator.master import (
    AGE_BANDS,
    APPT_CONFIRMED_SHARE,
    BOOKING_PURPOSES,
    CALL_COUNTERPART_WEIGHTS,
    CALL_DURATION_SEC,
    CALL_FLAGS,
    CALL_NOISE_SHARE,
    CALL_OUTBOUND_SHARE,
    CALL_PAIN_PURPOSES,
    CALL_PATIENT_RULES,
    CALL_PURPOSE_SHARE,
    CALL_SUBTOPICS,
    CALL_URGENCY_WEIGHTS,
    CHECKUP_CODE,
    EMERGENCY_VISIT_MARK,
    EVAL_MIN_PER_SPLIT,
    EVAL_RARE_PURPOSES,
    EVAL_RARE_SHARE,
    HAS_TODO_DEFAULT,
    HAS_TODO_SHARE,
    HOURS,
    IMPLIED_FLAG,
    LEDGER_DEBT_CALL_DAYS,
    LEDGER_DEBT_CALLS,
    OUTBOUND_MULTI_SHARE,
    PAIN_URGENT_SHARE,
    PLANNED_WEIGHTS,
    PROCEDURES,
    RECALL_CYCLE,
    SCALING_CODE,
    SCALING_MIN_AGE,
    SUBTOPIC_PURPOSE,
    age_band,
    age_on,
    business_minutes,
    call_day_weight,
    close_time,
    open_slots,
    recall_due_year,
)
from generator.records import open_plan_items

FMT = "%Y-%m-%d %H:%M:%S"
CHILD_MAX_AGE = next(hi for name, _, hi, _ in AGE_BANDS if name == "child")
PROCEDURE_NAME = {p["code"]: p["name"] for p in PROCEDURES}
PRICED = sorted(p["name"] for p in PROCEDURES if p["fee_total"] > 0)  # 처음 연락의 주제 시술 후보
TOPIC_PURPOSES = ("price_q", "insurance_q", "treatment_q")
OPEN = datetime.strptime(HOURS["open"], "%H:%M").time()
CLOSE = datetime.strptime(HOURS["weekday_close"], "%H:%M").time()


def any_minute(rng, d: date) -> datetime:
    """그날 개원 ~ 그 요일 마감 사이 분 단위 시각 균등 — 통화·전화 예약 같은 사건 시각 (§2.6, 점심은 따지지 않음)"""
    return datetime.combine(d, OPEN) + timedelta(
        minutes=int(rng.integers(business_minutes(d.weekday())))
    )


MAX_DATE_TRIES = 1000


def quota(shares: dict, n: int) -> dict:
    """비중 × n을 최대 잔여 방식으로 반올림한 건수 (합 = n). 비중 합은 1. 잔여가 같으면 dict 순서"""
    raw = {k: v * n for k, v in shares.items()}
    out = {k: int(x) for k, x in raw.items()}
    rest = sorted(raw, key=lambda k: -(raw[k] - out[k]))
    for k in rest[: n - sum(out.values())]:
        out[k] += 1
    return out


def _weighted(rng, weights: dict) -> str:
    names = list(weights)
    total = sum(weights.values())
    return names[int(rng.choice(len(names), p=[weights[k] / total for k in names]))]


def is_open(d: date, holidays) -> bool:
    return open_slots(d.weekday()) > 0 and d not in holidays


def prev_open_day(d: date, holidays) -> date:
    d -= timedelta(days=1)
    while not is_open(d, holidays):
        d -= timedelta(days=1)
    return d


def next_open_day(d: date, holidays) -> date:
    d += timedelta(days=1)
    while not is_open(d, holidays):
        d += timedelta(days=1)
    return d


def _in_hours(t: str) -> bool:
    at = datetime.strptime(t, FMT)
    return OPEN <= at.time() < datetime.strptime(close_time(at.weekday()), "%H:%M").time()


def first_open_day(year: int, month: int, holidays) -> date:
    """그달 첫 진료일 — 리콜 문자 날(§2.5)"""
    d = date(year, month, 1)
    while not open_slots(d.weekday()) or d in holidays:
        d += timedelta(days=1)
    return d


def recall_targets(run) -> list[tuple[datetime, int, str, date]]:
    """리콜 문자 대상 (그달 첫 진료일 개원 시각, 환자, before·after, 기한) — 8단계가 보내고 6단계 리콜 문의가 따른다(§2.5·§4.1).
    그 시각에 잡힌 예약이 없고 기한(마지막 completed 위생 내원 +
    RECALL_CYCLE)이 다음 달이면 before, 지난달이면 after"""
    hygiene, appts = defaultdict(list), defaultdict(list)
    for pid, at, typ, status, created, cancelled in run.conn.execute(
        "SELECT patient_id, scheduled_at, type, status, created_at, cancelled_at FROM emr_appointments"
    ):
        appts[pid].append((created, at, cancelled))
        if typ == "hygiene" and status == "completed":
            hygiene[pid].append(at)
    out = []
    y, m = run.range_start.year, run.range_start.month
    while date(y, m, 1) < run.today:
        d = first_open_day(y, m, run.holidays)
        t = datetime.combine(d, OPEN)
        ts = t.strftime(FMT)
        nxt = (y + 1, 1) if m == 12 else (y, m + 1)
        prv = (y - 1, 12) if m == 1 else (y, m - 1)
        if run.range_start <= d < run.today:
            for pid in sorted(hygiene):
                last = max((a for a in hygiene[pid] if a < ts), default=None)
                if last is None or any(
                    c <= ts < a and (x is None or x > ts) for c, a, x in appts[pid]
                ):
                    continue
                due = datetime.strptime(last, FMT).date() + RECALL_CYCLE
                kind = {nxt: "before", prv: "after"}.get((due.year, due.month))
                if kind:
                    out.append((t, pid, kind, due))
        y, m = nxt
    return out


class Ledger:
    """통화 시점 장부 — 환자별 내원·예약·수납을 메모리에. 시각 비교는 ISO 문자열"""

    def __init__(
        self,
        conn,
        plan_items: list,
        churn: dict,
        dormant: dict,
        recall_texts: list,
        emergency: frozenset = frozenset(),
    ):
        self.emergency = emergency  # 6b단계 응급 내원 예약 — 최근 진료에 응급 표시(뒤 통화가 일반 상담으로 이어 쓰지 않게)
        self.plan_items = plan_items  # 5단계 state — 항목마다 만든·닫힌 시각
        self.churn = churn  # 4단계 state — 이탈 환자 → 이탈일
        self.dormant = dormant  # 4단계 state — 휴면 환자 → 휴면일
        # 환자 → [(리콜 문자 시각, before·after, 기한)] — 8단계가 보낼 것(recall_targets)
        self.recall_texts = defaultdict(list)
        for t, pid, kind, due in recall_texts:
            self.recall_texts[pid].append((t.strftime(FMT), kind, due))
        self.ledger_debt = defaultdict(
            list
        )  # 환자 → [(장부 입력 시각, 실제 내원일, 실수로 생긴 미수)] (§2.4)
        for pid, at, day, debt in conn.execute(
            "SELECT p.patient_id, p.paid_at, date(e.true_paid_at), e.true_amount - p.amount_paid"
            " FROM truth.truth_ledger_errors e JOIN emr_payments p ON p.id = e.payment_id"
            " WHERE e.kind IN ('entry_missing', 'amount_typo') AND p.kind = 'visit'"
            " ORDER BY p.paid_at, p.id"
        ):
            self.ledger_debt[pid].append((at, day, debt))
        self.patients = {
            r[0]: {
                "id": r[0],
                "name": r[1],
                "birth": date.fromisoformat(r[2]),
                "sex": r[3],
                "phone": r[4],
                "family": r[5],
                "guardian": r[6],
                "registered": r[7],
                "private": r[8],
            }
            for r in conn.execute(
                "SELECT id, name, birth_date, sex, phone, family_id, guardian_patient_id,"
                " registered_at, has_private_insurance FROM emr_patients ORDER BY id"
            )
        }
        self.appts = {
            r[0]: {
                "id": r[0],
                "patient": r[1],
                "at": r[2],
                "type": r[3],
                "code": r[4],
                "status": r[5],
                "from": r[6],
                "via": r[7],
                "confirmed": r[8],
                "created": r[9],
                "cancelled": r[10],
            }
            for r in conn.execute(
                "SELECT id, patient_id, scheduled_at, type, planned_procedure_code, status,"
                " rescheduled_from_id, booked_via, confirmed, created_at, cancelled_at"
                " FROM emr_appointments ORDER BY id"
            )
        }
        self.by_patient = defaultdict(list)
        for a in self.appts.values():
            self.by_patient[a["patient"]].append(a)
        self.treatments = defaultdict(list)
        for r in conn.execute(
            "SELECT patient_id, performed_at, procedure_code, tooth_no, appointment_id FROM emr_treatments"
            " ORDER BY performed_at, id"
        ):
            self.treatments[r[0]].append(
                {"at": r[1], "procedure": PROCEDURE_NAME[r[2]], "tooth": r[3]}
                | ({"emergency": EMERGENCY_VISIT_MARK} if r[4] in emergency else {})
            )
        self.payments = defaultdict(list)
        for r in conn.execute(
            "SELECT patient_id, paid_at, amount_due - amount_paid FROM emr_payments ORDER BY paid_at, id"
        ):
            self.payments[r[0]].append((r[1], r[2]))
        self.alerts = defaultdict(list)
        for r in conn.execute(
            "SELECT a.patient_id, a.recorded_at, a.ends_at, t.label FROM emr_patient_alerts a"
            " JOIN emr_alert_types t USING (code) ORDER BY a.recorded_at, a.code"
        ):
            self.alerts[r[0]].append((r[1], r[2], r[3]))
        self.family = defaultdict(list)
        for p in self.patients.values():
            if p["family"] is not None:
                self.family[p["family"]].append(p)

    def gone(self, pid: int, t: str) -> bool:
        """시각 t에 이미 이탈한 환자 (§2.2)"""
        on = self.churn.get(pid)
        return on is not None and on.isoformat() <= t[:10]

    def asleep(self, pid: int, t: str) -> bool:
        """시각 t에 이미 휴면인 환자 (§2.6 (1)) — 새 수요가 없어 리콜 문자를 보고 걸지 않는다"""
        on = self.dormant.get(pid)
        return on is not None and on.isoformat() <= t[:10]

    def recent_treatment(self, pid: int, t: str) -> str | None:
        """경과 확인 규칙 창(CALL_PATIENT_RULES treatment_q outbound) 안의 가장 최근 completed 진료 내원 시술"""
        lo, hi = CALL_PATIENT_RULES[("treatment_q", "outbound")][1]
        d = date.fromisoformat(t[:10])
        recent = [
            a
            for a in self.by_patient[pid]
            if a["type"] == "treatment"
            and a["status"] == "completed"
            and (d - timedelta(days=hi)).isoformat()
            <= a["at"][:10]
            <= (d - timedelta(days=lo)).isoformat()
        ]
        return (
            PROCEDURE_NAME[max(recent, key=lambda a: (a["at"], a["id"]))["code"]]
            if recent
            else None
        )

    def recall_text(self, pid: int, t: str) -> dict | None:
        """시각 t 전 규칙 창(CALL_PATIENT_RULES recall_q inbound) 안에 받은 가장 최근 리콜 문자 — 리콜 문의 사실"""
        lo, hi = CALL_PATIENT_RULES[("recall_q", "inbound")][1]
        d = date.fromisoformat(t[:10])
        got = [
            (at, kind, due)
            for at, kind, due in self.recall_texts[pid]
            if at <= t and lo <= (d - date.fromisoformat(at[:10])).days <= hi
        ]
        if not got:
            return None
        at, kind, due = max(got)
        return {"sent_at": at, "kind": kind, "due_month": due.month}

    def scaled_in(self, pid: int, year: int, t: str) -> bool:
        """시각 t 전에 그 해 급여 스케일링(그 진료일 SCALING_MIN_AGE 이상)을 받음 — 4단계가 그해 두 번째부터 구강검진으로
        바꾸므로 성인 스케일링 진료 = 급여 사용"""
        name, birth = PROCEDURE_NAME[SCALING_CODE], self.patients[pid]["birth"]
        return any(
            x["procedure"] == name
            and x["at"][:4] == str(year)
            and x["at"] <= t
            and age_on(birth, date.fromisoformat(x["at"][:10])) >= SCALING_MIN_AGE
            for x in self.treatments[pid]
        )

    def completed_before(self, pid: int, day: str) -> list[dict]:
        return [
            a for a in self.by_patient[pid] if a["status"] == "completed" and a["at"][:10] < day
        ]

    def balance(self, pid: int, t: str) -> int:
        return sum(owed for at, owed in self.payments[pid] if at <= t)

    def next_appointment(self, pid: int, t: str) -> dict | None:
        """시각 t 직전에 잡혀 있던 가장 가까운 예약 — t에 생기는 예약은 아직 없고, t에 취소·변경되는 예약은 아직 있다"""
        live = [
            a
            for a in self.by_patient[pid]
            if a["created"] < t < a["at"] and (a["cancelled"] is None or a["cancelled"] >= t)
        ]
        return min(live, key=lambda a: a["at"]) if live else None

    def matches(self, pid: int, rule: tuple, t: str) -> bool:
        """CALL_PATIENT_RULES의 환자 조건 (규칙 없으면 visited)"""
        day = t[:10]
        visits = self.completed_before(pid, day)
        if not visits or self.patients[pid]["registered"] > day:
            return False
        kind, value = rule
        d = date.fromisoformat(day)
        if kind == "recall_text":
            return (
                not self.asleep(pid, t)
                and self.recall_text(pid, t) is not None
                and self.next_appointment(pid, t) is None
            )
        if kind == "balance":
            return self.balance(pid, t) > 0
        if kind == "recent_treatment":
            lo, hi = value
            return any(
                a["type"] == "treatment"
                and (d - timedelta(days=hi)).isoformat()
                <= a["at"][:10]
                <= (d - timedelta(days=lo)).isoformat()
                for a in visits
            )
        if kind == "recent_visit":
            return any(a["at"][:10] >= (d - timedelta(days=value)).isoformat() for a in visits)
        return True

    def facts(self, pid: int, t: str) -> dict:
        """통화 시각 t 시점 사실 (§4.1 절차 2). 청구·status 컬럼은 읽지 않는다"""
        p, day = self.patients[pid], t[:10]
        nxt = self.next_appointment(pid, t)
        visits = [v for v in self.treatments[pid] if v["at"] <= t][-3:][::-1]
        return {
            "patient": {
                "id": pid,
                "name": p["name"],
                "age": age_on(p["birth"], date.fromisoformat(day)),
                "sex": p["sex"],
            },
            "recent_visits": [
                {"date": v["at"][:10], "procedure": v["procedure"], "tooth": v["tooth"]}
                | ({"emergency": v["emergency"]} if v.get("emergency") else {})
                for v in visits
            ],
            "balance": self.balance(pid, t),
            "next_appointment": {"appointment_id": nxt["id"]}
            | _appt_fact(nxt)
            | ({"emergency": EMERGENCY_VISIT_MARK} if nxt["id"] in self.emergency else {})
            if nxt
            else None,
            "family": [
                m["name"]
                for m in self.family[p["family"]]
                if m["id"] != pid and m["registered"] <= day
            ]
            if p["family"] is not None
            else [],
            "alerts": [
                label
                for rec, ends, label in self.alerts[pid]
                if rec <= day and (ends is None or day < ends)
            ],
            "open_plan_items": open_plan_items(self.plan_items, pid, t),
        }

    def is_child(self, pid: int, day: str) -> bool:
        """보호자가 거는 환자 — 통화일에 소아이고 보호자 기록이 있음(TODAY에 청소년인 환자는 보호자 기록이 없다)"""
        p = self.patients[pid]
        return (
            p["guardian"] is not None
            and age_on(p["birth"], date.fromisoformat(day)) <= CHILD_MAX_AGE
        )

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
        return [
            (a, a["cancelled"])
            for a in appts
            if a["status"] == "cancelled"
            and a["id"] not in origins
            and _in_hours(a["cancelled"])
            and not led.gone(a["patient"], a["cancelled"])  # 이탈 뒤 당일 취소는 연락 없이 (§2.2)
        ]
    # confirm: 리마인드 날 개원 뒤 임의 분 — 8단계가 리마인드 문자를 그 전에 보낸다
    out = []
    for a in appts:
        if a["confirmed"]:
            day = prev_open_day(date.fromisoformat(a["at"][:10]), run.holidays)
            t = datetime.combine(day, OPEN) + timedelta(
                minutes=int(rng.integers(1, business_minutes(day.weekday())))
            )
            out.append((a, t.strftime(FMT)))
    return out


def _side(led: Ledger, pid: int, day: str) -> str:
    return "guardian" if led.is_child(pid, day) else "patient"


def _recency_p(days: list, run) -> list:
    """날짜마다 최근 가중(master.call_day_weight)을 확률로 — 예약류 이벤트·그 밖 통화일 추첨 (§4.1 6단계)"""
    w = [call_day_weight(d, run.range_start, run.today) for d in days]
    total = sum(w)
    return [x / total for x in w]


def stage_slots(run, rng, fake) -> None:
    led = Ledger(
        run.conn,
        run.state["plan_items"],
        run.state["churn"],
        run.state["dormant"],
        recall_targets(run),
    )
    n = run.cfg["calls_with_transcript"]
    days = [run.range_start + timedelta(days=i) for i in range((run.today - run.range_start).days)]
    days = [d for d in days if is_open(d, run.holidays)]
    day_p = _recency_p(days, run)
    slots = []
    for purpose, k in quota(CALL_PURPOSE_SHARE, n).items():
        out_share = CALL_OUTBOUND_SHARE.get(purpose, 0) / CALL_PURPOSE_SHARE[purpose]
        out = quota({"outbound": out_share, "inbound": 1 - out_share}, k)["outbound"]
        multi = quota({"multi": OUTBOUND_MULTI_SHARE, "single": 1 - OUTBOUND_MULTI_SHARE}, out)[
            "multi"
        ]
        shape = [("outbound", i < multi) for i in range(out)] + [("inbound", False)] * (k - out)
        if purpose in BOOKING_PURPOSES:
            events = _booking_events(led, purpose, run, rng)
            if len(events) < k:
                raise ValueError(f"{purpose} 이벤트 {len(events)}건 < 통화 {k}건")
            event_p = _recency_p([date.fromisoformat(t[:10]) for _, t in events], run)
            picks = rng.choice(len(events), size=k, replace=False, p=event_p).tolist()
            for (direction, structural), i in zip(shape, picks, strict=True):
                a, t = events[i]
                pid = a["patient"]
                new = purpose == "book" and led.patients[pid]["registered"] == t[:10]
                slots.append(
                    {
                        "purpose": purpose,
                        "direction": direction,
                        "structural": structural,
                        "t": t,
                        "patient": pid,
                        "about": pid,
                        "appointment": a,
                        "counterpart": "new_caller"
                        if new and not led.is_child(pid, t[:10])
                        else _side(led, pid, t[:10]),
                    }
                )  # 처음 오는 소아는 기존 환자인 보호자가 건다
            continue
        for direction, structural in shape:
            who = (
                "side"
                if direction == "outbound"
                else _weighted(rng, CALL_COUNTERPART_WEIGHTS[purpose])
            )
            rule = CALL_PATIENT_RULES.get((purpose, direction), ("visited", None))
            for _ in range(MAX_DATE_TRIES):
                d = days[int(rng.choice(len(days), p=day_p))]
                t = any_minute(rng, d).strftime(FMT)
                if who not in ("side", "insurer"):
                    about = None
                    break
                cands = [
                    pid
                    for pid in led.patients
                    if led.matches(pid, rule, t)
                    and (who == "side" or led.patients[pid]["private"])
                    and not led.gone(pid, t)  # 이탈 환자는 이탈일부터 통화 없음 (§2.2)
                ]
                if cands:
                    about = cands[int(rng.integers(len(cands)))]
                    break
            else:
                raise ValueError(
                    f"{purpose} {direction} {who}: {MAX_DATE_TRIES}일 동안 조건에 맞는 환자 없음"
                )
            side = who == "side"
            slots.append(
                {
                    "purpose": purpose,
                    "direction": direction,
                    "structural": structural,
                    "t": t,
                    "patient": about if side else None,
                    "about": about,
                    "appointment": None,
                    "counterpart": _side(led, about, t[:10]) if side else who,
                }
            )
    _ledger_debt_calls(led, slots, days, rng.spawn(1)[0])
    for s in slots:
        if s["patient"] is not None:
            speaker = led.speaker(s["patient"], s["t"][:10])
            s["caller"] = (led.patients[s["patient"]]["phone"], speaker["name"])
        else:
            s["caller"] = (next(run.state["phones"]), fake.name())
    run.state["call_slots"], run.state["call_ledger"] = slots, led


def _ledger_debt_calls(led, slots: list, days: list, rng) -> None:
    """수납 문의 발신 LEDGER_DEBT_CALLS건을 장부 실수 미수 환자에게 다시 배정 — 서로 다른 환자, 통화일은 그 미수가 장부에
    들어간 뒤 LEDGER_DEBT_CALL_DAYS일 안의 진료일(그 시각 잔액 > 0). 다른 슬롯은 그대로 (§4.1 환자 고르기)"""
    rule = CALL_PATIENT_RULES[("billing_q", "outbound")]
    billing = [s for s in slots if s["purpose"] == "billing_q" and s["direction"] == "outbound"]
    patients = sorted(led.ledger_debt)
    targets = rng.permutation(len(billing)).tolist()[:LEDGER_DEBT_CALLS]
    for k in rng.permutation(len(patients)).tolist():
        if not targets:
            break
        pid = patients[k]
        entered = led.ledger_debt[pid][0][0]
        start = datetime.strptime(entered, FMT).date()
        options = [d for d in days if start < d <= start + timedelta(days=LEDGER_DEBT_CALL_DAYS)]
        for d in [options[int(i)] for i in rng.permutation(len(options))]:
            t = any_minute(rng, d).strftime(FMT)
            if led.matches(pid, rule, t) and not led.gone(pid, t):
                s = billing[targets.pop()]
                s.update(t=t, patient=pid, about=pid, counterpart=_side(led, pid, t[:10]))
                break
    if targets:
        raise ValueError(
            f"장부 실수 미수 수납 문의 {LEDGER_DEBT_CALLS - len(targets)}건 < {LEDGER_DEBT_CALLS}건"
        )


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
        if s["purpose"] == "recall_q":
            fact["recall_text"] = led.recall_text(
                s["about"], s["t"]
            )  # 받은 문자 — 월을 지어내지 않게
        debts = [d for d in led.ledger_debt[s["about"]] if d[0] <= s["t"]]
        if s["purpose"] == "billing_q" and s["direction"] == "outbound" and debts:
            _, day, debt = debts[-1]  # 수납 문의 미수가 장부 실수 — 이미 냈다는 말 (⑧-1 근거)
            fact["already_paid"] = {"date": day, "amount": debt}
    if (
        s["purpose"] in TOPIC_PURPOSES or "price_q" in s["accepted"]
    ):  # 부 주제 가격 문의도 무엇의 가격인지
        fact["topic_procedure"] = _topic(rng, led, s, fact)
        if fact["topic_procedure"] == PROCEDURE_NAME[SCALING_CODE] and s["about"] is not None:
            if led.scaled_in(s["about"], _topic_year(s, fact), s["t"]):
                fact["scaling_used_this_year"] = (
                    True  # 연 1회 급여를 이미 씀 — 가격은 비급여로 (§2.4)
                )
    return fact


def _topic_year(s: dict, fact: dict) -> int:
    """급여 연 1회를 따지는 해 — 리콜 문의는 받은 문자의 기한 해(12월에 받은 '1월 검진' 문자면 새해), 그 밖은 통화한 해"""
    text = fact.get("recall_text")
    if s["purpose"] == "recall_q" and text:
        sent = date.fromisoformat(text["sent_at"][:10])
        return recall_due_year(sent, text["kind"], text["due_month"])
    return int(s["t"][:4])


def _topic(rng, led: Ledger, s: dict, fact: dict) -> str:
    """주제 시술 (§4.1 used_fact) — 비용이 있는 시술(PRICED)만"""
    a = s["appointment"]
    if (
        a is not None and fact["appointment"]["procedure"] in PRICED
    ):  # 0원(상담 등)이면 아래 후보에서
        return fact["appointment"]["procedure"]
    if (
        (s["purpose"], s["direction"]) == ("treatment_q", "outbound")
        and "price_q" not in s["accepted"]
        and s["about"] is not None
    ):
        return led.recent_treatment(s["about"], s["t"])  # 경과 확인 = 며칠 전 진료
    if s["purpose"] == "recall_q":
        # 검진 문자를 보고 묻는 가격 = 다음 검진의 위생 시술 — 성인은 기한 해에 아직이면 스케일링, 그 밖은 구강검진
        adult = fact["patient"]["age"] >= SCALING_MIN_AGE
        if adult and not led.scaled_in(s["about"], _topic_year(s, fact), s["t"]):
            return PROCEDURE_NAME[SCALING_CODE]
        return PROCEDURE_NAME[CHECKUP_CODE]
    if s["about"] is not None:
        nxt = fact["next_appointment"]
        known = _plan_topics(fact) or sorted(
            (
                {v["procedure"] for v in fact["recent_visits"]}
                | ({nxt["procedure"]} if nxt else set())
            )
            & set(PRICED)
        )
    else:
        known = []
    pool = known or PRICED
    topic = pool[int(rng.integers(len(pool)))]
    if (
        s["about"] is not None
        and "price_q" in s["accepted"]
        and topic not in (upcoming := _upcoming(fact))
    ):
        # 가격 문의는 앞으로 받을 시술로 — 전역 난수를 더 쓰지 않고 통화마다 정해진 자리에서 고른다(다른 통화가 밀리지 않게)
        topic = upcoming[zlib.crc32(f"{s['t']}|{s['about']}".encode()) % len(upcoming)]
    return topic


def _plan_topics(fact: dict) -> list[str]:
    """환자 측 주제 후보 1순위 — 열린 치료계획 항목의 시술(비용이 있는 것, §4.1 used_fact)"""
    return sorted({i["procedure"] for i in fact["open_plan_items"]} & set(PRICED))


def _upcoming(fact: dict) -> list[str]:
    """가격 문의 주제 후보 — 앞으로 받을 시술: 다음 예약 시술, 없으면 열린 치료계획 항목, 없으면 그 나이의 치료 시술 중
    지난 진료에 없는 것. 지난 진료면 '그때 얼마였죠?'(수납 문의)로 쓰여 라벨과 대화가 어긋난다"""
    nxt = fact["next_appointment"]
    if nxt and nxt["procedure"] in PRICED:
        return [nxt["procedure"]]
    if planned := _plan_topics(fact):
        return planned
    done = {v["procedure"] for v in fact["recent_visits"]}
    band = age_band(fact["patient"]["age"])
    return [
        n for n in PLANNED_WEIGHTS["treatment"][band] if n in PRICED and n not in done
    ] or PRICED


def _before_booking(rng, s: dict) -> None:
    """미확정 예약·변경 통화는 그날 예약을 잡은 시각보다 앞 — 통화 뒤 회신해서 확정된다(EMR 예약은 그 시각에 생김).
    개원 직후에 잡힌 예약이라 앞설 자리가 없으면 확정 통화로 둔다"""
    created = datetime.strptime(s["t"], FMT)
    room = int((created - datetime.combine(created.date(), OPEN)).total_seconds() // 60)
    if room > 1:
        s["t"] = (
            datetime.combine(created.date(), OPEN) + timedelta(minutes=int(rng.integers(1, room)))
        ).strftime(FMT)
    else:
        s["confirmed"] = 1


def stage_labels(run, rng, fake) -> None:
    slots, led = run.state["call_slots"], run.state["call_ledger"]
    # call_uid(7단계 LLM 캐시 키)·길이는 라벨 추첨보다 먼저 — 라벨 파라미터를 바꿔도 그대로
    lo, hi = CALL_DURATION_SEC
    uids = (rng.permutation(len(slots)) + 1).tolist()
    durations = rng.integers(lo, hi + 1, size=len(slots)).tolist()
    for s in slots:  # 확정 여부가 통화 시각을 정하므로 노이즈 할당보다 먼저 — 노이즈 비중을 바꿔도 통화 행은 그대로
        if s["purpose"] in ("book", "reschedule"):
            s["confirmed"] = int(rng.random() < APPT_CONFIRMED_SHARE)
            if not s["confirmed"]:
                _before_booking(rng, s)
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
            flags["appt_confirmed"] = s["confirmed"]
        flags["has_todo"] = int(rng.random() < HAS_TODO_SHARE.get(purpose, HAS_TODO_DEFAULT))
        if flags["emergency"]:
            urgency = "urgent"
        elif s["noise"] == "ambiguous_pain":
            urgency = "urgent" if rng.random() < PAIN_URGENT_SHARE else "normal"
        else:
            urgency = _weighted(rng, CALL_URGENCY_WEIGHTS)
        if (
            urgency == "urgent"
            and s["appointment"] is not None
            and s["appointment"]["at"][:10] != s["t"][:10]
        ):
            urgency = "normal"  # 오늘 봐야 하는 통화가 며칠 뒤 예약을 잡지는 않는다 — 추첨 뒤 덮어써 난수 흐름은 그대로
        s.update(
            flags=flags,
            urgency=urgency,
            accepted=[purpose] + ([SUBTOPIC_PURPOSE[sub]] if sub else []),
        )
        s["fact"] = _used_fact(rng, led, s)
        if s["fact"].get("already_paid"):
            s["flags"]["has_todo"] = (
                1  # 이미 냈다는 말 — 직원이 기록을 다시 확인할 일이 남는다(추첨 뒤 덮어씀)
            )
    order = sorted(range(len(slots)), key=lambda i: slots[i]["t"])
    width = max(4, len(str(len(slots))))
    calls, labels = [], []
    for row_id, i in enumerate(order, start=1):
        s = slots[i]
        uid = f"c-{uids[row_id - 1]:0{width}d}"
        calls.append(
            (
                row_id,
                uid,
                s["patient"],
                *s["caller"],
                s["direction"],
                s["t"],
                durations[row_id - 1],
            )
        )
        labels.append(
            (
                uid,
                s["purpose"],
                json.dumps(s["accepted"]),
                s["counterpart"],
                s["urgency"],
                *(s["flags"][f] for f in CALL_FLAGS),
                s["noise"],
                json.dumps(s["fact"], ensure_ascii=False),
            )
        )
    run.conn.executemany(
        "INSERT INTO ax_calls (id, call_uid, source, patient_id, caller_phone, caller_name, direction,"
        " started_at, duration_sec) VALUES (?, ?, 'generated', ?, ?, ?, ?, ?, ?)",
        calls,
    )
    run.conn.executemany(
        f"INSERT INTO truth.truth_call_labels VALUES ({', '.join('?' * 13)}, 'silver')", labels
    )


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
    for uid, purpose in conn.execute(
        "SELECT c.call_uid, l.primary_purpose FROM ax_calls c"
        " JOIN truth.truth_call_labels l USING (call_uid) ORDER BY c.id"
    ):
        by[purpose].append(uid)
    by = {c: by[c] for c in CALL_PURPOSE_SHARE if by[c]}
    size = run.cfg["eval_pool_size"]
    dev, test, odd = [], [], 0
    for c, k in _allocate(by, size).items():
        picked = [by[c][i] for i in rng.choice(len(by[c]), size=k, replace=False).tolist()]
        half = k // 2 + (
            k % 2 if odd % 2 == 0 else 0
        )  # 홀수 잔여는 카테고리를 돌며 dev/test 번갈아
        odd += k % 2
        dev += picked[:half]
        test += picked[half:]
    pool = [dev[i] for i in rng.permutation(len(dev)).tolist()] + [
        test[i] for i in rng.permutation(len(test)).tolist()
    ]
    split = {uid: "dev" for uid in dev} | {uid: "test" for uid in test}
    conn.executemany(
        "UPDATE ax_calls SET in_eval_pool = 1, eval_sort_order = ? WHERE call_uid = ?",
        [(i, uid) for i, uid in enumerate(pool, start=1)],
    )
    conn.executemany(
        "UPDATE truth.truth_call_labels SET split = ? WHERE call_uid = ?",
        [(s, uid) for uid, s in split.items()],
    )
    blind = run.cfg["blind_pool_size"]
    conn.executemany(
        "UPDATE ax_calls SET in_blind_pool = 1, blind_sort_order = ? WHERE call_uid = ?",
        [
            (i, pool[j])
            for i, j in enumerate(
                rng.choice(len(pool), size=blind, replace=False).tolist(), start=1
            )
        ],
    )
