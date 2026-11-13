"""⑥ 경영 리포트(backend/report.py) — 기간·숫자·변화 분해. 설계 docs/superpowers/specs/2026-11-13-report-design.md §1~3.
손으로 만든 source(실제 스키마 + master 값)로 숫자를 손계산과 맞춘다."""

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from backend import db, report
from generator.clinic import master_values_rows
from generator.master import DIAGNOSES, PROCEDURES

ROOT = Path(__file__).resolve().parents[2]
TODAY = date(2026, 12, 1)
DX_NOTE = "상병과 시행 행위 불일치"

APPT = (
    "INSERT INTO emr_appointments (id, patient_id, staff_id, scheduled_at, duration_min, type,"
    " planned_procedure_code, status, booked_via, created_at) VALUES (?, ?, 1, ?, 30,"
    " 'treatment', 'U0001', ?, 'phone', '2026-01-01 09:00:00')"
)
TREAT = (
    "INSERT INTO emr_treatments (id, appointment_id, patient_id, procedure_code, tooth_no,"
    " performed_at, fee_total, insured_amount, patient_amount) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0)"
)
CLAIM = (
    "INSERT INTO emr_claims (id, treatment_id, patient_id, chart_no, tooth_no, claim_month,"
    " service_date, procedure_code, claim_amount, submitted_at, status, paid_amount, adjust_note,"
    " reviewed_at) VALUES (?, ?, ?, 'C-1', ?, '2026-11', '2026-11-01', ?, ?, ?, ?, ?, ?, ?)"
)


def make_source(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript((ROOT / "shared" / "source_schema.sql").read_text(encoding="utf-8"))
    cols = list(PROCEDURES[0])
    conn.executemany(
        f"INSERT INTO emr_procedures ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        [tuple(p[c] for c in cols) for p in PROCEDURES],
    )
    conn.executemany("INSERT INTO emr_diagnoses VALUES (?, ?)", DIAGNOSES.items())
    conn.executemany("INSERT INTO ax_master_values VALUES (?, ?)", master_values_rows())
    conn.executemany(
        "INSERT INTO emr_staff (id, name, role, active) VALUES (?, ?, ?, ?)",
        [
            (1, "원장", "doctor", 1),
            (2, "위생1", "hygienist", 1),
            (3, "위생2", "hygienist", 1),
            (4, "데스크", "desk", 1),
            (5, "실장", "consultant", 0),  # 퇴사 — 인건비에 넣지 않음
        ],
    )
    conn.executemany(
        "INSERT INTO emr_patients (id, chart_no, name, birth_date, sex, phone, registered_at)"
        " VALUES (?, ?, ?, '1980-01-01', 'M', '010', ?)",
        [
            (1, "C-1", "가", "2020-01-01"),
            (2, "C-2", "나", "2026-11-05"),
            (3, "C-3", "다", "2026-10-10"),
        ],
    )
    conn.executemany(
        APPT,
        [
            (8, 1, "2026-09-15 10:00:00", "completed"),
            (1, 1, "2026-10-06 10:00:00", "completed"),
            (2, 3, "2026-10-06 11:00:00", "noshow"),
            (3, 3, "2026-10-14 10:00:00", "cancelled"),
            (4, 1, "2026-11-03 10:00:00", "completed"),
            (5, 2, "2026-11-10 10:00:00", "completed"),
            (6, 1, "2026-11-10 11:00:00", "noshow"),
            (7, 2, "2026-11-24 10:00:00", "completed"),
            (9, 2, "2026-12-01 10:00:00", "scheduled"),  # 기준일 — 끝나지 않은 기간
        ],
    )
    conn.executemany(
        TREAT,
        [
            (5, 8, 1, "U0005", "16", "2026-09-15 10:30:00", 80_000),
            (1, 1, 1, "U0001", "00", "2026-10-06 10:30:00", 60_000),
            (7, 1, 1, "U0009", "26", "2026-10-06 10:40:00", 550_000),
            (2, 4, 1, "U0009", "36", "2026-11-03 10:30:00", 550_000),
            (3, 5, 2, "U0001", "00", "2026-11-10 10:30:00", 60_000),
            (4, 7, 2, "U0011", "38", "2026-11-24 10:30:00", 30_000),
            (6, 7, 2, "U0011", "48", "2026-11-24 10:40:00", 30_000),
        ],
    )
    conn.executemany(
        "INSERT INTO emr_chart_notes (treatment_id, dx_code, note) VALUES (?, ?, '')",
        [(1, "D04"), (3, "D04"), (4, "D01"), (6, "D01")],
    )
    conn.executemany(
        "INSERT INTO emr_payments (patient_id, appointment_id, kind, amount_due, amount_paid,"
        " paid_at, method) VALUES (?, ?, 'visit', ?, ?, ?, 'card')",
        [
            (1, 1, 18_000, 18_000, "2026-10-06 11:00:00"),
            (1, 4, 500_000, 500_000, "2026-11-03 11:00:00"),
            (2, 5, 20_000, 20_000, "2026-11-10 11:00:00"),
        ],
    )
    conn.executemany(
        CLAIM,
        [
            # 10월 청구, 11월 통보 지급
            (2, 1, 1, "00", "U0001", 42_000, "2026-10-10", "paid", 42_000, None, "2026-11-15"),
            # 11월 상병 착오 — 스케일링 1건 4.2만, 단순발치 2건 3만(건수는 많고 금액은 작음)
            (1, 3, 2, "01", "U0001", 42_000, "2026-11-12", "adjusted", 0, DX_NOTE, "2026-11-20"),
            (4, 4, 2, "38", "U0011", 15_000, "2026-11-25", "adjusted", 0, DX_NOTE, "2026-11-28"),
            (5, 6, 2, "48", "U0011", 15_000, "2026-11-25", "adjusted", 0, DX_NOTE, "2026-11-28"),
        ],
    )
    conn.executemany(
        "INSERT INTO fin_bank_transactions (id, occurred_at, direction, amount, memo)"
        " VALUES (?, ?, ?, ?, ?)",
        [
            (1, "2026-10-05 09:00:00", "out", 100_000, "바른덴탈상사"),
            (2, "2026-11-05 09:00:00", "out", 300_000, "바른덴탈상사"),
            (3, "2026-11-06 09:00:00", "out", 200_000, "하늘치"),  # 잘린 적요 — 과목 없음
            (4, "2026-11-20 09:00:00", "out", 400_000, "온새미치과기공소"),  # 사람 확정
            (5, "2026-11-21 09:00:00", "in", 999_000, "카드사 입금"),
        ],
    )
    conn.execute(
        "INSERT INTO fin_card_expenses (id, used_at, merchant, amount)"
        " VALUES (1, '2026-11-15 12:00:00', '솔밭식당', 50000)"
    )
    conn.commit()
    conn.close()


@pytest.fixture
def conn(tmp_path):
    make_source(tmp_path / "source.sqlite")
    c = db.connect(tmp_path / "app.sqlite", tmp_path / "source.sqlite")
    db.init_schema(c)
    with c:
        c.execute(
            "INSERT INTO ax_expense_confirmations VALUES ('fin_bank_transactions', 4,"
            " '온새미치과기공소', '[]', '외주가공비', 'desk', '2026-11-21 10:00:00')"
        )
    yield c
    c.close()


def test_periods_finished_only(conn):
    assert report.periods(conn, TODAY, "monthly") == ["2026-09", "2026-10", "2026-11"]
    w = report.periods(conn, TODAY, "weekly")
    assert all(date.fromisoformat(x).weekday() == 0 for x in w)
    assert w[-1] == "2026-11-23"
    assert report.periods(conn, TODAY, "daily") == [
        "2026-09-15",
        "2026-10-06",
        "2026-11-03",
        "2026-11-10",
        "2026-11-24",
    ]


def test_monthly_numbers(conn):
    m = report.metrics(conn, TODAY, "monthly", "2026-11")
    assert m["sales"] == 670_000
    assert m["patient_paid"] == 520_000 and m["insurer_paid"] == 42_000
    assert m["collected"] == 562_000
    assert m["claimed"] == 72_000
    assert (m["visits"], m["noshow"], m["noshow_rate"]) == (4, 1, 25)
    assert m["new_patients"] == 1 and m["treatment_days"] == 3
    assert (m["material"], m["lab"]) == (300_000, 400_000)
    assert (m["account_none"], m["account_none_amount"]) == (1, 200_000)
    assert m["material_unconfirmed"] == 1  # 사람 확정한 기공비는 빼고
    assert m["outflow"] == 950_000
    assert m["profit_ex_labor"] == 562_000 - 950_000
    assert m["est.labor"] == 2 * 3_800_000 + 3_000_000
    assert m["est.profit"] == m["profit_ex_labor"] - m["est.labor"]
    assert "est.bep" not in m  # 재료비·기공비가 매출보다 많음
    assert (m["dx_errors"], m["dx_lost"]) == (3, 72_000)


def test_bep(conn):
    m = report.metrics(conn, TODAY, "monthly", "2026-10")
    fixed = m["outflow"] - m["material"] - m["lab"] + m["est.labor"]
    assert m["est.bep"] == round(fixed / (1 - (m["material"] + m["lab"]) / m["sales"]))


def test_category_diffs_sum_to_sales_diff(conn):
    m = report.metrics(conn, TODAY, "monthly", "2026-11")
    assert m["sales.prosth"] == 550_000 and m["sales.ortho"] == 0
    cats = [k for k in m if k.startswith("diff.sales.")]
    assert sum(m[k] for k in cats) == m["diff.sales"] == 670_000 - 610_000


def test_pct_rounded_int(conn):
    m = report.metrics(conn, TODAY, "monthly", "2026-11")
    assert m["pct.sales"] == round(m["diff.sales"] * 100 / m["prev.sales"])
    assert m["diff.noshow_rate"] == 25 - 50 and "pct.noshow_rate" not in m
    assert all(isinstance(v, int) for v in m.values())


def test_empty_prev_period(conn):
    m = report.metrics(conn, TODAY, "monthly", "2026-09")  # 8월은 데이터 없음
    assert m["prev.sales"] == 0 and m["diff.sales"] == 80_000 and "pct.sales" not in m
    assert m["material"] == 0 and m["account_none"] == 0  # 출금이 없는 달


def test_labor_setting_overrides(conn):
    db.set_setting(conn, "labor_cost_assumed", {"desk": 2_500_000})
    got = report.labor(conn)
    assert got["wage"] == {"hygienist": 3_800_000, "desk": 2_500_000, "consultant": 3_800_000}
    assert got["headcount"] == {"hygienist": 2, "desk": 1, "consultant": 0}


def test_weekly_has_no_monthly_keys(conn):
    m = report.metrics(conn, TODAY, "weekly", "2026-11-23")
    assert "material" not in m and not any(report.is_est(k) for k in m)
    assert m["sales"] == 60_000 and m["prev.sales"] == 0  # 11/16 주는 비어 있음


def test_daily_prev_is_previous_treatment_day(conn):
    assert report.prev_period(conn, TODAY, "daily", "2026-11-03") == "2026-10-06"
    assert report.prev_period(conn, TODAY, "daily", "2026-09-15") is None
    m = report.metrics(conn, TODAY, "daily", "2026-11-03")
    assert m["prev.sales"] == 610_000


def test_dx_top_by_amount(conn):
    top = report.dx_top(conn, "2026-11")
    assert top == {"name": f"스케일링 × {DIAGNOSES['D04']}", "n": 1, "lost": 42_000}
    assert report.dx_top(conn, "2026-10") is None


def test_is_est():
    assert report.is_est("est.labor") and report.is_est("prev.est.profit")
    assert not report.is_est("sales")


def test_account_names_in_master(conn):
    row = conn.execute(
        "SELECT value FROM source.ax_master_values WHERE name = 'expense_accounts'"
    ).fetchone()
    assert {report.MATERIAL, report.LAB} <= set(json.loads(row[0]))


def test_prev_week(conn):
    assert (
        report.prev_period(conn, TODAY, "weekly", "2026-11-23")
        == (date(2026, 11, 23) - timedelta(days=7)).isoformat()
    )


def test_pct_sign_follows_direction_when_prev_negative(conn):
    m = report.metrics(conn, TODAY, "monthly", "2026-11")
    assert m["prev.profit_ex_labor"] < 0 and m["diff.profit_ex_labor"] < 0
    assert m["pct.profit_ex_labor"] < 0  # 손실이 커졌으면 음수


# ── 후검증(설계 §4) ──
M = {"sales": 48_213_000, "diff.sales": -3_204_000, "pct.sales": -6, "est.profit": 5_120_000}
X = report.label_numbers("monthly", "2026-11", "2026-10")
EST = "대표 원장 보수·세금 전"


def _out(text: str, used: list[str]) -> dict:
    return {"points": [{"text": text, "used": used}]}


def test_check_passes_rounded_and_abs():
    out = _out(
        "11월 매출은 4,821만 원으로 10월보다 320만 원(6%) 줄었습니다.",
        ["sales", "diff.sales", "pct.sales"],
    )
    assert report.check_report(M, out, X)


def test_check_passes_exact_won():
    assert report.check_report(M, _out("매출 48,213,000원", ["sales"]), X)


def test_check_rejects_unknown_key():
    assert not report.check_report(M, _out("매출 4,821만 원", ["sales.x"]), X)


def test_check_rejects_number_not_in_used():
    assert not report.check_report(M, _out("매출 4,821만 원, 6% 감소", ["sales"]), X)


def test_check_rejects_eok():
    assert not report.check_report(M, _out("매출 4억 8천만 원", ["sales"]), X)


def test_check_est_needs_marks():
    assert not report.check_report(M, _out("[추정] 손익 512만 원", ["est.profit"]), X)
    assert report.check_report(M, _out(f"[추정] 손익 512만 원({EST})", ["est.profit"]), X)


def test_check_no_est_no_mark():
    assert not report.check_report(M, _out("[추정] 매출 4,821만 원", ["sales"]), X)


def test_check_empty_points():
    assert report.check_report(M, {"points": []}, X)


def test_check_too_many_points():
    p = {"text": "매출 4,821만 원", "used": ["sales"]}
    assert not report.check_report(M, {"points": [p] * 4}, X)


def test_check_missing_field():
    assert not report.check_report(M, {"points": [{"text": "매출"}]}, X)
    assert not report.check_report(M, {}, X)


def test_label_numbers():
    assert report.label_numbers("monthly", "2026-11") == {2026, 11}
    # 11/9(월)~11/15(일), 11월 둘째 주
    assert report.label_numbers("weekly", "2026-11-09") == {2026, 11, 9, 15, 2}
    assert report.label_numbers("daily", "2026-11-03") == {2026, 11, 3}


# ── 저장·일괄 생성(설계 §5) ──
EMPTY = {"points": []}
BAD = {"points": [{"text": "매출 1원", "used": ["nope"]}]}


def _gw(batch, *turns):
    from tests.backend.test_prep import FakeGw, res

    return FakeGw(*[res(t) for t in turns], batch=lambda cid: res(batch))


def _n(conn) -> int:
    return sum(len(report.periods(conn, TODAY, s)) for s in report.SCOPES)


def _rows(conn):
    return conn.execute("SELECT * FROM ax_reports ORDER BY scope, period").fetchall()


def test_build_saves_and_skips(conn):
    n = _n(conn)
    out = report.build(conn, _gw(EMPTY), TODAY, use_batch=True, poll_seconds=0)
    assert out == {"rows": n, "called": n, "skipped": 0, "failed": 0}
    rows = _rows(conn)
    assert len(rows) == n and all(r["valid"] == 1 for r in rows)
    r = next(r for r in rows if (r["scope"], r["period"]) == ("monthly", "2026-11"))
    assert json.loads(r["metrics"]) == report.metrics(conn, TODAY, "monthly", "2026-11")
    assert r["metrics_hash"] == report.metrics_hash(json.loads(r["metrics"]))
    again = report.build(conn, _gw(EMPTY), TODAY, use_batch=True, poll_seconds=0)
    assert again["called"] == 0 and again["skipped"] == n


def test_build_regenerates_stale(conn):
    report.build(conn, _gw(EMPTY), TODAY, use_batch=True, poll_seconds=0)
    with (
        conn
    ):  # 11월 재료비 출금을 소모품비로 확정 — 같은 출금처 기억으로 10월 출금 과목도 바뀐다(⑧-2)
        conn.execute(
            "INSERT INTO ax_expense_confirmations VALUES ('fin_bank_transactions', 2,"
            " '바른덴탈상사', '[]', '소모품비', 'desk', '2026-11-30 10:00:00')"
        )
    gw = _gw(EMPTY)
    out = report.build(conn, gw, TODAY, use_batch=True, poll_seconds=0)
    assert out["called"] == 2  # 주간·일간은 재료비가 없어 그대로
    assert [r["custom_id"] for r in gw.batches[-1]] == ["monthly_2026-10", "monthly_2026-11"]


def test_build_fail_saves_invalid(conn):
    n = _n(conn)
    out = report.build(conn, _gw(BAD, *[BAD] * n), TODAY, use_batch=True, poll_seconds=0)
    assert out["failed"] == n
    assert all(r["valid"] == 0 and r["points"] is None for r in _rows(conn))


def test_build_retries_once(conn):
    n = _n(conn)
    gw = _gw(BAD, *[EMPTY] * n)
    out = report.build(conn, gw, TODAY, use_batch=True, poll_seconds=0)
    assert out["failed"] == 0 and len(gw.calls) == n
    assert all(r["valid"] == 1 for r in _rows(conn))


def test_build_input_has_periods_and_metrics(conn):
    gw = _gw(EMPTY)
    report.build(conn, gw, TODAY, use_batch=True, poll_seconds=0)
    req = next(r for r in gw.batches[0] if r["custom_id"] == "monthly_2026-11")
    sent = json.loads(req["messages"][0]["content"].split("\n", 1)[1])
    assert sent["period"] == "2026-11" and sent["prev_period"] == "2026-10"
    assert sent["metrics"]["sales"] == 670_000
