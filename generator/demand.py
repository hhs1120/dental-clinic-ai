"""3단계 첫 수요 — 구환의 첫 리콜 기한과 창 전체의 치료 수요 발생 (가상 데이터 생성 설계 §2.6 (1)(2)).
DB에 쓰지 않고 run.state에 둔다. 4단계는 열린 수요가 있으면 새 수요를 그 뒤로 미루기만 한다 —
수요 발생을 자기 시드로 미리 뽑아 두면 예약 파라미터를 바꿔도 수요는 그대로다."""

from datetime import date, timedelta
from typing import NamedTuple

from generator.master import (
    COURSE_VISITS,
    EPISODE_MIX,
    MAX_LEAD_DAYS,
    RECALL_CYCLE,
    RECALL_JITTER_DAYS,
    TOOTH_TREATMENT_MIN_AGE,
    add_years,
    age_band,
    age_on,
)

DAYS_PER_MONTH = 365.25 / 12


class Arrival(NamedTuple):
    day: date
    visit_type: str  # treatment | consult
    visits: int  # T1·상담 1, Tn COURSE_VISITS


def recall_due(rng, last_hygiene: date) -> date:
    return (
        last_hygiene
        + RECALL_CYCLE
        + timedelta(days=int(rng.integers(-RECALL_JITTER_DAYS, RECALL_JITTER_DAYS + 1)))
    )


def draw_episode(rng, band: str) -> tuple[str, int]:
    kinds = list(EPISODE_MIX[band])
    kind = kinds[int(rng.choice(len(kinds), p=[EPISODE_MIX[band][k] for k in kinds]))]
    if kind == "consult":
        return "consult", 1
    if kind == "T1":
        return "treatment", 1
    return "treatment", int(rng.integers(COURSE_VISITS[0], COURSE_VISITS[1] + 1))


def arrivals(rng, birth: date, start: date, end: date, rates: dict) -> list[Arrival]:
    """[start, end) 안의 치료 수요 — 그날 나이 구간의 월 발생률(rates)로 포아송 과정. 생일마다 발생률을 다시 본다(무기억성).
    TOOTH_TREATMENT_MIN_AGE세 미만은 발생률 0"""
    out, t = [], 0.0  # start로부터의 일수(실수)
    total = (end - start).days
    while t < total:
        day = start + timedelta(days=int(t))
        age = age_on(birth, day)
        band = age_band(age)
        # 다음 생일. 2월 29일생은 평년 add_years가 2/28을 주는데 age_on은 그날을 아직 생일 전으로 본다 → 최소 하루 뒤
        boundary = min(max((add_years(birth, age + 1) - start).days, int(t) + 1), total)
        per_day = rates[band] / DAYS_PER_MONTH if age >= TOOTH_TREATMENT_MIN_AGE else 0
        t_next = t + (rng.exponential(1 / per_day) if per_day > 0 else float("inf"))
        if t_next >= boundary:
            t = float(boundary)
            continue
        t = t_next
        out.append(Arrival(start + timedelta(days=int(t)), *draw_episode(rng, band)))
    return out


def stage(run, rng, fake) -> None:
    people = run.state["patients"]
    lapsed = set(
        rng.choice(len(people), round(run.cfg["lapse_rate"] * len(people)), replace=False).tolist()
    )
    rates = run.cfg["treatment_demand_monthly"]
    due, arr = {}, {}
    for k, p in enumerate(people):
        d = recall_due(rng, p["last_hygiene"])
        if d < run.range_start:  # 이미 지난 기한은 첫날 몰리지 않게 첫 리드 기간에 나눠 받는다
            d = run.range_start + timedelta(days=int(rng.integers(MAX_LEAD_DAYS)))
        due[p["id"]] = None if k in lapsed else d
        arr[p["id"]] = arrivals(rng, p["birth_date"], run.range_start, run.today, rates)
    run.state["recall_due"] = due
    run.state["arrivals"] = arr
