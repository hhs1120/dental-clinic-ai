"""⑤-3 청구 기준 찾기(backend/claim_search.py) — 설계 docs/superpowers/specs/2026-11-10-claim-review-design.md §3.
③ 검색 엔진을 청구 기준 문서의 절 청크로 좁히는지(다른 문서·문서 머리 청크 빼기), 절 제목 파싱, API 503·결과를 본다."""

from datetime import date

import pytest
from fastapi.testclient import TestClient

from backend import api, claim_search, db
from backend.embeddings import FakeEmbedder
from backend.rag_index import build_index
from backend.rag_search import Searcher
from shared.clinic import Clinic
from tests.backend.test_missed import Source

CLINIC = Clinic(today=date(2026, 12, 1), range_start=date(2026, 1, 1), eval_pool_size=150)
DOCS = [
    (
        1,
        "claim_standard",
        "치주 분야 청구 기준",
        "# 치주 분야 청구 기준\n\n머리말 치석제거 치주치료.\n\n"
        "## 치석제거 뒤 치주치료 연결\n\n- 치석제거를 시행한 날부터 90일 이내에 이어서 치주치료를 하면 재평가 소견을 적는다.\n\n"
        "## 치주치료 산정 소견\n\n- 치주낭 깊이가 4mm 이상인 부위가 확인될 때 산정한다.\n",
    ),
    (
        2,
        "claim_standard",
        "예방 분야 청구 기준",
        "# 예방 분야 청구 기준\n\n머리말.\n\n## 스케일링 연간 산정 횟수\n\n- 스케일링은 연 1회 급여로 산정한다.\n",
    ),
    (
        3,
        "manual",
        "치석제거 안내",
        "# 치석제거 안내\n\n## 치석제거 뒤 치주치료\n\n- 치석제거 뒤 치주치료는 다음 달에 예약한다.\n",
    ),
]


@pytest.fixture
def paths(tmp_path):
    s = Source(tmp_path / "source.sqlite")
    s.conn.executemany(
        "INSERT INTO ax_documents (id, type, title, version, effective_date, path, body)"
        " VALUES (?, ?, ?, '1', '2024-01-01', 'x.md', ?)",
        DOCS,
    )
    s.app().close()
    paths = (tmp_path / "app.sqlite", s.path)
    conn = db.connect(*paths)
    build_index(conn, FakeEmbedder())
    conn.close()
    return paths


def test_only_claim_sections(paths):
    conn = db.connect(*paths)
    got = claim_search.search(Searcher(conn, CLINIC, FakeEmbedder()), "치석제거 다음 달 치주치료")
    assert got and len(got) <= claim_search.TOP_K
    assert {g["doc_title"] for g in got} <= {"치주 분야 청구 기준", "예방 분야 청구 기준"}
    assert all(g["heading"] != g["doc_title"] for g in got)  # 머리말 청크 없음
    assert len(got) == 3  # 절 청크 3개뿐(머리말 2개·다른 문서는 빠짐)


def test_heading_parsed(paths):
    conn = db.connect(*paths)
    got = claim_search.search(Searcher(conn, CLINIC, FakeEmbedder()), "스케일링 연 1회")
    hit = next(g for g in got if g["heading"] == "스케일링 연간 산정 횟수")
    assert hit["doc_title"] == "예방 분야 청구 기준" and hit["text"].startswith("- 스케일링")
    assert hit["chunk_id"].startswith("doc:2:")


@pytest.fixture
def client():
    yield (
        lambda paths: (
            api.app.dependency_overrides.update(
                {api.get_db_paths: lambda: paths, api.get_models: lambda: (FakeEmbedder(), None)}
            )
            or TestClient(api.app)
        )
    )
    api.app.dependency_overrides.clear()


def test_api_results(client, paths):
    r = client(paths).get("/claims/standards", params={"q": "치주낭 4mm"})
    assert r.status_code == 200
    got = r.json()["results"]
    assert got and set(got[0]) == {"chunk_id", "doc_title", "heading", "text"}


def test_api_no_index_503(client, tmp_path):
    s = Source(tmp_path / "source.sqlite")
    s.app().close()
    r = client((tmp_path / "app.sqlite", s.path)).get("/claims/standards", params={"q": "x"})
    assert r.status_code == 503 and "make index" in r.json()["detail"]


def test_eval_hook_returns_headings(paths):
    """평가 훅 — 절 제목 순위(evaluation/claim_qa.py 채점이 절 단위로 센다)"""
    from backend.eval_hooks import App

    conn = db.connect(*paths)
    app = App(conn, embedder_for=lambda model: FakeEmbedder())
    got = app.claim_search("스케일링 연 1회", "reranker=off")
    assert "스케일링 연간 산정 횟수" in got and len(got) <= claim_search.TOP_K
