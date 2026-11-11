"""⑧-1 재무 대조 짝짓기(backend/recon_match.py) — 설계 docs/superpowers/specs/2026-11-11-finance-recon-design.md §1.
손으로 만든 source(실제 스키마 + master 값)에 장부 수납·카드 승인·정산·통장 입금을 한 줄씩 더해, 원인마다 불일치 건이
하나로 묶이는지와 디코이가 건이 되지 않는지를 본다. 공휴일은 비운다(master_values_rows() 기본)."""

import json
from datetime import date, datetime

import pytest

from backend import recon_match
from generator import master
from generator.paths import ROOT
from tests.backend.test_missed import Source

DAY = date(2026, 12, 1)
ISSUER = "가온카드"  # 율 0.012 · 정산 2 은행 영업일
RATE = master.CARD_ISSUERS[ISSUER]["fee_rate"]


class Books(Source):
    """장부·금융 행을 더하는 도우미 — 정산·현금 입금은 settle()이 장부·승인에서 맞게 만든다"""

    def __init__(self, path):
        super().__init__(path)
        self.patient(1)
        self.patient(2)
        self.conn.execute("UPDATE emr_patients SET name = '김보호' WHERE id = 2")
        self.conn.execute("UPDATE emr_patients SET guardian_patient_id = 2 WHERE id = 1")

    def pay(self, at, amount, method, *, due=None, no=None, pid=1, kind="visit") -> int:
        return self.conn.execute(
            "INSERT INTO emr_payments (patient_id, kind, amount_due, amount_paid, paid_at, method,"
            " approval_no) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (pid, kind, amount if due is None else due, amount, at, method, no),
        ).lastrowid

    def sale(self, at, amount, no, *, cancel_of=None, issuer=ISSUER) -> int:
        return self.conn.execute(
            "INSERT INTO fin_card_sales (approved_at, issuer, amount, approval_no, cancel_of)"
            " VALUES (?, ?, ?, ?, ?)",
            (at, issuer, amount, no, cancel_of),
        ).lastrowid

    def bank(self, at, amount, memo) -> int:
        return self.conn.execute(
            "INSERT INTO fin_bank_transactions (occurred_at, direction, amount, memo)"
            " VALUES (?, 'in', ?, ?)",
            (at, amount, memo),
        ).lastrowid

    def deposit(
        self, sales_date, total, *, fee=None, on=None, issuer=ISSUER, bank=True, sales_total=None
    ) -> int:
        d = date.fromisoformat(sales_date)
        cfg = master.CARD_ISSUERS[issuer]
        on = on or master.add_bank_days(d, cfg["settle_days"], set()).isoformat()
        fee = int(total * cfg["fee_rate"]) if fee is None else fee
        if bank:
            self.bank(f"{on} 08:00:00", total - fee, issuer)
        return self.conn.execute(
            "INSERT INTO fin_card_deposits (deposited_on, issuer, sales_date, sales_total, fee,"
            " amount) VALUES (?, ?, ?, ?, ?, ?)",
            (
                on,
                issuer,
                sales_date,
                total if sales_total is None else sales_total,
                fee,
                total - fee,
            ),
        ).lastrowid

    def settle(self, *days, skip=()):
        """그날 승인의 정산(취소 차감)과 그 기간 장부 현금의 통장 입금 — skip에 든 날짜는 만들지 않는다"""
        for d in days:
            if d in skip:
                continue
            total = self.conn.execute(
                "SELECT sum(CASE WHEN cancel_of IS NULL THEN amount ELSE -amount END)"
                " FROM fin_card_sales WHERE date(approved_at) = ?",
                (d,),
            ).fetchone()[0]
            if total:
                self.deposit(d, total)
        cash: dict[str, int] = {}
        for at, amount in self.conn.execute(
            "SELECT paid_at, amount_paid FROM emr_payments WHERE method = 'cash' AND amount_paid > 0"
        ):
            dep = master.cash_deposit_day(date.fromisoformat(at[:10]), set()).isoformat()
            cash[dep] = cash.get(dep, 0) + amount
        return cash

    def cash_in(self, cash: dict[str, int], **change):
        """settle()이 돌려준 현금 입금 — change로 입금일별 금액을 바꿔 넣는다"""
        for dep, amount in cash.items():
            self.bank(f"{dep} 13:10:00", change.get(dep, amount), master.CASH_DEPOSIT_MEMO)


@pytest.fixture
def b(tmp_path):
    return Books(tmp_path / "source.sqlite")


def detect(b: Books) -> list[dict]:
    conn = b.app()
    return recon_match.detect(recon_match.load(conn, DAY))


def refs(item) -> set[str]:
    return {r["table"] for r in item["fin"]}


def clean_day(b: Books, d="2026-11-03"):
    b.pay(f"{d} 10:00:00", 50_000, "card", no="11111111")
    b.sale(f"{d} 10:00:00", 50_000, "11111111")
    b.pay(f"{d} 11:00:00", 30_000, "cash")
    b.pay(f"{d} 12:00:00", 20_000, "transfer")
    b.bank(f"{d} 12:00:00", 20_000, "환자1")


def test_bank_helpers_match_master():
    cfg_holidays = json.loads((ROOT / "config" / "holidays.json").read_text(encoding="utf-8"))
    holidays = {date.fromisoformat(x) for x in cfg_holidays}
    d = date(2026, 8, 1)
    while d < DAY:
        assert recon_match.bank_day(d, holidays) == master.bank_day(d, holidays)
        for n in (2, 3):
            assert recon_match.add_bank_days(d, n, holidays) == master.add_bank_days(
                d, n, holidays
            )
        assert recon_match.cash_deposit_day(
            d, list(master.CASH_DEPOSIT_WEEKDAYS), holidays
        ) == master.cash_deposit_day(d, holidays)
        d = date.fromordinal(d.toordinal() + 1)


def test_clean_day_no_items(b):
    clean_day(b)
    b.cash_in(b.settle("2026-11-03"))
    assert detect(b) == []


def test_split_payment_one_item(b):
    p = b.pay("2026-11-03 10:00:00", 50_000, "card", no="X")
    b.sale("2026-11-03 10:00:00", 20_000, "X")
    b.sale("2026-11-03 10:00:00", 30_000, "Y")
    b.settle("2026-11-03")
    [item] = detect(b)
    assert item["payment_ids"] == [p]
    assert [r["table"] for r in item["fin"]] == ["fin_card_sales", "fin_card_sales"]
    assert item["channel"] == "card"


def test_cancel_reapprove_detected_with_zero_diff(b):
    b.pay("2026-11-03 10:00:00", 30_000, "card", no="X")
    b.sale("2026-11-03 10:00:00", 30_000, "X")
    b.sale("2026-11-03 10:01:00", 30_000, "X", cancel_of="X")
    b.sale("2026-11-03 10:02:00", 30_000, "Z")
    b.settle("2026-11-03")
    [item] = detect(b)
    assert len(item["fin"]) == 3
    assert item["ledger_total"] == item["fin_total"] == 30_000


def test_fee_mismatch_item(b):
    b.pay("2026-11-03 10:00:00", 100_000, "card", no="X")
    b.sale("2026-11-03 10:00:00", 100_000, "X")
    b.deposit("2026-11-03", 100_000, fee=int(100_000 * (RATE + 0.003)))
    [item] = detect(b)
    assert item["channel"] == "settlement"
    assert item["date"] == "2026-11-03"
    assert refs(item) == {"fin_card_deposits", "fin_bank_transactions"}


def test_deposit_delay_item(b):
    b.pay("2026-11-03 10:00:00", 100_000, "card", no="X")
    b.sale("2026-11-03 10:00:00", 100_000, "X")
    b.deposit("2026-11-03", 100_000, on="2026-11-06")  # 예상 11-05
    [item] = detect(b)
    assert item["channel"] == "settlement"
    assert item["date"] == "2026-11-03"


def test_fee_rounding_matches_generator(b):
    """라온카드 23,000 × 0.011 = 252.999…(float) — int()는 252, round()는 253. 생성기와 같은 int()여야 오탐이 없다"""
    total, raon = 23_000, "라온카드"
    assert int(total * master.CARD_ISSUERS[raon]["fee_rate"]) != round(
        total * master.CARD_ISSUERS[raon]["fee_rate"]
    )
    b.pay("2026-11-03 10:00:00", total, "card", no="X")
    b.sale("2026-11-03 10:00:00", total, "X", issuer=raon)
    b.deposit("2026-11-03", total, issuer=raon)
    assert detect(b) == []


def test_amount_typo_card(b):
    b.pay("2026-11-03 10:00:00", 3_500, "card")  # 손 입력(승인번호 없음), 0 하나 빠뜨림
    b.sale("2026-11-03 10:00:00", 35_000, "R1")
    b.settle("2026-11-03")
    [item] = detect(b)
    assert item["ledger_total"] == 3_500 and item["fin_total"] == 35_000


def test_amount_typo_cash(b):
    b.pay("2026-11-03 10:00:00", 1_800, "cash")
    cash = b.settle()
    b.cash_in(cash, **{"2026-11-09": 18_000})
    [item] = detect(b)
    assert item["channel"] == "cash"
    assert item["date"] == "2026-11-03"  # 그 입금 기간의 마지막 진료일


def test_date_shift_transfer_neighbor_open_day(b):
    b.pay(
        "2026-11-03 09:00:00", 10_000, "card", no="A"
    )  # 11-03·11-05만 진료일, 11-04는 장부 행 없음
    b.sale("2026-11-03 09:00:00", 10_000, "A")
    p = b.pay("2026-11-05 13:47:00", 80_000, "transfer")
    b.bank("2026-11-03 11:00:00", 80_000, "환자1")
    b.settle("2026-11-03")
    [item] = detect(b)
    assert item["dates"] == ["2026-11-03", "2026-11-05"]
    assert item["date"] == "2026-11-03"
    assert item["payment_ids"] == [p]


def test_method_wrong_cash_vs_card(b):
    p = b.pay("2026-11-03 11:30:00", 18_000, "cash")  # 실제로는 카드
    b.pay("2026-11-04 10:00:00", 10_000, "cash")
    b.sale("2026-11-03 11:30:00", 18_000, "R1")
    cash = b.settle("2026-11-03")
    b.cash_in(cash, **{"2026-11-09": 10_000})
    [item] = detect(b)
    assert p in item["payment_ids"]  # 현금 건은 그 입금 기간의 장부 현금 행을 모두 담는다
    assert {"fin_card_sales", "fin_bank_transactions"} <= refs(item)
    assert item["date"] == "2026-11-03"  # 카드 날(기간 마지막 진료일 11-04보다 이름)


def test_entry_missing_zero_row_not_paired(b):
    b.pay("2026-11-03 12:30:00", 0, "card", due=36_000)  # 받은 금액 0원 진료 행
    b.sale("2026-11-03 12:30:00", 36_000, "R1")
    b.settle("2026-11-03")
    [item] = detect(b)
    assert item["payment_ids"] == []
    assert refs(item) == {"fin_card_sales"}


def test_guardian_name_transfer_not_item(b):
    b.pay("2026-11-03 12:00:00", 20_000, "transfer", pid=1)
    b.bank("2026-11-03 12:00:00", 20_000, "김보호")
    assert detect(b) == []


def test_weekly_cash_deposit_not_item(b):
    for d in ("2026-11-02", "2026-11-03", "2026-11-04"):
        b.pay(f"{d} 10:00:00", 10_000, "cash")
    cash = b.settle()
    assert cash == {"2026-11-09": 30_000}
    b.cash_in(cash)
    assert detect(b) == []


@pytest.mark.parametrize(("minutes", "items"), [(29, 0), (31, 2)])
def test_transfer_window(b, minutes, items):
    b.pay("2026-11-03 12:00:00", 20_000, "transfer")
    b.bank(f"2026-11-03 12:{minutes:02d}:00", 20_000, "환자1")
    assert len(detect(b)) == items


def test_pending_day_not_detected(b):
    b.pay("2026-11-28 10:00:00", 10_000, "card", no="X")
    b.sale(
        "2026-11-28 10:00:00", 99_000, "Q"
    )  # 장부와 안 맞지만 정산이 기준일(12-01) 이후라 대조 전
    conn = b.app()
    books = recon_match.load(conn, DAY)
    assert recon_match.day_status(books, date(2026, 11, 28)) == "pending"
    assert recon_match.detect(books) == []


def test_nhis_and_card_deposit_not_transfer(b):
    clean_day(b)
    b.bank("2026-11-03 10:00:00", 500_000, master.NHIS_MEMO)
    b.cash_in(b.settle("2026-11-03"))
    assert detect(b) == []


def test_key_stable(b):
    b.pay("2026-11-03 10:00:00", 3_500, "card")
    b.sale("2026-11-03 10:00:00", 35_000, "R1")
    b.pay("2026-11-02 10:00:00", 2_000, "card")
    b.sale("2026-11-02 10:00:00", 20_000, "R2")
    b.settle("2026-11-02", "2026-11-03")
    conn = b.app()
    first = recon_match.detect(recon_match.load(conn, DAY))
    second = recon_match.detect(recon_match.load(conn, DAY))
    assert [i["key"] for i in first] == [i["key"] for i in second]
    assert len({i["key"] for i in first}) == 2


def _count(conn) -> int:
    seen: list[str] = []
    conn.set_trace_callback(seen.append)
    recon_match.load(conn, DAY)
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
            bk.sale(at, 10_000, f"N{i}")
        counts.append(_count(bk.app()))
    assert counts[0] == counts[1]


def test_cash_period_with_pending_day_not_detected(b):
    """입금 기간에 대조 전 날이 섞이면(11-27은 카드 정산이 기준일 뒤) 그 입금은 통째로 대조 전"""
    b.pay("2026-11-25 10:00:00", 10_000, "cash")
    b.pay("2026-11-27 10:00:00", 20_000, "cash")
    cash = b.settle()
    assert cash == {"2026-11-30": 30_000}
    b.cash_in(cash)
    assert detect(b) == []


def test_missing_deposit_item(b):
    """카드 매출은 있는데 정산 입금이 아예 안 옴(예상일이 기준일 전) — 승인 행으로 정산 건"""
    b.pay("2026-11-03 10:00:00", 100_000, "card", no="X")
    s = b.sale("2026-11-03 10:00:00", 100_000, "X")
    [item] = detect(b)
    assert item["channel"] == "settlement"
    assert item["fin"] == [{"table": "fin_card_sales", "id": s}]
    assert item["date"] == "2026-11-03"


def test_deposit_without_bank_row_item(b):
    """정산 행은 맞는데 통장에 그 입금이 없음"""
    b.pay("2026-11-03 10:00:00", 100_000, "card", no="X")
    b.sale("2026-11-03 10:00:00", 100_000, "X")
    b.deposit("2026-11-03", 100_000, bank=False)
    [item] = detect(b)
    assert item["channel"] == "settlement"
    assert refs(item) == {"fin_card_deposits"}


def test_orphan_cancel_is_kept(b):
    """다음 날 취소(같은 날 원승인 없음) — 버리지 않고 금융 행으로 남는다"""
    b.pay("2026-11-03 10:00:00", 30_000, "card", no="X")
    b.sale("2026-11-03 10:00:00", 30_000, "X")
    c = b.sale("2026-11-04 10:00:00", 30_000, "X", cancel_of="X")
    b.settle("2026-11-03", "2026-11-04")
    found = detect(b)
    assert any({"table": "fin_card_sales", "id": c} in i["fin"] for i in found)


def test_distant_cash_deposits_not_shift(b):
    """+5,000 입금과 −5,000 입금 사이에 정상 입금이 끼면 날짜 밀림 한 건으로 묶지 않는다"""
    b.pay("2026-10-06 10:00:00", 10_000, "cash")
    b.pay("2026-10-13 10:00:00", 10_000, "cash")
    b.pay("2026-10-20 10:00:00", 10_000, "cash")
    cash = b.settle()
    assert list(cash) == ["2026-10-12", "2026-10-19", "2026-10-26"]
    b.cash_in(cash, **{"2026-10-12": 15_000, "2026-10-26": 5_000})
    assert len(detect(b)) == 2


def test_two_cash_deposits_same_day(b):
    """같은 날 현금 입금 두 줄은 합쳐서 본다(하나를 덮어쓰지 않는다)"""
    b.pay("2026-11-03 10:00:00", 30_000, "cash")
    b.bank("2026-11-09 13:10:00", 10_000, master.CASH_DEPOSIT_MEMO)
    b.bank("2026-11-09 15:00:00", 20_000, master.CASH_DEPOSIT_MEMO)
    assert detect(b) == []
