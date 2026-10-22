"""4단계 예약 시뮬레이션 — range_start부터 TODAY 전날까지 하루씩 진행 (가상 데이터 생성 설계 §2.6 (3)).
예약·상태·시각만 정한다(진료·수납·청구는 5단계). 신환은 여기서 등록한다.

하루 순서: 1. 그날 예약 처리(상태 추첨, 코스 다음 회차 데스크 예약) 2. 전화 예약(기한이 된 수요·재예약·신환)
3. 변경 4. 리마인드. 휴진일은 아무 일도 없다.
- 신환 수 = 그날 잡힌 기존 환자 예약(전화·데스크, 변경 제외) B × s/(1−s), s = NEW_PATIENT_SHARE (확률적 반올림)
- lapse(첫 리콜 무응답)는 구환 첫 리콜만(3단계). 내원하면 다음 리콜이 다시 생긴다
"""
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from generator.demand import arrivals, recall_due
from generator.master import (CHECKUP_CODE, COURSE_INTERVAL_DAYS, COURSE_VISITS, EPISODE_MIX, FAMILY_SIZE,
                              GUARDIAN_MIN_AGE_GAP, HOURS, HYGIENE_SCALING_BANDS, MAX_LEAD_DAYS, NEW_PATIENT_SHARE,
                              PLANNED_WEIGHTS, PROCEDURES, RESCHEDULE_MAX_DAYS, SAMEDAY_CANCEL_LEAD_MIN, SATURDAY,
                              SCALING_CODE, SCALING_MIN_AGE, SLOT_MIN, VISIT_MIX, RECALL_CYCLE_MONTHS, add_years,
                              age_band, age_on, daily_cap, open_slots)
from generator.patients import new_patient_candidates

ROLE = {"treatment": "doctor", "hygiene": "hygienist", "consult": "consultant"}
CODE = {p["name"]: p["code"] for p in PROCEDURES}
DURATION = {p["code"]: p["duration_min"] for p in PROCEDURES}


def _t(hhmm: str) -> time:
    return time.fromisoformat(hhmm)


OPEN = _t(HOURS["open"])
DAY_MINUTES = (datetime.combine(date.min, _t(HOURS["weekday_close"])) - datetime.combine(date.min, OPEN)).seconds // 60


@dataclass
class Episode:
    visit_type: str
    remaining: int
    call_on: date | None = None  # 예약이 없을 때 전화할 날 (노쇼·당일 취소 뒤 재예약, 데스크 예약 실패)
    missed: "Appt | None" = None  # 노쇼·당일 취소한 예약 — 재예약은 같은 시술 (연령 구간·연도가 바뀌면 다시 뽑음)


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
    scaling_years: set = field(default_factory=set)  # 급여 스케일링(SCALING_MIN_AGE 이상)을 받은 해


@dataclass
class Appt:
    id: int
    patient: Patient
    staff_id: int
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


class Sim:
    def __init__(self, run, rng):
        self.run, self.rng, self.cfg = run, rng, run.cfg
        self.holidays = run.holidays
        self.staff = {role: [r[0] for r in run.conn.execute("SELECT id FROM emr_staff WHERE role = ? ORDER BY id", (role,))]
                      for role in ROLE.values()}
        self.hygienist_turn = 0
        self.appts: list[Appt] = []
        self.changes_on: dict[date, list[Appt]] = {}
        self.by_day: dict[date, list[Appt]] = {}
        self.day_count: dict[date, int] = {}
        self.patients: list[Patient] = []
        for p in run.state["patients"]:
            self.patients.append(Patient(p["id"], p["birth_date"], list(run.state["arrivals"][p["id"]]),
                                         run.state["recall_due"][p["id"]], p["last_hygiene"]))
        self.surname = dict(run.conn.execute("SELECT id, surname FROM emr_families"))
        self.birth = {p.id: p.birth for p in self.patients}  # 구환 — 신환 소아의 보호자 후보
        self.phone = dict(run.conn.execute("SELECT id, phone FROM emr_patients"))
        self.new_rows: list[tuple] = []
        self.candidates = new_patient_candidates(run)

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
            if self.is_open(d) and self.day_count.get(d, 0) < daily_cap(self.cfg["visits_per_day"], d.weekday()) and d not in patient.days:
                return d
            d += timedelta(days=1)
        return None

    def pick_start(self, d: date, duration: int) -> datetime:
        """그날 duration분 예약의 시작 시각 균등 추첨"""
        starts = self.starts(d, duration)
        return starts[int(self.rng.integers(len(starts)))]

    def any_minute(self, d: date) -> datetime:
        """그날 개원~평일 마감 사이 분 단위 시각 균등 추첨 — 통화·취소 시각. 점심·토요일 마감은 따지지 않는다"""
        return datetime.combine(d, OPEN) + timedelta(minutes=int(self.rng.integers(DAY_MINUTES)))

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

    def staff_for(self, visit_type: str) -> int:
        ids = self.staff[ROLE[visit_type]]
        if visit_type != "hygiene":
            return ids[0]
        self.hygienist_turn += 1
        return ids[(self.hygienist_turn - 1) % len(ids)]

    def book(self, patient: Patient, visit_type: str, code: str, start: datetime, created: datetime, via: str,
             rescheduled_from: int | None = None) -> Appt:
        a = Appt(len(self.appts) + 1, patient, self.staff_for(visit_type), start, DURATION[code], visit_type, code,
                 via, created, rescheduled_from)
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

    def find_booking(self, patient: Patient, visit_type: str, first: date,
                     missed: "Appt | None" = None) -> tuple[str, datetime] | None:
        """first부터 첫 가능일 → 그날 기준(나이·그해 스케일링)으로 시술 → 시술 소요로 시각.
        재예약(missed)은 그날의 code_key가 놓친 예약과 같으면 같은 시술"""
        d = self.find_day(patient, first, self.run.range_end)
        if d is None:
            return None
        if missed is not None and self.code_key(patient, d) == self.code_key(patient, missed.start.date()):
            code = missed.code
        else:
            code = self.planned_code(patient, visit_type, d)
        return code, self.pick_start(d, DURATION[code])

    def phone_book(self, patient: Patient, visit_type: str, d: date, missed: "Appt | None" = None) -> Appt | None:
        """희망 리드 1~MAX_LEAD_DAYS일 뒤 첫 가능일, created_at = 그날 임의 시각"""
        created = self.any_minute(d)
        found = self.find_booking(patient, visit_type, d + timedelta(days=int(self.rng.integers(1, MAX_LEAD_DAYS + 1))),
                                  missed)
        if found is None:
            return None
        return self.book(patient, visit_type, *found, created, "phone")

    # ── 하루 ──────────────────────────────────────────────

    def process(self, d: date) -> int:
        """1. 그날 예약 처리. 데스크 예약 수를 돌려준다"""
        desk = 0
        noshow, sameday = self.cfg["noshow_rate"], self.cfg["sameday_cancel_rate"]
        for a in sorted((a for a in self.by_day.get(d, []) if a.status == "scheduled"), key=lambda a: (a.start, a.id)):
            p, r = a.patient, self.rng.random()
            p.appt = None
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
                p.ep.call_on, p.ep.missed = d + timedelta(days=int(self.rng.integers(lo, hi + 1))), a
                continue
            if a.type == "hygiene":
                p.last_hygiene = d
            if a.code == SCALING_CODE and age_on(p.birth, d) >= SCALING_MIN_AGE:
                p.scaling_years.add(d.year)
            p.recall_due = recall_due(self.rng, p.last_hygiene or d)
            p.ep.remaining -= 1
            if p.ep.remaining == 0:
                p.ep = None
                continue
            # 코스 다음 회차: 그 자리에서 데스크 예약 (created_at = 내원 종료 시각). 가능일이 없으면 내일 전화
            interval = int(self.rng.integers(COURSE_INTERVAL_DAYS[0], COURSE_INTERVAL_DAYS[1] + 1))
            found = self.find_booking(p, a.type, d + timedelta(days=interval))
            if found is None:
                p.ep.call_on = d + timedelta(days=1)
                continue
            self.book(p, a.type, *found, a.start + timedelta(minutes=a.duration), "desk")
            desk += 1
        return desk

    def due_demand(self, p: Patient, d: date) -> tuple[str, Episode] | None:
        """기한이 된 수요 중 가장 이른 것 (같으면 치료 먼저) — 예약이 잡혀야 시작하므로 여기서는 보기만"""
        arrival = p.queue[0] if p.queue and p.queue[0].day <= d else None
        recall = p.recall_due if p.recall_due is not None and p.recall_due <= d else None
        if arrival is not None and (recall is None or arrival.day <= recall):
            return "arrival", Episode(arrival.visit_type, arrival.visits)
        if recall is not None:
            return "recall", Episode("hygiene", 1)
        return None

    def phone_calls(self, d: date) -> int:
        """2. 기존 환자 전화 예약 (순서는 무작위). 잡힌 예약 수를 돌려준다. 가능일이 없으면 수요는 남고 내일 다시"""
        booked = 0
        waiting = [p for p in self.patients if p.appt is None and (
            (p.ep is not None and p.ep.call_on is not None and p.ep.call_on <= d)
            or (p.ep is None and self.due_demand(p, d) is not None))]
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
                if c["birth_date"] >= d:  # 출생 전 등록 금지 — 버림
                    continue
                family = self.pick_family(c) if c["band"] == "child" else None
                if c["band"] == "child" and family is None:
                    continue
                break
            visit_type = self.weighted(VISIT_MIX)
            visits = 1
            if visit_type == "treatment":
                mix = EPISODE_MIX[c["band"]]
                visits = 1 if self.weighted({"T1": mix["T1"], "Tn": mix["Tn"]}) == "T1" else \
                    int(self.rng.integers(COURSE_VISITS[0], COURSE_VISITS[1] + 1))
            pid = len(self.patients) + 1
            p = Patient(pid, c["birth_date"], [], None, None, Episode(visit_type, visits))
            if self.phone_book(p, visit_type, d) is None:
                return  # 빈자리가 없으면 그날 신환은 더 받지 않는다(후보는 버림)
            self.patients.append(p)
            p.queue = arrivals(self.rng, p.birth, d + timedelta(days=1), self.run.today,
                               self.cfg["treatment_demand_monthly"])
            self.register(p, c, family, d)

    def pick_family(self, c: dict) -> int | None:
        """신환 소아의 가족 = 상한 미만 가족 중 보호자가 나이 차를 만족하는 것 (균등). 보호자는 그 가족의 보호자"""
        fams = self.run.state["families"]
        ok = [fid for fid in sorted(fams) if len(fams[fid]["members"]) < FAMILY_SIZE[1]
              and self.birth[fams[fid]["guardian"]] <= add_years(c["birth_date"], -GUARDIAN_MIN_AGE_GAP)]
        return ok[int(self.rng.integers(len(ok)))] if ok else None

    def register(self, p: Patient, c: dict, family: int | None, d: date) -> None:
        guardian = None
        surname, phone = c["surname"], c["phone"]
        if family is not None:
            fam = self.run.state["families"][family]
            fam["members"].append(p.id)
            guardian, surname = fam["guardian"], self.surname[family]
            phone = self.phone[guardian]
        self.new_rows.append((p.id, f"C-{p.id:05d}", surname + c["first_name"], c["birth_date"].isoformat(), c["sex"],
                              phone, family, guardian, d.isoformat(), RECALL_CYCLE_MONTHS, c["has_private_insurance"],
                              c["note"]))

    def changes(self, d: date) -> None:
        """3. 변경: 오늘이 변경일인 예약 → 원래 날짜 뒤 1~RESCHEDULE_MAX_DAYS일의 첫 가능일로. 가능일이 없으면 변경하지 않음"""
        for a in [a for a in self.changes_on.get(d, []) if a.status == "scheduled"]:
            p = a.patient
            self.occupy(a, -1)
            orig = a.start.date()
            day = self.find_day(p, orig + timedelta(days=1), orig + timedelta(days=RESCHEDULE_MAX_DAYS))
            if day is None:
                self.occupy(a, +1)
                continue
            start = self.pick_start(day, a.duration)
            t = self.any_minute(d)
            a.status, a.cancelled = "cancelled", t
            self.book(p, a.type, a.code, start, t, "phone", rescheduled_from=a.id)

    def reminders(self, d: date) -> None:
        """4. 리마인드: 오늘 전에 잡혀 있고 오늘 취소되지 않은 다음 진료일 예약 → confirm_rate로 confirmed (날짜 단위).
        오늘 잡은 예약은 대상 아님 — 문자·확인 통화가 created_at 뒤 같은 날에 들어갈 자리가 없을 수 있어서"""
        for a in sorted(self.by_day.get(self.next_open(d), []), key=lambda a: a.id):
            if a.created.date() < d and (a.cancelled is None or a.cancelled.date() > d):
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
            " registered_at, recall_cycle_months, has_private_insurance, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            self.new_rows)
        fmt = "%Y-%m-%d %H:%M:%S"
        conn.executemany(
            "INSERT INTO emr_appointments (id, patient_id, staff_id, scheduled_at, duration_min, type,"
            " planned_procedure_code, status, rescheduled_from_id, booked_via, confirmed, created_at, cancelled_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(a.id, a.patient.id, a.staff_id, a.start.strftime(fmt), a.duration, a.type, a.code, a.status,
              a.rescheduled_from, a.booked_via, a.confirmed, a.created.strftime(fmt),
              a.cancelled.strftime(fmt) if a.cancelled else None) for a in self.appts])


def stage(run, rng, fake) -> None:
    sim = Sim(run, rng)
    sim.run_days()
    sim.write()
