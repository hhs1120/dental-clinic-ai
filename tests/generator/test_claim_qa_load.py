"""truth_claim_qa 적재(generator/build.py load_claim_qa) — ⑤-3 청구 기준 질문 승인 JSON(evaluation/claim_qa.py 검토)을
make data가 정답 저장소에 넣는다(설계 docs/superpowers/specs/2026-11-10-claim-review-design.md §3)."""

import json
import sqlite3

import pytest

from generator.build import TRUTH_SCHEMA, load_claim_qa
from generator.doc_catalog import CLAIM_HEADINGS

ROW = {
    "id": "cq-R02-1",
    "reason": "R02",
    "question": "치석제거 다음 달로 넘어가면 왜 깎여?",
    "answer_heading": CLAIM_HEADINGS["R02"],
    "warnings": [],
    "split": "dev",
}


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.execute("ATTACH DATABASE ':memory:' AS truth")
    for stmt in TRUTH_SCHEMA.read_text(encoding="utf-8").split(";"):
        if "CREATE TABLE truth_claim_qa" in stmt:
            c.execute(stmt.replace("CREATE TABLE ", "CREATE TABLE truth."))
    return c


def test_load_rows(conn, tmp_path):
    path = tmp_path / "claim_qa.json"
    path.write_text(json.dumps([ROW, ROW | {"id": "cq-R02-2", "warnings": ["절 제목"]}]), "utf-8")
    assert load_claim_qa(conn, path) == 2
    got = conn.execute(
        "SELECT id, answer_heading, warnings, split FROM truth.truth_claim_qa ORDER BY id"
    )
    assert got.fetchall() == [
        ("cq-R02-1", "치석제거 뒤 치주치료 연결", "[]", "dev"),
        ("cq-R02-2", "치석제거 뒤 치주치료 연결", '["절 제목"]', "dev"),
    ]


def test_unknown_heading_rejected(conn, tmp_path):
    path = tmp_path / "claim_qa.json"
    path.write_text(json.dumps([ROW | {"answer_heading": "없는 절"}]), "utf-8")
    with pytest.raises(ValueError):
        load_claim_qa(conn, path)


def test_missing_file_skips(conn, tmp_path):
    assert load_claim_qa(conn, tmp_path / "none.json") == 0
    assert load_claim_qa(conn, None) == 0
