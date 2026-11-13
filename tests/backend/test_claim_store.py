"""⑤ AI 점검 공용 저장·실행(backend/claim_store.py) — 2b(claim_omission)·⑤-3 차트 문장(claim_evidence)이 같이 쓴다.
작은 가짜 Kind로 저장·재사용·한 번 더·서술 없는 줄을 본다(설계 docs/superpowers/specs/2026-11-10-claim-review-design.md §2b·§3)."""

import json

import pytest

from backend import claim_check, claim_store
from shared.llm import LLMResult
from tests.backend.test_claims import add_claim
from tests.backend.test_missed import Source

MODEL = "claude-sonnet-5-5"
NOTE = "치주낭 5mm, 출혈 있음. 잇몸치료 시행함."


def llm(data):
    return LLMResult(True, "", data, MODEL, False, 1, 1, 0.001, 1)


class FakeGw:
    config = {"aliases": {"chat": [MODEL]}, "models": {MODEL: {}}}

    def __init__(self, *turns):
        self.turns, self.calls = list(turns), []

    def complete(self, alias, **kw):
        self.calls.append(kw)
        return self.turns.pop(0) if self.turns else llm({"omissions": []})


KIND = claim_store.Kind(
    table="ax_claim_omissions",
    task="claim_omission",
    alias="chat",
    version="v2",
    facts=lambda row: {"note": row["note"] or "", "procedure": row["procedure_name"]},
    schema=lambda conn: {"type": "object"},
    check=lambda facts, r: all(claim_check.found(q, facts["note"]) for q in r["quotes"]),
    empty={"quotes": []},
)
GOOD = {"quotes": ["치주낭 5mm, 출혈 있음"]}
BAD = {"quotes": ["차트에 없는 문장입니다"]}


@pytest.fixture
def src(tmp_path):
    return Source(tmp_path / "source.sqlite")


def rows(conn):
    return [
        dict(r)
        for r in conn.execute(
            "SELECT c.id AS claim_id, pr.name AS procedure_name, n.note FROM source.emr_claims c"
            " JOIN source.emr_procedures pr ON pr.code = c.procedure_code"
            " JOIN source.emr_chart_notes n ON n.treatment_id = c.treatment_id ORDER BY c.id"
        )
    ]


def claim(src, pid, note=NOTE):
    cid = add_claim(src, pid, status="pending")
    src.conn.execute(
        "UPDATE emr_chart_notes SET note = ? WHERE treatment_id ="
        " (SELECT treatment_id FROM emr_claims WHERE id = ?)",
        (note, cid),
    )
    return cid


def test_build_saves_reuses_and_retries(src):
    a = claim(src, 1)
    b = claim(src, 2)
    conn = src.app()
    out = claim_store.build(
        conn, FakeGw(llm(GOOD), llm(BAD), llm(BAD)), KIND, rows(conn), use_batch=False
    )
    assert (out["called"], out["failed"], out["skipped"]) == (2, 1, 0)
    got = claim_store.saved(conn, KIND)
    assert json.loads(got[a]["result"]) == GOOD and got[b]["result"] is None
    gw = FakeGw(llm(GOOD))
    out = claim_store.build(conn, gw, KIND, rows(conn), use_batch=False)
    assert (out["called"], out["skipped"]) == (1, 1) and len(gw.calls) == 1
    assert claim_store.todo_ids(conn, KIND, rows(conn), MODEL) == set()


def test_empty_text_saves_empty_result(src):
    cid = claim(src, 1, note="")
    conn = src.app()
    gw = FakeGw()
    out = claim_store.build(conn, gw, KIND, rows(conn), use_batch=False)
    assert gw.calls == [] and out["empty"] == 1
    assert json.loads(claim_store.saved(conn, KIND, [cid])[cid]["result"]) == {"quotes": []}


def test_hash_matches_claim_check():
    """해시 계산이 2a와 같다 — 공용 모듈로 옮겨도 저장된 2b 행이 그대로 다시 쓰인다"""
    facts = {"procedure": "스케일링", "note": NOTE}
    assert claim_store.facts_hash(facts) == claim_check._hash(facts)


def test_request_uses_prompt_and_schema(src):
    claim(src, 1)
    conn = src.app()
    gw = FakeGw(llm(GOOD))
    claim_store.build(conn, gw, KIND, rows(conn), use_batch=False)
    (call,) = gw.calls
    assert call["schema"] == {"type": "object"} and call["prompt_version"] == "v2"
    assert call["ref"][0] == "claim" and NOTE in call["messages"][0]["content"]
