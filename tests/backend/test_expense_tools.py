"""⑧-2 증빙 탐색 에이전트 조회 도구(backend/expense_tools.py) — 설계 docs/superpowers/specs/2026-11-11-finance-recon-design.md
§4. 손으로 만든 source(test_expense.Outflows)에서 도구를 직접 불러 후보 범위·입력 글을 본다."""

import pytest
from pydantic import ValidationError

from backend import expense, expense_tools
from backend.harness.loop import ToolContext
from backend.harness.store import RunStore
from tests.backend.test_expense import Outflows

SUBMIT = "submit_match"
BANK = "fin_bank_transactions"


@pytest.fixture
def o(tmp_path):
    return Outflows(tmp_path / "source.sqlite")


def _key(conn, table: str, oid: int) -> str:
    data = expense._load(conn, None)
    return expense.outflow_key(expense._outflow(data, table, oid))


def call(conn, key: str, name: str, **args):
    """그 출금을 대상으로 실행 하나를 만들어 도구를 부른다 — 인자는 Pydantic으로 정규화"""
    store = RunStore(conn)
    with conn:
        run_id = store.create_run("expense_match", key, "v1", "me")
    tool = {t.name: t for t in expense_tools.tools(SUBMIT)}[name]
    norm = tool.args.model_validate({"purpose": "확인"} | args).model_dump(mode="json")
    out = tool.fn(ToolContext(run_id, "expense_match", key, store), norm)
    with conn:
        store.finish(run_id, "done", "me")
    return out


def test_search_limited_to_candidates(o):
    """검색은 그 출금의 확정 후보 범위만 — 창 밖·다른 달은 안 나오고, 같은 달 별칭 발행처((주)…)는 나온다"""
    oid = o.bank("2026-11-30 10:00:00", "하늘치재", 50_000)
    alias = o.doc("tax_invoice", "(주)하늘치재", "2026-11-02", 50_000)  # 창 밖이지만 같은 달
    o.doc("tax_invoice", "하늘치재", "2026-10-01", 50_000)  # 다른 달·창 밖
    c = o.app()
    r = call(c, _key(c, BANK, oid), "search_docs", issuer="하늘치재")
    assert [d["id"] for d in r["rows"]] == [alias]
    assert "body" not in r["rows"][0] and r["lookup_no"] == 1 and r["summary"]


def test_search_filters_amount_and_text(o):
    oid = o.bank("2026-11-10 10:00:00", "바른덴탈상사", 30_000)
    hit = o.doc("statement", "바른덴탈상사", "2026-11-08", 30_000)
    o.doc("statement", "바른덴탈상사", "2026-11-07", 90_000)
    c = o.app()
    key = _key(c, BANK, oid)
    by_amount = call(c, key, "search_docs", amount_min=20_000, amount_max=40_000)
    assert [d["id"] for d in by_amount["rows"]] == [hit]
    assert [d["id"] for d in call(c, key, "search_docs", text="30000원")["rows"]] == [hit]


def test_read_doc_outside_candidates_errors(o):
    oid = o.bank("2026-11-10 10:00:00", "하늘치재", 50_000)
    inside = o.doc("tax_invoice", "하늘치재", "2026-11-09", 50_000)
    outside = o.doc("tax_invoice", "하늘치재", "2026-08-01", 50_000)
    c = o.app()
    key = _key(c, BANK, oid)
    assert call(c, key, "read_doc", document_id=inside)["body"]
    with pytest.raises(ValueError):
        call(c, key, "read_doc", document_id=outside)


def test_vendors_and_accounts(o):
    oid = o.bank("2026-11-10 10:00:00", "하늘치재", 50_000)
    c = o.app()
    key = _key(c, BANK, oid)
    vs = call(c, key, "vendors")["rows"]
    assert {"name", "card_name", "pay", "account", "by_item"} <= set(vs[0])
    assert "재료비" in call(c, key, "accounts")["accounts"]


def test_purpose_required():
    for t in expense_tools.tools(SUBMIT):
        with pytest.raises(ValidationError):
            t.args.model_validate({})


def test_input_text_has_no_rule_answer(o):
    """규칙이 증빙을 찾는 출금이어도 입력에는 출금만 — 증빙 id·발행처 증빙·과목·꼬리표가 없다(설계 §4)"""
    oid = o.bank("2026-11-10 10:00:00", "하늘치재", 50_000)
    d = o.doc("tax_invoice", "하늘치재", "2026-11-09", 50_000)
    c = o.app()
    data = expense._load(c, None)
    text = expense_tools.input_text(expense._outflow(data, BANK, oid))
    assert "하늘치재" in text and "50,000원" in text and "2026-11-10" in text
    assert d and "증빙" not in text and "재료비" not in text
    assert "못 찾음" not in text and "확인 필요" not in text and "세금계산서" not in text
