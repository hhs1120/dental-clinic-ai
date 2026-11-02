"""평가 훅 (앱 설계 §1). 평가 스크립트가 앱의 search·chat을 같은 프로세스에서 부른다. 조건은
'embedding=…,reranker=…'이고 Searcher를 조건별로 하나씩 둔다."""

from pathlib import Path

from backend import db
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
    supports_chat = False  # ③ 챗 착수 때 켠다

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
        return [
            f"리랭커 실패 {s.rerank_failures}회 ({'·'.join(key)})"
            for key, s in self.searchers.items()
            if s.rerank_failures
        ]

    def search(self, question: str, condition: str | None = None) -> dict:
        found = self._searcher(condition).search(question, top_k=CANDIDATES)
        return {bucket: [c["id"] for c in chunks] for bucket, chunks in found.items()}

    def chat(self, question: str, qa_id: str, condition: str | None = None) -> dict:
        raise NotImplementedError("③ 챗 착수 때")


def make_app(app_db: Path, source_db: Path) -> App:
    return App(db.connect(app_db, source_db))
