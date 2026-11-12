"""4단계 예약 시뮬레이션 — range_start부터 TODAY 전날까지 하루씩 진행 (가상 데이터 생성 설계 §2.6 (3)).
예약·상태·시각만 정한다(진료·수납·청구는 5단계). 신환은 여기서 등록한다.

하루 순서: 1. 그날 예약 처리(상태 추첨, 치료계획, 코스 다음 회차 데스크 예약) 2. 전화 예약(기한이 된 수요·재예약·신환)
3. 변경 4. 리마인드. 휴진일은 아무 일도 없다.
- 신환 수 = 그날 잡힌 기존 환자 예약(전화·데스크, 변경 제외) B × s/(1−s), s = NEW_PATIENT_SHARE (확률적 반올림)
- lapse(첫 리콜 무응답)는 구환 첫 리콜만(3단계). 내원하면 다음 리콜이 다시 생긴다
- 휴면(DORMANT_MONTHLY_RATE): 휴면일(지수분포, 자기 난수)부터 리콜·새 치료 수요 없음. 다른 치과로 옮긴 건 아님(⑨ 정답 아님)
- 치료계획(§2.3): Tn 첫 내원·상담·치주 위생(PERIO_PLAN_RATE)이 계획을 만들고, 코스는 항목 순서대로 예약한다.
  그만둠(PLAN_DROP_RATE)은 계획을 받아들일 때 한 번 — 몇 번째 항목 뒤에 멈출지 함께 뽑는다. 치아는 5단계가 정한다
- 이탈(§2.2): 구환 중 CHURN_SHARE, 이탈일은 창 시작 ~ TODAY − CHURN_MIN_DAYS 균등(자기 난수). 이탈일부터 전화 예약·
  변경·재예약·리마인드 확인이 없고, 잡혀 있던 예약은 노쇼 또는 당일 취소. 정답은 truth_patient_churn
"""

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from generator.demand import arrivals, recall_due
from generator.master import (
    CHAIRS,
    CHECKUP_CODE,
    CHURN_MIN_DAYS,
    CHURN_SHARE,
    COURSE_INTERVAL_DAYS,
    DAYS_PER_MONTH,
    DOCTOR_CONCURRENCY,
    DORMANT_MONTHLY_RATE,
    EPISODE_MIX,
    FAMILY_SIZE,
    GUARDIAN_MIN_AGE_GAP,
    HOURS,
    HYGIENE_SCALING_BANDS,
    MAX_LEAD_DAYS,
    MIN_VISIT_AGE,
    NEW_PATIENT_SHARE,
    PERIO_PLAN_RATE,
    PLAN_DROP_RATE,
    PLAN_TEMPLATES,
    PLAN_UNBOOKED_RATE,
    PLANNED_WEIGHTS,
    PROCEDURES,
    RECALL_CYCLE_MONTHS,
    RESCHEDULE_MAX_DAYS,
    SAMEDAY_CANCEL_LEAD_MIN,
    SATURDAY,
    SCALING_CODE,
    SCALING_MIN_AGE,
    SLOT_MIN,
    TOOTH_TREATMENT_MIN_AGE,
    VISIT_MIX,
    add_years,
    age_band,
    age_on,
    business_minutes,
    daily_cap,
    open_slots,
)
from generator.patients import new_patient_candidates

ROLE = {"treatment": "doctor", "hygiene": "hygienist", "consult": "consultant"}
# 담당 1명이 동시에 맡는 예약 수 (§1 (4)). 상담은 제약 없음
CONCURRENCY = {"treatment": DOCTOR_CONCURRENCY, "hygiene": 1}
CODE = {p["name"]: p["code"] for p in PROCEDURES}
NAME = {p["code"]: p["name"] for p in PROCEDURES}
DURATION = {p["code"]: p["duration_min"] for p in PROCEDURES}


def _t(hhmm: str) -> time:
    return time.fromisoformat(hhmm)


OPEN = _t(HOURS["open"])


@dataclass
class PlanItem:
    id: int
    seq: int
    code: str


@dataclass
class Plan:
    id: int
    patient_id: int
    created: datetime  # 만든 내원의 종료 시각
    appointment_id: int
    items: list


@dataclass
class Episode:
    visit_type: str
    course: bool = False  # Tn — 첫 내원이 계획을 만든다
    plan: Plan | None = None
    done: int = 0  # 이 코스에서 마친 항목 수
    stop_after: int = 0  # 이만큼 마치면 끝(그만둠이면 항목 수보다 적다)
    call_on: date | None = (
        None  # 예약이 없을 때 전화할 날 (노쇼·당일 취소 뒤 재예약, 데스크 예약 실패)
    )
    missed: "Appt | None" = (
        None  # 노쇼·당일 취소한 예약 — 재예약은 같은 시술 (연령 구간·연도가 바뀌면 다시 뽑음)
    )


@dataclass
class Patient:
    id: int
    birth: date
    queue: list  # 치료 수요 Arrival — 날짜순, 아직 시작하지 않은 것
    recall_due: date | None
    last_hygiene: date | None
    ep: Episode | None = None
    appt: "Appt | None" = None  # 지금 잡혀 있는 예약 (끝나면 None)
    days: set = field(default_factory=set)  # 비취소 예약이 있는 날짜 (같은 날 중복 금지)
    scaling_years: set = field(
        default_factory=set
    )  # 급여 스케일링(SCALING_MIN_AGE 이상)을 받은 해
    dormant: date | None = None  # 휴면일(DORMANT_MONTHLY_RATE) — 이날부터 리콜·새 치료 수요 없음


@dataclass
class Appt:
    id: int
    patient: Patient
    staff_id: int
    chair: int | None  # 상담은 None (§1 (3))
    start: datetime
    duration: int
    type: str
    code: str
    booked_via: str
    created: datetime
    rescheduled_from: int | None = None
    status: str = "scheduled"
    cancelled: datetime | None = None
    confirmed: int = 0
    change_day: date | None = None
    episode: Episode | None = (
        None  # 이 예약이 속한 수요 — 5단계가 코스 안에서 같은 치아를 유지하려고 (창 이전 내원은 None)
    )
    plan_item: int | None = None  # 이행하는 치료계획 항목 id


def pick_churn(run, rng) -> dict[int, date]:
    """이탈 환자 → 이탈일. 구환(3단계까지 있는 환자) 중 CHURN_SHARE, 이탈일은 창 시작 ~ TODAY − CHURN_MIN_DAYS 균등"""
    ids = sorted(p["id"] for p in run.state["patients"])
    k = round(CHURN_SHARE * len(ids))
    span = (run.today - timedelta(days=CHURN_MIN_DAYS) - run.range_start).days
    chosen = sorted(int(i) for i in rng.choice(ids, size=k, replace=False))
    return {pid: run.range_start + timedelta(days=int(rng.integers(span + 1))) for pid in chosen}


class Sim:
    def __init__(self, run, rng, churn: dict[int, date], dormant_rng):
        self.run, self.rng, self.cfg = run, rng, run.cfg
        self.churn = churn
        self.dormant_rng = dormant_rng  # 휴면 추첨은 자기 난수 — 시뮬레이션 난수를 쓰지 않게
        self.holidays = run.holidays
        self.staff = {
            role: [
                r[0]
                for r in run.conn.execute(
                    "SELECT id FROM emr_staff WHERE role = ? ORDER BY id", (role,)
                )
            ]
            for role in ROLE.values()
        }
        self.appts: list[Appt] = []
        self.changes_on: dict[date, list[Appt]] = {}
        self.by_day: dict[date, list[Appt]] = {}
        self.day_count: dict[date, int] = {}
        self.patients: list[Patient] = []
        self.plans: list[Plan] = []
        self.item_count = 0
        for p in run.state["patients"]:
            self.patients.append(
                Patient(
                    p["id"],
                    p["birth_date"],
                    list(run.state["arrivals"][p["id"]]),
                    run.state["recall_due"][p["id"]],
                    p["last_hygiene"],
                    dormant=self.dormant_day(run.range_start),
                )
            )
        self.surname = dict(run.conn.execute("SELECT id, surname FROM emr_families"))
        self.birth = {p.id: p.birth for p in self.patients}  # 구환 — 신환 소아의 보호자 후보
        self.phone = dict(run.conn.execute("SELECT id, phone FROM emr_patients"))
        self.new_rows: list[tuple] = []
        self.candidates = new_patient_candidates(run)
        self.add_pre_window(run.state.get("pre_window", {}))

    def add_pre_window(self, visits: dict) -> None:
        """3b단계가 뽑은 창 이전 내원 — 완료된 위생 예약으로 먼저 넣는다(id 앞번호, 위생사·체어 균등 — 창 밖이라
        체어·동시 진료 제약은 보지 않는다, §5.3). 그해 급여 스케일링이면 연 1회 이력에 넣어 창 안 리콜이 다시 급여로 잡히지 않게"""
        for p in self.patients:
            v = visits.get(p.id)
            if v is None:
                continue
            hygienists = self.staff["hygienist"]
            a = Appt(
                len(self.appts) + 1,
                p,
                hygienists[int(self.rng.integers(len(hygienists)))],
                int(self.rng.integers(1, CHAIRS + 1)),
                v["start"],
                DURATION[v["code"]],
                "hygiene",
                v["code"],
                "phone",
                v["created"],
                status="completed",
            )
            self.appts.append(a)
            p.days.add(a.start.date())
            if a.code == SCALING_CODE and age_on(p.birth, a.start.date()) >= SCALING_MIN_AGE:
                p.scaling_years.add(a.start.year)

    # ── 달력·용량 ─────────────────────────────────────────

    def is_open(self, d: date) -> bool:
        return open_slots(d.weekday()) > 0 and d not in self.holidays

    def next_open(self, d: date) -> date:
        d += timedelta(days=1)
        while not self.is_open(d):
            d += timedelta(days=1)
        return d

    @staticmethod
    def starts(d: date, duration: int) -> list[datetime]:
        """그날 duration분 예약이 들어갈 수 있는 시작 시각 (영업시간 안, 평일 점심과 겹치지 않음)"""
        sat = d.weekday() == SATURDAY
        close = datetime.combine(d, _t(HOURS["saturday_close" if sat else "weekday_close"]))
        lunch = [datetime.combine(d, _t(h)) for h in HOURS["lunch"]]
        out, s = [], datetime.combine(d, OPEN)
        while s + timedelta(minutes=duration) <= close:
            if sat or not (s < lunch[1] and s + timedelta(minutes=duration) > lunch[0]):
                out.append(s)
            s += timedelta(minutes=SLOT_MIN)
        return out

    def find_day(self, patient: Patient, first: date, last: date) -> date | None:
        """first~last(range_end까지) 중 진료일 + 하루 상한 미만 + 같은 환자 같은 날 없음인 첫 날"""
        d = first
        while d <= min(last, self.run.range_end):
            if (
                self.is_open(d)
                and self.day_count.get(d, 0) < daily_cap(self.cfg["visits_per_day"], d.weekday())
                and d not in patient.days
            ):
                return d
            d += timedelta(days=1)
        return None

    def openings(self, d: date, duration: int, visit_type: str) -> dict:
        """그날 duration분 visit_type 예약이 들어갈 시작 시각 → (빈 체어, 빈 담당) — §1 (3)·(4).
        체어나 담당이 하나도 없는 시각은 빠진다"""
        out = {}
        for s in self.starts(d, duration):
            e = s + timedelta(minutes=duration)
            busy = [
                a
                for a in self.by_day.get(d, [])
                if a.status != "cancelled"
                and a.start < e
                and s < a.start + timedelta(minutes=a.duration)
            ]
            chairs = [None]
            if visit_type != "consult":
                used = {a.chair for a in busy}
                chairs = [c for c in range(1, CHAIRS + 1) if c not in used]
            cap = CONCURRENCY.get(visit_type)
            staff = [
                sid
                for sid in self.staff[ROLE[visit_type]]
                if cap is None or sum(a.staff_id == sid for a in busy) < cap
            ]
            if chairs and staff:
                out[s] = (chairs, staff)
        return out

    def pick(self, options: list):
        return options[int(self.rng.integers(len(options)))]

    def find_slot(
        self, patient: Patient, visit_type: str, first: date, last: date, code_for
    ) -> tuple | None:
        """first~last 중 첫 가능일(§2.6 (3) — 운영 제약 + 같은 환자 같은 날 없음)의 (시술, 시작, 체어, 담당).
        시술은 code_for(그날)로 정하고, 그 소요로 들어갈 시각이 없으면 다음 날. 시각·체어·담당은 각각 균등"""
        d = first
        while (d := self.find_day(patient, d, last)) is not None:
            code = code_for(d)
            openings = self.openings(d, DURATION[code], visit_type)
            if openings:
                start = self.pick(sorted(openings))
                chairs, staff = openings[start]
                return code, start, self.pick(chairs), self.pick(staff)
            d += timedelta(days=1)
        return None

    def any_minute(self, d: date) -> datetime:
        """그날 개원~평일 마감 사이 분 단위 시각 균등 추첨 — 통화·취소 시각. 점심·토요일 마감은 따지지 않는다"""
        return datetime.combine(d, OPEN) + timedelta(
            minutes=int(self.rng.integers(business_minutes(d.weekday())))
        )

    def dormant_day(self, start: date) -> date:
        """휴면일 — start부터 월 DORMANT_MONTHLY_RATE의 지수분포 (자기 난수)"""
        return start + timedelta(
            days=int(self.dormant_rng.exponential(DAYS_PER_MONTH / DORMANT_MONTHLY_RATE))
        )

    def gone(self, p: Patient, d: date) -> bool:
        """그날 이미 이탈한 환자"""
        return p.id in self.churn and d >= self.churn[p.id]

    def occupy(self, a: Appt, sign: int) -> None:
        d = a.start.date()
        self.day_count[d] = self.day_count.get(d, 0) + sign
        (a.patient.days.add if sign > 0 else a.patient.days.discard)(d)

    # ── 예약 ──────────────────────────────────────────────

    def weighted(self, weights: dict) -> str:
        names = list(weights)
        total = sum(weights.values())
        return names[int(self.rng.choice(len(names), p=[weights[n] / total for n in names]))]

    def planned_code(self, patient: Patient, visit_type: str, on: date) -> str:
        band = age_band(age_on(patient.birth, on))
        if visit_type == "hygiene" and band in HYGIENE_SCALING_BANDS:
            return CHECKUP_CODE if on.year in patient.scaling_years else SCALING_CODE
        return CODE[self.weighted(PLANNED_WEIGHTS[visit_type][band])]

    def book(
        self,
        patient: Patient,
        visit_type: str,
        code: str,
        start: datetime,
        chair: int | None,
        staff_id: int,
        created: datetime,
        via: str,
        rescheduled_from: int | None = None,
    ) -> Appt:
        a = Appt(
            len(self.appts) + 1,
            patient,
            staff_id,
            chair,
            start,
            DURATION[code],
            visit_type,
            code,
            via,
            created,
            rescheduled_from,
            episode=patient.ep,
        )
        ep = patient.ep
        if ep is not None and ep.plan is not None:
            a.plan_item = ep.plan.items[ep.done].id
        if rescheduled_from is None and self.rng.random() < self.cfg["reschedule_rate"]:
            # 변경일 = created 다음 날 ~ 예약 전날 중 진료일. 없으면 변경 없음
            days, d = [], created.date() + timedelta(days=1)
            while d < start.date():
                if self.is_open(d):
                    days.append(d)
                d += timedelta(days=1)
            if days:
                a.change_day = days[int(self.rng.integers(len(days)))]
                self.changes_on.setdefault(a.change_day, []).append(a)
        self.appts.append(a)
        self.by_day.setdefault(start.date(), []).append(a)
        self.occupy(a, +1)
        patient.appt = a
        return a

    @staticmethod
    def code_key(patient: Patient, on: date) -> tuple:
        """예정 시술을 정하는 조건 — 연령 구간과 연도(그해 스케일링)"""
        return age_band(age_on(patient.birth, on)), on.year

    def find_booking(
        self, patient: Patient, visit_type: str, first: date, missed: "Appt | None" = None
    ) -> tuple | None:
        """first부터 첫 가능일 → 그날 기준(나이·그해 스케일링)으로 시술 → 시술 소요로 시각·체어·담당.
        재예약(missed)은 그날의 code_key가 놓친 예약과 같으면 같은 시술"""

        def code_for(d: date) -> str:
            if patient.ep is not None and patient.ep.plan is not None:
                return patient.ep.plan.items[patient.ep.done].code  # 코스는 다음 항목 그대로
            if missed is not None and self.code_key(patient, d) == self.code_key(
                patient, missed.start.date()
            ):
                return missed.code
            return self.planned_code(patient, visit_type, d)

        return self.find_slot(patient, visit_type, first, self.run.range_end, code_for)

    def phone_book(
        self, patient: Patient, visit_type: str, d: date, missed: "Appt | None" = None
    ) -> Appt | None:
        """희망 리드 1~MAX_LEAD_DAYS일 뒤 첫 가능일, created_at = 그날 임의 시각"""
        created = self.any_minute(d)
        found = self.find_booking(
            patient,
            visit_type,
            d + timedelta(days=int(self.rng.integers(1, MAX_LEAD_DAYS + 1))),
            missed,
        )
        if found is None:
            return None
        return self.book(patient, visit_type, *found, created, "phone")

    # ── 하루 ──────────────────────────────────────────────

    def process(self, d: date) -> int:
        """1. 그날 예약 처리. 데스크 예약 수를 돌려준다"""
        desk = 0
        noshow, sameday = self.cfg["noshow_rate"], self.cfg["sameday_cancel_rate"]
        for a in sorted(
            (a for a in self.by_day.get(d, []) if a.status == "scheduled"),
            key=lambda a: (a.start, a.id),
        ):
            p, r = a.patient, self.rng.random()
            p.appt = None
            if self.gone(p, d):  # 잡혀 있던 예약 — 노쇼 또는 당일 취소, 수요는 버린다
                if r * (noshow + sameday) < noshow:
                    a.status = "noshow"
                else:
                    lead = int(self.rng.integers(1, SAMEDAY_CANCEL_LEAD_MIN + 1))
                    a.status, a.cancelled = "cancelled", a.start - timedelta(minutes=lead)
                    self.occupy(a, -1)
                p.ep = None
                continue
            if r < noshow:
                a.status = "noshow"
            elif r < noshow + sameday:
                lead = int(self.rng.integers(1, SAMEDAY_CANCEL_LEAD_MIN + 1))
                a.status, a.cancelled = "cancelled", a.start - timedelta(minutes=lead)
                self.occupy(a, -1)
            else:
                a.status = "completed"
            if a.status != "completed":
                lo, hi = self.cfg["rebook_delay_days"]
                p.ep.call_on, p.ep.missed = (
                    d + timedelta(days=int(self.rng.integers(lo, hi + 1))),
                    a,
                )
                continue
            if a.type == "hygiene":
                p.last_hygiene = d
            if a.code == SCALING_CODE and age_on(p.birth, d) >= SCALING_MIN_AGE:
                p.scaling_years.add(d.year)
            p.recall_due = recall_due(self.rng, p.last_hygiene or d)
            if not self.after_visit(p, a):
                p.ep = None
                continue
            # 코스 다음 회차: 그 자리에서 데스크 예약 (created_at = 내원 종료 시각). 가능일이 없으면 내일 전화
            interval = int(self.rng.integers(COURSE_INTERVAL_DAYS[0], COURSE_INTERVAL_DAYS[1] + 1))
            found = self.find_booking(p, "treatment", d + timedelta(days=interval))
            if found is None:
                p.ep.call_on = d + timedelta(days=1)
                continue
            self.book(p, "treatment", *found, a.start + timedelta(minutes=a.duration), "desk")
            desk += 1
        return desk

    def new_plan(self, p: Patient, a: Appt, names: tuple) -> Plan:
        plan = Plan(len(self.plans) + 1, p.id, a.start + timedelta(minutes=a.duration), a.id, [])
        for seq, name in enumerate(names, 1):
            self.item_count += 1
            plan.items.append(PlanItem(self.item_count, seq, CODE[name]))
        self.plans.append(plan)
        return plan

    def plan_names(self, p: Patient, a: Appt) -> tuple | None:
        """completed 내원이 만드는 계획의 항목 시술 — Tn 첫 내원은 그 시술의 템플릿, 상담은 연령 구간 치료 시술로만
        된 후보 중 균등(없으면 그 구간 치료 가중으로 첫 시술을 뽑아 템플릿), 성인·노인 스케일링은 PERIO_PLAN_RATE로 치주.
        치아 치료를 못 하는 나이(TOOTH_TREATMENT_MIN_AGE 미만)는 계획 없음"""
        on = a.start.date()
        if age_on(p.birth, on) < TOOTH_TREATMENT_MIN_AGE:
            return None
        band = age_band(age_on(p.birth, on))
        if a.type == "treatment":
            return PLAN_TEMPLATES["treatment"][NAME[a.code]] if p.ep.course else None
        if a.type == "consult":
            pool = PLANNED_WEIGHTS["treatment"][band]
            fits = [
                t for t in PLAN_TEMPLATES["consult"][NAME[a.code]] if all(n in pool for n in t)
            ]
            if fits:
                return fits[int(self.rng.integers(len(fits)))]
            return PLAN_TEMPLATES["treatment"][self.weighted(pool)]
        if (
            a.code == SCALING_CODE
            and band in HYGIENE_SCALING_BANDS
            and self.rng.random() < PERIO_PLAN_RATE
        ):
            return PLAN_TEMPLATES["hygiene"]
        return None

    def accept(self, ep: Episode) -> None:
        """계획을 받아들일 때 그만둠을 한 번 뽑는다 — 그만두면 1 ~ 항목 수 − 1번째 항목 뒤에서 멈춘다"""
        n = len(ep.plan.items)
        ep.stop_after = n
        if n > 1 and self.rng.random() < PLAN_DROP_RATE:
            ep.stop_after = int(self.rng.integers(1, n))

    def after_visit(self, p: Patient, a: Appt) -> bool:
        """completed 내원 뒤 치료계획 — 코스 다음 항목을 예약해야 하면 True (p.ep가 그 코스)"""
        ep = p.ep
        if ep.plan is not None:
            ep.done += 1
            return ep.done < ep.stop_after
        names = self.plan_names(p, a)
        if names is None:
            return False
        plan = self.new_plan(p, a, names)
        if a.type == "treatment":  # Tn 첫 내원 = 항목 1
            a.plan_item = plan.items[0].id
            ep.plan, ep.done = plan, 1
            self.accept(ep)
            return ep.done < ep.stop_after
        if self.rng.random() < PLAN_UNBOOKED_RATE:
            return False
        p.ep = Episode("treatment", course=True, plan=plan)
        self.accept(p.ep)
        return True

    def due_demand(self, p: Patient, d: date) -> tuple[str, Episode] | None:
        """기한이 된 수요 중 가장 이른 것 (같으면 치료 먼저) — 예약이 잡혀야 시작하므로 여기서는 보기만"""
        if p.dormant is not None and d >= p.dormant:
            return None
        arrival = p.queue[0] if p.queue and p.queue[0].day <= d else None
        recall = p.recall_due if p.recall_due is not None and p.recall_due <= d else None
        if arrival is not None and (recall is None or arrival.day <= recall):
            return "arrival", Episode(arrival.visit_type, course=arrival.visits > 1)
        if recall is not None:
            return "recall", Episode("hygiene")
        return None

    def phone_calls(self, d: date) -> int:
        """2. 기존 환자 전화 예약 (순서는 무작위). 잡힌 예약 수를 돌려준다. 가능일이 없으면 수요는 남고 내일 다시"""
        booked = 0
        waiting = [
            p
            for p in self.patients
            if p.appt is None
            and not self.gone(p, d)
            and (
                (p.ep is not None and p.ep.call_on is not None and p.ep.call_on <= d)
                or (p.ep is None and self.due_demand(p, d) is not None)
            )
        ]
        for k in self.rng.permutation(len(waiting)).tolist():
            p = waiting[k]
            if p.ep is not None:
                if self.phone_book(p, p.ep.visit_type, d, p.ep.missed) is None:
                    p.ep.call_on = d + timedelta(days=1)
                else:
                    p.ep.call_on, p.ep.missed = None, None
                    booked += 1
                continue
            source, ep = self.due_demand(p, d)
            p.ep = ep
            if self.phone_book(p, ep.visit_type, d) is None:
                p.ep = None
                continue
            if source == "arrival":
                p.queue.pop(0)
            else:
                p.recall_due = None
            booked += 1
        return booked

    def new_patients(self, d: date, booked: int) -> None:
        """그날 기존 환자 예약 수에 비례해 신환을 꺼내 등록 (확률적 반올림)"""
        x = booked * NEW_PATIENT_SHARE / (1 - NEW_PATIENT_SHARE)
        k = int(x) + (self.rng.random() < x - int(x))
        for _ in range(k):
            while True:
                c = next(self.candidates)
                if age_on(c["birth_date"], d) < MIN_VISIT_AGE:  # 출생 전·영아 등록 금지 — 버림
                    continue
                family = self.pick_family(c) if c["band"] == "child" else None
                if c["band"] == "child" and family is None:
                    continue
                break
            visit_type = self.weighted(VISIT_MIX)
            if visit_type == "treatment" and age_on(c["birth_date"], d) < TOOTH_TREATMENT_MIN_AGE:
                visit_type = "hygiene"  # 영구치가 나기 전 — 치아 단위 치료 대신 검진
            course = False
            if visit_type == "treatment":
                mix = EPISODE_MIX[c["band"]]
                course = self.weighted({"T1": mix["T1"], "Tn": mix["Tn"]}) == "Tn"
            pid = len(self.patients) + 1
            p = Patient(pid, c["birth_date"], [], None, None, Episode(visit_type, course=course))
            if self.phone_book(p, visit_type, d) is None:
                return  # 빈자리가 없으면 그날 신환은 더 받지 않는다(후보는 버림)
            self.patients.append(p)
            p.dormant = self.dormant_day(d + timedelta(days=1))
            p.queue = arrivals(
                self.rng,
                p.birth,
                d + timedelta(days=1),
                self.run.today,
                self.cfg["treatment_demand_monthly"],
            )
            self.register(p, c, family, d)

    def pick_family(self, c: dict) -> int | None:
        """신환 소아의 가족 = 상한 미만 가족 중 보호자가 나이 차를 만족하는 것 (균등). 보호자는 그 가족의 보호자"""
        fams = self.run.state["families"]
        ok = [
            fid
            for fid in sorted(fams)
            if len(fams[fid]["members"]) < FAMILY_SIZE[1]
            and self.birth[fams[fid]["guardian"]]
            <= add_years(c["birth_date"], -GUARDIAN_MIN_AGE_GAP)
        ]
        return ok[int(self.rng.integers(len(ok)))] if ok else None

    def register(self, p: Patient, c: dict, family: int | None, d: date) -> None:
        guardian = None
        surname, phone = c["surname"], c["phone"]
        if family is not None:
            fam = self.run.state["families"][family]
            fam["members"].append(p.id)
            guardian, surname = fam["guardian"], self.surname[family]
            phone = self.phone[guardian]
        self.new_rows.append(
            (
                p.id,
                f"C-{p.id:05d}",
                surname + c["first_name"],
                c["birth_date"].isoformat(),
                c["sex"],
                phone,
                family,
                guardian,
                d.isoformat(),
                RECALL_CYCLE_MONTHS,
                c["has_private_insurance"],
            )
        )

    def changes(self, d: date) -> None:
        """3. 변경: 오늘이 변경일인 예약 → 원래 날짜 뒤 1~RESCHEDULE_MAX_DAYS일의 첫 가능일로. 가능일이 없으면 변경하지 않음"""
        for a in [
            a
            for a in self.changes_on.get(d, [])
            if a.status == "scheduled" and not self.gone(a.patient, d)
        ]:
            p = a.patient
            self.occupy(a, -1)
            orig = a.start.date()
            found = self.find_slot(
                p,
                a.type,
                orig + timedelta(days=1),
                orig + timedelta(days=RESCHEDULE_MAX_DAYS),
                lambda _, code=a.code: code,
            )
            if found is None:
                self.occupy(a, +1)
                continue
            t = self.any_minute(d)
            a.status, a.cancelled = "cancelled", t
            self.book(p, a.type, *found, t, "phone", rescheduled_from=a.id)

    def reminders(self, d: date) -> None:
        """4. 리마인드: 오늘 전에 잡혀 있고 오늘 취소되지 않은 다음 진료일 예약 → confirm_rate로 confirmed (날짜 단위).
        오늘 잡은 예약은 대상 아님 — 문자·확인 통화가 created_at 뒤 같은 날에 들어갈 자리가 없을 수 있어서"""
        for a in sorted(self.by_day.get(self.next_open(d), []), key=lambda a: a.id):
            if (
                a.created.date() < d
                and (a.cancelled is None or a.cancelled.date() > d)
                and not self.gone(a.patient, d)
            ):
                a.confirmed = int(self.rng.random() < self.cfg["confirm_rate"])

    def run_days(self) -> None:
        d = self.run.range_start
        while d < self.run.today:
            if self.is_open(d):
                desk = self.process(d)
                booked = self.phone_calls(d)
                self.new_patients(d, desk + booked)
                self.changes(d)
                self.reminders(d)
            d += timedelta(days=1)

    def write(self) -> None:
        conn = self.run.conn
        conn.executemany(
            "INSERT INTO emr_patients (id, chart_no, name, birth_date, sex, phone, family_id, guardian_patient_id,"
            " registered_at, recall_cycle_months, has_private_insurance) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            self.new_rows,
        )
        fmt = "%Y-%m-%d %H:%M:%S"
        conn.executemany(
            "INSERT INTO emr_appointments (id, patient_id, staff_id, scheduled_at, duration_min, type,"
            " planned_procedure_code, status, rescheduled_from_id, booked_via, confirmed, created_at, cancelled_at,"
            " chair_no, plan_item_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    a.id,
                    a.patient.id,
                    a.staff_id,
                    a.start.strftime(fmt),
                    a.duration,
                    a.type,
                    a.code,
                    a.status,
                    a.rescheduled_from,
                    a.booked_via,
                    a.confirmed,
                    a.created.strftime(fmt),
                    a.cancelled.strftime(fmt) if a.cancelled else None,
                    a.chair,
                    a.plan_item,
                )
                for a in self.appts
            ],
        )
        # 예약 id → 에피소드 번호(처음 나온 순) — 5단계 치아 유지, 5b R01. 앱 DB에는 두지 않는다(회차 추적 없음)
        numbers: dict[int, int] = {}
        self.run.state["episode"] = {
            a.id: numbers.setdefault(id(a.episode), len(numbers))
            for a in self.appts
            if a.episode is not None
        }
        conn.executemany(
            "INSERT INTO truth.truth_appointment_courses VALUES (?, ?)",
            self.run.state["episode"].items(),
        )
        self.run.state["plans"] = self.plans  # 계획 테이블은 5단계가 치아를 정해 적재
        self.run.state["churn"] = self.churn  # 5단계 병행 후보에서 빼고, 6단계 통화 후보에서 뺀다
        # 휴면 환자 → 휴면일 — 6단계 리콜 문의 후보에서 빼고, 휴면 테스트가 본다
        self.run.state["dormant"] = {
            p.id: p.dormant for p in self.patients if p.dormant < self.run.today
        }
        conn.executemany(
            "INSERT INTO truth.truth_patient_churn VALUES (?, 'churn', ?)",
            [(pid, on.isoformat()) for pid, on in sorted(self.churn.items())],
        )


def stage(run, rng, fake) -> None:
    # 이탈·휴면 추첨은 자기 난수 — 시뮬레이션 난수를 쓰지 않게
    churn_rng, dormant_rng = rng.spawn(2)
    sim = Sim(run, rng, pick_churn(run, churn_rng), dormant_rng)
    sim.run_days()
    sim.write()
