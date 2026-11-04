"""앱 API (앱 설계 §9, 앱 뼈대 설계안 '백엔드'). 기록 검색은 평가에 쓴 Searcher를 그대로 부른다.
sqlite3 연결은 만든 스레드에 묶이고 의존성과 핸들러는 다른 워커 스레드에서 돌 수 있어, 핸들러를 동기 def로 쓰고
연결·Searcher는 핸들러 안에서 만들어 닫는다. 임베더·리랭커만 프로세스에 하나 둔다."""

import datetime as dt
import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from pydantic import ValidationError

from backend import db, emr
from backend.embeddings import make_embedder
from backend.rag_search import (
    DEFAULT_EMBEDDING,
    DEFAULT_RERANKER,
    SEARCH_TOP_K,
    MissingVectors,
    SearchArgs,
    Searcher,
    voyage_rerank,
)
from backend.search_cards import card
from shared.clinic import Clinic, load_clinic

ROOT = Path(__file__).resolve().parents[1]
NO_INDEX = f"검색 색인이 없습니다 — make index EMBEDDING={DEFAULT_EMBEDDING}을 먼저 실행하세요"
NO_DATA = "데이터가 없습니다 — make data를 먼저 실행하세요"

app = FastAPI(title="치과 AX")


class EmbeddingFailed(RuntimeError):
    """임베딩 호출 실패(키 없음 포함) — 503으로 답한다"""


class GuardedEmbedder:
    """embed의 예외를 EmbeddingFailed로 바꾼다 — 검색 안의 다른 오류와 구분해 원인 문구로 답하게"""

    def __init__(self, inner):
        self.inner, self.model = inner, inner.model

    def embed(self, texts: list[str], kind: str):
        try:
            return self.inner.embed(texts, kind)
        except Exception as e:
            raise EmbeddingFailed(str(e)) from e


def get_db_paths() -> tuple[Path, Path]:
    return ROOT / "data" / "app.sqlite", ROOT / "data" / "source.sqlite"


@lru_cache
def _models():
    return GuardedEmbedder(make_embedder(DEFAULT_EMBEDDING)), voyage_rerank(DEFAULT_RERANKER)


def get_models():
    try:
        return _models()
    except Exception as e:  # 키 없음 등 — 실패는 캐시되지 않아 키를 넣으면 다음 요청에 다시 만든다
        raise HTTPException(503, f"임베딩 준비 실패: {e}") from e


@lru_cache
def _clinic() -> Clinic:
    return load_clinic()


def _open(paths: tuple[Path, Path], missing: str) -> sqlite3.Connection:
    """파일이 없으면 연결 전에 503 — connect가 빈 app.sqlite를 만들지 않게"""
    if not all(Path(p).exists() for p in paths):
        raise HTTPException(503, missing)
    conn = db.connect(*paths)
    conn.execute("PRAGMA query_only = ON")
    return conn


def to_search_args(
    q: str,
    source_types: str | None,
    date_from: str | None,
    date_to: str | None,
    chart_no: str | None,
) -> SearchArgs:
    """API 인자 → SearchArgs (설계안 변환 표). 잘못된 값은 ValueError"""
    types = None
    if source_types is not None:
        types = [t.strip() for t in source_types.split(",") if t.strip()]
        if not types:  # SearchArgs는 빈 목록을 '전체'로 받으므로 여기서 막는다
            raise ValueError("source_types가 비어 있습니다")
    return SearchArgs(
        query=q.strip(),
        source_types=types,
        date_from=date_from or None,
        date_to=date_to or None,
        patient_chart_no=(chart_no or "").strip().upper() or None,
        top_k=SEARCH_TOP_K,
    )


@app.get("/search")
def search(
    q: str,
    paths: Annotated[tuple[Path, Path], Depends(get_db_paths)],
    models: Annotated[tuple, Depends(get_models)],
    source_types: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    chart_no: str | None = None,
) -> dict:
    try:
        args = to_search_args(q, source_types, date_from, date_to, chart_no)
    except ValidationError as e:
        # 화면에는 첫 오류만 짧게 — pydantic 원문은 여러 줄에 링크까지 붙는다
        err = e.errors()[0]
        raise HTTPException(422, f"{'.'.join(map(str, err['loc']))}: {err['msg']}") from e
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    conn = _open(paths, NO_INDEX)
    try:
        if not conn.execute("SELECT 1 FROM ax_chunks LIMIT 1").fetchone():
            raise HTTPException(503, NO_INDEX)
        try:
            searcher = Searcher(conn, _clinic(), *models)
            found = searcher.search(**args.model_dump(mode="json"))
        except MissingVectors as e:
            raise HTTPException(503, NO_INDEX) from e
        except EmbeddingFailed as e:
            raise HTTPException(503, f"임베딩 호출 실패: {e}") from e
        return {
            bucket: [card(conn, c, searcher.replaced_by) for c in chunks]
            for bucket, chunks in found.items()
        }
    finally:
        conn.close()


def _open_source(paths: tuple[Path, Path]) -> sqlite3.Connection:
    """source.sqlite만 읽기 전용으로 — 색인(app.sqlite)이 없어도 병원 정보·EMR·예약표는 나온다"""
    source = Path(paths[1])
    if not source.exists():
        raise HTTPException(503, NO_DATA)
    conn = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


@app.get("/clinic")
def clinic(paths: Annotated[tuple[Path, Path], Depends(get_db_paths)]) -> dict:
    conn = _open_source(paths)
    try:
        name = conn.execute("SELECT name FROM emr_clinic").fetchone()[0]
    finally:
        conn.close()
    return {"name": name, "today": _clinic().today.isoformat()}


@app.get("/emr/schedule")
def emr_schedule(
    paths: Annotated[tuple[Path, Path], Depends(get_db_paths)], date: str | None = None
) -> dict:
    clinic = _clinic()
    try:
        day = dt.date.fromisoformat(date) if date else clinic.today
    except ValueError as e:
        raise HTTPException(422, f"date: {e}") from e
    conn = _open_source(paths)
    try:
        return emr.schedule_day(conn, day, clinic.today, clinic.range_start)
    finally:
        conn.close()


@app.get("/emr/patients")
def emr_patients(q: str, paths: Annotated[tuple[Path, Path], Depends(get_db_paths)]) -> list:
    if not q.strip():
        raise HTTPException(422, "q: 검색어가 비어 있습니다")
    conn = _open_source(paths)
    try:
        return emr.search_patients(conn, q.strip(), _clinic().today)
    finally:
        conn.close()


@app.get("/emr/patients/{chart_no}")
def emr_patient(chart_no: str, paths: Annotated[tuple[Path, Path], Depends(get_db_paths)]) -> dict:
    conn = _open_source(paths)
    try:
        record = emr.patient_record(conn, chart_no.strip(), _clinic().today)
    finally:
        conn.close()
    if record is None:
        raise HTTPException(404, "환자를 찾을 수 없습니다")
    return record


@app.get("/emr/tour")
def emr_tour(paths: Annotated[tuple[Path, Path], Depends(get_db_paths)]) -> dict:
    conn = _open_source(paths)
    try:
        tour = emr.tour_patient(conn, _clinic().today)
    finally:
        conn.close()
    if tour is None:
        raise HTTPException(404, "기준일 예약이 없습니다")
    return tour
