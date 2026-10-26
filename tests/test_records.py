"""5단계 진료·수납·청구·심사 결과 — small을 실제로 생성해 필수 검사 전부와 생성 규칙을 본다 (가상 데이터 생성 설계 §2.4·§2.6 (4))."""
import sqlite3
from collections import Counter, defaultdict
from datetime import date, datetime

import pytest
import yaml

from generator import build, verify
from generator.master import (ADJUSTED_PAID_RANGE, FULL_MOUTH, PAYMENT_METHOD_WEIGHTS, PROCEDURES, SCALING_CODE,
                              TOOTH_CHOICES, UNPAID_PAID_RANGE)
from generator.paths import ROOT

CONFIG = ROOT / "config" / "generator.yaml"
CFG = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
TOOTH_LEVEL = {p["code"] for p in PROCEDURES if p["is_tooth_level"]}


def _open(out):
    conn = sqlite3.connect(out / "app.sqlite")
    conn.row_factory = sqlite3.Row
    conn.execute("ATTACH DATABASE ? AS truth", (str(out / "truth.sqlite"),))
    return conn


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    out = tmp_path_factory.mktemp("records")
    run = build.generate("small", out / "app.sqlite", out / "truth.sqlite", CONFIG)
    conn = _open(out)
    return conn, verify.load_ctx(CONFIG, conn), run


def _rows(conn, sql, *params):
    return [dict(r) for r in conn.execute(sql, params)]


@pytest.mark.parametrize("check", verify.REQUIRED, ids=lambda c: c.__name__)
def test_required_checks(db, check):
    conn, ctx, _ = db
    assert check(conn, ctx) == []


@pytest.mark.parametrize("check", [verify.check_tooth_level, verify.check_child_procedures], ids=lambda c: c.__name__)
def test_tooth_warnings(db, check):
    conn, ctx, _ = db
    assert check(conn, ctx) == []


def test_treatment_is_planned_procedure(db):
    """진료 기록은 예정 시술 그대로 (§2.3)"""
    conn, _, _ = db
    assert _rows(conn, "SELECT t.id FROM emr_treatments t JOIN emr_appointments a ON a.id = t.appointment_id"
                       " WHERE t.procedure_code != a.planned_procedure_code") == []


def test_course_keeps_tooth(db):
    """코스(같은 수요의 연속 내원) 안에서는 치아 단위 시술이 같은 치아 — 그 시술의 후보에 있을 때 (§2.1)"""
    conn, _, run = db
    episode = run.state["episode"]  # 예약 id → 에피소드 번호 (4단계)
    rows = _rows(conn, "SELECT t.appointment_id, t.procedure_code, t.tooth_no FROM emr_treatments t"
                       " ORDER BY t.performed_at, t.id")
    courses = defaultdict(list)
    for r in rows:
        if r["procedure_code"] in TOOTH_LEVEL and r["appointment_id"] in episode:
            courses[episode[r["appointment_id"]]].append(r)
    multi = [c for c in courses.values() if len(c) > 1]
    assert multi
    for course in multi:
        first = course[0]["tooth_no"]
        for r in course[1:]:
            if first in TOOTH_CHOICES[r["procedure_code"]]:
                assert r["tooth_no"] == first, course


def test_unpaid_and_settlement(db):
    """미수 = amount_due > 0 내원의 config unpaid_rate, 부분 수납은 UNPAID_PAID_RANGE, 그중 settle_next_visit은 다음 내원 때 정산 (§2.4)"""
    conn, _, _ = db
    visits = _rows(conn, "SELECT * FROM emr_payments WHERE kind = 'visit' AND amount_due > 0")
    unpaid = [r for r in visits if r["amount_paid"] < r["amount_due"]]
    assert abs(len(unpaid) / len(visits) - CFG["unpaid_rate"]) < 0.03
    lo, hi = UNPAID_PAID_RANGE
    for r in unpaid:
        assert lo * r["amount_due"] <= r["amount_paid"] <= hi * r["amount_due"] and r["amount_paid"] % 10 == 0
    settlements = _rows(conn, "SELECT p.*, a.status FROM emr_payments p JOIN emr_appointments a ON a.id = p.appointment_id"
                              " WHERE p.kind = 'settlement'")
    assert settlements and all(r["status"] == "completed" and r["amount_paid"] > 0 for r in settlements)
    # 정산 행은 내원 종료 시각 — 같은 예약의 visit 행과 같은 시각
    paid_at = {r["appointment_id"]: r["paid_at"] for r in _rows(conn, "SELECT * FROM emr_payments WHERE kind = 'visit'")}
    assert all(r["paid_at"] == paid_at[r["appointment_id"]] for r in settlements)


def test_visit_paid_at_is_end_of_visit(db):
    conn, _, _ = db
    for r in _rows(conn, "SELECT p.paid_at, a.scheduled_at, a.duration_min FROM emr_payments p"
                         " JOIN emr_appointments a ON a.id = p.appointment_id WHERE p.kind = 'visit'"):
        assert (datetime.fromisoformat(r["paid_at"]) - datetime.fromisoformat(r["scheduled_at"])).seconds == r["duration_min"] * 60


def test_payment_methods(db):
    conn, _, _ = db
    counts = Counter(r["method"] for r in _rows(conn, "SELECT method FROM emr_payments"))
    total = sum(counts.values())
    for method, weight in PAYMENT_METHOD_WEIGHTS.items():
        assert abs(counts[method] / total - weight / 100) < 0.03, method


def test_claim_status_by_month(db):
    """확정 월은 결과 세 가지가 다 나오고, 제출 월은 익월 1일 제출 (§2.4)"""
    conn, ctx, _ = db
    settled = Counter(r["status"] for r in _rows(conn, "SELECT status FROM emr_claims WHERE claim_month < ?"
                                                       " AND date(service_date) >= ?",
                                                 min(verify._submitted_months(ctx)), ctx.range_start.isoformat()))
    assert set(settled) == {"paid", "adjusted", "denied"}
    for r in _rows(conn, "SELECT claim_month, submitted_at FROM emr_claims WHERE status != 'pending'"):
        y, m = map(int, r["claim_month"].split("-"))
        nxt = date(y + m // 12, m % 12 + 1, 1)
        assert datetime.fromisoformat(r["submitted_at"]).date() == nxt


def test_adjusted_amount(db):
    conn, _, _ = db
    lo, hi = ADJUSTED_PAID_RANGE
    rows = _rows(conn, "SELECT claim_amount, paid_amount FROM emr_claims WHERE status = 'adjusted'")
    assert rows
    for r in rows:
        assert lo * r["claim_amount"] - 10 <= r["paid_amount"] <= hi * r["claim_amount"] and r["paid_amount"] % 10 == 0


def test_external_history_only_on_scaling(db):
    conn, _, _ = db
    external = {(r["category"], r["note"]) for r in verify.ADJUST_REASONS.values() if r["type"] == "external"}
    for r in _rows(conn, "SELECT procedure_code, status, adjust_code, adjust_note FROM emr_claims WHERE adjust_code IS NOT NULL"):
        if (r["adjust_code"], r["adjust_note"]) in external:
            assert (r["procedure_code"], r["status"]) == (SCALING_CODE, "denied")


def test_pre_window_treatment_and_claim(db):
    """창 이전 내원도 진료 기록(수가대로)은 있고, 청구는 TODAY와 같은 해 급여 스케일링만 paid (§2.6 (1))"""
    conn, ctx, _ = db
    pre = _rows(conn, "SELECT t.id, t.procedure_code, t.tooth_no, c.status FROM emr_treatments t"
                      " LEFT JOIN emr_claims c ON c.treatment_id = t.id WHERE date(t.performed_at) < ?",
                ctx.range_start.isoformat())
    assert pre and all(r["tooth_no"] == FULL_MOUTH for r in pre)
    claimed = [r for r in pre if r["status"] is not None]
    assert claimed and all(r["procedure_code"] == SCALING_CODE and r["status"] == "paid" for r in claimed)


def test_review_and_teeth_independent_of_payment_params(db, tmp_path):
    """수납 파라미터(config unpaid_rate)를 바꿔도 진료·청구·심사 결과는 그대로 — 5단계 안에서도 난수를 나눈다 (§5.1)"""
    conn, _, _ = db
    cfg = {**CFG, "unpaid_rate": 0.5}
    path = tmp_path / "generator.yaml"
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    build.generate("small", tmp_path / "app.sqlite", tmp_path / "truth.sqlite", path)
    other = _open(tmp_path)
    for table in ("emr_treatments", "emr_claims"):
        assert [tuple(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY id")] == \
               [tuple(r) for r in other.execute(f"SELECT * FROM {table} ORDER BY id")], table
    assert [tuple(r) for r in conn.execute("SELECT * FROM emr_payments ORDER BY id")] != \
           [tuple(r) for r in other.execute("SELECT * FROM emr_payments ORDER BY id")]
