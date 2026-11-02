"""하이브리드 검색·리랭커 (앱 설계 §3)."""

from datetime import date

import pytest
from pydantic import ValidationError

from backend import db
from backend.embeddings import FakeEmbedder
from backend.rag_search import (
    MissingVectors,
    RerankFailed,
    Searcher,
    fts_query,
    fuse,
    haiku_rerank,
    parse_condition,
    search_tool,
)
from backend.vector_store import VectorStore
from shared.clinic import Clinic
from shared.llm import LLMResult

CLINIC = Clinic(today=date(2026, 12, 1), range_start=date(2026, 1, 1))


def add(conn, id_, kind, text, chart=None, day=None):
    conn.execute(
        "INSERT INTO ax_chunks (id, source_type, text, chart_no, event_date) VALUES (?, ?, ?, ?, ?)",
        (id_, kind, text, chart, day),
    )
    conn.execute("INSERT INTO ax_chunks_fts VALUES (?, ?)", (id_, text))


@pytest.fixture
def indexed(conn):
    add(conn, "doc:1:0", "document", "틀니 수리 접수 절차", day=None)
    add(conn, "doc:2:0", "document", "주차 무료 시간 안내", day="2026-05-01")
    add(conn, "doc:3:0", "document", "내년 휴진 안내", day="2027-01-01")
    add(
        conn,
        "call:c-1:0",
        "call",
        "[환자: 김철수(C-00001) | 2026-07-03 | 수신] 틀니 수리 문의",
        "C-00001",
        "2026-07-03",
    )
    add(
        conn,
        "thread:C-00002:2026-07",
        "message_thread",
        "[환자: 박영희(C-00002) | 2026-07] 예약 확인",
        "C-00002",
        "2026-07-31",
    )
    add(
        conn,
        "chart:7",
        "chart_note",
        "[차트 C-00003 | 2026-07-03 | 스케일링] 치석 제거",
        "C-00003",
        "2026-07-03",
    )
    e = FakeEmbedder()
    ids = [r[0] for r in conn.execute("SELECT id FROM ax_chunks")]
    texts = [r[0] for r in conn.execute("SELECT text FROM ax_chunks")]
    VectorStore(conn, e.model).upsert(ids, e.embed(texts, "document"))
    conn.commit()
    return conn


def searcher(conn, reranker=None):
    return Searcher(conn, CLINIC, FakeEmbedder(), reranker)


def test_fts_query_keeps_word_and_stripped_particle():
    assert fts_query("틀니를 수리") == '"틀니를"* OR "틀니"* OR "수리"*'


def test_fts_query_escapes_quotes_and_symbols():
    assert '"' not in fts_query('"그" AND (x)').replace('"그"*', "").replace('"AND"*', "").replace(
        '"x"*', ""
    ).replace(" OR ", "")


def test_fts_query_empty_input():
    assert fts_query("?!") == ""


def test_buckets_are_separate(indexed):
    r = searcher(indexed).search("틀니 수리")
    assert {c["id"] for c in r["manual_results"]} <= {"doc:1:0", "doc:2:0"}
    assert {c["id"] for c in r["case_results"]} <= {
        "call:c-1:0",
        "thread:C-00002:2026-07",
        "chart:7",
    }


def test_keyword_hit_ranks_first_in_each_bucket(indexed):
    r = searcher(indexed).search("틀니 수리")
    assert r["manual_results"][0]["id"] == "doc:1:0"
    assert r["case_results"][0]["id"] == "call:c-1:0"


def test_future_events_are_excluded(indexed):
    ids = {c["id"] for c in searcher(indexed).search("휴진")["manual_results"]}
    assert "doc:3:0" not in ids


def test_undated_documents_are_kept(indexed):
    assert "doc:1:0" in {c["id"] for c in searcher(indexed).search("틀니")["manual_results"]}


def test_filters(indexed):
    s = searcher(indexed)
    assert [c["id"] for c in s.search("틀니", patient_chart_no="C-00002")["case_results"]] == [
        "thread:C-00002:2026-07"
    ]
    r = s.search("틀니", source_types=["chart_note"])
    assert r["manual_results"] == [] and [c["id"] for c in r["case_results"]] == ["chart:7"]
    r = s.search("틀니", date_from="2026-07-10", date_to="2026-08-01")
    assert [c["id"] for c in r["case_results"]] == ["thread:C-00002:2026-07"]


def test_top_k_limits_each_bucket(indexed):
    r = searcher(indexed).search("틀니", top_k=1)
    assert len(r["manual_results"]) == 1 and len(r["case_results"]) == 1


def test_only_top_candidates_reach_reranker_per_bucket(indexed):
    seen = []

    def spy(query, chunks):
        seen.append(len(chunks))
        return list(reversed(chunks))

    r = searcher(indexed, spy).search("틀니 수리", top_k=5)
    assert len(seen) == 2 and r["manual_results"][-1]["id"] == "doc:1:0"


def test_reranker_failure_keeps_fusion_order(indexed):
    def boom(query, chunks):
        raise RuntimeError("장애")

    s = searcher(indexed, boom)
    assert s.search("틀니 수리")["manual_results"][0]["id"] == "doc:1:0"
    assert s.rerank_failures == 2


def result(data, ok=True):
    return LLMResult(ok, "", data, "m", False, 0, 0, 0.0, 0, stop_reason="end_turn", content=[])


class Gw:
    def __init__(self, r):
        self.r, self.calls = r, []

    def complete(self, alias, **k):
        self.calls.append({"alias": alias, **k})
        return self.r


CH = [{"id": "a", "text": "가"}, {"id": "b", "text": "나"}, {"id": "c", "text": "다"}]


def test_haiku_order_is_applied():
    gw = Gw(result({"order": [3, 1, 2]}))
    assert [c["id"] for c in haiku_rerank(gw)("질문", CH)] == ["c", "a", "b"]
    assert gw.calls[0]["alias"] == "rerank"


def test_haiku_wraps_chunks_as_external_data():
    gw = Gw(result({"order": [1, 2, 3]}))
    haiku_rerank(gw)("질문", CH)
    assert '<external_data source="chunk:1">' in gw.calls[0]["messages"][0]["content"]


def test_haiku_bad_numbers_are_ignored_and_missing_kept():
    gw = Gw(result({"order": [9, 2, 2]}))
    assert [c["id"] for c in haiku_rerank(gw)("질문", CH)] == ["b", "a", "c"]


def test_haiku_failure_is_raised_for_searcher_to_count():
    with pytest.raises(RerankFailed):
        haiku_rerank(Gw(result(None, ok=False)))("질문", CH)


def test_parse_condition():
    assert parse_condition(None) == {}
    assert parse_condition("embedding=voyage-4,reranker=rerank-3") == {
        "embedding": "voyage-4",
        "reranker": "rerank-3",
    }
    with pytest.raises(ValueError):
        parse_condition("이상한값")
    with pytest.raises(ValueError):
        parse_condition("color=red")


def test_vector_only_match_is_found(indexed):
    """키워드가 없어도 벡터로 찾는다 — 같은 문장을 임베딩한 FakeEmbedder로 확인"""
    r = searcher(indexed).search("치석 제거 없는말", top_k=5)
    assert "chart:7" in {c["id"] for c in r["case_results"]}


def test_search_tool_returns_buckets_with_text(indexed):
    class Ctx:
        pass

    tool = search_tool(searcher(indexed))
    args = tool.args.model_validate({"query": "틀니"}).model_dump()
    r = tool.fn(Ctx(), args)
    assert r["manual_results"][0]["text"].startswith("틀니")
    assert "chart_no" not in r["manual_results"][0]


def test_make_app_search_returns_ids_by_bucket(paths, tmp_path):
    from backend.eval_hooks import App

    conn = db.connect(*paths)
    db.init_schema(conn)
    add(conn, "doc:1:0", "document", "틀니 수리")
    VectorStore(conn, "fake").upsert(["doc:1:0"], FakeEmbedder().embed(["틀니 수리"], "document"))
    conn.commit()
    app = App(conn, embedder_for=lambda model: FakeEmbedder())
    r = app.search("틀니", "reranker=off")
    assert r == {"manual_results": ["doc:1:0"], "case_results": []}
    assert app.search("틀니", "reranker=off") == r and len(app.searchers) == 1
    with pytest.raises(ValueError):
        app.search("틀니", "reranker=없는것")
    conn.close()


def notices(*ids):
    return [{"id": i} for i in ids]


def test_replacement_takes_the_place_of_old_notice(conn):
    s = Searcher(conn, CLINIC, FakeEmbedder())
    got = s.latest_first(notices("doc:101:0", "doc:109:0", "doc:102:0", "doc:102:1"))
    assert [c["id"] for c in got] == ["doc:102:0", "doc:101:0", "doc:109:0", "doc:102:1"]


def test_old_notice_stays_when_replacement_is_not_a_candidate(conn):
    s = Searcher(conn, CLINIC, FakeEmbedder())
    assert [c["id"] for c in s.latest_first(notices("doc:101:0"))] == ["doc:101:0"]


def test_same_title_of_other_type_is_not_a_replacement(conn):
    s = Searcher(conn, CLINIC, FakeEmbedder())
    got = s.latest_first(notices("doc:104:0", "doc:102:0"))
    assert [c["id"] for c in got] == ["doc:104:0", "doc:102:0"]


def test_case_chunks_are_untouched(conn):
    s = Searcher(conn, CLINIC, FakeEmbedder())
    assert [c["id"] for c in s.latest_first(notices("call:c-1:0", "chart:7"))] == [
        "call:c-1:0",
        "chart:7",
    ]


def test_search_applies_latest_first_after_reranker(paths):
    conn = db.connect(*paths)
    db.init_schema(conn)
    add(conn, "doc:101:0", "document", "휴진 안내 옛 공지")
    add(conn, "doc:102:0", "document", "휴진 안내 새 공지")
    e = FakeEmbedder()
    VectorStore(conn, e.model).upsert(
        ["doc:101:0", "doc:102:0"], e.embed(["휴진 안내 옛 공지", "휴진 안내 새 공지"], "document")
    )
    conn.commit()
    first_old = lambda query, chunks: sorted(chunks, key=lambda c: c["id"])  # noqa: E731
    r = Searcher(conn, CLINIC, e, first_old).search("휴진")
    assert [c["id"] for c in r["manual_results"]] == ["doc:102:0", "doc:101:0"]
    conn.close()


def test_fuse_prefers_chunk_in_both_lists():
    assert fuse([["a", "b"], ["c", "b"]])[0] == "b"


def test_fuse_rank_matters():
    assert fuse([["a", "b", "c"]]) == ["a", "b", "c"]


def test_missing_vectors_are_refused(paths):
    conn = db.connect(*paths)
    db.init_schema(conn)
    add(conn, "doc:1:0", "document", "틀니 수리")
    with pytest.raises(MissingVectors):
        Searcher(conn, CLINIC, FakeEmbedder())
    conn.close()


@pytest.mark.parametrize(
    "bad",
    [
        {"query": ""},
        {"query": "q", "source_types": ["documents"]},
        {"query": "q", "date_from": "2026-11"},
        {"query": "q", "top_k": -1},
        {"query": "q", "top_k": 26},
    ],
)
def test_search_args_are_validated(conn, bad):
    tool = search_tool(Searcher(conn, CLINIC, FakeEmbedder()))
    with pytest.raises(ValidationError):
        tool.args.model_validate(bad)


def test_search_tool_description_has_today(conn):
    assert "2026-12-01" in search_tool(Searcher(conn, CLINIC, FakeEmbedder())).description


def test_llm_reranker_is_refused_for_tool(conn):
    s = Searcher(conn, CLINIC, FakeEmbedder(), haiku_rerank(Gw(result({"order": []}))))
    with pytest.raises(ValueError):
        search_tool(s)


def test_app_reports_rerank_failures(paths):
    from backend.eval_hooks import App

    conn = db.connect(*paths)
    db.init_schema(conn)
    add(conn, "doc:1:0", "document", "틀니")
    VectorStore(conn, "fake").upsert(["doc:1:0"], FakeEmbedder().embed(["틀니"], "document"))
    conn.commit()
    app = App(conn, embedder_for=lambda model: FakeEmbedder())
    assert app.supports_chat is False and app.warnings() == []

    def boom(query, chunks):
        raise RuntimeError("장애")

    app._reranker = lambda name: boom
    app.search("틀니", "reranker=x")
    assert "리랭커 실패 1회" in app.warnings()[0]
    conn.close()
