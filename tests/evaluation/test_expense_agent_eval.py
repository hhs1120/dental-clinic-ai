"""⑧-2 AI 채점(evaluation/expense.py score_agent, 평가 설계 §5 ⑧-2, 설계 §4) — 규칙 기준선과 나란히, 짝 비교·규칙 결과
묶음별·확신도별·자동 확정 오류율·실행 없음. 손으로 만든 출금·증빙·정답에 대본 게이트웨이로 AI 결과를 남긴다."""

import json
import sqlite3
from datetime import date

import pytest

from backend import expense, expense_agent
from evaluation import expense as ex
from generator.paths import ROOT
from tests.backend.test_expense import Outflows
from tests.backend.test_expense_agent import submit
from tests.backend.test_recon_agent import Script

DAY = date(2026, 12, 1)
BANK = "fin_bank_transactions"
LOOK = ("tool", "search_docs", {"purpose": "같은 금액 증빙"})


@pytest.fixture
def world(tmp_path):
    """두 출금 — 이음통신(규칙이 고지서를 찾음, 통과) · 새빛전화(규칙이 디코이 고지서를 고름, 정답은 다른 고지서)"""
    o = Outflows(tmp_path / "source.sqlite")
    a = o.bank("2026-09-10 10:00:00", "이음통신", 33_000)
    da = o.doc("bill", "이음통신", "2026-09-05", 33_000)
    b = o.bank("2026-09-20 10:00:00", "새빛전화", 22_000)
    decoy = o.doc("bill", "새빛전화", "2026-09-19", 22_000)  # 규칙이 고르는 가까운 날
    right = o.doc("bill", "새빛전화", "2026-09-15", 22_000)
    c = o.app()
    t = sqlite3.connect(tmp_path / "truth.sqlite")
    t.executescript((ROOT / "ground_truth" / "schema.sql").read_text("utf-8"))
    t.executemany(
        "INSERT INTO truth_expense_matches VALUES (?, ?, ?, ?, ?)",
        [
            (BANK, a, json.dumps([da]), "통신비", "dev"),
            (BANK, b, json.dumps([right]), "통신비", "dev"),
        ],
    )
    t.commit()
    t.close()
    c.execute("ATTACH DATABASE ? AS truth", (str(tmp_path / "truth.sqlite"),))
    return c, tmp_path, {"a": a, "b": b, "da": da, "decoy": decoy, "right": right}


def key(c, oid):
    return expense.outflow_key(expense._outflow(expense._load(c, None), BANK, oid))


class ByTarget(Script):
    """실행 대상(출금 키)마다 다른 대본 — {키: 단계들}"""

    def __init__(self, conn, plans: dict):
        super().__init__(conn, LOOK)
        self.conn, self.plans = conn, plans

    def complete(self, alias, **kw):
        target = self.conn.execute(
            "SELECT target FROM ax_agent_runs WHERE id = ?", (kw["ref"][1],)
        ).fetchone()[0]
        self.steps = list(self.plans[target])
        return super().complete(alias, **kw)


def ai(c, tmp_path, picks: dict, confidence="high"):
    """picks = {출금 id: (증빙 ids, 과목)}"""
    plans = {
        key(c, oid): (LOOK, submit(ids, account=acct, confidence=confidence))
        for oid, (ids, acct) in picks.items()
    }
    gw = ByTarget(c, plans)
    expense_agent.run_batch(c, gw, DAY, batch=False, state=tmp_path / "s.json")


def test_agent_score_and_pair(world):
    c, tmp, ids = world
    ai(c, tmp, {ids["a"]: ([ids["da"]], "통신비"), ids["b"]: ([ids["right"]], "통신비")})
    r = ex.score_agent(c, "dev")
    assert r["rule"]["match"] == {"k": 1, "n": 2} and r["agent"]["match"] == {"k": 2, "n": 2}
    assert r["agent"]["account"] == {"k": 2, "n": 2}
    assert r["pair_match"] == {"rescued": 1, "lost": 0, "both": 1, "neither": 0}
    assert r["missing"] == 0 and r["prompt_version"] == expense_agent.PROMPT_VERSION


def test_by_group_and_confidence(world):
    c, tmp, ids = world
    ai(c, tmp, {ids["a"]: ([ids["da"]], "통신비"), ids["b"]: ([ids["right"]], "통신비")}, "mid")
    r = ex.score_agent(c, "dev")
    assert r["by_group"]["passed"]["rule_match"] == {"k": 1, "n": 2}
    assert r["by_group"]["passed"]["agent_match"] == {"k": 2, "n": 2}
    assert r["by_confidence"]["mid"]["match"] == {"k": 2, "n": 2}
    assert r["by_confidence"]["high"]["match"] == {"k": 0, "n": 0}


def test_auto_error_counts_condition_rows_only(world):
    """AI가 규칙과 같은 디코이를 고르면 자동 확정 조건을 넘지만 틀림 — 오류 1/2"""
    c, tmp, ids = world
    ai(c, tmp, {ids["a"]: ([ids["da"]], "통신비"), ids["b"]: ([ids["decoy"]], "통신비")})
    r = ex.score_agent(c, "dev")
    assert r["auto"] == {"k": 1, "n": 2}


def test_missing_run_counts_wrong(world):
    c, _, _ = world
    r = ex.score_agent(c, "dev")
    assert r["missing"] == 2 and r["agent"]["match"] == {"k": 0, "n": 2}
    assert r["pair_match"]["lost"] == 1


def test_test_split_hides_rows(world):
    c, _, _ = world
    c.execute("UPDATE truth.truth_expense_matches SET split = 'test'")
    r = ex.score_agent(c, "test")
    assert r["rows"] is None and r["agent"]["match"]["n"] == 2


def test_render_has_table(world):
    from evaluation.eval import _render_expense

    c, _, _ = world
    text = "\n".join(_render_expense(ex.score_agent(c, "dev")))
    assert "⑧-2" in text and "규칙" in text and "AI" in text
