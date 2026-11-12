"""⑧-2 증빙 탐색 에이전트(backend/expense_agent.py) — 설계 docs/superpowers/specs/2026-11-11-finance-recon-design.md §4.
대본 게이트웨이(test_recon_agent.Script)로 도구 호출 순서를 정해 두고 제출 훅·상한·결과·일괄 대상을 본다."""

import pytest

from backend import expense, expense_agent
from backend.harness.store import RunStore
from tests.backend.test_expense import DAY, Outflows
from tests.backend.test_recon_agent import Script

BANK = "fin_bank_transactions"
LOOK = ("tool", "search_docs", {"purpose": "하늘치재 증빙", "issuer": "하늘치재"})


def submit(doc_ids, account="재료비", lookups=(1,), confidence="high", see=None):
    return (
        "tool",
        expense_agent.SUBMIT,
        {
            "document_ids": list(doc_ids),
            "account": account,
            "confidence": confidence,
            "reasons": [{"text": "같은 날 같은 금액", "lookups": list(lookups)}],
            "see": see,
        },
    )


@pytest.fixture
def o(tmp_path):
    return Outflows(tmp_path / "source.sqlite")


def one(o):
    """출금 하나와 맞는 세금계산서 하나 — (conn, 키, 증빙 id, 후보 밖 증빙 id)"""
    oid = o.bank("2026-11-10 10:00:00", "하늘치재", 50_000)
    d = o.doc("tax_invoice", "하늘치재", "2026-11-09", 50_000)
    far = o.doc("tax_invoice", "하늘치재", "2026-08-01", 50_000)
    c = o.app()
    key = expense.outflow_key(expense._outflow(expense._load(c, None), BANK, oid))
    return c, key, d, far


def run(c, tmp_path, *steps):
    gw = Script(c, *steps)
    out = expense_agent.run_batch(c, gw, DAY, batch=False, state=tmp_path / "s.json")
    return out


def test_submit_saves_match(o, tmp_path):
    c, key, d, _ = one(o)
    assert run(c, tmp_path, LOOK, submit([d]))["done"] == 1
    r = expense_agent.result(c, key)
    assert (r["status"], r["document_ids"], r["account"], r["confidence"]) == (
        "done",
        [d],
        "재료비",
        "high",
    )
    assert r["reasons"][0]["lookups"] == [1] and r["see"] is None
    kinds = [x["kind"] for x in r["lines"]]
    assert kinds == ["hypothesis", "lookup", "conclusion"]
    assert expense_agent.results(c, {key})[key] == r


def test_submit_outside_candidates_blocked(o, tmp_path):
    """후보 밖 증빙 제출 → 훅이 막고 다시 제출하면 통과(확정 때 400이 나지 않게)"""
    c, key, d, far = one(o)
    run(c, tmp_path, LOOK, submit([far]), submit([d]))
    r = expense_agent.result(c, key)
    assert r["document_ids"] == [d]
    steps = RunStore(c).steps(r["run_id"])
    assert [s["rule_id"] for s in steps if s["kind"] == "tool"] == [
        None,
        expense_agent.EVIDENCE_RULE,
        None,
    ]


def test_reason_lookups_must_exist(o, tmp_path):
    c, key, d, _ = one(o)
    run(c, tmp_path, LOOK, submit([d], lookups=[2]), submit([d]))
    steps = RunStore(c).steps(expense_agent.result(c, key)["run_id"])
    assert expense_agent.EVIDENCE_RULE in [s["rule_id"] for s in steps]


def test_unknown_account_blocked(o, tmp_path):
    c, key, d, _ = one(o)
    run(c, tmp_path, LOOK, submit([d], account="없는과목"), submit([d]))
    assert expense_agent.result(c, key)["account"] == "재료비"


def test_empty_documents_is_no_evidence(o, tmp_path):
    c, key, _, _ = one(o)
    run(c, tmp_path, LOOK, submit([], confidence="mid", see="영수증 확인"))
    r = expense_agent.result(c, key)
    assert (r["document_ids"], r["see"]) == ([], "영수증 확인")


def test_call_limit_reads_no_result(o, tmp_path):
    c, key, _, _ = one(o)
    run(c, tmp_path, LOOK)  # 조회만 되풀이
    r = expense_agent.result(c, key)
    assert r["status"] == "handed_off" and r["account"] is None and r["document_ids"] is None
    assert r["tool_calls"] == expense_agent.EXPENSE_MAX_TOOL_CALLS


def test_targets_skip_done_and_month_filter(o, tmp_path):
    c, key, d, _ = one(o)
    assert [expense.outflow_key(x) for x in expense_agent.targets(c, DAY)] == [key]
    assert expense_agent.targets(c, DAY, ["2026-10"]) == []
    run(c, tmp_path, LOOK, submit([d]))
    assert expense_agent.targets(c, DAY) == []
    c.execute("UPDATE ax_agent_runs SET status = 'failed'")
    assert len(expense_agent.targets(c, DAY)) == 1


def test_targets_skip_after_today(o):
    o.bank("2026-12-02 10:00:00", "하늘치재", 50_000)
    assert expense_agent.targets(o.app(), DAY) == []


def test_input_has_no_rule_answer(o, tmp_path):
    c, _, d, _ = one(o)
    gw = Script(c, LOOK, submit([d]))
    expense_agent.run_batch(c, gw, DAY, batch=False, state=tmp_path / "s.json")
    assert "하늘치재" in gw.first and "재료비" not in gw.first and "세금계산서" not in gw.first


def test_prompt_asks_korean_and_no_written_reasoning():
    """v2 — 따로 쓴 글은 한국어 한 줄(화면 조사 과정 줄), 도구 전에 생각 글을 시키지 않는다(AI 실험 기록 §26)"""
    text = expense_agent.spec(expense_agent.registry()[1]).system(expense_agent.PROMPT_VERSION)
    assert expense_agent.PROMPT_VERSION == "v2"
    assert "한국어" in text and "`purpose`" in text and "`가설:`" not in text
