"""앱 API (앱 설계 §9, 앱 뼈대 설계안 '백엔드'). 키 없이 돈다 — FakeEmbedder, 리랭커 끔."""

import shutil
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from backend import db
from backend.api import GuardedEmbedder, app, get_db_paths, get_models
from backend.embeddings import FakeEmbedder
from backend.rag_index import build_index
from backend.rag_search import SEARCH_TOP_K, Searcher
from shared.clinic import load_clinic


@pytest.fixture
def indexed_paths(source_paths):
    conn = db.connect(*source_paths)
    db.init_schema(conn)
    build_index(conn, FakeEmbedder())
    conn.close()
    return source_paths


def override(paths, models):
    app.dependency_overrides[get_db_paths] = lambda: paths
    app.dependency_overrides[get_models] = lambda: models


@pytest.fixture
def client_with(indexed_paths):
    def make(paths=indexed_paths, models=None):
        override(paths, models or (GuardedEmbedder(FakeEmbedder()), None))
        return TestClient(app)

    yield make
    app.dependency_overrides.clear()


@pytest.fixture
def client(client_with):
    return client_with()


def ids(found):
    return {k: [c["id"] for c in v] for k, v in found.items()}


def test_search_matches_searcher(client, indexed_paths):
    r = client.get("/search", params={"q": "틀니 수리"})
    assert r.status_code == 200
    conn = db.connect(*indexed_paths)
    want = Searcher(conn, load_clinic(), FakeEmbedder()).search("틀니 수리", top_k=SEARCH_TOP_K)
    assert ids(r.json()) == ids(want)


def test_buckets_split(client):
    body = client.get("/search", params={"q": "틀니"}).json()
    assert {c["source_type"] for c in body["manual_results"]} == {"document"}
    assert {c["source_type"] for c in body["case_results"]} == {
        "call",
        "message_thread",
        "chart_note",
    }


def test_filters_passed(client):
    body = client.get("/search", params={"q": "치석", "source_types": "chart_note"}).json()
    assert body["manual_results"] == []
    assert {c["source_type"] for c in body["case_results"]} == {"chart_note"}
    body = client.get("/search", params={"q": "틀니", "chart_no": " c-00001 "}).json()
    assert body["manual_results"] == []
    assert {c["chart_no"] for c in body["case_results"]} == {"C-00001"}
    params = {"q": "미백", "date_from": "2026-05-01", "date_to": ""}
    body = client.get("/search", params=params).json()
    # v1 공지·시행일 없는 매뉴얼은 빠진다
    assert [c["id"] for c in body["manual_results"]] == ["doc:2:0"]


def test_card_fields(client):
    body = client.get("/search", params={"q": "틀니 거래처 미백"}).json()
    by_id = {c["id"]: c for c in body["manual_results"] + body["case_results"]}
    assert by_id["call:c-0001:0"]["patient_name"] == "김철수"
    assert by_id["call:c-0001:0"]["direction"] == "수신"
    assert by_id["call:c-0002:0"]["caller_name"] == "김미영"
    assert by_id["doc:1:0"]["is_outdated"] is True
    assert by_id["chart:7"]["body"] == "통증 호소(좌측) 치석 제거 6개월 후 재내원"


@pytest.mark.parametrize(
    "params",
    [
        {"q": "   "},
        {"q": "틀니", "source_types": ""},
        {"q": "틀니", "source_types": " , "},
        {"q": "틀니", "source_types": "document,fax"},
        {"q": "틀니", "date_from": "2026-13-01"},
    ],
)
def test_bad_args_422(client, params):
    assert client.get("/search", params=params).status_code == 422


def test_search_query_with_fts_syntax(client):
    for q in ['"크라운*(', "틀니 OR", "-", "a AND"]:
        assert client.get("/search", params={"q": q}).status_code == 200


def test_search_reversed_dates_is_empty_not_error(client):
    params = {"q": "틀니", "date_from": "2026-12-01", "date_to": "2026-01-01"}
    r = client.get("/search", params=params)
    assert r.status_code == 200
    assert r.json() == {"manual_results": [], "case_results": []}


def test_missing_db_files_503_without_creating(client_with, tmp_path):
    app_db, source_db = tmp_path / "none" / "app.sqlite", tmp_path / "none" / "source.sqlite"
    r = client_with(paths=(app_db, source_db)).get("/search", params={"q": "틀니"})
    assert r.status_code == 503 and "make index" in r.json()["detail"]
    assert not app_db.exists()


def test_empty_index_503(client_with, source_paths, tmp_path):
    # client_with가 tmp_path의 app.sqlite를 색인하므로 다른 폴더에 청크 없는 app.sqlite를 둔다
    (tmp_path / "empty").mkdir()
    paths = (tmp_path / "empty" / "app.sqlite", tmp_path / "empty" / "source.sqlite")
    shutil.copy(source_paths[1], paths[1])
    conn = db.connect(*paths)
    db.init_schema(conn)
    conn.close()
    r = client_with(paths=paths).get("/search", params={"q": "틀니"})
    assert r.status_code == 503 and "make index" in r.json()["detail"]


def test_missing_vectors_503(client_with):
    models = (GuardedEmbedder(FakeEmbedder(model="other")), None)
    r = client_with(models=models).get("/search", params={"q": "틀니"})
    assert r.status_code == 503 and "make index" in r.json()["detail"]


def test_embedding_failure_503(client_with):
    class Broken(FakeEmbedder):
        def embed(self, texts, kind):
            raise RuntimeError("키 없음")

    r = client_with(models=(GuardedEmbedder(Broken()), None)).get("/search", params={"q": "틀니"})
    assert r.status_code == 503
    assert "임베딩 호출 실패" in r.json()["detail"] and "키 없음" in r.json()["detail"]


def test_clinic(client):
    assert client.get("/clinic").json() == {
        "name": "클로드치과",
        "today": load_clinic().today.isoformat(),
    }


def test_clinic_missing_db_503(client_with, tmp_path):
    r = client_with(paths=(tmp_path / "a.sqlite", tmp_path / "s.sqlite")).get("/clinic")
    assert r.status_code == 503 and "make data" in r.json()["detail"]


def test_concurrent_requests_on_real_server(indexed_paths):
    # 연결을 핸들러 밖(의존성)에서 열면 스레드가 달라 sqlite3가 막는다 — TestClient로는 재현되지 않는다
    override(indexed_paths, (GuardedEmbedder(FakeEmbedder()), None))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started
        base = f"http://127.0.0.1:{port}"

        def status(_):
            return httpx.get(f"{base}/search", params={"q": "틀니"}, timeout=10).status_code

        with ThreadPoolExecutor(16) as pool:
            codes = list(pool.map(status, range(48)))
        assert codes == [200] * 48
    finally:
        server.should_exit = True
        thread.join(10)
        app.dependency_overrides.clear()


def test_clinic_needs_only_source(client_with, source_paths, tmp_path):
    # 색인(app.sqlite)이 없어도 병원 이름·기준일은 나와야 한다 — 머리 띠가 '—'가 되지 않게
    app_db = tmp_path / "none" / "app.sqlite"
    r = client_with(paths=(app_db, source_paths[1])).get("/clinic")
    assert r.status_code == 200 and r.json()["name"] == "클로드치과"
    assert not app_db.exists()


def test_bad_date_detail_is_short(client):
    # 손으로 고친 URL이면 화면 오류 띠에 pydantic 원문 대신 짧은 문구가 나온다
    detail = client.get("/search", params={"q": "틀니", "date_from": "2026-13-01"}).json()[
        "detail"
    ]
    assert "date_from" in detail and "pydantic.dev" not in detail and "\n" not in detail
