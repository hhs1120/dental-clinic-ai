"""⑧-1 조사 에이전트 조회 도구(backend/recon_tools.py) — 설계 docs/superpowers/specs/2026-11-11-finance-recon-design.md
§3. 손으로 만든 source(test_recon_match.Books)에서 도구를 직접 불러 결과 모양·기간 상한·조회 번호를 본다."""

from datetime import date

import pytest
from pydantic import ValidationError

from backend import recon_match, recon_tools
from backend.harness.loop import ToolContext
from backend.harness.store import RunStore
from generator import master
from tests.backend.test_recon_match import DAY, ISSUER, RATE, Books

SUBMIT = "submit_cause"


@pytest.fixture
def b(tmp_path):
    return Books(tmp_path / "source.sqlite")


def call(conn, name: str, **args):
    """실행 하나를 만들어 그 실행의 도구로 부른다 — 인자는 Pydantic으로 정규화"""
    store = RunStore(conn)
    with conn:  # 대상마다 실행 하나 — 부를 때마다 새 대상
        n = conn.execute("SELECT count(*) FROM ax_agent_runs").fetchone()[0]
        run_id = store.create_run("recon_cause", f"k{n}", "v1", "me")
    tool = {t.name: t for t in recon_tools.tools(SUBMIT)}[name]
    norm = tool.args.model_validate({"purpose": "확인"} | args).model_dump(mode="json")
    return tool.fn(ToolContext(run_id, "recon_cause", "k", store), norm)


def test_tools_read_only():
    ts = recon_tools.tools(SUBMIT)
    assert tuple(t.name for t in ts) == recon_tools.LOOKUPS
    assert not any(t.writes for t in ts)


def test_ledger_day_includes_zero_rows_and_neighbors(b):
    b.pay("2026-11-03 10:00:00", 30_000, "card", no="X")
    b.pay("2026-11-03 11:00:00", 0, "card", due=55_000)
    b.pay("2026-11-05 10:00:00", 20_000, "cash")
    r = call(b.app(), "ledger_day", day="2026-11-03")
    assert [x["amount_paid"] for x in r["rows"]] == [30_000, 0]
    assert r["rows"][1]["amount_due"] == 55_000
    assert r["next_open_day"] == "2026-11-05" and r["prev_open_day"] is None
    assert r["lookup_no"] == 1 and r["summary"]


def test_find_amount_spans_days(b):
    b.pay("2026-11-03 10:00:00", 55_000, "transfer")
    b.bank("2026-11-06 14:00:00", 55_000, "김보호")
    b.bank("2026-11-06 15:00:00", 44_000, "이환자")
    r = call(b.app(), "find_amount", amount=55_000, date_from="2026-11-01", date_to="2026-11-10")
    assert {x["table"] for x in r["rows"]} == {"emr_payments", "fin_bank_transactions"}
    assert len(r["rows"]) == 2


def test_card_deposits_expected_values(b):
    b.pay("2026-11-03 10:00:00", 100_000, "card", no="X")
    b.sale("2026-11-03 10:00:00", 100_000, "X")
    fee = int(100_000 * RATE)
    b.deposit("2026-11-03", 100_000, fee=fee + 300)
    r = call(b.app(), "card_deposits", sales_date="2026-11-03")
    [d] = r["rows"]
    assert d["issuer"] == ISSUER
    assert d["approved_total"] == 100_000
    assert d["fee"] - d["expected_fee"] == 300
    cfg = master.CARD_ISSUERS[ISSUER]
    assert (
        d["expected_on"]
        == master.add_bank_days(date(2026, 11, 3), cfg["settle_days"], set()).isoformat()
    )
    assert d["bank_in"] is True


def test_visits_balance_family(b):
    a = b.appt(1, "2026-11-03")
    b.treatment(1, a, "2026-11-03", "U0005", "11")
    b.pay("2026-10-01 10:00:00", 10_000, "card", due=40_000)
    conn = b.app()
    v = call(conn, "visits_day", day="2026-11-03")
    assert v["rows"][0]["patient_id"] == 1 and v["rows"][0]["procedures"]
    assert call(conn, "patient_balance", patient_id=1, before="2026-11-03")["balance"] == 30_000
    f = call(conn, "family", patient_id=1)
    assert f["guardian"]["name"] == "김보호"
    assert call(conn, "oral_care_prices")["prices"]


def test_range_limits():
    by = {t.name: t for t in recon_tools.tools(SUBMIT)}
    with pytest.raises(ValidationError):
        by["find_amount"].args.model_validate(
            {"purpose": "p", "amount": 1, "date_from": "2026-11-01", "date_to": "2026-12-03"}
        )
    with pytest.raises(ValidationError):
        by["card_sales"].args.model_validate(
            {"purpose": "p", "date_from": "2026-11-01", "date_to": "2026-11-09"}
        )
    with pytest.raises(ValidationError):
        by["bank_tx"].args.model_validate(
            {"purpose": "p", "date_from": "2026-11-01", "date_to": "2026-11-16"}
        )


def test_range_limit_is_inclusive():
    """상한 = 날짜 차이 N일까지 — 모델이 ±15일(차이 30)·31일 차이로 부르는 흔한 꼴이 막히지 않게"""
    by = {t.name: t for t in recon_tools.tools(SUBMIT)}
    by["find_amount"].args.model_validate(
        {"purpose": "p", "amount": 1, "date_from": "2026-11-01", "date_to": "2026-12-02"}
    )
    by["card_sales"].args.model_validate(
        {"purpose": "p", "date_from": "2026-11-01", "date_to": "2026-11-08"}
    )


def test_purpose_required_checked_optional():
    """조회마다 purpose(확인하려는 것)는 필수, checked(직전 결과가 맞았는지)는 선택 — 조사 과정 줄의 가설·비교(설계 §3)"""
    for t in recon_tools.tools(SUBMIT):
        with pytest.raises(ValidationError):
            t.args.model_validate({"day": "2026-11-03"})
        with pytest.raises(ValidationError):
            t.args.model_validate({"purpose": "x" * (recon_tools.NOTE_MAX + 1)})
    by = {t.name: t for t in recon_tools.tools(SUBMIT)}
    a = by["oral_care_prices"].args.model_validate({"purpose": "조합", "checked": "맞음"})
    assert a.purpose == "조합" and a.checked == "맞음"
    assert by["oral_care_prices"].args.model_validate({"purpose": "조합"}).checked is None


def test_summaries_show_filters(b):
    """조회 줄 요약에 조건이 보인다 — 카드 승인 금액 범위, 정산 카드사(설계 §3 "조회 도구와 조건")"""
    conn = b.app()
    r = call(
        conn,
        "card_sales",
        date_from="2026-11-03",
        date_to="2026-11-03",
        amount_min=3_000,
        amount_max=4_000,
    )
    assert "3,000~4,000원" in r["summary"]
    assert ISSUER in call(conn, "card_deposits", sales_date="2026-11-03", issuer=ISSUER)["summary"]


def test_oral_care_combos(b):
    """금액이 구강용품 몇 개의 합으로 정확히 되는지 코드가 계산한다(개수 제한 없이 — 3개 이상 판매도)"""
    conn = b.app()
    prices = master.ORAL_CARE_PRICES
    a, c = sorted(prices.values())[:2]
    r = call(conn, "oral_care_prices", amount=a * 2 + c)
    assert r["combos"] and all(
        sum(prices[n] * k for n, k in x.items()) == a * 2 + c for x in r["combos"]
    )
    none = call(conn, "oral_care_prices", amount=1)
    assert none["combos"] == [] and "조합 없음" in none["summary"]
    assert call(conn, "oral_care_prices")["combos"] is None


def _step(tool, status="pass", err=0):
    return {"kind": "tool", "tool": tool, "hook_status": status, "is_error": err}


def test_lookup_numbers_skip_errors():
    steps = [
        {"kind": "input"},
        _step("ledger_day"),
        _step("card_sales", err=1),
        _step("bank_tx", status="blocked", err=1),
        _step(SUBMIT),
    ]
    assert [s["tool"] for s in recon_tools.lookup_steps(steps, SUBMIT)] == ["ledger_day"]


def test_input_text_has_no_rule_cause(b):
    b.pay("2026-11-03 10:00:00", 3_500, "card")
    b.sale("2026-11-03 10:00:00", 35_000, "R1")
    b.settle("2026-11-03")
    books = recon_match.load(b.app(), DAY)
    [item] = recon_match.detect(books)
    text = recon_tools.input_text(item, books, DAY)
    assert "3,500" in text and "35,000" in text
    assert "amount_typo" not in text and "금액 오입력" not in text
