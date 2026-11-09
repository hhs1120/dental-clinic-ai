"""기록 검색 요약 (설계 2026-11-09 검색 요약 "요약 입력과 검사") — 대본 게이트웨이로 요약 응답을 정해 둔다."""

import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from backend import db, search_summary
from backend.embeddings import FakeEmbedder
from backend.rag_search import SearchArgs, Searcher
from backend.vector_store import VectorStore
from shared.clinic import Clinic
from shared.llm import BudgetExceeded, LLMResult
from tests.backend.script import Script

SOURCE_SCHEMA = Path(__file__).parents[2] / "shared" / "source_schema.sql"
CLINIC = Clinic(today=date(2026, 12, 1), range_start=date(2026, 1, 1), eval_pool_size=150)
CHART_TEXT = "[차트 C-00001 | 2026-11-05 | 치주치료] 스케일링 3일 뒤 재평가 (잇몸치료 · 사흘)"
ARGS = SearchArgs(query="주차", top_k=10)
NEW, MANUAL, OLD = "doc:2:0", "doc:3:0", "doc:1:0"


@pytest.fixture
def conn(tmp_path):
    source = tmp_path / "s.sqlite"
    s = sqlite3.connect(source)
    s.executescript(SOURCE_SCHEMA.read_text(encoding="utf-8"))
    s.execute("PRAGMA foreign_keys = OFF")
    s.executemany(
        "INSERT INTO ax_documents VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (1, "notice", "주차 안내", "1", "2026-01-01", "n1.md", "진료 당일 1시간 무료"),
            (2, "notice", "주차 안내", "2", "2026-06-01", "n2.md", "진료 당일 2시간 무료"),
            (3, "manual", "주차 등록", "1", None, "m1.md", "데스크에서 차량 번호 입력"),
        ],
    )
    s.execute(
        "INSERT INTO emr_chart_notes (id, treatment_id, dx_code, note)"
        " VALUES (1, 102, 'K05', '스케일링 3일 뒤 재평가')"
    )
    s.commit()
    s.close()
    c = db.connect(tmp_path / "a.sqlite", source)
    db.init_schema(c)
    chunks = [
        (OLD, "document", "주차 안내\n진료 당일 1시간 무료", None, "2026-01-01"),
        (NEW, "document", "주차 안내\n진료 당일 2시간 무료", None, "2026-06-01"),
        (MANUAL, "document", "주차 등록\n데스크에서 차량 번호 입력", None, None),
        ("chart:102", "chart_note", CHART_TEXT, "C-00001", "2026-11-05"),
    ]
    for cid, kind, text, chart_no, day in chunks:
        c.execute(
            "INSERT INTO ax_chunks (id, source_type, text, chart_no, event_date, doc_type)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                cid,
                kind,
                text,
                chart_no,
                day,
                {MANUAL: "manual", "chart:102": None}.get(cid, "notice"),
            ),
        )
        c.execute("INSERT INTO ax_chunks_fts VALUES (?, ?)", (cid, text))
    emb = FakeEmbedder()
    VectorStore(c, emb.model).upsert(
        [x[0] for x in chunks], emb.embed([x[2] for x in chunks], "document")
    )
    c.commit()
    yield c
    c.close()


def out(text="진료 당일 2시간 무료입니다[1].", cited=(NEW,), refused=False, ok=True):
    data = {"text": text, "cited_ids": list(cited), "refused": refused} if ok else None
    return LLMResult(ok, "", data, "m", False, 1, 1, 0.01, 1, error=None if ok else "x")


def run(conn, *turns, args=ARGS, gw=None, **kw):
    gw = gw or Script(*turns)
    return search_summary.summarize(
        conn, gw, Searcher(conn, CLINIC, FakeEmbedder()), args, **kw
    ), gw


def _rule(conn, rule_id):
    row = conn.execute(
        "SELECT blocked FROM ax_rule_counts WHERE rule_id = ?", (rule_id,)
    ).fetchone()
    return row[0] if row else 0


def _rows(conn):
    return conn.execute(
        "SELECT status, is_eval FROM ax_search_logs WHERE kind = 'search_summary'"
    ).fetchall()


def test_answer_with_numbered_citations(conn):
    r, gw = run(conn, out())
    assert r["status"] == "answered" and r["text"] == "진료 당일 2시간 무료입니다[1]."
    assert r["cited_ids"] == [NEW] and r["reused"] is False and len(gw.calls) == 1
    assert [tuple(x) for x in _rows(conn)] == [("answered", 0)]
    assert gw.calls[0]["alias"] == "chat" and gw.calls[0]["task"] == "search_summary"


def test_outdated_notice_not_in_input(conn):
    r, gw = run(conn, out())
    assert OLD not in json.dumps(gw.calls[0]["messages"], ensure_ascii=False)
    assert OLD not in r["retrieved_ids"] and NEW in r["retrieved_ids"]


def test_marker_digits_are_not_numbers():
    ids = ["a", "b", "c"]
    assert search_summary.check("무료입니다[1][2][3].", ids, ids, ["무료"]) is None
    assert (
        search_summary.check("7일 걸립니다[1].", ["a"], ["a"], ["무료"]) == search_summary.NUMBER
    )


def test_unverified_number_retries_then_number_failed(conn):
    bad = out("진료 당일 9시간 무료[1].")  # 9는 어느 결과에도 없다
    r, gw = run(conn, bad, bad)
    assert r["status"] == "number_failed" and r["text"] == search_summary.FAILED_TEXT
    assert r["cited_ids"] == [] and len(gw.calls) == 2
    assert _rule(conn, search_summary.NUMBER) == 2


def test_cite_outside_retries_once_then_answers(conn):
    r, gw = run(conn, out(cited=("doc:9:0",)), out())
    assert r["status"] == "answered" and len(gw.calls) == 2
    assert _rule(conn, search_summary.CITE_OUTSIDE) == 1


def test_sentence_without_marker_is_cite_missing(conn):
    bad = out("진료 당일 2시간 무료입니다[1]. 예약은 전화로 합니다.")
    r, _ = run(conn, bad, bad)
    assert r["status"] == "cite_failed" and _rule(conn, search_summary.CITE_MISSING) == 2


def test_marker_out_of_range_is_cite_outside(conn):
    r, gw = run(conn, out("진료 당일 2시간 무료입니다[2]."), out())
    assert r["status"] == "answered" and len(gw.calls) == 2
    assert _rule(conn, search_summary.CITE_OUTSIDE) == 1


def test_unused_citation_is_cite_outside(conn):
    r, gw = run(conn, out(cited=(NEW, MANUAL)), out())
    assert len(gw.calls) == 2 and _rule(conn, search_summary.CITE_OUTSIDE) == 1


def test_refused_shows_fixed_text(conn):
    r, _ = run(conn, out("", (), refused=True))
    assert r["status"] == "refused" and r["text"] == search_summary.REFUSED_TEXT
    assert r["cited_ids"] == []


class OldOnly:
    """옛 공지만 찾는 검색기"""

    replaced_by = {1: 2}

    def search(self, **kw):
        return {
            "manual_results": [{"id": OLD, "source_type": "document", "text": "x"}],
            "case_results": [],
        }

    def drain_usage(self):
        return []


def test_only_outdated_notices_skips_model(conn):
    gw = Script()
    r = search_summary.summarize(conn, gw, OldOnly(), ARGS)
    assert r["status"] == "refused" and r["text"] == search_summary.REFUSED_TEXT
    assert gw.calls == [] and _rows(conn) == [] and r["log_id"] is None


def test_budget_exceeded_is_budget(conn):
    r, _ = run(conn, BudgetExceeded("상한"))
    assert r["status"] == "budget" and r["text"] == search_summary.FAILED_TEXT


def test_gateway_failure_is_failed(conn):
    r, _ = run(conn, out(ok=False))
    assert r["status"] == "failed" and r["text"] == search_summary.FAILED_TEXT


def test_same_search_reuses_row(conn):
    run(conn, out())
    r, gw = run(conn)
    assert r["reused"] is True and gw.calls == [] and r["status"] == "answered"
    assert r["cited_ids"] == [NEW] and len(_rows(conn)) == 1


def test_prompt_version_change_makes_new_row(conn, monkeypatch):
    run(conn, out())
    monkeypatch.setattr(search_summary, "PROMPT_VERSION", "v2")
    monkeypatch.setattr(search_summary.prompts, "load", lambda task, v: {"system": "v2"})
    r, gw = run(conn, out())
    assert r["reused"] is False and len(gw.calls) == 1 and len(_rows(conn)) == 2


def test_other_filters_make_new_row(conn):
    run(conn, out())
    r, gw = run(conn, out(), args=SearchArgs(query="주차", top_k=10, date_to="2026-12-01"))
    assert r["reused"] is False and len(gw.calls) == 1


def test_failed_row_is_not_reused(conn):
    run(conn, out(ok=False))
    r, gw = run(conn, out())
    assert r["status"] == "answered" and len(gw.calls) == 1


def test_eval_run_does_not_reuse(conn):
    run(conn, out())
    r, gw = run(conn, out(), is_eval=True)
    assert r["reused"] is False and len(gw.calls) == 1
    assert sorted(tuple(x) for x in _rows(conn)) == [("answered", 0), ("answered", 1)]


def test_chart_input_has_no_search_brackets(conn):
    _, gw = run(conn, out())
    sent = json.dumps(gw.calls[0]["messages"], ensure_ascii=False)
    assert "스케일링 3일 뒤 재평가" in sent and "(잇몸치료 · 사흘)" not in sent


def test_marker_after_period_belongs_to_previous_sentence():
    """모델이 '…있습니다. [1]'처럼 마침표 뒤 띄우고 번호를 붙여도 앞 문장의 번호다(실측 5건 중 4건 재시도)"""
    ids = ["a", "b"]
    assert search_summary.check("가입니다. [1] 나입니다. [2]", ids, ids, []) is None
    assert (
        search_summary.check("가입니다. [1] 나입니다.", ids[:1], ids[:1], [])
        == search_summary.CITE_MISSING
    )


def test_answer_text_joins_marker_to_sentence(conn):
    r, _ = run(conn, out("진료 당일 2시간 무료입니다. [1]"))
    assert r["status"] == "answered" and r["text"] == "진료 당일 2시간 무료입니다.[1]"


def test_retry_does_not_hold_write_lock(conn):
    """다시 부르는 동안 앱 DB 쓰기 잠금을 쥐지 않는다 — 막힌 횟수는 바로 커밋한다"""
    seen = []

    class Watch(Script):
        def complete(self, alias, **kwargs):
            seen.append(conn.in_transaction)
            return super().complete(alias, **kwargs)

    run(conn, gw=Watch(out(cited=(OLD,)), out()))
    assert seen == [False, False] and _rule(conn, search_summary.CITE_OUTSIDE) == 1


def test_unexpected_error_logs_failed_and_raises(conn):
    with pytest.raises(RuntimeError):
        run(conn, RuntimeError("x"))
    assert [tuple(x) for x in _rows(conn)] == [("failed", 0)]


def test_dotted_date_and_ellipsis_stay_in_sentence():
    ids = ["a"]
    for text in ("시행일은 2026. 3. 1.부터입니다[1].", "주차 등록... 데스크에서 합니다[1]."):
        assert search_summary.check(text, ids, ids, ["2026. 3. 1."]) is None


def test_marker_must_end_sentence():
    ids = ["a"]
    assert (
        search_summary.check("주차[1]는 무료입니다.", ids, ids, []) == search_summary.CITE_MISSING
    )
