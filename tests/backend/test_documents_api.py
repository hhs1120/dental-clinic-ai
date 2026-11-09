"""문서함 API (기능 정의 §4 ③ 문서함, 설계 2026-11-09 검색 요약 "문서함 API") — 공지는 최신 버전만, 본문은 버전 목록과 함께.
source_paths: 공지 1·2 "미백 비용" v1/v2, 매뉴얼 3."""

import pytest
from fastapi.testclient import TestClient

from backend.api import app, get_db_paths


@pytest.fixture
def client(source_paths):
    """source.sqlite만 — 문서함은 색인(app.sqlite) 없이 열린다"""
    app.dependency_overrides[get_db_paths] = lambda: source_paths
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_list_documents_latest_notice_only(client):
    docs = client.get("/documents").json()["documents"]
    assert [d["id"] for d in docs if d["type"] == "notice"] == [2]
    assert {d["id"] for d in docs} == {2, 3}
    assert set(docs[0]) == {"id", "type", "title", "version", "effective_date"}


def test_document_detail_has_versions(client):
    d = client.get("/documents/2").json()
    assert d["body"].startswith("# 변경 내용")
    assert [v["id"] for v in d["versions"]] == [2, 1] and d["latest_id"] == 2


def test_document_old_version_points_to_latest(client):
    assert client.get("/documents/1").json()["latest_id"] == 2


def test_manual_has_no_versions(client):
    d = client.get("/documents/3").json()
    assert d["versions"] == [] and d["latest_id"] == 3


def test_missing_document_is_404(client):
    assert client.get("/documents/999").status_code == 404


def test_documents_work_without_index(client, source_paths):
    """app.sqlite 없이(색인 전)도 열린다"""
    assert not source_paths[0].exists()
    assert client.get("/documents").status_code == 200
