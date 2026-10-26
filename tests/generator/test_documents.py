"""9단계 RAG 문서 (generator/documents.py, 가상 데이터 생성 설계 §4.2 '9단계 구현 결정') — 빈 스키마 DB에 계획을 쓰고
verify 문서 검사·LLM 작업 내보내기·들여오기를 본다."""
import json
import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest
import yaml

from generator import documents, llm_jobs, verify
from generator.doc_catalog import BY_KEY, CATALOG
from generator.llm_jobs import LLMContext
from generator.paths import ROOT
from tests.generator.verify_db import CTX, SCHEMA, TRUTH_SCHEMA

CFG = yaml.safe_load((ROOT / "config" / "generator.yaml").read_text(encoding="utf-8"))
DOC_CHECKS = [verify.check_document_counts, verify.check_doc_meta_match, verify.check_doc_planted_facts,
              verify.check_doc_similar_clean, verify.check_notice_versions, verify.check_claim_sections,
              verify.check_doc_patient_names, verify.check_forbidden_topics]


@pytest.fixture
def conn(tmp_path):
    truth = sqlite3.connect(tmp_path / "truth.sqlite")
    truth.executescript(TRUTH_SCHEMA)
    truth.close()
    conn = sqlite3.connect(tmp_path / "app.sqlite")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute("ATTACH DATABASE ? AS truth", (str(tmp_path / "truth.sqlite"),))
    documents.stage_plan(SimpleNamespace(conn=conn), None, None)
    return conn


def _ctx():
    return replace(CTX, range_start=CFG["range"]["start"], today=CFG["today"],
                   documents_core=CFG["documents"]["core"], documents_distractor=CFG["documents"]["distractor"])


def _paths(tmp_path):
    return {k: tmp_path / k for k in ("llm_cache", "llm_jobs", "llm_results")}


def test_plan_drafts_pass_document_checks(conn):
    """초안(가짜 게이트웨이면 그대로 남음)만으로 verify 문서 검사를 통과한다"""
    assert {c.__name__: c(conn, _ctx()) for c in DOC_CHECKS if c(conn, _ctx())} == {}


def test_plan_rows(conn):
    rows = {r["doc_key"]: dict(r) for r in conn.execute(
        "SELECT d.*, m.* FROM ax_documents d JOIN truth.truth_doc_meta m ON m.doc_id = d.id")}
    assert len(rows) == len(CATALOG)
    new, old = rows["notice-whitening"], rows["old-whitening"]
    assert (new["version"], old["version"]) == ("2", "1") and new["supersedes_doc_id"] == old["id"]
    assert old["pair_doc_id"] == new["id"] and old["kind"] == "old_version" and old["is_distractor"] == 1
    sim = rows["sim-booking-basics"]
    assert sim["type"] == "manual" and sim["pair_doc_id"] == rows["manual-noshow"]["id"]
    assert rows["manual-noshow"]["path"] == "docs/manual/manual-noshow.md" and rows["manual-noshow"]["effective_date"] is None


def test_payload_hides_distractor_status():
    """입력에 방해 여부·종류가 없다 — 비슷한 주제는 다루지 않을 주제만, 타 병원은 출처만"""
    for d in CATALOG:
        text = json.dumps(documents.payload(d), ensure_ascii=False)
        assert all(w not in text for w in ("distractor", "similar", "old_version", "방해", d.key)), d.key
    assert documents.payload(BY_KEY["sim-booking-basics"])["skip_topic"] == BY_KEY["manual-noshow"].topic
    ext = documents.payload(BY_KEY["ext-noshow"])
    assert "clinic" not in ext and ext["source"]


def test_checks():
    sim = documents.checks(BY_KEY["sim-booking-basics"])
    assert sim["must_include"] == [] and {"3회", "3개월"} <= set(sim["forbidden_words"])
    claim = documents.checks(BY_KEY["claim-치주"])
    assert ["## 치석제거 뒤 치주치료 연결"] in claim["must_include"]
    assert documents.checks(BY_KEY["ext-noshow"])["forbidden_words"] == ["AX치과"]
    assert documents.checks(BY_KEY["edu-brushing"])["length"] == list(documents.DOC_LENGTH_CHARS)


def test_bodies_export_then_apply(conn, tmp_path):
    paths = _paths(tmp_path)
    ctx = LLMContext(paths, fake=False)
    documents.stage_bodies(SimpleNamespace(conn=conn, state={"llm": ctx}), None, None)
    assert ctx.finish() == {"document": len(CATALOG)}
    jobs = {j["item"]: j for j in (json.loads(line) for line in
                                    (paths["llm_jobs"] / "document.jsonl").read_text(encoding="utf-8").splitlines())}
    job = jobs["manual-noshow"]
    body = "# 예약 부도 관리\n\n" + "예약 부도가 3회 쌓이면 3개월 동안 전화 예약만 받고, 당일에는 부도 안내 문자를 보냅니다. " * 12
    paths["llm_results"].mkdir()
    (paths["llm_results"] / "document.jsonl").write_text(
        json.dumps({"key": job["key"], "output": {"body": body}, "source": "claude-code"}, ensure_ascii=False) + "\n",
        encoding="utf-8")
    assert llm_jobs.import_results(paths)["document"]["ok"] == 1
    documents.stage_bodies(SimpleNamespace(conn=conn, state={"llm": LLMContext(paths, fake=False)}), None, None)
    assert conn.execute("SELECT body FROM ax_documents WHERE path = 'docs/manual/manual-noshow.md'").fetchone()[0] == body


def test_bodies_fake_keeps_draft(conn, tmp_path):
    before = conn.execute("SELECT body FROM ax_documents ORDER BY id").fetchall()
    documents.stage_bodies(SimpleNamespace(conn=conn, state={"llm": LLMContext(_paths(tmp_path), fake=True)}), None, None)
    assert conn.execute("SELECT body FROM ax_documents ORDER BY id").fetchall() == before
