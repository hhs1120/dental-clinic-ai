"""종이 차트 이관 API (backend/api.py /chart-ocr/*, 차트 OCR 설계 §설계 3) — 손으로 만든 source + 가짜 업스테이지
(실제 응답 고정값을 이름·생년월일만 바꿔 돌려준다)."""

# ruff: noqa: F401, F811  (다른 테스트 모듈의 fixture를 가져와 쓴다)

import base64
import json
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import api, chart_ocr
from tests.backend.test_emr_bridge import CLINIC, MISSED, NEW, conn, src

NOW = datetime(2026, 12, 1, 9, 5, 0)
FIXTURES = Path(__file__).parent / "fixtures"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


class FakeUpstage:
    """(image, filename) → (OCR 응답, 정보 추출 응답) — 김철수(MISSED)로 바꾼 고정값, 부른 횟수를 센다"""

    def __init__(self, fail: bool = False):
        self.calls, self.fail = 0, fail
        self.ocr = json.loads((FIXTURES / "upstage_ocr.json").read_text(encoding="utf-8"))
        ext = json.loads((FIXTURES / "upstage_extract.json").read_text(encoding="utf-8"))
        content = json.loads(ext["choices"][0]["message"]["content"])
        content |= {"patient_name": "김철수", "birth_date": "1980-01-01"}
        ext["choices"][0]["message"]["content"] = json.dumps(content, ensure_ascii=False)
        self.extract = ext

    def __call__(self, image: bytes, filename: str) -> tuple[dict, dict]:
        self.calls += 1
        if self.fail:
            raise chart_ocr.UpstageError("업스테이지 오류 500: boom")
        return self.ocr, self.extract


@pytest.fixture
def upstage():
    return FakeUpstage()


@pytest.fixture
def client(conn, tmp_path, upstage):
    samples = tmp_path / "paper"
    samples.mkdir()
    (samples / "chart-C-00001.png").write_bytes(PNG)
    api.app.dependency_overrides[api.get_db_paths] = lambda: (
        tmp_path / "a.sqlite",
        tmp_path / "s.sqlite",
    )
    api.app.dependency_overrides[api.get_clinic] = lambda: CLINIC
    api.app.dependency_overrides[api.get_now] = lambda: NOW
    api.app.dependency_overrides[api.get_upstage] = lambda: upstage
    api.app.dependency_overrides[api.get_paper_charts] = lambda: samples
    yield TestClient(api.app)
    api.app.dependency_overrides.clear()


def _final(detail: dict, quadrant: int = 4) -> dict:
    """화면에서 사람이 고친 것처럼 — 첫 진료에 사분면·FDI를 정한다"""
    f = detail["fields"]
    f["visits"][0] |= {"quadrant": quadrant, "fdi": "46"}
    return f


def test_sample_creates_draft_with_words_and_fields(client):
    assert client.get("/chart-ocr/samples").json() == [
        {"name": "chart-C-00001.png", "imported_id": None}
    ]
    d = client.post("/chart-ocr/samples/chart-C-00001.png").json()
    assert d["status"] == "draft" and d["filename"] == "chart-C-00001.png"
    assert len(d["words"]) == 304 and d["low_confidence"] == chart_ocr.LOW_CONFIDENCE
    assert d["fields"]["patient_name"] == "김철수" and len(d["fields"]["visits"]) == 12
    assert d["patient"] == {"id": MISSED, "chart_no": "C-00001", "name": "김철수"}
    assert client.get(f"/chart-ocr/{d['id']}/image").content == PNG
    assert client.get("/chart-ocr/samples").json()[0]["imported_id"] == d["id"]


def test_sample_reuses_saved_import(client, upstage):
    a = client.post("/chart-ocr/samples/chart-C-00001.png").json()
    b = client.post("/chart-ocr/samples/chart-C-00001.png").json()
    assert a["id"] == b["id"] and upstage.calls == 1


def test_unknown_sample_404(client):
    assert client.post("/chart-ocr/samples/nope.png").status_code == 404


def test_upload_creates_draft_and_lists(client):
    body = {"filename": "scan.png", "image_base64": base64.b64encode(PNG).decode()}
    d = client.post("/chart-ocr", json=body).json()
    assert d["status"] == "draft"
    rows = client.get("/chart-ocr").json()
    assert [(r["id"], r["status"], r["patient_name"]) for r in rows] == [
        (d["id"], "draft", "김철수")
    ]


def test_upload_too_large_413_and_not_image_415(client):
    big = base64.b64encode(PNG + b"0" * api.CHART_OCR_MAX_BYTES).decode()
    assert (
        client.post("/chart-ocr", json={"filename": "a.png", "image_base64": big}).status_code
        == 413
    )
    txt = base64.b64encode(b"hello").decode()
    assert (
        client.post("/chart-ocr", json={"filename": "a.txt", "image_base64": txt}).status_code
        == 415
    )


def test_upstage_error_502_no_row(client, upstage):
    upstage.fail = True
    r = client.post("/chart-ocr/samples/chart-C-00001.png")
    assert r.status_code == 502 and "boom" in r.json()["detail"]
    assert client.get("/chart-ocr").json() == []


def test_approve_then_409(client):
    d = client.post("/chart-ocr/samples/chart-C-00001.png").json()
    body = {"final": _final(d), "patient_id": MISSED, "part": "데스크"}
    got = client.post(f"/chart-ocr/{d['id']}/approve", json=body).json()
    assert got["status"] == "approved" and got["final"]["visits"][0]["fdi"] == "46"
    assert client.post(f"/chart-ocr/{d['id']}/approve", json=body).status_code == 409
    assert client.post(f"/chart-ocr/{d['id']}/reject", json={"part": "데스크"}).status_code == 409
    assert client.post("/chart-ocr/999/approve", json=body).status_code == 404


def test_approve_without_patient_422(client):
    d = client.post("/chart-ocr/samples/chart-C-00001.png").json()
    body = {"final": _final(d), "patient_id": None, "part": "데스크"}
    assert client.post(f"/chart-ocr/{d['id']}/approve", json=body).status_code == 422
    body["patient_id"] = 999
    assert client.post(f"/chart-ocr/{d['id']}/approve", json=body).status_code == 422


def test_reject(client):
    d = client.post("/chart-ocr/samples/chart-C-00001.png").json()
    got = client.post(f"/chart-ocr/{d['id']}/reject", json={"part": "데스크"}).json()
    assert got["status"] == "rejected"


def test_abbreviations(client):
    assert client.get("/chart-ocr/abbreviations").json()["OHI"] == "구강 위생 교육"


def test_emr_patient_shows_paper_records(client):
    d = client.post("/chart-ocr/samples/chart-C-00001.png").json()
    body = {"final": _final(d), "patient_id": MISSED, "part": "데스크"}
    client.post(f"/chart-ocr/{d['id']}/approve", json=body)
    rec = client.get("/emr/patients/C-00001").json()["paper_records"]
    assert rec[0] == {
        "import_id": d["id"],
        "date": "2010-02-08",
        "teeth": "전악",
        "fdi": "46",
        "treatment": d["fields"]["visits"][0]["treatment"],
    }
    assert len(rec) == 12
    assert client.get("/emr/patients/C-00002").json()["paper_records"] == []
