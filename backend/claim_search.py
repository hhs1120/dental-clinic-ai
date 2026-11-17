"""⑤-3 청구 기준 찾기 — ③ 자료실의 검색 엔진(backend/rag_search.py 하이브리드 + 리랭커)을 청구 기준 문서(doc_type
claim_standard)의 절 청크로 좁혀 절 원문을 돌려준다. AI가 답 문장을 쓰지 않는다. 문서 머리 청크(제목·머리말 — 첫 줄에
' > '가 없는 것)는 뺀다(설계 docs/superpowers/specs/2026-11-10-claim-review-design.md §3)."""

from backend.rag_search import CANDIDATES, Searcher

TOP_K = 5
DOC_TYPE = "claim_standard"
_SEP = " > "  # 색인 청크 첫 줄 '문서 제목 > 절 제목'(backend/rag_index.py _documents)


def _card(chunk: dict) -> dict | None:
    head, _, text = chunk["text"].partition("\n")
    if _SEP not in head:
        return None
    title, heading = head.split(_SEP, 1)
    return {"chunk_id": chunk["id"], "doc_title": title, "heading": heading, "text": text.strip()}


def search(searcher: Searcher, query: str, k: int = TOP_K) -> list[dict]:
    """청구 기준 절 k개 — [{chunk_id, doc_title, heading, text}], 검색 순위 그대로"""
    found = searcher.search(query, source_types=["document"], doc_type=DOC_TYPE, top_k=CANDIDATES)[
        "manual_results"
    ]
    return [c for c in map(_card, found) if c][:k]
