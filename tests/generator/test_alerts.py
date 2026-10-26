"""4b단계 주의사항 — 실제로 생성해 정의 적재·비율·출처 규칙을 본다 (가상 데이터 생성 설계 §2.2).
필수 검사 전부는 tests/generator/test_records.py가 본다."""
import math
import sqlite3
from collections import Counter, defaultdict
from datetime import date

import pytest

from generator import build, verify
from generator.master import ALERT_TYPES, age_on
from generator.paths import ROOT

CONFIG = ROOT / "config" / "generator.yaml"


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    out = tmp_path_factory.mktemp("alerts")
    build.generate(out / "app.sqlite", out / "truth.sqlite", CONFIG, fake_llm=True)
    conn = sqlite3.connect(out / "app.sqlite")
    conn.row_factory = sqlite3.Row
    conn.execute("ATTACH DATABASE ? AS truth", (str(out / "truth.sqlite"),))
    return conn, verify.load_ctx(CONFIG, conn)


def _rows(conn, sql, *params):
    return [dict(r) for r in conn.execute(sql, params)]


def test_alert_types_loaded(db):
    conn, _ = db
    assert {r["code"]: (r["label"], r["category"]) for r in _rows(conn, "SELECT * FROM emr_alert_types")} == \
           {code: (a["label"], a["category"]) for code, a in ALERT_TYPES.items()}


@pytest.mark.parametrize("check", [verify.check_alert_target, verify.check_alert_source, verify.check_alert_recorded_at,
                                   verify.check_alert_ends, verify.check_alerts_per_patient], ids=lambda c: c.__name__)
def test_alert_checks(db, check):
    conn, ctx = db
    assert check(conn, ctx) == []


def test_intake_rates(db):
    """문진 주의사항 수 ≈ Σ(환자별 기록일 나이·성별에 맞는 비율) — 기대 30건 이상인 코드만, 4σ (환자당 3개 상한으로 조금 덜 나올 수 있음)"""
    conn, _ = db
    actual = Counter(r["code"] for r in _rows(conn, "SELECT code FROM emr_patient_alerts WHERE source = 'intake'"))
    expected = defaultdict(float)
    start = verify.load_ctx(CONFIG, conn).range_start.isoformat()
    pre = dict(conn.execute("SELECT patient_id, max(date(scheduled_at)) FROM emr_appointments"
                            " WHERE status = 'completed' AND date(scheduled_at) < ? GROUP BY patient_id", (start,)).fetchall())
    first = dict(conn.execute("SELECT patient_id, min(date(scheduled_at)) FROM emr_appointments"
                              " WHERE status = 'completed' GROUP BY patient_id").fetchall())
    for p in _rows(conn, "SELECT id, birth_date, sex, registered_at FROM emr_patients"):
        # 문진 기록일 — 신환은 첫 내원일(내원한 적 없으면 문진 없음), 구환은 창 이전 내원일
        recorded = first.get(p["id"]) if p["registered_at"] >= start else pre.get(p["id"], p["registered_at"])
        if recorded is None:
            continue
        day = date.fromisoformat(recorded)
        age = age_on(date.fromisoformat(p["birth_date"]), day)
        for code, a in ALERT_TYPES.items():
            for lo, hi, sex, rate in a["rules"]:
                if lo <= age and (hi is None or age <= hi) and sex in (None, p["sex"]):
                    expected[code] += rate
    checked = [c for c, e in expected.items() if e >= 30]
    assert checked
    for code in checked:
        assert abs(actual[code] - expected[code]) <= 4 * math.sqrt(expected[code]), (code, actual[code], expected[code])


def test_visit_alerts(db):
    """진료 중 발견 = 관련 시술을 창 안에서 받은 환자 중 비율 (4σ)"""
    conn, ctx = db
    visits = _rows(conn, "SELECT patient_id, planned_procedure_code AS code FROM emr_appointments"
                         " WHERE status = 'completed' AND date(scheduled_at) >= ?", ctx.range_start.isoformat())
    actual = Counter(r["code"] for r in _rows(conn, "SELECT code FROM emr_patient_alerts WHERE source = 'visit'"))
    for code, a in ALERT_TYPES.items():
        if a["source"] != "visit":
            continue
        eligible = len({v["patient_id"] for v in visits if v["code"] in a["procedures"]})
        e = eligible * a["rate"]
        assert abs(actual[code] - e) <= 4 * math.sqrt(e) + 1, (code, actual[code], e)
    assert actual["ANES_FAIL"] > 0


def test_some_patients_have_several_alerts(db):
    conn, _ = db
    per = Counter(r["patient_id"] for r in _rows(conn, "SELECT patient_id FROM emr_patient_alerts"))
    assert max(per.values()) >= 2


def test_lasting_alerts_end(db):
    """임신·협조 어려운 소아는 끝나는 날이 있고, 준비표 범위(TODAY)에는 끝난 것도 있다"""
    conn, ctx = db
    rows = _rows(conn, "SELECT code, ends_at FROM emr_patient_alerts WHERE code IN ('PREGNANT', 'CHILD_COOP')")
    assert rows and all(r["ends_at"] for r in rows)
    assert any(r["ends_at"] <= ctx.today.isoformat() for r in rows)


def test_never_visited_new_patients_have_no_intake(db):
    conn, ctx = db
    assert _rows(conn, "SELECT a.id FROM emr_patient_alerts a JOIN emr_patients p ON p.id = a.patient_id"
                       " WHERE p.registered_at >= ? AND NOT EXISTS (SELECT 1 FROM emr_appointments x"
                       " WHERE x.patient_id = p.id AND x.status = 'completed')", ctx.range_start.isoformat()) == []
