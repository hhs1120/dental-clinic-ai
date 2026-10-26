"""1~3단계 — 마스터, 환자(구환·신환 후보), 첫 수요 (가상 데이터 생성 설계 §1·§2.2·§2.6 (1))."""
import re
import sqlite3
from collections import Counter
from datetime import date, timedelta

import numpy as np
import pytest
import yaml

from generator import build, clinic, demand, patients
from generator.master import (AGE_BANDS, COURSE_VISITS, EPISODE_MIX, FAMILY_SHARE, FAMILY_SIZE,
                              GUARDIAN_MIN_AGE_GAP, MAX_LEAD_DAYS, PRIVATE_INSURANCE_SHARE,
                              PROCEDURES, RECALL_CYCLE, RECALL_CYCLE_MONTHS, RECALL_JITTER_DAYS, REGISTERED_FROM, STAFF,
                              add_years, age_band, age_on)
from generator.paths import ROOT
from generator.seeding import stage_faker, stage_rng

CFG = yaml.safe_load((ROOT / "config" / "generator.yaml").read_text(encoding="utf-8"))
N = CFG["returning_patients"]
TODAY = CFG["today"]
START = CFG["range"]["start"]


def _new_run():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(build.APP_SCHEMA.read_text(encoding="utf-8"))
    return build.new_run(conn, CFG)


def _stage(run, name, fn):
    fn(run, stage_rng(run.seed, name), stage_faker(run.seed, name))


@pytest.fixture(scope="module")
def run():
    run = _new_run()
    for name, fn in build.STAGES[:3]:
        _stage(run, name, fn)
    return run


def test_stages_1_to_3_registered_in_order():
    assert [name for name, _ in build.STAGES[:3]] == ["master", "patients", "demand"]
    assert [fn for _, fn in build.STAGES[:3]] == [clinic.stage, patients.stage, demand.stage]


# ── 1단계 마스터 ──────────────────────────────────────────────

def test_master_staff_and_procedures(run):
    roles = Counter(r["role"] for r in run.conn.execute("SELECT role FROM emr_staff"))
    assert roles == Counter(dict(STAFF))
    assert run.conn.execute("SELECT count(*) FROM emr_procedures").fetchone()[0] == len(PROCEDURES)
    assert run.conn.execute("SELECT count(*) FROM emr_clinic").fetchone()[0] == 1


# ── 2단계 환자 ──────────────────────────────────────────────

def _patients(run):
    return [dict(r) for r in run.conn.execute("SELECT * FROM emr_patients ORDER BY id")]


def test_returning_patient_count_and_chart_order(run):
    rows = _patients(run)
    assert len(rows) == N
    assert [r["chart_no"] for r in rows] == [f"C-{i:05d}" for i in range(1, N + 1)]
    assert [r["id"] for r in rows] == list(range(1, N + 1))
    regs = [r["registered_at"] for r in rows]
    assert regs == sorted(regs)  # 차트 번호는 등록순


def test_registration_before_last_hygiene_and_window(run):
    last = {p["id"]: p["last_hygiene"] for p in run.state["patients"]}
    for r in _patients(run):
        reg, birth = date.fromisoformat(r["registered_at"]), date.fromisoformat(r["birth_date"])
        assert REGISTERED_FROM <= reg < last[r["id"]] < START
        assert birth < reg
        assert last[r["id"]] >= START - RECALL_CYCLE


def test_age_bands_exact_shares(run):
    counts = Counter(age_band(age_on(date.fromisoformat(r["birth_date"]), TODAY)) for r in _patients(run))
    for name, *_, share in AGE_BANDS:
        assert abs(counts[name] / N - share) <= 1 / N


def test_families(run):
    rows = _patients(run)
    members = Counter(r["family_id"] for r in rows if r["family_id"] is not None)
    assert all(FAMILY_SIZE[0] <= n <= FAMILY_SIZE[1] for n in members.values())
    assert abs(sum(members.values()) - round(FAMILY_SHARE * N)) <= 1
    assert run.conn.execute("SELECT count(*) FROM emr_families").fetchone()[0] == len(members)
    fams = run.state["families"]
    assert {fid: sorted(f["members"]) for fid, f in fams.items()} == \
        {fid: sorted(r["id"] for r in rows if r["family_id"] == fid) for fid in members}


def test_family_guardian_surname_and_age_gap(run):
    by_id = {r["id"]: r for r in _patients(run)}
    surname = dict(run.conn.execute("SELECT id, surname FROM emr_families"))
    for fid, f in run.state["families"].items():
        g = by_id[f["guardian"]]
        assert g["name"].startswith(surname[fid])
        for pid in f["members"]:
            m = by_id[pid]
            if age_band(age_on(date.fromisoformat(m["birth_date"]), TODAY)) in ("child", "teen"):
                assert m["name"].startswith(surname[fid])  # 소아·청소년은 가족 성
                # 보호자는 소아·청소년보다 GUARDIAN_MIN_AGE_GAP세 이상 많다
                assert date.fromisoformat(g["birth_date"]) <= add_years(date.fromisoformat(m["birth_date"]),
                                                                         -GUARDIAN_MIN_AGE_GAP)


def test_children_have_adult_guardian_same_family_surname_phone(run):
    by_id = {r["id"]: r for r in _patients(run)}
    surname = dict(run.conn.execute("SELECT id, surname FROM emr_families"))
    for r in by_id.values():
        band = age_band(age_on(date.fromisoformat(r["birth_date"]), TODAY))
        if band != "child":
            assert r["guardian_patient_id"] is None
            continue
        g = by_id[r["guardian_patient_id"]]
        assert age_band(age_on(date.fromisoformat(g["birth_date"]), TODAY)) == "adult"
        assert r["family_id"] == g["family_id"] is not None
        assert r["phone"] == g["phone"]
        assert r["name"].startswith(surname[r["family_id"]]) and g["name"].startswith(surname[r["family_id"]])


def test_phones_fake_and_unique_except_children(run):
    rows = _patients(run)
    assert all(re.fullmatch(r"010-0000-\d{4}", r["phone"]) for r in rows)
    own = [r["phone"] for r in rows if r["guardian_patient_id"] is None]
    assert len(own) == len(set(own))


def test_flags_exact_shares(run):
    rows = _patients(run)
    assert sum(r["has_private_insurance"] for r in rows) == round(PRIVATE_INSURANCE_SHARE * N)
    assert "note" not in rows[0]  # 주의사항은 emr_patient_alerts(4b단계)로 — 가상 데이터 생성 설계 §2.2
    assert {r["recall_cycle_months"] for r in rows} == {RECALL_CYCLE_MONTHS}
    assert {r["sex"] for r in rows} == {"M", "F"}


def test_new_patient_candidates_follow_age_bands_and_new_phones():
    # 남은 번호를 소비하므로 모듈 공용 run과 따로
    run = _new_run()
    for name, fn in build.STAGES[:2]:
        _stage(run, name, fn)
    taken = {r["phone"] for r in _patients(run)}
    pool = patients.new_patient_candidates(run)
    cands = [next(pool) for _ in range(2000)]
    assert not {c["phone"] for c in cands} & taken
    assert len({c["phone"] for c in cands}) == len(cands)
    counts = Counter(c["band"] for c in cands)
    for name, *_, share in AGE_BANDS:
        assert counts[name] / len(cands) == pytest.approx(share, abs=0.03)
    assert all(age_band(age_on(c["birth_date"], TODAY)) == c["band"] for c in cands)


# ── 3단계 첫 수요 ──────────────────────────────────────────────

def test_first_recall_due(run):
    due = run.state["recall_due"]
    last = {p["id"]: p["last_hygiene"] for p in run.state["patients"]}
    lapsed = [pid for pid, d in due.items() if d is None]
    assert set(due) == set(last)
    assert len(lapsed) == round(CFG["lapse_rate"] * N)
    for pid, d in due.items():
        if d is None:
            continue
        # 지터로 START 앞이 된 기한은 첫 리드 기간에 나눠 받음 — 나머지는 마지막 위생 + 주기 ± 지터 그대로
        assert d >= START
        assert abs((d - (last[pid] + RECALL_CYCLE)).days) <= RECALL_JITTER_DAYS or d < START + timedelta(days=MAX_LEAD_DAYS)


def test_treatment_arrivals_in_window_sorted(run):
    arrivals = run.state["arrivals"]
    assert set(arrivals) == {p["id"] for p in run.state["patients"]}
    for items in arrivals.values():
        days = [a.day for a in items]
        assert days == sorted(days) and all(START <= d < TODAY for d in days)
        for a in items:
            assert a.visit_type in ("treatment", "consult")
            assert COURSE_VISITS[0] <= a.visits <= COURSE_VISITS[1] or a.visits == 1
            assert a.visit_type == "treatment" or a.visits == 1


def test_arrival_rate_matches_config():
    rng = np.random.default_rng(0)
    rates = {"child": 0.0, "teen": 0.0, "adult": 0.5, "senior": 0.0}
    birth, start, end = date(1980, 1, 1), date(2026, 1, 1), date(2027, 1, 1)
    counts = [len(demand.arrivals(rng, birth, start, end, rates)) for _ in range(400)]
    assert np.mean(counts) == pytest.approx(0.5 * 12, rel=0.05)
    # 연령 구간이 바뀌면 그날부터 새 발생률 — 2026-07-01에 19세(청소년 → 성인)
    counts = [len(demand.arrivals(rng, date(2007, 7, 1), start, end, rates)) for _ in range(400)]
    assert np.mean(counts) == pytest.approx(0.5 * 6, rel=0.08)


def test_arrivals_leap_day_birth_terminates():
    # 2월 29일생: 평년의 다음 생일 경계가 앞으로 나아가야 한다 (무한 루프 회귀)
    rates = {b: 0.1 for b, *_ in AGE_BANDS}
    out = demand.arrivals(np.random.default_rng(0), date(2000, 2, 29), date(2026, 1, 1), date(2027, 1, 1), rates)
    assert all(date(2026, 1, 1) <= a.day < date(2027, 1, 1) for a in out)


@pytest.mark.parametrize("band", [b for b, *_ in AGE_BANDS])
def test_episode_mix(band):
    rng = np.random.default_rng(1)
    draws = [demand.draw_episode(rng, band) for _ in range(20000)]
    kinds = Counter("consult" if t == "consult" else ("T1" if v == 1 else "Tn") for t, v in draws)
    for kind, share in EPISODE_MIX[band].items():
        assert kinds[kind] / len(draws) == pytest.approx(share, abs=0.015)
    assert {v for t, v in draws if t == "treatment" and v > 1} == set(range(COURSE_VISITS[0], COURSE_VISITS[1] + 1))
