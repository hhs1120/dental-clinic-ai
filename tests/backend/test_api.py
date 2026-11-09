"""앱 API (앱 설계 §9, 앱 뼈대 설계안 '백엔드'). 키 없이 돈다 — FakeEmbedder, 리랭커 끔."""

import json
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


def test_db_paths_follow_data_dir_env(monkeypatch, tmp_path):
    """AX_DATA_DIR — Playwright가 data/ 복사본으로 API를 띄울 때(진짜 판정 DB를 더럽히지 않게)"""
    from backend.api import get_db_paths

    monkeypatch.setenv("AX_DATA_DIR", str(tmp_path))
    assert get_db_paths() == (tmp_path / "app.sqlite", tmp_path / "source.sqlite")
    monkeypatch.delenv("AX_DATA_DIR")
    assert get_db_paths()[0].parent.name == "data"


# ── POST /chat (설계 2026-11-09 챗 "API·화면") ─────────────────────────────


def _events(text: str) -> list[tuple[str, dict]]:
    import json

    out = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        out.append((lines["event"], json.loads(lines["data"])))
    return out


@pytest.fixture
def chat_client(client_with):
    from backend.api import get_chat_gateway
    from tests.backend.test_followup import Script

    def make(*runs):
        c = client_with()
        app.dependency_overrides[get_chat_gateway] = lambda: lambda conn: Script(conn, *runs)
        return c

    return make


SEARCH_RUN = [
    ("tool", "search_docs", {"query": "틀니 수리"}),
    ("tool", "answer", {"text": "접수 후 기공소로 보낸다", "cited_ids": ["doc:3:0"]}),
]


def _log_rows(paths):
    conn = db.connect(*paths)
    rows = conn.execute("SELECT status FROM ax_search_logs").fetchall()
    conn.close()
    return [r[0] for r in rows]


def test_chat_streams_steps_then_answer(chat_client):
    r = chat_client(SEARCH_RUN).post("/chat", json={"question": "틀니 수리 어떻게 해?"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    events = _events(r.text)
    assert [e for e, _ in events] == ["step", "answer"]
    assert events[0][1] == {"tool": "search_docs"}
    answer = events[1][1]
    assert answer["status"] == "answered" and answer["text"] == "접수 후 기공소로 보낸다"
    assert [s["id"] for s in answer["sources"]] == ["doc:3:0"] and answer["sources"][0]["title"]


def test_chat_rejects_blank_and_long_question(chat_client, indexed_paths):
    from backend.api import CHAT_MAX_QUESTION

    c = chat_client(SEARCH_RUN)
    assert c.post("/chat", json={"question": "   "}).status_code == 422
    assert c.post("/chat", json={"question": "가" * (CHAT_MAX_QUESTION + 1)}).status_code == 422
    assert _log_rows(indexed_paths) == []


def test_chat_log_written_even_if_stream_closed(chat_client, indexed_paths):
    c = chat_client(SEARCH_RUN)
    with c.stream("POST", "/chat", json={"question": "틀니 수리 어떻게 해?"}) as r:
        next(r.iter_lines())  # 첫 줄만 읽고 닫는다
    for _ in range(50):
        if _log_rows(indexed_paths) and "running" not in _log_rows(indexed_paths):
            break
        time.sleep(0.05)
    assert _log_rows(indexed_paths) == ["answered"]


def test_chat_without_index_is_503(client_with, source_paths, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    paths = (empty / "app.sqlite", shutil.copy(source_paths[1], empty / "source.sqlite"))
    conn = db.connect(*paths)
    db.init_schema(conn)
    conn.close()
    assert client_with(paths=paths).post("/chat", json={"question": "틀니"}).status_code == 503


def test_search_records_voyage_usage(client_with, indexed_paths):
    """기록 검색의 Voyage 호출도 ax_llm_calls에 — 하루 상한이 센다(리뷰 2)"""

    class Counting(FakeEmbedder):
        def embed_with_usage(self, texts, kind):
            return self.embed(texts, kind), 7

    c = client_with(models=(GuardedEmbedder(Counting()), None))
    assert c.get("/search", params={"q": "틀니 수리"}).status_code == 200
    conn = db.connect(*indexed_paths)
    rows = conn.execute("SELECT task, model_alias, input_tokens FROM ax_llm_calls").fetchall()
    conn.close()
    assert [tuple(r) for r in rows] == [("search_embed", "voyage", 7)]


@pytest.fixture
def summary_client(client_with):
    """요약 게이트웨이를 대본으로(설계 2026-11-09 검색 요약 "요약 API")"""
    from backend.api import get_chat_gateway
    from tests.backend.script import Script

    def make(*turns, **kw):
        c = client_with(**kw)
        gw = Script(*turns)
        app.dependency_overrides[get_chat_gateway] = lambda: lambda conn: gw
        return c, gw

    return make


def summary_out(text, cited):
    from shared.llm import LLMResult

    data = {"text": text, "cited_ids": cited, "refused": False}
    return LLMResult(True, "", data, "m", False, 1, 1, 0.01, 1)


def test_summary_returns_numbered_sources(summary_client):
    c, _ = summary_client(summary_out("접수 후 기공소로 보낸다[1].", ["doc:3:0"]))
    r = c.post("/search/summary", json={"q": "틀니 수리"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "answered" and body["text"] == "접수 후 기공소로 보낸다[1]."
    assert [s["id"] for s in body["sources"]] == ["doc:3:0"]
    assert body["sources"][0]["title"]  # 검색 카드 형식


def test_summary_same_filters_as_search(summary_client):
    c, gw = summary_client(summary_out("틀니 수리 문의[1].", ["call:c-0001:0"]))
    c.post("/search/summary", json={"q": "틀니 수리", "source_types": "call"})
    sent = json.dumps(gw.calls[0]["messages"], ensure_ascii=False)
    assert "doc:" not in sent and "call:" in sent


def test_summary_bad_args_is_422(summary_client):
    c, gw = summary_client()
    assert c.post("/search/summary", json={"q": "틀니", "source_types": ""}).status_code == 422
    assert gw.calls == []


def test_summary_without_index_is_503(client_with, source_paths, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    paths = (empty / "app.sqlite", shutil.copy(source_paths[1], empty / "source.sqlite"))
    conn = db.connect(*paths)
    db.init_schema(conn)
    conn.close()
    assert client_with(paths=paths).post("/search/summary", json={"q": "틀니"}).status_code == 503


def test_summary_records_voyage_usage(summary_client, indexed_paths):
    class Counting(FakeEmbedder):
        def embed_with_usage(self, texts, kind):
            return self.embed(texts, kind), 7

    c, _ = summary_client(
        summary_out("접수 후 기공소로 보낸다[1].", ["doc:3:0"]),
        models=(GuardedEmbedder(Counting()), None),
    )
    assert c.post("/search/summary", json={"q": "틀니 수리"}).status_code == 200
    conn = db.connect(*indexed_paths)
    rows = conn.execute("SELECT task FROM ax_llm_calls WHERE model_alias = 'voyage'").fetchall()
    conn.close()
    assert [r[0] for r in rows] == ["search_embed"]
