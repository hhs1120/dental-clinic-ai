"""Voyage 검색 호출 비용 기록 (설계 2026-11-09 챗 "Voyage 비용")."""

from datetime import date

from backend.embeddings import FakeEmbedder
from backend.harness.store import SqliteLlmLog
from backend.rag_search import Searcher
from backend.usage import record_voyage
from shared.clinic import Clinic
from shared.llm import load_config

CLINIC = Clinic(today=date(2026, 12, 1), range_start=date(2026, 1, 1), eval_pool_size=150)


def test_voyage_rows_count_toward_daily_limit(conn):
    total = record_voyage(conn, [("search_embed", "voyage-4", 1_000_000)], is_eval=False, ref=None)
    row = conn.execute(
        "SELECT task, model_alias, model_actual, cost_usd FROM ax_llm_calls"
    ).fetchone()
    price = load_config()["models"]["voyage-4"]["input"]
    assert tuple(row) == ("search_embed", "voyage", "voyage-4", price) and total == price
    assert SqliteLlmLog(conn).spent(date.today(), "runtime") == price


def test_ref_and_eval_flag_kept(conn):
    record_voyage(conn, [("search_rerank", "rerank-3", 10)], is_eval=True, ref=("agent_run", 7))
    row = conn.execute("SELECT is_eval, ref_type, ref_id FROM ax_llm_calls").fetchone()
    assert tuple(row) == (1, "agent_run", "7")


class CountingEmbedder(FakeEmbedder):
    """Voyage처럼 호출마다 토큰 수를 돌려준다"""

    def embed_with_usage(self, texts, kind):
        return self.embed(texts, kind), 5


def _rerank():
    def rerank(query, chunks):
        return chunks

    rerank.model = "rerank-3"
    rerank.with_usage = lambda query, chunks: (chunks, 9)
    return rerank


def _index(conn, emb):
    from backend.vector_store import VectorStore

    conn.execute(
        "INSERT INTO ax_chunks (id, source_type, text) VALUES ('doc:1:0', 'document', '틀니')"
    )
    conn.execute("INSERT INTO ax_chunks_fts VALUES ('doc:1:0', '틀니')")
    VectorStore(conn, emb.model).upsert(["doc:1:0"], emb.embed(["틀니"], "document"))


def test_searcher_drains_embed_and_rerank_usage(conn):
    emb = CountingEmbedder()
    _index(conn, emb)
    s = Searcher(conn, CLINIC, emb, _rerank())
    s.search("틀니")
    assert s.drain_usage() == [("search_embed", "fake", 5), ("search_rerank", "rerank-3", 9)]
    assert s.drain_usage() == []


def test_usage_is_per_searcher_not_shared(conn):
    """임베더·리랭커는 프로세스에 하나라도 사용량은 요청(검색기)마다 — 다른 요청에 붙지 않게(리뷰 2)"""
    emb, rr = CountingEmbedder(), _rerank()
    _index(conn, emb)
    a, b = Searcher(conn, CLINIC, emb, rr), Searcher(conn, CLINIC, emb, rr)
    a.search("틀니")
    assert b.drain_usage() == [] and len(a.drain_usage()) == 2


def test_fake_embedder_has_no_usage(conn):
    assert Searcher(conn, CLINIC, FakeEmbedder()).drain_usage() == []
