"""⑧-2 규칙 비교·자동 확정(backend/expense_auto.py) — 설계 docs/superpowers/specs/2026-11-11-finance-recon-design.md §4
"자동 확정". 규칙 = AI(증빙 집합·과목)이고 금액 차이 0·품목으로 정하지 않는 거래처·증빙을 다른 후보와 나누지 않을 때만,
정책 기본 끔, 사람 확정은 자동 확정을 되돌린다."""

import pytest

from backend import api, autoclose, expense, expense_agent, expense_auto
from tests.backend.test_expense import AT, DAY, Outflows
from tests.backend.test_expense_agent import submit
from tests.backend.test_recon_agent import Script

BANK = "fin_bank_transactions"
CARD = "fin_card_expenses"
LOOK = ("tool", "search_docs", {"purpose": "같은 금액 증빙"})


@pytest.fixture
def o(tmp_path):
    return Outflows(tmp_path / "source.sqlite")


def key_of(c, table, oid) -> str:
    return expense.outflow_key(expense._outflow(expense._load(c, None), table, oid))


def investigate(c, tmp_path, *steps):
    expense_agent.run_batch(c, Script(c, *steps), DAY, batch=False, state=tmp_path / "s.json")


def phone(o):
    """통신비 자동이체 한 건 + 같은 금액 고지서 — 규칙이 고지서를 찾는다"""
    oid = o.bank("2026-11-10 10:00:00", "이음통신", 33_000)
    d = o.doc("bill", "이음통신", "2026-11-05", 33_000)
    return oid, d


def on(c):
    autoclose.set_policy(c, expense_auto.KIND, True)


def test_agree_compares_sorted_sets():
    a = {"document_ids": [1, 3], "account": "재료비"}
    assert expense_auto.agree([3, 1], "재료비", a)
    assert not expense_auto.agree([3, 1], "소모품비", a)
    assert not expense_auto.agree([3], "재료비", a)
    assert not expense_auto.agree([], None, None)
    assert not expense_auto.agree([], "통신비", {"document_ids": None, "account": None})


def test_refresh_off_by_default(o, tmp_path):
    oid, d = phone(o)
    c = o.app()
    investigate(c, tmp_path, LOOK, submit([d], account="통신비"))
    assert expense_auto.refresh(c, DAY) == {"off": True}


def test_autoconfirm_when_rule_equals_agent(o, tmp_path):
    oid, d = phone(o)
    c = o.app()
    investigate(c, tmp_path, LOOK, submit([d], account="통신비"))
    on(c)
    assert expense_auto.refresh(c, DAY)["closed"] == 1
    k = key_of(c, BANK, oid)
    assert expense_auto.active(c, {k})[k]["document_ids"] == [d]


def test_batch_refreshes_auto(o, tmp_path):
    """일괄이 끝나면 자동 확정을 다시 계산한다"""
    oid, d = phone(o)
    c = o.app()
    on(c)
    investigate(c, tmp_path, LOOK, submit([d], account="통신비"))
    assert expense_auto.active(c, {key_of(c, BANK, oid)})


def test_different_doc_or_account_not_auto(o, tmp_path):
    oid, d = phone(o)
    c = o.app()
    investigate(c, tmp_path, LOOK, submit([d], account="지급수수료"))  # 과목이 거래처 기본과 다름
    on(c)
    assert expense_auto.refresh(c, DAY)["closed"] == 0


def test_by_item_vendor_not_auto(o, tmp_path):
    """쇼핑몰은 규칙 = AI여도 자동 확정 없음(거래처 표에 기본 과목이 있어도)"""
    o.card("2026-11-10 12:00:00", "페이온*모아덴탈몰", 30_000)
    d = o.doc("order_email", "모아덴탈몰", "2026-11-10", 30_000)
    c = o.app()
    investigate(c, tmp_path, LOOK, submit([d], account="소모품비"))
    on(c)
    assert expense_auto.refresh(c, DAY)["closed"] == 0


def test_shared_doc_candidates_not_auto(o, tmp_path):
    """두 출금이 같은 증빙을 고르면 둘 다 사람에게"""
    a = o.bank("2026-11-10 10:00:00", "이음통신", 33_000)
    b = o.bank("2026-11-11 10:00:00", "이음통신", 33_000)
    d = o.doc("bill", "이음통신", "2026-11-09", 33_000)
    c = o.app()
    investigate(c, tmp_path, LOOK, submit([d], account="통신비"))
    on(c)
    assert expense_auto.refresh(c, DAY)["closed"] == 0
    assert expense_auto.active(c, {key_of(c, BANK, a), key_of(c, BANK, b)}) == {}


def test_human_confirm_reverts_auto(o, tmp_path):
    oid, d = phone(o)
    c = o.app()
    investigate(c, tmp_path, LOOK, submit([d], account="통신비"))
    on(c)
    expense_auto.refresh(c, DAY)
    expense.confirm(c, BANK, oid, [d], "지급수수료", by="desk", at=AT, today=DAY)
    k = key_of(c, BANK, oid)
    assert expense_auto.active(c, {k}) == {}
    row = c.execute("SELECT reverted_by FROM ax_auto_actions WHERE target = ?", (k,)).fetchone()
    assert row["reverted_by"] == expense_auto.HUMAN_REVERT
    assert expense_auto.refresh(c, DAY)["closed"] == 0  # 사람 확정은 다시 닫지 않는다


def test_auto_doc_counts_as_owned(o, tmp_path):
    """되돌리지 않은 자동 확정이 가진 증빙은 다른 출금에 확정할 수 없다(확정 제한)"""
    oid, d = phone(o)
    other = o.bank("2026-11-12 10:00:00", "이음통신", 33_000)
    c = o.app()
    investigate(c, tmp_path, LOOK, submit([d], account="통신비"))
    on(c)
    c.execute(  # 두 번째 출금의 AI 결과를 없앤다(실패로) — 첫 출금만 자동 확정 후보
        "UPDATE ax_agent_runs SET status = 'failed' WHERE target = ?", (key_of(c, BANK, other),)
    )
    expense_auto.refresh(c, DAY)
    assert expense_auto.active(c, {key_of(c, BANK, oid)})
    with pytest.raises(ValueError, match="이미 다른 출금"):
        expense.confirm(c, BANK, other, [d], "통신비", by="desk", at=AT, today=DAY)


def test_auto_ignores_missing_outflows(o, tmp_path):
    oid, d = phone(o)
    c = o.app()
    investigate(c, tmp_path, LOOK, submit([d], account="통신비"))
    on(c)
    expense_auto.refresh(c, DAY)
    assert expense_auto.active(c, {"other"}) == {}  # 지금 화면에 없는 키는 세지 않는다


def test_revert_endpoint_accepts_expense_auto():
    assert expense_auto.KIND in set(api.AUTO_KINDS)
