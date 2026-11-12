"""⑧-2 지출 증빙 — 규칙 기준선(backend/expense.py)·화면 집계·사람 확정(재무 대조 설계 §2)."""

import json
from datetime import date

import pytest

from backend import expense
from generator.clinic import master_values_rows
from generator.master import VENDORS
from tests.backend.test_missed import Source


def test_rule_from_master_values_equals_master():
    """앱(ax_master_values 거래처 표)과 평가(master.VENDORS)가 같은 거래처 사전을 만든다 — 규칙 = 채점한 기준선"""
    mv = {n: json.loads(v) for n, v in master_values_rows()}
    a, b = expense.vendors_by_label(mv["expense_vendors"]), expense.vendors_by_label(VENDORS)
    assert set(a) == set(b)
    assert all(
        {k: a[x][k] for k in ("name", "pay", "account")}
        == {k: b[x][k] for k in ("name", "pay", "account")}
        for x in a
    )


# ── 화면 집계·확정·오늘 화면 줄 (설계 §2) ─────────────────────────────────

DAY = date(2026, 12, 1)  # 앱 기준일 — 달 첫날이어도 오늘 화면 줄은 지난달
AT = "2026-12-01 09:00:00"


class Outflows(Source):
    """통장 출금·병원 카드 사용·증빙을 한 줄씩 더하는 도우미(master 값은 Source가 싣는다)"""

    def bank(self, at, memo, amount) -> int:
        return self.conn.execute(
            "INSERT INTO fin_bank_transactions (occurred_at, direction, amount, memo)"
            " VALUES (?, 'out', ?, ?)",
            (at, amount, memo),
        ).lastrowid

    def card(self, at, merchant, amount) -> int:
        return self.conn.execute(
            "INSERT INTO fin_card_expenses (used_at, merchant, amount) VALUES (?, ?, ?)",
            (at, merchant, amount),
        ).lastrowid

    def doc(self, kind, issuer, on, amount) -> int:
        return self.conn.execute(
            "INSERT INTO fin_expense_documents (kind, issuer, issued_on, amount, body)"
            " VALUES (?, ?, ?, ?, ?)",
            (kind, issuer, on, amount, f"{issuer} {amount}원"),
        ).lastrowid


@pytest.fixture
def o(tmp_path):
    return Outflows(tmp_path / "source.sqlite")


def test_unknown_label_needs_check_then_remembered(o):
    """거래처 표에 없는 가맹점명 — 과목이 비고 확인 필요, 한 번 확정하면 같은 글자의 다음 출금에 채워진다"""
    o.card("2026-11-03 12:00:00", "KCP*솔밭식당", 18_000)
    o.card("2026-11-20 12:00:00", "KCP*솔밭식당", 21_000)
    c = o.app()
    first, second = expense.sheet(c, DAY, "2026-11")["rows"]
    assert (first["account"], first["account_from"], first["check"]) == (None, None, True)
    assert first["vendor"] is None
    expense.confirm(c, first["table"], first["id"], [], "복리후생비", by="desk", at=AT, today=DAY)
    first, second = expense.sheet(c, DAY, "2026-11")["rows"]
    assert (first["account"], first["account_from"]) == ("복리후생비", "confirmed")
    assert first["confirmed"] == {"by": "desk", "at": AT}
    assert (second["account"], second["account_from"], second["check"]) == (
        "복리후생비",
        "memory",
        False,
    )
    assert second["confirmed"] is None


def test_by_item_vendor_never_remembered(o):
    """품목으로 정하는 쇼핑몰 — 복리후생비로 확정해도 다음 주문은 거래처 과목, 늘 확인 필요"""
    o.card("2026-11-03 12:00:00", "페이온*모아덴탈몰", 30_000)
    o.card("2026-11-20 12:00:00", "페이온*모아덴탈몰", 40_000)
    c = o.app()
    first = expense.sheet(c, DAY, "2026-11")["rows"][0]
    assert (first["account"], first["vendor"], first["check"]) == ("소모품비", "모아덴탈몰", True)
    expense.confirm(c, first["table"], first["id"], [], "복리후생비", by="desk", at=AT, today=DAY)
    second = expense.sheet(c, DAY, "2026-11")["rows"][1]
    assert (second["account"], second["account_from"], second["check"]) == (
        "소모품비",
        "vendor",
        True,
    )


def test_month_end_mismatch_none_then_pick_many(o):
    """월말 이체가 그달 명세서 합계와 안 맞으면 증빙 없음 — 후보(창 밖이어도 그달 그 거래처 증빙)에서 여러 장 확정"""
    oid = o.bank("2026-11-30 10:00:00", "바른덴탈상사", 100_000)
    early = o.doc("statement", "바른덴탈상사", "2026-11-02", 30_000)  # 출금 28일 전 — 창 밖
    mid = o.doc("statement", "바른덴탈상사", "2026-11-20", 40_000)
    late = o.doc("statement", "바른덴탈상사", "2026-11-25", 50_000)
    o.doc("statement", "하늘치재", "2026-10-01", 100_000)  # 다른 거래처·창 밖
    c = o.app()
    row = expense.sheet(c, DAY, "2026-11")["rows"][0]
    assert (row["docs"], row["diff"], row["account"]) == ([], None, "재료비")
    cands = expense.candidates(c, "fin_bank_transactions", oid)
    assert [d["id"] for d in cands] == [late, mid, early]  # 날짜 가까운 순
    expense.confirm(
        c, "fin_bank_transactions", oid, [mid, late], "재료비", by="desk", at=AT, today=DAY
    )
    sheet = expense.sheet(c, DAY, "2026-11")
    row = sheet["rows"][0]
    assert ([d["id"] for d in row["docs"]], row["diff"]) == ([mid, late], 10_000)
    assert sheet["chips"]["found"] == 1 and sheet["chips"]["open"] == 0


def test_confirm_rejects_bad_input(o):
    """지금 출금·과목 목록·후보 증빙과 맞지 않는 확정은 ValueError"""
    oid = o.card("2026-11-03 12:00:00", "솔밭식당", 18_000)
    near = o.doc("receipt", "솔밭식당", "2026-11-03", 18_000)
    far = o.doc("receipt", "솔밭식당", "2026-08-01", 18_000)
    c = o.app()
    t = "fin_card_expenses"
    for args in (
        (t, oid + 99, [near], "복리후생비"),  # 없는 출금
        ("fin_card_sales", oid, [near], "복리후생비"),  # 출금 표가 아님
        (t, oid, [near], "간식비"),  # 모르는 과목
        (t, oid, [far], "복리후생비"),  # 후보 밖 증빙
    ):
        with pytest.raises(ValueError):
            expense.confirm(c, *args, by="desk", at=AT, today=DAY)
    expense.confirm(c, t, oid, [near], "복리후생비", by="desk", at=AT, today=DAY)
    assert c.execute("SELECT COUNT(*) FROM ax_expense_confirmations").fetchone()[0] == 1


def test_open_summary_is_last_month_only(o):
    """오늘 화면 줄 — 기준일의 지난달 출금 중 증빙 없음·과목 확인 필요이면서 확정 안 된 수"""
    o.card("2026-10-10 12:00:00", "KCP*낯선가게", 9_000)  # 10월 — 세지 않음
    none = o.card("2026-11-05 12:00:00", "솔밭식당", 50_000)  # 영수증 없음
    o.card("2026-11-06 12:00:00", "페이온*모아덴탈몰", 30_000)  # 품목으로 정함
    o.doc("order_email", "모아덴탈몰", "2026-11-06", 30_000)
    o.card("2026-11-07 12:00:00", "솔밭식당", 60_000)  # 증빙 찾음·과목 정해짐
    o.doc("receipt", "솔밭식당", "2026-11-07", 60_000)
    c = o.app()
    assert expense.open_summary(c, DAY) == {"count": 2, "month": "2026-11"}
    expense.confirm(c, "fin_card_expenses", none, [], "복리후생비", by="desk", at=AT, today=DAY)
    assert expense.open_summary(c, DAY) == {"count": 1, "month": "2026-11"}


def test_open_summary_zero(o):
    o.card("2026-11-07 12:00:00", "솔밭식당", 60_000)
    o.doc("receipt", "솔밭식당", "2026-11-07", 60_000)
    assert expense.open_summary(o.app(), DAY) == {"count": 0, "month": None}


def test_sheet_matches_rule(o):
    """행의 증빙 = rule_match, 거래처 기본 과목 = rule_account — 화면의 규칙 = 채점한 기준선"""
    o.bank("2026-11-30 10:00:00", "바른덴탈상사", 90_000)
    o.doc("statement", "바른덴탈상사", "2026-11-10", 40_000)
    o.doc("statement", "바른덴탈상사", "2026-11-20", 50_000)
    o.doc("tax_invoice", "바른덴탈상사", "2026-11-30", 90_000)
    o.bank("2026-11-25 09:00:00", "한결빌딩관", 3_300_000)  # 잘린 적요
    o.doc("tax_invoice", "한결빌딩관리", "2026-11-24", 3_300_000)
    o.card("2026-11-12 12:00:00", "솔밭식당", 44_000)
    o.doc("receipt", "솔밭식당", "2026-11-11", 44_000)
    o.doc("receipt", "솔밭식당", "2026-11-14", 44_000)
    c = o.app()
    mv = {n: json.loads(v) for n, v in master_values_rows()}
    vendors = expense.vendors_by_label(mv["expense_vendors"])
    docs = [
        dict(r)
        for r in c.execute("SELECT id, kind, issuer, issued_on, amount FROM fin_expense_documents")
    ]
    sheet = expense.sheet(c, DAY, "2026-11")
    assert len(sheet["rows"]) == 3
    for r in sheet["rows"]:
        out = {"table": r["table"], "at": r["at"], "amount": r["amount"], "label": r["label"]}
        assert [d["id"] for d in r["docs"]] == expense.rule_match(out, docs, vendors)
        assert r["account"] == expense.rule_account(out, vendors)
    # 시각순: 식당 · 잘린 적요(거래처 없음 — 금액·날짜로 증빙은 찾고 과목은 빔) · 월말 이체(명세서 + 세금계산서)
    assert [len(r["docs"]) for r in sheet["rows"]] == [1, 1, 3]
    assert [r["check"] for r in sheet["rows"]] == [False, True, False]
    assert sheet["rows"][2]["diff"] == 0  # 명세서 합 — 같은 거래의 세금계산서는 더하지 않는다
    assert sheet["chips"] == {
        "outflows": 3,
        "amount": 90_000 + 3_300_000 + 44_000,
        "found": 3,
        "none": 0,
        "check": 1,
        "differ": 0,  # AI 결과 없음
        "open": 3,
    }
    assert sheet["accounts"] == mv["expense_accounts"]


def test_months_and_unknown_month(o):
    o.card("2026-10-10 12:00:00", "솔밭식당", 9_000)
    o.card("2026-11-10 12:00:00", "솔밭식당", 9_000)
    o.card("2026-12-01 12:00:00", "솔밭식당", 9_000)  # 기준일 당일부터는 아직
    c = o.app()
    got = expense.sheet(c, DAY)
    assert (got["months"], got["month"]) == (["2026-10", "2026-11"], "2026-11")
    with pytest.raises(ValueError):
        expense.sheet(c, DAY, "2026-12")


def test_month_end_candidates_include_alias_issuer_whole_month(o):
    """월말 이체 후보 = 창 안 증빙 + 출금한 달 발행 증빙 전부 — 발행처 별칭((주)…)·잘린 적요여도 사람이 고를 수 있다"""
    oid = o.bank("2026-11-30 10:00:00", "바른덴탈상사", 100_000)
    alias = o.doc("statement", "(주)바른덴탈상사", "2026-11-02", 60_000)  # 창 밖·별칭
    cut = o.bank("2026-11-30 11:00:00", "하늘치", 50_000)  # 잘린 적요 — 거래처 모름
    sky = o.doc("statement", "(주)하늘치재", "2026-11-03", 50_000)
    o.doc("statement", "(주)바른덴탈상사", "2026-10-02", 60_000)  # 다른 달·창 밖
    c = o.app()
    assert alias in [d["id"] for d in expense.candidates(c, "fin_bank_transactions", oid)]
    assert sky in [d["id"] for d in expense.candidates(c, "fin_bank_transactions", cut)]
    expense.confirm(
        c, "fin_bank_transactions", oid, [alias], "재료비", by="desk", at=AT, today=DAY
    )


def test_label_containing_by_item_vendor_never_remembered(o):
    """결제대행사가 덧붙은 쇼핑몰 표기(KCP*페이온*모아덴탈몰) — 거래처 표에 없어도 품목으로 정하는 거래처라 기억 안 함"""
    o.card("2026-11-03 12:00:00", "KCP*페이온*모아덴탈몰", 30_000)
    o.card("2026-11-20 12:00:00", "KCP*페이온*모아덴탈몰", 40_000)
    c = o.app()
    first = expense.sheet(c, DAY, "2026-11")["rows"][0]
    assert (first["account"], first["check"], first["by_item"]) == (None, True, True)
    expense.confirm(c, first["table"], first["id"], [], "복리후생비", by="desk", at=AT, today=DAY)
    second = expense.sheet(c, DAY, "2026-11")["rows"][1]
    assert (second["account"], second["account_from"], second["check"]) == (None, None, True)


def test_chips_count_only_unconfirmed_work(o):
    """과목 확인 필요·증빙 못 찾음은 남은 일 — 확정한 줄은 세지 않는다(오늘 화면 줄과 같은 기준)"""
    shop = o.card("2026-11-03 12:00:00", "페이온*모아덴탈몰", 30_000)
    none = o.card("2026-11-05 12:00:00", "솔밭식당", 50_000)
    c = o.app()
    assert {k: expense.sheet(c, DAY)["chips"][k] for k in ("none", "check", "open")} == {
        "none": 2,  # 둘 다 증빙 없음
        "check": 1,
        "open": 2,
    }
    expense.confirm(c, "fin_card_expenses", shop, [], "소모품비", by="desk", at=AT, today=DAY)
    expense.confirm(c, "fin_card_expenses", none, [], "복리후생비", by="desk", at=AT, today=DAY)
    sheet = expense.sheet(c, DAY)
    assert {k: sheet["chips"][k] for k in ("none", "check", "open")} == {
        "none": 0,
        "check": 0,
        "open": 0,
    }
    assert [r["check"] for r in sheet["rows"]] == [False, False]


# ── 확정 제한·출금 키 (설계 §4) ─────────────────────────────────────


def test_confirm_rejects_doc_owned_by_other_outflow(o):
    """같은 증빙은 한 출금에만 — 다른 출금에 살아 있는 확정이 있으면 막고, 같은 출금 덮어쓰기는 된다"""
    a = o.bank("2026-11-10 10:00:00", "하늘치재", 50_000)
    b = o.bank("2026-11-12 10:00:00", "하늘치재", 50_000)
    d = o.doc("tax_invoice", "하늘치재", "2026-11-09", 50_000)
    c = o.app()
    bank = "fin_bank_transactions"
    expense.confirm(c, bank, a, [d], "재료비", by="desk", at=AT, today=DAY)
    with pytest.raises(ValueError, match="이미 다른 출금에 확정된 증빙"):
        expense.confirm(c, bank, b, [d], "재료비", by="desk", at=AT, today=DAY)
    expense.confirm(c, bank, a, [d], "소모품비", by="desk", at=AT, today=DAY)


def test_confirm_rejects_outflow_after_today(o):
    oid = o.bank("2026-12-02 10:00:00", "하늘치재", 50_000)
    with pytest.raises(ValueError, match="기준일 이후 출금"):
        expense.confirm(
            o.app(), "fin_bank_transactions", oid, [], "재료비", by="desk", at=AT, today=DAY
        )


def test_outflow_key_stable_and_distinct():
    a = {
        "table": "fin_bank_transactions",
        "at": "2026-11-10 10:00:00",
        "amount": 50_000,
        "label": "하늘치재",
    }
    assert expense.outflow_key(a) == expense.outflow_key(dict(a))
    assert len(expense.outflow_key(a)) == 12
    assert expense.outflow_key(a) != expense.outflow_key(a | {"at": "2026-11-10 10:01:00"})


# ── AI 제안·규칙과 다름·자동 확정 (설계 §4) ─────────────────────────────


def _ai(c, tmp_path, *steps):
    from backend import expense_agent
    from tests.backend.test_recon_agent import Script

    expense_agent.run_batch(c, Script(c, *steps), DAY, batch=False, state=tmp_path / "s.json")


def _submit(ids, account):
    from tests.backend.test_expense_agent import submit

    return submit(ids, account=account)


LOOK = ("tool", "search_docs", {"purpose": "같은 금액 증빙"})


def _phone(o):
    oid = o.bank("2026-11-10 10:00:00", "이음통신", 33_000)
    d = o.doc("bill", "이음통신", "2026-11-05", 33_000)
    return oid, d


def test_row_has_agent_and_agree(o, tmp_path):
    oid, d = _phone(o)
    c = o.app()
    assert expense.sheet(c, DAY, "2026-11")["rows"][0]["agent"] is None
    _ai(c, tmp_path, LOOK, _submit([d], "통신비"))
    row = expense.sheet(c, DAY, "2026-11")["rows"][0]
    assert row["key"] == expense.outflow_key(row) and row["agree"] is True
    a = row["agent"]
    assert (a["document_ids"], a["account"], a["confidence"]) == ([d], "통신비", "high")
    assert a["docs"][0]["id"] == d and a["lines"]


def test_differ_chip_and_open_summary_counts_differ(o, tmp_path):
    """규칙은 고지서를 찾았지만 AI는 증빙 없음 — 규칙과 다름, 지난달 오늘 화면 수에 든다"""
    oid, d = _phone(o)
    c = o.app()
    _ai(c, tmp_path, LOOK, _submit([], "통신비"))
    sheet = expense.sheet(c, DAY, "2026-11")
    assert sheet["rows"][0]["agree"] is False and sheet["chips"]["differ"] == 1
    assert expense.open_summary(c, DAY)["count"] == 1


def test_open_summary_skips_agreeing_rows(o, tmp_path):
    _phone(o)
    c = o.app()
    assert expense.open_summary(c, DAY)["count"] == 0  # 규칙이 찾고 과목도 정해짐
    d = c.execute("SELECT id FROM source.fin_expense_documents").fetchone()[0]
    _ai(c, tmp_path, LOOK, _submit([d], "통신비"))
    assert expense.open_summary(c, DAY)["count"] == 0


def test_auto_row_shows_auto_result(o, tmp_path):
    from backend import autoclose, expense_auto

    oid, d = _phone(o)
    c = o.app()
    autoclose.set_policy(c, expense_auto.KIND, True)
    _ai(c, tmp_path, LOOK, _submit([d], "통신비"))
    row = expense.sheet(c, DAY, "2026-11")["rows"][0]
    assert row["auto"]["id"] and row["account_from"] == "auto" and row["confirmed"] is None
    assert expense.sheet(c, DAY, "2026-11")["chips"]["open"] == 0


def test_sheet_query_count_independent_of_rows(tmp_path):
    counts = []
    for n in (3, 30):
        (tmp_path / str(n)).mkdir()
        s = Outflows(tmp_path / str(n) / "source.sqlite")
        for i in range(n):
            s.bank(f"2026-11-{1 + i % 28:02d} 10:00:00", "이음통신", 1_000 + i)
        c = s.app()
        seen: list[str] = []
        c.set_trace_callback(seen.append)
        expense.sheet(c, DAY, "2026-11")
        c.set_trace_callback(None)
        counts.append(len(seen))
    assert counts[0] == counts[1]
