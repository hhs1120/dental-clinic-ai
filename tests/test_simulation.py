"""4단계 예약 시뮬레이션 — small을 실제로 생성해 예약만 보는 필수 검사와 시뮬레이션 규칙을 본다 (가상 데이터 생성 설계 §2.6 (3))."""
import sqlite3
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

import numpy as np
import pytest
import yaml

from generator import build, schedule, verify
from generator.master import (CHECKUP_CODE, COURSE_INTERVAL_DAYS, FAMILY_SIZE, GUARDIAN_MIN_AGE_GAP,
                              HYGIENE_SCALING_BANDS, MAX_LEAD_DAYS, PLANNED_WEIGHTS, PROCEDURES, RESCHEDULE_MAX_DAYS, SAMEDAY_CANCEL_LEAD_MIN,
                              SCALING_CODE, SCALING_MIN_AGE, add_years, age_band, age_on)
from generator.paths import ROOT
from generator.seeding import stage_faker, stage_rng

CONFIG = ROOT / "config" / "generator.yaml"
CFG = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
DURATION = {p["code"]: p["duration_min"] for p in PROCEDURES}
ROLE = {"treatment": "doctor", "hygiene": "hygienist", "consult": "consultant"}

# 예약만 보는 필수 검사 — 진료·수납·청구(5단계)가 필요한 검사는 묶음 4(기능 정의 §9 준비 2)에서
APPOINTMENT_CHECKS = [
    verify.check_foreign_keys, verify.check_status_from_today, verify.check_timestamps_not_future,
    verify.check_patient_same_day, verify.check_confirmed_not_future, verify.check_confirmed_reminded,
    verify.check_created_before_scheduled, verify.check_cancel_time, verify.check_cancelled_at_status,
    verify.check_cancellation_kind, verify.check_reschedule_pair,
    verify.check_fake_phone, verify.check_pre_window_visit,
]


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    out = tmp_path_factory.mktemp("sim")
    app, truth = out / "app.sqlite", out / "truth.sqlite"
    run = build.generate("small", app, truth, CONFIG)
    conn = sqlite3.connect(app)
    conn.row_factory = sqlite3.Row
    conn.execute("ATTACH DATABASE ? AS truth", (str(truth),))
    return conn, verify.load_ctx(CONFIG, conn), run


@pytest.fixture(scope="module")
def large(tmp_path_factory):
    """small(8~12월)보다 긴 창이 필요한 규칙용 — 성인 한 해 2회 위생 내원 (리콜 12개월이라 large에도 드묾, 생성 1~2초)"""
    out = tmp_path_factory.mktemp("sim_large")
    build.generate("large", out / "app.sqlite", out / "truth.sqlite", CONFIG)
    conn = sqlite3.connect(out / "app.sqlite")
    conn.row_factory = sqlite3.Row
    return conn


def _appts(conn):
    rows = [dict(r) for r in conn.execute("SELECT a.*, s.role FROM emr_appointments a"
                                          " JOIN emr_staff s ON s.id = a.staff_id ORDER BY a.id")]
    for r in rows:
        r["start"] = datetime.fromisoformat(r["scheduled_at"])
        r["created"] = datetime.fromisoformat(r["created_at"])
        r["cancelled"] = datetime.fromisoformat(r["cancelled_at"]) if r["cancelled_at"] else None
    return rows


def _window(conn, ctx):
    """창 안 예약 — 창 이전 내원(scheduled_at < range_start)은 test_pre_window_visit에서"""
    return [r for r in _appts(conn) if r["start"].date() >= ctx.range_start]


def _patients(conn):
    return {r["id"]: dict(r) for r in conn.execute("SELECT * FROM emr_patients")}


@pytest.mark.parametrize("check", APPOINTMENT_CHECKS, ids=lambda c: c.__name__)
def test_required_appointment_checks(db, check):
    conn, ctx, _ = db
    assert check(conn, ctx) == []


def test_window_and_today_snapshot(db):
    conn, ctx, _ = db
    rows = _window(conn, ctx)
    assert len(rows) > 1000
    assert all(ctx.range_start <= r["start"].date() <= ctx.range_end for r in rows)
    # TODAY 전 예약은 모두 처리됨(scheduled 없음), TODAY 이후는 scheduled/cancelled(필수 검사)
    assert not [r for r in rows if r["start"].date() < ctx.today and r["status"] == "scheduled"]
    assert {r["status"] for r in rows} == {"completed", "noshow", "cancelled", "scheduled"}


def test_staff_role_and_duration(db):
    conn, _, _ = db
    rows = _appts(conn)
    assert all(ROLE[r["type"]] == r["role"] for r in rows)
    assert all(r["duration_min"] == DURATION[r["planned_procedure_code"]] for r in rows)
    hygienists = Counter(r["staff_id"] for r in rows if r["type"] == "hygiene")
    assert len(hygienists) == 3 and max(hygienists.values()) - min(hygienists.values()) <= 1  # 순환


def test_planned_procedure_fits_age(db):
    conn, _, _ = db
    pts = _patients(conn)
    by_code = {p["code"]: p for p in PROCEDURES}
    scaling_years = Counter()
    for r in _appts(conn):
        age = age_on(date.fromisoformat(pts[r["patient_id"]]["birth_date"]), r["start"].date())
        proc = by_code[r["planned_procedure_code"]]
        if proc["age_group"] == "child":
            assert age_band(age) == "child"
        if r["type"] == "hygiene":
            assert proc["category"] == "preventive"
        if r["planned_procedure_code"] == SCALING_CODE and age >= SCALING_MIN_AGE and r["status"] == "completed":
            scaling_years[(r["patient_id"], r["start"].year)] += 1
    assert scaling_years and max(scaling_years.values()) == 1  # 급여 스케일링 연 1회


def test_planned_code_hygiene_scaling_rule():
    run = build.new_run(sqlite3.connect(":memory:"), CFG, "small")
    run.conn.executescript(build.APP_SCHEMA.read_text(encoding="utf-8"))
    for name, fn in build.STAGES[:3]:
        fn(run, stage_rng(run.seed, name), stage_faker(run.seed, name))
    sim = schedule.Sim(run, np.random.default_rng(0))
    on = date(2026, 9, 1)
    adult = schedule.Patient(0, date(1980, 1, 1), [], None, None)
    assert sim.planned_code(adult, "hygiene", on) == SCALING_CODE
    adult.scaling_years.add(2026)
    assert sim.planned_code(adult, "hygiene", on) == CHECKUP_CODE
    assert sim.planned_code(adult, "hygiene", date(2027, 1, 5)) == SCALING_CODE  # 해가 바뀌면 다시 스케일링
    names = {p["code"]: p["name"] for p in PROCEDURES}
    child = schedule.Patient(0, date(2020, 1, 1), [], None, None)
    assert {names[sim.planned_code(child, "hygiene", on)] for _ in range(50)} == set(PLANNED_WEIGHTS["hygiene"]["child"])


def test_adult_checkup_only_after_scaling_that_year(large):
    """리콜 12개월이라 성인 2회째 위생 내원은 드물다 — 있으면 검진이어야 한다는 불변식만 본다.
    규칙 자체는 test_planned_code_hygiene_scaling_rule"""
    pts = _patients(large)
    scaled = set()
    for r in sorted(_appts(large), key=lambda r: r["start"]):
        age = age_on(date.fromisoformat(pts[r["patient_id"]]["birth_date"]), r["start"].date())
        if r["type"] != "hygiene" or age < SCALING_MIN_AGE:
            continue
        key = (r["patient_id"], r["start"].year)
        if r["planned_procedure_code"] == CHECKUP_CODE:
            assert key in scaled
        if r["planned_procedure_code"] == SCALING_CODE and r["status"] == "completed":
            scaled.add(key)


def test_rebook_keeps_missed_procedure(db):
    """노쇼·당일 취소 뒤 재예약은 같은 시술 (연령 구간·연도가 바뀌면 다시 뽑음)"""
    conn, _, _ = db
    pts = _patients(conn)
    by_patient = defaultdict(list)
    for r in _appts(conn):
        by_patient[r["patient_id"]].append(r)
    checked = 0
    for rows in by_patient.values():
        for m in rows:
            missed = m["status"] == "noshow" or (m["cancelled"] and m["cancelled"].date() == m["start"].date())
            later = [n for n in rows if n["created"] > (m["cancelled"] or m["start"])]
            if not missed or not later:
                continue
            n = min(later, key=lambda n: n["created"])
            birth = date.fromisoformat(pts[m["patient_id"]]["birth_date"])
            key = lambda r: (age_band(age_on(birth, r["start"].date())), r["start"].year)
            assert n["type"] == m["type"]
            if key(n) == key(m):
                assert n["planned_procedure_code"] == m["planned_procedure_code"]
                checked += 1
    assert checked > 0


def test_desk_booking_follows_completed_visit(db):
    conn, _, _ = db
    rows = _appts(conn)
    ends = {(r["patient_id"], r["start"] + timedelta(minutes=r["duration_min"]))
            for r in rows if r["status"] == "completed"}
    desk = [r for r in rows if r["booked_via"] == "desk" and r["rescheduled_from_id"] is None]
    assert desk
    for r in desk:
        assert (r["patient_id"], r["created"]) in ends  # 내원 종료 시각에 데스크에서
        assert r["type"] == "treatment"
        assert (r["start"].date() - r["created"].date()).days >= COURSE_INTERVAL_DAYS[0]


def test_phone_booking_time_and_lead(db):
    conn, ctx, _ = db
    for r in _appts(conn):
        if r["booked_via"] != "phone":
            continue
        assert r["created"].date() not in ctx.holidays
        if r["rescheduled_from_id"] is None:
            assert (r["start"].date() - r["created"].date()).days >= 1


def test_reschedule_pairs(db):
    conn, _, _ = db
    rows = {r["id"]: r for r in _appts(conn)}
    pairs = [(rows[r["rescheduled_from_id"]], r) for r in rows.values() if r["rescheduled_from_id"]]
    assert pairs
    for old, new in pairs:
        assert (new["patient_id"], new["type"], new["planned_procedure_code"]) == \
            (old["patient_id"], old["type"], old["planned_procedure_code"])
        assert new["booked_via"] == "phone"
        assert 1 <= (new["start"].date() - old["start"].date()).days <= RESCHEDULE_MAX_DAYS
        assert old["cancelled"].date() < old["start"].date()  # 변경일은 예약 전날까지


def test_sameday_cancel_shortly_before_start(db):
    """당일 취소 시각 = 예약 1~SAMEDAY_CANCEL_LEAD_MIN분 전 (영업시간과 무관 — 첫 슬롯 예약도 당일 취소 가능)"""
    conn, _, _ = db
    sameday = [r for r in _appts(conn) if r["cancelled"] and r["cancelled"].date() == r["start"].date()]
    assert sameday
    assert all(timedelta(minutes=1) <= r["start"] - r["cancelled"] <= timedelta(minutes=SAMEDAY_CANCEL_LEAD_MIN)
               for r in sameday)


def test_one_open_appointment_per_patient(db):
    """환자당 열린 수요 1개 — 예약을 잡는 날 그 환자의 다른 예약이 아직 끝나지 않았으면 안 된다(변경 쌍 제외).
    같은 날은 시각 순서를 따지지 않는다(그날 내원한 환자가 같은 날 다음 예약 전화를 할 수 있음)"""
    conn, _, _ = db
    by_patient = defaultdict(list)
    for r in _appts(conn):
        r["resolved"] = r["cancelled"] or r["start"]
        by_patient[r["patient_id"]].append(r)
    for rows in by_patient.values():
        for a in rows:
            for b in rows:
                if b is a or b["id"] == a["rescheduled_from_id"]:
                    continue
                assert not (b["created"] < a["created"] and a["created"].date() < b["resolved"].date()), (a["id"], b["id"])


def test_new_patients(db):
    conn, ctx, run = db
    pts = _patients(conn)
    first = {}
    for r in _appts(conn):
        first.setdefault(r["patient_id"], r)
    new = [p for p in pts.values() if date.fromisoformat(p["registered_at"]) >= ctx.range_start]
    assert new
    assert all(p["chart_no"] == f"C-{p['id']:05d}" for p in pts.values())
    family_size = Counter(p["family_id"] for p in pts.values() if p["family_id"])
    assert max(family_size.values()) <= FAMILY_SIZE[1]
    for p in new:
        a = first[p["id"]]
        # 신환 정의: registered_at = 첫 예약 created_at 날짜, 전화 예약
        assert a["created"].date() == date.fromisoformat(p["registered_at"]) and a["booked_via"] == "phone"
        birth = date.fromisoformat(p["birth_date"])
        assert birth < date.fromisoformat(p["registered_at"])
        if age_band(age_on(birth, ctx.today)) == "child":
            g = pts[p["guardian_patient_id"]]
            assert g["family_id"] == p["family_id"] and g["phone"] == p["phone"]
            assert date.fromisoformat(g["registered_at"]) < ctx.range_start  # 구환 보호자
            assert date.fromisoformat(g["birth_date"]) <= add_years(birth, -GUARDIAN_MIN_AGE_GAP)
            assert run.state["families"][p["family_id"]]["guardian"] == g["id"]


def test_lapsed_patients_skip_first_recall(db):
    conn, ctx, run = db
    lapsed = {pid for pid, d in run.state["recall_due"].items() if d is None}
    first = {}
    for r in _window(conn, ctx):
        first.setdefault(r["patient_id"], r)
    seen = [pid for pid in lapsed if pid in first]
    assert seen and all(first[pid]["type"] != "hygiene" for pid in seen)


def test_phone_lead_spreads_early_days(db):
    """시작 직후 수요가 첫날에 몰리지 않음 — 첫 진료일 예약은 리드 1일 이상이라 0, 첫 MAX_LEAD_DAYS 안에 분산"""
    conn, ctx, _ = db
    days = Counter(r["start"].date() for r in _appts(conn) if r["status"] != "cancelled")
    assert days[ctx.range_start] == 0
    early = [days[ctx.range_start + timedelta(days=k)] for k in range(1, MAX_LEAD_DAYS + 1)]
    assert sum(early) > 0


def test_pre_window_visit(db):
    """구환마다 창 이전 마지막 위생 내원 1행 — 3단계 리콜의 기준일 근처 진료일, 등록 뒤 전화 예약 (가상 데이터 생성 설계 §2.6 (1))"""
    conn, ctx, run = db
    pts = _patients(conn)
    last = {p["id"]: p["last_hygiene"] for p in run.state["patients"]}
    pre = [r for r in _appts(conn) if r["start"].date() < ctx.range_start]
    assert Counter(r["patient_id"] for r in pre) == Counter(last.keys())  # 구환마다 정확히 1행
    names = {p["code"]: p["name"] for p in PROCEDURES}
    for r in pre:
        d, p = r["start"].date(), pts[r["patient_id"]]
        reg = date.fromisoformat(p["registered_at"])
        assert (r["type"], r["status"], r["booked_via"], r["confirmed"]) == ("hygiene", "completed", "phone", 0)
        assert reg < d and reg <= r["created"].date() < d
        assert abs((d - last[r["patient_id"]]).days) <= 7
        band = age_band(age_on(date.fromisoformat(p["birth_date"]), d))
        if band in HYGIENE_SCALING_BANDS:
            assert r["planned_procedure_code"] == SCALING_CODE
        else:
            assert names[r["planned_procedure_code"]] in PLANNED_WEIGHTS["hygiene"][band]
    assert verify.check_pre_window_visit(conn, ctx) == []


def test_pre_window_scaling_counts_toward_yearly_limit():
    """창 이전 급여 스케일링은 그해를 연 1회 이력에 넣는다 — 같은 해 창 안 위생 내원은 구강검진 (§2.6 (1)).
    리콜 12개월이라 생성 데이터에는 같은 해 재방문이 거의 없어 Sim 상태로 본다"""
    run = build.new_run(sqlite3.connect(":memory:"), CFG, "small")
    run.conn.executescript(build.APP_SCHEMA.read_text(encoding="utf-8"))
    for name, fn in build.STAGES[:4]:  # master·patients·demand·pre_window
        fn(run, stage_rng(run.seed, name), stage_faker(run.seed, name))
    sim = schedule.Sim(run, np.random.default_rng(0))
    visits = run.state["pre_window"]
    scaled = [p for p in sim.patients if p.id in visits and visits[p.id]["code"] == SCALING_CODE
              and age_on(p.birth, visits[p.id]["start"].date()) >= SCALING_MIN_AGE]
    assert scaled
    for p in scaled:
        year = visits[p.id]["start"].year
        assert year in p.scaling_years
        assert sim.planned_code(p, "hygiene", date(year, 12, 31)) == CHECKUP_CODE
