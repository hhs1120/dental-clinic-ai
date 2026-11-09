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
    def __init__(self):
        super().__init__()
        self.usage = []

    def embed(self, texts, kind):
        self.usage.append((self.model, 5))
        return super().embed(texts, kind)


def test_searcher_drains_embed_and_rerank_usage(conn):
    def rerank(query, chunks):
        rerank.usage.append(("rerank-3", 9))
        return chunks

    rerank.usage = []
    conn.execute(
        "INSERT INTO ax_chunks (id, source_type, text) VALUES ('doc:1:0', 'document', '틀니')"
    )
    conn.execute("INSERT INTO ax_chunks_fts VALUES ('doc:1:0', '틀니')")
    emb = CountingEmbedder()
    from backend.vector_store import VectorStore

    VectorStore(conn, emb.model).upsert(["doc:1:0"], emb.embed(["틀니"], "document"))
    emb.usage.clear()
    s = Searcher(conn, CLINIC, emb, rerank)
    s.search("틀니")
    assert s.drain_usage() == [("search_embed", "fake", 5), ("search_rerank", "rerank-3", 9)]
    assert s.drain_usage() == []


def test_fake_embedder_has_no_usage(conn):
    assert Searcher(conn, CLINIC, FakeEmbedder()).drain_usage() == []
