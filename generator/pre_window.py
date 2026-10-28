"""3b단계 창 이전 내원 — 구환의 창 이전 마지막 위생 내원 1행 (가상 데이터 생성 설계 §2.6 (1)).
2단계가 고른 날짜(run.state)를 그 근처 진료일로 맞추고 시술·시각·예약 시각을 뽑는다. 행은 4단계가 쓴다
(담당 위생사 순환을 이어 가고, 그해 급여 스케일링을 연 1회 이력에 넣으려고).
2·3단계 결과를 바꾸지 않게 자기 시드로 따로 뽑는다 — 리콜 기한(3단계)은 2단계 날짜 그대로라 며칠 어긋날 수 있다(지터 안).
"""

from datetime import date, datetime, timedelta

from generator.master import (
    HYGIENE_SCALING_BANDS,
    MAX_LEAD_DAYS,
    PLANNED_WEIGHTS,
    PROCEDURES,
    SCALING_CODE,
    age_band,
    age_on,
    business_minutes,
    open_slots,
)
from generator.schedule import DURATION, OPEN, Sim

CODE = {p["name"]: p["code"] for p in PROCEDURES}


def _is_open(d: date, holidays) -> bool:
    return open_slots(d.weekday()) > 0 and d not in holidays


def visit_day(last: date, registered: date, range_start: date, holidays) -> date:
    """last 이전의 가장 가까운 진료일(등록일 뒤) — 없으면 last 이후 창 시작 전 첫 진료일, 그것도 없으면 last"""
    d = last
    while d > registered:
        if _is_open(d, holidays):
            return d
        d -= timedelta(days=1)
    d = last + timedelta(days=1)
    while d < range_start:
        if _is_open(d, holidays):
            return d
        d += timedelta(days=1)
    return last


def stage(run, rng, fake) -> None:
    registered = {
        pid: date.fromisoformat(r)
        for pid, r in run.conn.execute("SELECT id, registered_at FROM emr_patients")
    }
    visits = {}
    for p in run.state["patients"]:
        d = visit_day(p["last_hygiene"], registered[p["id"]], run.range_start, run.holidays)
        band = age_band(age_on(p["birth_date"], d))
        if band in HYGIENE_SCALING_BANDS:
            code = SCALING_CODE
        else:
            weights = PLANNED_WEIGHTS["hygiene"][band]
            names = list(weights)
            total = sum(weights.values())
            code = CODE[names[int(rng.choice(len(names), p=[weights[n] / total for n in names]))]]
        starts = Sim.starts(d, DURATION[code])
        start = starts[int(rng.integers(len(starts)))]
        # 전화 예약: 등록일 이후 · 내원 1~MAX_LEAD_DAYS일 전의 진료일 (없으면 등록일)
        days = [d - timedelta(days=k) for k in range(1, MAX_LEAD_DAYS + 1)]
        days = [x for x in days if x >= registered[p["id"]] and _is_open(x, run.holidays)]
        booked = days[int(rng.integers(len(days)))] if days else registered[p["id"]]
        created = datetime.combine(booked, OPEN) + timedelta(
            minutes=int(rng.integers(business_minutes(booked.weekday())))
        )
        visits[p["id"]] = {"code": code, "start": start, "created": created}
    run.state["pre_window"] = visits
