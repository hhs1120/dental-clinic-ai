"""verify 재무 검사(8b단계 — 가상 데이터 생성 설계 §2.7·§5.3 재무)가 위반을 잡는지 — 정상 DB를 한 곳씩 깨뜨린다.
fixture 재무: 카드 승인 1~3(가온카드), 정산 입금 10/08·11/05, 통장 = 그 두 입금 + 11/23 현금 80,000"""

import json
from dataclasses import replace
from datetime import date

import pytest

from generator import verify
from generator.master import recon_months, recon_test_months
from tests.generator.verify_db import CTX, make_db

FIN_CHECKS = [
    verify.check_card_sales,
    verify.check_card_deposits,
    verify.check_bank_inflows,
    verify.check_recon_mismatches,
]


def test_valid_db_passes_fin_checks(tmp_path):
    conn = make_db(tmp_path)
    assert {c.__name__: c(conn, CTX) for c in FIN_CHECKS if c(conn, CTX)} == {}
    assert all(c in verify.REQUIRED for c in FIN_CHECKS)


BREAKS = {
    "sale_amount": ("UPDATE fin_card_sales SET amount = 21000 WHERE id = 1", "check_card_sales"),
    "sale_missing": ("DELETE FROM fin_card_sales WHERE id = 2", "check_card_sales"),
    "sale_extra": (
        "INSERT INTO fin_card_sales VALUES (4, '2026-11-03 15:00:00', '가온카드', 3000, '20000001', NULL)",
        "check_card_sales",
    ),
    "sale_issuer": (
        "UPDATE fin_card_sales SET issuer = '없는카드' WHERE id = 1",
        "check_card_sales",
    ),
    "cancel_outside_mismatch": (
        "INSERT INTO fin_card_sales VALUES (4, '2026-10-06 11:01:00', '가온카드', 20000, '10000001', '10000001')",
        "check_card_sales",
    ),
    "deposit_fee": (
        "UPDATE fin_card_deposits SET fee = 300, amount = 19700 WHERE id = 1",
        "check_card_deposits",
    ),
    "deposit_day": (
        "UPDATE fin_card_deposits SET deposited_on = '2026-10-09' WHERE id = 1",
        "check_card_deposits",
    ),
    "deposit_missing": ("DELETE FROM fin_card_deposits WHERE id = 2", "check_card_deposits"),
    "deposit_total": (
        "UPDATE fin_card_deposits SET sales_total = 30000 WHERE id = 2",
        "check_card_deposits",
    ),
    "bank_card_missing": ("DELETE FROM fin_bank_transactions WHERE id = 1", "check_bank_inflows"),
    "bank_cash_amount": (
        "UPDATE fin_bank_transactions SET amount = 70000 WHERE id = 3",
        "check_bank_inflows",
    ),
    "bank_cash_day": (
        "UPDATE fin_bank_transactions SET occurred_at = '2026-11-20 13:10:00' WHERE id = 3",
        "check_bank_inflows",
    ),
    "bank_stranger": (
        "INSERT INTO fin_bank_transactions VALUES (4, '2026-11-18 10:30:00', 'in', 5000, '박민수')",
        "check_bank_inflows",
    ),
}


@pytest.mark.parametrize("case", BREAKS)
def test_break_is_caught(tmp_path, case):
    sql, check = BREAKS[case]
    conn = make_db(tmp_path)
    conn.execute(sql)
    assert getattr(verify, check)(conn, CTX), f"{check}가 {case}를 못 잡음"


def test_deposit_not_due_before_today(tmp_path):
    """입금 예정일이 TODAY 이후인 매출은 정산이 없어야 한다 — 11/30(월) 승인은 12/02 예정"""
    conn = make_db(tmp_path)
    conn.execute(
        "INSERT INTO fin_card_sales VALUES (4, '2026-11-30 10:00:00', '가온카드', 5000, '20000009', NULL)"
    )
    assert verify.check_card_deposits(conn, CTX) == []
    conn.execute(
        "INSERT INTO fin_card_deposits VALUES (3, '2026-12-01', '가온카드', '2026-11-30', 5000, 60, 4940)"
    )
    assert verify.check_card_deposits(conn, CTX)


def test_transfer_payer_patient_or_guardian(tmp_path):
    """4번 수납(2번 소아 환자 11/18 80,000)을 이체로 — 입금자는 본인 또는 보호자(김철수)"""
    conn = make_db(tmp_path)
    conn.execute("UPDATE emr_payments SET method = 'transfer' WHERE id = 4")
    conn.execute("DELETE FROM fin_bank_transactions WHERE id = 3")
    conn.execute(
        "INSERT INTO fin_bank_transactions VALUES (3, '2026-11-18 10:30:00', 'in', 80000, '김철수')"
    )
    assert verify.check_bank_inflows(conn, CTX) == []
    conn.execute("UPDATE fin_bank_transactions SET memo = '박민수' WHERE id = 3")
    assert verify.check_bank_inflows(conn, CTX)


def test_bank_uses_true_payment(tmp_path):
    """입력 누락(장부 0원)이어도 통장 현금은 실제 받은 80,000 — 실제 값으로 맞춘다"""
    conn = make_db(tmp_path)
    conn.execute("UPDATE emr_payments SET amount_paid = 0 WHERE id = 4")
    conn.execute(
        "INSERT INTO truth.truth_ledger_errors (id, kind, payment_id, true_amount, true_paid_at, true_method)"
        " VALUES (1, 'entry_missing', 4, 80000, '2026-11-18 10:30:00', 'cash')"
    )
    assert verify.check_bank_inflows(conn, CTX) == []
    assert verify.check_recon_mismatches(conn, CTX)  # 장부 실수에 불일치 정답이 없음
    _mismatch(conn, "2026-11-18", "entry_missing", [("fin_bank_transactions", 3)], [4], "test", 1)
    assert (
        verify.check_recon_mismatches(conn, replace(CTX, recon_mismatches={"entry_missing": 1}))
        == []
    )


def _mismatch(conn, on, cause, fin, payments, split, ledger=None):
    conn.execute(
        "INSERT INTO truth.truth_recon_mismatches (occurred_on, cause, fin_ids, payment_ids, split, ledger_error_id)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            on,
            cause,
            json.dumps([{"table": t, "id": i} for t, i in fin]),
            json.dumps(payments),
            split,
            ledger,
        ),
    )


def _non_clinical(conn, amount=3000, split="test"):
    """11/03 장부 없는 칫솔 3,000원 승인 — 정산·통장 입금도 그만큼 늘린다"""
    conn.execute(
        f"INSERT INTO fin_card_sales VALUES (4, '2026-11-03 15:00:00', '가온카드', {amount}, '20000001', NULL)"
    )
    total = 34000 + amount
    fee = int(total * 0.012)
    conn.execute(
        f"UPDATE fin_card_deposits SET sales_total = {total}, fee = {fee}, amount = {total - fee} WHERE id = 2"
    )
    conn.execute(f"UPDATE fin_bank_transactions SET amount = {total - fee} WHERE id = 2")
    _mismatch(conn, "2026-11-03", "non_clinical_sale", [("fin_card_sales", 4)], [], split)
    return replace(CTX, recon_mismatches={"non_clinical_sale": 1})


def test_non_clinical_sale(tmp_path):
    conn = make_db(tmp_path)
    ctx = _non_clinical(conn)
    assert {c.__name__: c(conn, ctx) for c in FIN_CHECKS if c(conn, ctx)} == {}
    assert verify.check_recon_mismatches(conn, CTX)  # 건수가 메타와 다름


@pytest.mark.parametrize(
    "kwargs", [{"amount": 3100}, {"split": "dev"}]
)  # 가격표에 없는 금액 / 11월은 test
def test_non_clinical_sale_break(tmp_path, kwargs):
    conn = make_db(tmp_path)
    ctx = _non_clinical(conn, **kwargs)
    assert verify.check_recon_mismatches(conn, ctx)


def test_split_payment(tmp_path):
    """2번 수납(18,000, 10000002)을 10,000 + 8,000 두 승인으로"""
    conn = make_db(tmp_path)
    conn.execute("UPDATE fin_card_sales SET amount = 10000 WHERE id = 2")
    conn.execute(
        "INSERT INTO fin_card_sales VALUES (4, '2026-11-03 10:30:00', '다솜카드', 8000, '20000002', NULL)"
    )
    conn.execute(
        "UPDATE fin_card_deposits SET sales_total = 26000, fee = 312, amount = 25688 WHERE id = 2"
    )
    conn.execute("UPDATE fin_bank_transactions SET amount = 25688 WHERE id = 2")
    # 다솜카드 11/03분 8,000 — 정산 2영업일 11/05, 율 1.35%
    conn.execute(
        "INSERT INTO fin_card_deposits VALUES (3, '2026-11-05', '다솜카드', '2026-11-03', 8000, 108, 7892)"
    )
    conn.execute(
        "INSERT INTO fin_bank_transactions VALUES (4, '2026-11-05 08:00:00', 'in', 7892, '다솜카드')"
    )
    _mismatch(
        conn,
        "2026-11-03",
        "split_payment",
        [("fin_card_sales", 2), ("fin_card_sales", 4)],
        [2],
        "test",
    )
    ctx = replace(CTX, recon_mismatches={"split_payment": 1})
    assert {c.__name__: c(conn, ctx) for c in FIN_CHECKS if c(conn, ctx)} == {}
    conn.execute("UPDATE fin_card_sales SET amount = 7000 WHERE id = 4")
    assert verify.check_recon_mismatches(conn, ctx)


def test_cancel_reapprove(tmp_path):
    """1번 수납(20,000, 10000001) 승인 → 취소 → 새 번호 재승인. 정산 합계는 그대로"""
    conn = make_db(tmp_path)
    conn.execute(
        "INSERT INTO fin_card_sales VALUES (4, '2026-10-06 11:01:00', '가온카드', 20000, '10000001', '10000001')"
    )
    conn.execute(
        "INSERT INTO fin_card_sales VALUES (5, '2026-10-06 11:02:00', '가온카드', 20000, '20000003', NULL)"
    )
    _mismatch(
        conn,
        "2026-10-06",
        "cancel_reapprove",
        [("fin_card_sales", 1), ("fin_card_sales", 4), ("fin_card_sales", 5)],
        [1],
        "test",  # 10월 — 평가 달 8~11월의 마지막 두 달
    )
    ctx = replace(CTX, recon_mismatches={"cancel_reapprove": 1})
    assert {c.__name__: c(conn, ctx) for c in FIN_CHECKS if c(conn, ctx)} == {}
    conn.execute("UPDATE fin_card_sales SET amount = 19000 WHERE id = 5")
    assert verify.check_recon_mismatches(conn, ctx)


def test_fee_mismatch_and_delay(tmp_path):
    """10/06분 정산의 수수료가 율과 다르면 수수료 차이 불일치, 하루 늦으면 입금 지연 불일치 — 정답이 있어야 통과"""
    conn = make_db(tmp_path)
    conn.execute("UPDATE fin_card_deposits SET fee = 300, amount = 19700 WHERE id = 1")
    conn.execute("UPDATE fin_bank_transactions SET amount = 19700 WHERE id = 1")
    assert verify.check_card_deposits(conn, CTX)
    _mismatch(
        conn,
        "2026-10-06",
        "fee_mismatch",
        [("fin_card_deposits", 1), ("fin_bank_transactions", 1)],
        [],
        "test",  # 10월 — 마지막 두 달
    )
    assert verify.check_card_deposits(conn, CTX) == []
    conn.execute(
        "UPDATE fin_card_deposits SET deposited_on = '2026-11-06' WHERE id = 2"
    )  # 11/05 → 다음 영업일
    conn.execute(
        "UPDATE fin_bank_transactions SET occurred_at = '2026-11-06 08:00:00' WHERE id = 2"
    )
    assert verify.check_card_deposits(conn, CTX)
    _mismatch(
        conn,
        "2026-11-03",
        "deposit_delay",
        [("fin_card_deposits", 2), ("fin_bank_transactions", 2)],
        [],
        "test",
    )
    ctx = replace(CTX, recon_mismatches={"fee_mismatch": 1, "deposit_delay": 1})
    assert {c.__name__: c(conn, ctx) for c in FIN_CHECKS if c(conn, ctx)} == {}


def test_recon_months_follow_today():
    """평가 달 = TODAY가 속한 달 직전 RECON_MONTH_COUNT개월, test = 마지막 RECON_TEST_COUNT달 (해 넘김 포함)"""
    assert recon_months(date(2026, 12, 1)) == ("2026-08", "2026-09", "2026-10", "2026-11")
    assert recon_months(date(2027, 2, 15)) == ("2026-10", "2026-11", "2026-12", "2027-01")
    assert recon_test_months(date(2026, 12, 1)) == ("2026-10", "2026-11")


def test_one_ledger_error_per_cash_deposit(tmp_path):
    """같은 현금 입금(11/23)에 장부 실수 불일치가 둘이면 잡힌다 — 통장에는 합계 차이 하나만 보여 둘을 가려낼 수 없다"""
    conn = make_db(tmp_path)
    conn.execute("UPDATE emr_payments SET amount_paid = 0 WHERE id = 4")
    conn.execute(
        "INSERT INTO truth.truth_ledger_errors (id, kind, payment_id, true_amount, true_paid_at, true_method)"
        " VALUES (1, 'entry_missing', 4, 80000, '2026-11-18 10:30:00', 'cash')"
    )
    _mismatch(conn, "2026-11-18", "entry_missing", [("fin_bank_transactions", 3)], [4], "test", 1)
    _mismatch(conn, "2026-11-18", "entry_missing", [("fin_bank_transactions", 3)], [4], "test", 1)
    found = verify.check_recon_mismatches(
        conn, replace(CTX, recon_mismatches={"entry_missing": 2})
    )
    assert any(r.get("problem") == "같은 현금 입금에 장부 실수 불일치가 둘 이상" for r in found)


# ── ⑧-2 지출 ─────────────────────────────────────────────────
# 10월 임대료 자동이체(세금계산서), 11/03 솔밭식당 카드(영수증), 11/05 모아덴탈몰 카드(결제대행사 표기, 주문 메일 — 커피 원두라
# 복리후생비). 평가 달 8~11월 중 10·11월 = test
EXPENSES = [
    "INSERT INTO fin_bank_transactions VALUES (10, '2026-10-26 06:00:00', 'out', 3300000, '한결빌딩관리')",
    "INSERT INTO fin_card_expenses VALUES (1, '2026-11-03 12:30:00', '솔밭식당', 45000)",
    "INSERT INTO fin_card_expenses VALUES (2, '2026-11-05 15:00:00', '페이온*모아덴탈몰', 30000)",
    "INSERT INTO fin_expense_documents VALUES (1, 'tax_invoice', '한결빌딩관리', '2026-10-20', 3300000,"
    " '세금계산서\n품목 월 임대료\n합계금액 3,300,000원')",
    "INSERT INTO fin_expense_documents VALUES (2, 'receipt', '솔밭식당', '2026-11-03', 45000,"
    " '솔밭식당\n점심 식대 45,000원\n합계 45,000원')",
    "INSERT INTO fin_expense_documents VALUES (3, 'order_email', '모아덴탈몰', '2026-11-05', 30000,"
    " '[모아덴탈몰] 주문 완료\n- 커피 원두 30,000원\n결제금액 30,000원')",
    "INSERT INTO truth.truth_expense_matches VALUES ('fin_bank_transactions', 10, '[1]', '지급임차료', 'test')",
    "INSERT INTO truth.truth_expense_matches VALUES ('fin_card_expenses', 1, '[2]', '복리후생비', 'test')",
    "INSERT INTO truth.truth_expense_matches VALUES ('fin_card_expenses', 2, '[3]', '복리후생비', 'test')",
]


@pytest.fixture
def exp_db(tmp_path):
    conn = make_db(tmp_path)
    for sql in EXPENSES:
        conn.execute(sql)
    return conn


def test_expenses_valid(exp_db):
    assert verify.check_expense_matches(exp_db, CTX) == []
    assert verify.check_lab_statements(exp_db, CTX) == []


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM truth.truth_expense_matches WHERE outflow_id = 10",  # 정답 없음
        "UPDATE fin_expense_documents SET issuer = '다른상사' WHERE id = 1",
        "UPDATE fin_expense_documents SET issued_on = '2026-10-27' WHERE id = 1",  # 출금 뒤 발행
        "UPDATE truth.truth_expense_matches SET account = '소모품비' WHERE outflow_id = 2",  # 품목은 복리후생비
        "UPDATE truth.truth_expense_matches SET account = '수선비' WHERE outflow_id = 10",
        "UPDATE truth.truth_expense_matches SET split = 'dev' WHERE outflow_id = 10",  # 10월 = test
        "UPDATE fin_card_expenses SET merchant = '모르는가게' WHERE id = 1",
        "UPDATE truth.truth_expense_matches SET document_ids = '[]' WHERE outflow_id = 10",  # 자동이체 증빙 없음
        "UPDATE fin_bank_transactions SET amount = 3290000 WHERE id = 10",  # 금액 차이 1건 ≠ round(0.05 × 3)
        "UPDATE truth.truth_expense_matches SET document_ids = '[2, 3]' WHERE outflow_id = 1",  # 증빙 두 출금에
    ],
)
def test_expense_break(exp_db, sql):
    exp_db.execute(sql)
    assert verify.check_expense_matches(exp_db, CTX)


def test_lab_statement_lines(tmp_path):
    """기공 명세서 줄 = 창 안 기공 진료 — 1번 환자 11/10 36번 인레이 진료가 있으면 그 줄로 통과, 진료가 없으면 잡힌다"""
    conn = make_db(tmp_path)
    body = "거래명세서\n- 김철수 36번 인레이 80,000원\n합계 80,000원"
    conn.execute(
        "INSERT INTO fin_expense_documents VALUES (1, 'statement', '온새미치과기공소', '2026-11-09', 80000, ?)",
        (body,),
    )
    assert verify.check_lab_statements(conn, CTX)
    conn.execute(
        "INSERT INTO emr_appointments (patient_id, staff_id, scheduled_at, duration_min, type,"
        " planned_procedure_code, status, booked_via, created_at, chair_no) VALUES"
        " (1, 1, '2026-11-10 10:00:00', 60, 'treatment', 'U0007', 'completed', 'phone', '2026-11-01 10:00:00', 3)"
    )
    aid = conn.execute("SELECT max(id) FROM emr_appointments").fetchone()[0]
    conn.execute(
        "INSERT INTO emr_treatments (appointment_id, patient_id, procedure_code, tooth_no, performed_at,"
        " fee_total, insured_amount, patient_amount) VALUES (?, 1, 'U0007', '36', '2026-11-10 10:00:00', 350000, 0, 350000)",
        (aid,),
    )
    assert verify.check_lab_statements(conn, CTX) == []
    conn.execute(
        "UPDATE fin_expense_documents SET body = replace(body, '80,000원\n합계', '90,000원\n합계')"
    )
    assert verify.check_lab_statements(conn, CTX)
