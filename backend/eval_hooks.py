"""평가 훅 (앱 설계 §1). 평가 스크립트가 앱의 search·chat·baseline을 같은 프로세스에서 부른다. 조건은
'embedding=…,reranker=…'이고 Searcher를 조건별로 하나씩 둔다. 챗은 평가 쪽이 넘긴 app.sqlite 사본에 쓴다
(설계 2026-11-09 챗 "평가")."""

from pathlib import Path

from backend import chat as chat_agent
from backend import claim_search, db
from backend.embeddings import make_embedder
from backend.harness.llm import make_gateway
from backend.rag_search import (
    CANDIDATES,
    DEFAULT_EMBEDDING,
    DEFAULT_RERANKER,
    Searcher,
    haiku_rerank,
    parse_condition,
    voyage_rerank,
)
from shared.clinic import load_clinic


class App:
    supports_chat = True

    def __init__(self, conn, embedder_for=make_embedder):
        self.conn, self.embedder_for = conn, embedder_for
        self.clinic = load_clinic()
        self.searchers = {}

    def _reranker(self, name: str):
        if name == "off":
            return None
        if name == "haiku":
            return haiku_rerank(make_gateway(self.conn, is_eval=True))
        if name.startswith("rerank"):
            return voyage_rerank(name)
        raise ValueError(f"모르는 리랭커: {name}")

    def _searcher(self, condition: str | None) -> Searcher:
        c = parse_condition(condition)
        key = (c.get("embedding", DEFAULT_EMBEDDING), c.get("reranker", DEFAULT_RERANKER))
        if key not in self.searchers:
            self.searchers[key] = Searcher(
                self.conn, self.clinic, self.embedder_for(key[0]), self._reranker(key[1])
            )
        return self.searchers[key]

    def warnings(self) -> list[str]:
        """평가 수치를 믿기 어렵게 만든 일 — 리포트에 남고 test는 거절된다"""
        empty = not self.conn.execute("SELECT 1 FROM ax_chunks LIMIT 1").fetchone()
        return ["색인 비어 있음 — make index 먼저"] * empty + [
            f"리랭커 실패 {s.rerank_failures}회 ({'·'.join(key)})"
            for key, s in self.searchers.items()
            if s.rerank_failures
        ]

    def search(self, question: str, condition: str | None = None) -> dict:
        s = self._searcher(condition)
        found = s.search(question, top_k=CANDIDATES)
        s.drain_usage()  # 검색 지표용 직접 호출 — 챗 비용에 섞이지 않게 비운다(기록하지 않음, 금액 작음)
        return {bucket: [c["id"] for c in chunks] for bucket, chunks in found.items()}

    def claim_search(self, question: str, condition: str | None = None) -> list[str]:
        """⑤-3 청구 기준 찾기 — 절 제목 순위(평가 설계 §5 ⑤-3, 절 단위 채점)"""
        s = self._searcher(condition)
        found = claim_search.search(s, question)
        s.drain_usage()  # search와 같이 기록하지 않는다(금액 작음)
        return [c["heading"] for c in found]

    def chat(self, question: str, qa_id: str, condition: str | None = None) -> dict:
        gw = make_gateway(self.conn, is_eval=True)
        return chat_agent.chat(
            self.conn, gw, self._searcher(condition), self.clinic, question, is_eval=True
        )

    def baseline(self, question: str, qa_id: str, condition: str | None = None) -> dict:
        gw = make_gateway(self.conn, is_eval=True)
        return chat_agent.baseline(
            self.conn, gw, self._searcher(condition), question, is_eval=True
        )


def make_app(app_db: Path, source_db: Path) -> App:
    return App(db.connect(app_db, source_db))
