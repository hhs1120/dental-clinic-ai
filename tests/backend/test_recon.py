"""⑧-1 재무 대조 규칙 원인·일계표·사람 확정(backend/recon.py) — 설계 docs/superpowers/specs/2026-11-11-finance-recon-design.md
§1. 규칙 원인은 규칙 기준선이라(평가 설계 §5 ⑧-1) 원인마다 한 시나리오, 규칙 순서가 정해진 대로인지를 본다."""

from datetime import datetime

import pytest

from backend import recon, recon_match
from generator import master
from tests.backend.test_recon_match import DAY, RATE, Books, clean_day


@pytest.fixture
def b(tmp_path):
    return Books(tmp_path / "source.sqlite")


def items(b: Books):
    conn = b.app()
    books = recon_match.load(conn, DAY)
    return conn, books, recon_match.detect(books)


def only_cause(b: Books) -> str:
    _, books, found = items(b)
    [item] = found
    return recon.rule_cause(item, books)


def split_payment(b):
    b.pay("2026-11-03 10:00:00", 50_000, "card", no="X")
    b.sale("2026-11-03 10:00:00", 20_000, "X")
    b.sale("2026-11-03 10:00:00", 30_000, "Y")
    b.settle("2026-11-03")


def cancel_reapprove(b):
    b.pay("2026-11-03 10:00:00", 30_000, "card", no="X")
    b.sale("2026-11-03 10:00:00", 30_000, "X")
    b.sale("2026-11-03 10:01:00", 30_000, "X", cancel_of="X")
    b.sale("2026-11-03 10:02:00", 30_000, "Z")
    b.settle("2026-11-03")


def fee_mismatch(b):
    b.pay("2026-11-03 10:00:00", 100_000, "card", no="X")
    b.sale("2026-11-03 10:00:00", 100_000, "X")
    b.deposit("2026-11-03", 100_000, fee=int(100_000 * (RATE + 0.003)))


def deposit_delay(b):
    b.pay("2026-11-03 10:00:00", 100_000, "card", no="X")
    b.sale("2026-11-03 10:00:00", 100_000, "X")
    b.deposit("2026-11-03", 100_000, on="2026-11-06")


def amount_typo_card(b):
    b.pay("2026-11-03 10:00:00", 3_500, "card")
    b.sale("2026-11-03 10:00:00", 35_000, "R1")
    b.settle("2026-11-03")


def amount_typo_cash(b):
    b.pay("2026-11-03 10:00:00", 1_800, "cash")
    b.pay("2026-11-04 10:00:00", 5_000, "cash")
    b.cash_in(b.settle(), **{"2026-11-09": 23_000})


def date_shift(b):
    b.pay("2026-11-03 09:00:00", 10_000, "card", no="A")
    b.sale("2026-11-03 09:00:00", 10_000, "A")
    b.pay("2026-11-05 13:47:00", 80_000, "transfer")
    b.bank("2026-11-03 11:00:00", 80_000, "환자1")
    b.settle("2026-11-03")


def method_wrong(b):
    b.pay("2026-11-03 11:30:00", 18_000, "cash")
    b.pay("2026-11-04 10:00:00", 10_000, "cash")
    b.sale("2026-11-03 11:30:00", 18_000, "R1")
    b.cash_in(b.settle("2026-11-03"), **{"2026-11-09": 10_000})


def entry_missing(b):
    b.pay("2026-11-03 12:30:00", 0, "card", due=36_000)
    b.sale("2026-11-03 12:30:00", 36_000, "R1")
    b.settle("2026-11-03")


def non_clinical_sale(b):
    b.pay("2026-11-03 09:00:00", 10_000, "card", no="A")
    b.sale("2026-11-03 09:00:00", 10_000, "A")
    b.sale("2026-11-03 15:20:00", 13_000, "R9")  # 시린이 치약 8,500 + 치간칫솔 4,500
    b.settle("2026-11-03")


@pytest.mark.parametrize(
    ("build", "cause"),
    [
        (split_payment, "split_payment"),
        (cancel_reapprove, "cancel_reapprove"),
        (fee_mismatch, "fee_mismatch"),
        (deposit_delay, "deposit_delay"),
        (amount_typo_card, "amount_typo"),
        (amount_typo_cash, "amount_typo"),
        (date_shift, "date_shift"),
        (method_wrong, "method_wrong"),
        (entry_missing, "entry_missing"),
        (non_clinical_sale, "non_clinical_sale"),
    ],
)
def test_rule_cause_each(b, build, cause):
    build(b)
    assert only_cause(b) == cause


def test_rule_order_entry_missing_beats_oral_care(b):
    b.pay("2026-11-03 15:00:00", 0, "card", due=13_000)
    b.sale("2026-11-03 15:20:00", 13_000, "R9")
    b.settle("2026-11-03")
    assert only_cause(b) == "entry_missing"


def test_rule_fallbacks(tmp_path):
    fin_only = Books(tmp_path / "a.sqlite")
    fin_only.sale("2026-11-03 15:20:00", 77_000, "R9")  # 가격표 합이 아니고 0원 행도 없음
    fin_only.settle("2026-11-03")
    assert only_cause(fin_only) == "entry_missing"
    ledger_only = Books(tmp_path / "b.sqlite")
    ledger_only.pay("2026-11-03 12:00:00", 20_000, "transfer")  # 통장에 없음
    assert only_cause(ledger_only) == "amount_typo"


def test_ledger_day_columns(b):
    clean_day(b, "2026-11-03")
    b.pay("2026-11-04 10:00:00", 5_000, "cash")
    b.cash_in(b.settle("2026-11-03"))
    conn = b.app()
    r = recon.ledger(conn, DAY, "2026-11")
    days = {d["date"]: d for d in r["days"]}
    assert days["2026-11-03"]["card"] == {"ledger": 50_000, "fin": 50_000, "diff": 0}
    assert days["2026-11-03"]["transfer"] == {"ledger": 20_000, "fin": 20_000, "diff": 0}
    assert days["2026-11-03"]["cash"]["fin"] is None  # 입금 기간 마지막 진료일(11-04)에만
    assert days["2026-11-04"]["cash"] == {
        "ledger": 5_000,
        "fin": 35_000,
        "diff": 0,
        "deposit": {"on": "2026-11-09", "days": 2},
    }
    assert all(d["status"] == "ok" and d["items"] == [] for d in r["days"])


def test_ledger_zero_diff_day_has_badge_items(b):
    cancel_reapprove(b)
    r = recon.ledger(b.app(), DAY, "2026-11")
    [day] = r["days"]
    assert day["card"]["diff"] == 0
    [item] = day["items"]
    assert item["rule_cause"] == "cancel_reapprove"
    assert "parts" not in item
    assert {x["label"] for x in item["fin_rows"]} >= {"가온카드", "가온카드 취소"}
    assert r["chips"]["mismatch_days"] == 1


def test_ledger_pending_days(b):
    b.pay("2026-11-28 10:00:00", 10_000, "card", no="X")
    b.sale("2026-11-28 10:00:00", 99_000, "Q")
    r = recon.ledger(b.app(), DAY, "2026-11")
    [day] = r["days"]
    assert day["status"] == "pending" and day["items"] == []


def test_ledger_month_default_and_list(b):
    b.pay("2026-10-05 10:00:00", 10_000, "cash")
    b.pay("2026-11-03 10:00:00", 10_000, "cash")
    b.cash_in(b.settle())
    conn = b.app()
    r = recon.ledger(conn, DAY)
    assert r["months"] == ["2026-10", "2026-11"]
    assert r["month"] == "2026-11"
    assert r["causes"] == master.RECON_CAUSE_GROUPS
    with pytest.raises(ValueError):
        recon.ledger(conn, DAY, "2025-01")


def test_confirm_overwrite_and_shown(b):
    amount_typo_card(b)
    split_payment_day = "2026-11-02"
    b.pay(f"{split_payment_day} 10:00:00", 2_000, "card")
    b.sale(f"{split_payment_day} 10:00:00", 20_000, "R2")
    b.settle(split_payment_day)
    conn = b.app()
    assert recon.open_count(conn, DAY) == 2
    key = recon.ledger(conn, DAY)["days"][-1]["items"][0]["key"]
    recon.confirm(conn, key, "amount_typo", "데스크", "2026-12-01 09:00", DAY)
    recon.confirm(conn, key, "entry_missing", "원장", "2026-12-01 09:05", DAY)
    conn.commit()
    [row] = conn.execute("SELECT * FROM ax_recon_confirmations").fetchall()
    assert row["proposed_by"] == "rule" and row["proposed_cause"] == "amount_typo"
    item = next(i for d in recon.ledger(conn, DAY)["days"] for i in d["items"] if i["key"] == key)
    assert item["confirmation"] == {
        "cause": "entry_missing",
        "proposed_cause": "amount_typo",
        "by": "원장",
        "at": "2026-12-01 09:05",
    }
    assert recon.open_count(conn, DAY) == 1


def test_confirm_rejects_unknown(b):
    amount_typo_card(b)
    conn = b.app()
    key = recon.ledger(conn, DAY)["days"][0]["items"][0]["key"]
    for k, cause in [(key, "nope"), (key, "unknown"), ("000000000000", "amount_typo")]:
        with pytest.raises(ValueError):
            recon.confirm(conn, k, cause, "데스크", "2026-12-01 09:00", DAY)


def _count(conn) -> int:
    seen: list[str] = []
    conn.set_trace_callback(seen.append)
    recon.ledger(conn, DAY, "2026-11")
    recon.open_count(conn, DAY)
    conn.set_trace_callback(None)
    return len(seen)


def test_query_count_independent_of_rows(tmp_path):
    counts = []
    for n in (10, 300):
        (tmp_path / str(n)).mkdir()
        bk = Books(tmp_path / str(n) / "source.sqlite")
        for i in range(n):
            at = datetime(2026, 11, 2 + i % 20, 10, i % 60).strftime("%Y-%m-%d %H:%M:%S")
            bk.pay(at, 10_000, "card", no=f"N{i}")
            bk.sale(at, 10_000 if i % 7 else 20_000, f"N{i}")
        counts.append(_count(bk.app()))
    assert counts[0] == counts[1]


def test_tenfold_only_same_channel(b):
    """이체 장부 80,000(날짜 밀림)과 같은 날 구강용품 카드 8,000은 10배여도 묶지 않는다 — 결제수단이 다르다"""
    b.pay("2026-11-03 09:00:00", 10_000, "card", no="A")
    b.sale("2026-11-03 09:00:00", 10_000, "A")
    b.pay("2026-11-05 09:00:00", 10_000, "card", no="B")
    b.sale("2026-11-05 09:00:00", 10_000, "B")
    b.bank("2026-11-03 11:00:00", 80_000, "환자1")
    b.pay("2026-11-05 13:47:00", 80_000, "transfer")
    b.sale("2026-11-05 15:00:00", 8_000, "R9")  # 칫솔 3,000 + 치실 5,000
    b.settle("2026-11-03", "2026-11-05")
    _, books, found = items(b)
    assert sorted(recon.rule_cause(i, books) for i in found) == ["date_shift", "non_clinical_sale"]
