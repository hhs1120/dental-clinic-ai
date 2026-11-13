"""⑤-3 서류 사유 차트 문장(backend/claim_evidence.py) — 설계 docs/superpowers/specs/2026-11-10-claim-review-design.md §3.
입력에 금액·조정 결과·판단이 없는지, 서류 사유 건만 고르는지, 인용 후검증·빈 답을 본다."""

import json
from datetime import date, timedelta

import pytest

from backend import claim_evidence as ce
from generator.master import ADJUST_REASONS, APPEAL_DAYS
from shared.llm import LLMResult
from tests.backend.test_claims import add_claim
from tests.backend.test_missed import Source

DAY = date(2026, 12, 1)
MODEL = "claude-sonnet-5-5"
D01 = ADJUST_REASONS["D01"]["note"]  # 내역설명 — 서류 사유
R05 = ADJUST_REASONS["R05"]["note"]  # 발치 난이도 — 요건 사유
C01 = ADJUST_REASONS["C01"]["note"]  # 상병 착오
NOTE = "치주낭 5mm, 출혈 있음, 치석 많음. 잇몸치료 시행함. 잇몸치료 2회 예정."
STD = (
    "# 공통 분야 청구 기준\n\n## 내역설명 기재\n\n"
    "- 요건을 충족했음을 보여 주는 근거를 청구 명세의 내역설명란에 적는다.\n"
)


def llm(data):
    return LLMResult(True, "", data, MODEL, False, 1, 1, 0.001, 1)


class FakeGw:
    config = {"aliases": {"chat": [MODEL]}, "models": {MODEL: {}}}

    def __init__(self, *turns):
        self.turns, self.calls = list(turns), []

    def complete(self, alias, **kw):
        self.calls.append(kw)
        return self.turns.pop(0) if self.turns else llm({"quotes": []})


@pytest.fixture
def src(tmp_path):
    s = Source(tmp_path / "source.sqlite")
    s.conn.execute(
        "INSERT INTO ax_documents (id, type, title, version, effective_date, path, body)"
        " VALUES (1, 'claim_standard', '공통 분야 청구 기준', '1', '2024-01-01', 'x.md', ?)",
        (STD,),
    )
    return s


def reviewed_for(days_left: int) -> str:
    return (DAY + timedelta(days=days_left) - timedelta(days=APPEAL_DAYS)).isoformat()


def claim(src, pid, *, note=D01, days_left=10, text=NOTE, status="adjusted"):
    cid = add_claim(src, pid, status=status, note=note, reviewed=reviewed_for(days_left))
    src.conn.execute(
        "UPDATE emr_chart_notes SET note = ? WHERE treatment_id ="
        " (SELECT treatment_id FROM emr_claims WHERE id = ?)",
        (text, cid),
    )
    return cid


def build(conn, gw, scope="all"):
    return ce.build(conn, gw, ce.targets(conn, DAY, scope=scope), use_batch=False)


def test_facts_hide_amount_and_judgment(src):
    claim(src, 1)
    conn = src.app()
    (row,) = ce.targets(conn, DAY, scope="app")
    f = ce.evidence_facts(row)
    assert set(f) == {"procedure", "tooth", "note", "standard"}
    assert f["standard"]["heading"] == "내역설명 기재" and "내역설명란" in f["standard"]["body"]
    blob = json.dumps(f, ensure_ascii=False)
    assert D01 not in blob and "adjusted" not in blob and "10000" not in blob


def test_targets_documentation_only(src):
    doc = claim(src, 1)
    claim(src, 2, note=R05)
    claim(src, 3, note=C01)
    old = claim(src, 4, days_left=-3)  # 기한 지남
    denied = claim(src, 5, status="denied")
    conn = src.app()
    assert {r["claim_id"] for r in ce.targets(conn, DAY, scope="app")} == {doc, denied}
    assert {r["claim_id"] for r in ce.targets(conn, DAY, scope="all")} == {doc, old, denied}


def test_build_and_results(src):
    cid = claim(src, 1)
    conn = src.app()
    gw = FakeGw(llm({"quotes": ["치주낭 5mm, 출혈 있음", "잇몸치료 2회 예정"]}))
    out = build(conn, gw)
    assert (out["called"], out["failed"]) == (1, 0)
    got = ce.results(conn, [cid])[cid]
    assert got["quotes"] == ["치주낭 5mm, 출혈 있음", "잇몸치료 2회 예정"] and got["valid"]
    assert got["prompt_version"] == ce.PROMPT_VERSION
    assert gw.calls[0]["schema"]["properties"]["quotes"]["maxItems"] == 3


def test_quote_not_in_chart_retried_then_none(src):
    cid = claim(src, 1)
    conn = src.app()
    bad = llm({"quotes": ["차트에 없는 문장입니다"]})
    assert build(conn, FakeGw(bad, bad))["failed"] == 1
    got = ce.results(conn, [cid])[cid]
    assert got["quotes"] is None and not got["valid"]


def test_empty_answer_kept(src):
    """옮겨 적을 문장이 없으면 빈 목록 — 통과한 답이다(방사선 증빙처럼 차트에 없는 자료)"""
    cid = claim(src, 1)
    conn = src.app()
    build(conn, FakeGw(llm({"quotes": []})))
    assert ce.results(conn, [cid])[cid]["quotes"] == []


def test_todo_ids_app(src):
    cid = claim(src, 1)
    claim(src, 2, days_left=-3)
    conn = src.app()
    assert ce.todo_ids(conn, DAY, MODEL) == {cid}
    build(conn, FakeGw(), scope="app")
    assert ce.todo_ids(conn, DAY, MODEL) == set()
