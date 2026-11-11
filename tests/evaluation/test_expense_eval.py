"""⑧-2 매칭·과목 규칙 기준선(평가 설계 §1 원칙 8·§5 ⑧-2) — evaluation/expense.py. 데이터 재설계보다 먼저 고정한다."""

import json
import sqlite3

from evaluation import expense as ex
from generator.paths import ROOT

_ROWS = {
    "바른덴탈상사": {
        "name": "바른덴탈상사",
        "pay": "month_end_transfer",
        "account": "재료비",
        "card_name": None,
    },
    "모아덴탈몰": {
        "name": "모아덴탈몰",
        "pay": "card",
        "account": "소모품비",
        "card_name": "페이온*모아덴탈몰",
    },
    "한결빌딩관리": {
        "name": "한결빌딩관리",
        "pay": "auto_debit",
        "account": "지급임차료",
        "card_name": None,
    },
}
VENDORS = ex.vendors_by_label(list(_ROWS.values()))


def doc(i, kind, issuer, on, amount):
    return {"id": i, "kind": kind, "issuer": issuer, "issued_on": on, "amount": amount}


def test_one_to_one_nearest_in_window():
    out = {
        "table": "fin_card_expenses",
        "at": "2026-11-10 12:00:00",
        "amount": 30000,
        "label": "페이온*모아덴탈몰",
    }
    docs = [
        doc(1, "order_email", "모아덴탈몰", "2026-11-10", 30000),
        doc(2, "order_email", "모아덴탈몰", "2026-11-01", 30000),
    ]
    assert ex.rule_match(out, docs, VENDORS) == [1]


def test_month_end_sum_with_tax_invoice():
    out = {
        "table": "fin_bank_transactions",
        "at": "2026-11-30 17:00:00",
        "amount": 300000,
        "label": "바른덴탈상사",
    }
    docs = [
        doc(1, "statement", "바른덴탈상사", "2026-11-05", 100000),
        doc(2, "statement", "바른덴탈상사", "2026-11-20", 200000),
        doc(3, "tax_invoice", "바른덴탈상사", "2026-11-30", 300000),
        doc(4, "statement", "바른덴탈상사", "2026-10-20", 50000),
    ]
    assert ex.rule_match(out, docs, VENDORS) == [1, 2, 3]


def test_no_doc_returns_empty():
    out = {
        "table": "fin_bank_transactions",
        "at": "2026-11-20 06:00:00",
        "amount": 990000,
        "label": "한결빌딩관리",
    }
    assert (
        ex.rule_match(out, [doc(1, "bill", "한결빌딩관리", "2026-11-12", 900000)], VENDORS) == []
    )


def test_account_from_card_name_and_memo():
    assert ex.rule_account({"label": "페이온*모아덴탈몰"}, VENDORS) == "소모품비"
    assert ex.rule_account({"label": "바른덴탈상사"}, VENDORS) == "재료비"
    assert ex.rule_account({"label": "모르는 가맹점"}, VENDORS) is None


def test_score_dev_only_rows(tmp_path):
    src = sqlite3.connect(tmp_path / "source.sqlite")
    src.executescript((ROOT / "shared" / "source_schema.sql").read_text("utf-8"))
    src.execute(
        "INSERT INTO fin_card_expenses VALUES (1, '2026-11-10 12:00:00', '페이온*모아덴탈몰', 30000)"
    )
    src.execute(
        "INSERT INTO fin_card_expenses VALUES (2, '2026-11-11 12:00:00', '페이온*모아덴탈몰', 40000)"
    )
    src.execute(
        "INSERT INTO fin_expense_documents VALUES (1, 'order_email', '모아덴탈몰', '2026-11-10', 30000, '본문')"
    )
    src.commit()
    t = sqlite3.connect(tmp_path / "truth.sqlite")
    t.executescript((ROOT / "ground_truth" / "schema.sql").read_text("utf-8"))
    t.execute(
        "INSERT INTO truth_expense_matches VALUES ('fin_card_expenses', 1, ?, '소모품비', 'dev')",
        (json.dumps([1]),),
    )
    t.execute(
        "INSERT INTO truth_expense_matches VALUES ('fin_card_expenses', 2, '[]', '복리후생비', 'test')"
    )
    t.commit()
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("ATTACH DATABASE ? AS source", (str(tmp_path / "source.sqlite"),))
    c.execute("ATTACH DATABASE ? AS truth", (str(tmp_path / "truth.sqlite"),))
    r = ex.score(c, "dev", VENDORS)
    assert r == {"n": 1, "match": {"k": 1, "n": 1}, "account": {"k": 1, "n": 1}}


# ── 데이터 재설계 모양이 규칙을 속이는지(규칙은 고정) ─────────────────


def test_decoy_closer_fools_rule():
    """자동이체 — 진짜 고지서는 5일 전, 견적(확정 전)이 1일 전이면 규칙은 견적을 고른다"""
    out = {
        "table": "fin_bank_transactions",
        "at": "2026-11-20 06:00:00",
        "amount": 900000,
        "label": "한결빌딩관리",
    }
    docs = [
        doc(1, "bill", "한결빌딩관리", "2026-11-15", 900000),
        doc(2, "bill", "한결빌딩관리", "2026-11-19", 900000),
    ]
    assert ex.rule_match(out, docs, VENDORS) == [2]  # 정답은 [1]


def test_issuer_alias_fools_month_end():
    out = {
        "table": "fin_bank_transactions",
        "at": "2026-11-30 17:00:00",
        "amount": 300000,
        "label": "바른덴탈상사",
    }
    docs = [doc(1, "statement", "(주)바른덴탈상사", "2026-11-05", 300000)]
    assert ex.rule_match(out, docs, VENDORS) == []  # 정답은 [1]


def test_memo_alias_fools_account():
    assert ex.rule_account({"label": "바른덴탈상"}, VENDORS) is None  # 정답은 재료비


def test_two_orders_one_payment_fools_rule():
    out = {
        "table": "fin_card_expenses",
        "at": "2026-11-10 12:00:00",
        "amount": 50000,
        "label": "페이온*모아덴탈몰",
    }
    docs = [
        doc(1, "order_email", "모아덴탈몰", "2026-11-10", 20000),
        doc(2, "order_email", "모아덴탈몰", "2026-11-10", 30000),
    ]
    assert ex.rule_match(out, docs, VENDORS) == []  # 정답은 [1, 2]
