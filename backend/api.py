"""앱 API (앱 설계 §9, 앱 뼈대 설계안 '백엔드'). 기록 검색은 평가에 쓴 Searcher를 그대로 부른다.
sqlite3 연결은 만든 스레드에 묶이고 의존성과 핸들러는 다른 워커 스레드에서 돌 수 있어, 핸들러를 동기 def로 쓰고
연결·Searcher는 핸들러 안에서 만들어 닫는다. 임베더·리랭커만 프로세스에 하나 둔다."""

import datetime as dt
import os
import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, ValidationError

from backend import blind, calls, db, emr, judge_queue, metrics
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
from shared.call_labels import (
    COUNTERPART_KO,
    FLAG_KO,
    PAIR_CHOICES,
    PURPOSE_KO,
    URGENCY_KO,
)
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
    """환경 변수 AX_DATA_DIR이 있으면 그 폴더 — Playwright가 data/ 복사본으로 띄운다(make e2e)"""
    data = Path(os.environ.get("AX_DATA_DIR") or ROOT / "data")
    return data / "app.sqlite", data / "source.sqlite"


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


# ── 통화·② 판정 화면 (통화·② 판정 화면 설계 "API") ─────────────────────────


def get_clinic() -> Clinic:
    return _clinic()


def get_now() -> dt.datetime:
    return dt.datetime.now()


def get_gateway_factory():
    """연결 → 게이트웨이. 호출 기록이 그 연결의 ax_llm_calls에 남는다(테스트는 FakeGateway로 바꾼다)"""
    from backend.harness.llm import make_gateway

    return make_gateway


Paths = Annotated[tuple[Path, Path], Depends(get_db_paths)]
ClinicDep = Annotated[Clinic, Depends(get_clinic)]
Now = Annotated[dt.datetime, Depends(get_now)]
GatewayFactory = Annotated[object, Depends(get_gateway_factory)]


def _open_rw(paths: tuple[Path, Path]) -> sqlite3.Connection:
    """쓰기 연결 — 앱 DB가 없으면 만들지 않고 503, 스키마는 맞춘다"""
    if not all(Path(p).exists() for p in paths):
        raise HTTPException(503, NO_DATA)
    conn = db.connect(*paths)
    db.init_schema(conn)
    return conn


def _stamp(now: dt.datetime) -> str:
    return now.strftime("%Y-%m-%d %H:%M:%S")


@app.get("/labels")
def labels() -> dict:
    return {
        "purpose": PURPOSE_KO,
        "counterpart": COUNTERPART_KO,
        "urgency": URGENCY_KO,
        "flag": FLAG_KO,
    }


@app.get("/calls/today")
def calls_today(paths: Paths, clinic: ClinicDep) -> dict:
    conn = _open_rw(paths)
    try:
        return calls.today_board(conn, clinic.today)
    finally:
        conn.close()


@app.get("/calls")
def calls_list(
    paths: Paths,
    filter: str | None = None,
    q: str | None = None,
    before: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> dict:
    """limit — 화면이 "더 보기"로 펼친 만큼 한 번에 다시 받을 때"""
    conn = _open_rw(paths)
    try:
        return calls.list_calls(conn, flt=filter or None, q=q, before=before, limit=limit)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    finally:
        conn.close()


@app.get("/calls/{call_uid}")
def call_detail(call_uid: str, paths: Paths) -> dict:
    conn = _open_rw(paths)
    try:
        d = calls.call_detail(conn, call_uid)
    finally:
        conn.close()
    if d is None:
        raise HTTPException(404, "통화를 찾을 수 없습니다")
    return d


class NewCall(BaseModel):
    direction: str
    transcript: str
    phone: str | None = None
    part: str


@app.post("/calls")
def add_call(body: NewCall, paths: Paths, clinic: ClinicDep, now: Now, gw: GatewayFactory) -> dict:
    conn = _open_rw(paths)
    try:
        uid = calls.add_call(
            conn,
            gw(conn),
            direction=body.direction,
            transcript=body.transcript,
            phone=body.phone,
            part=body.part,
            now=now,
            today=clinic.today,
        )
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    finally:
        conn.close()
    return {"call_uid": uid}


@app.post("/calls/{call_uid}/classify")
def reclassify(call_uid: str, paths: Paths, gw: GatewayFactory) -> dict:
    conn = _open_rw(paths)
    try:
        if calls.call_detail(conn, call_uid) is None:
            raise HTTPException(404, "통화를 찾을 수 없습니다")
        calls.classify_one(conn, gw(conn), call_uid)
        return calls.call_detail(conn, call_uid)
    finally:
        conn.close()


class TodoDone(BaseModel):
    part: str
    done: bool


@app.post("/calls/{call_uid}/todos/{item}/done")
def todo_done(call_uid: str, item: int, body: TodoDone, paths: Paths, now: Now) -> dict:
    conn = _open_rw(paths)
    try:
        d = calls.call_detail(conn, call_uid)
        if d is None or d["classification"]["id"] is None:
            raise HTTPException(404, "통화를 찾을 수 없습니다")
        try:
            calls.set_todo_done(
                conn,
                d["classification"]["id"],
                item,
                part=body.part,
                at=_stamp(now),
                done=body.done,
            )
        except KeyError as e:
            raise HTTPException(404, "할 일을 찾을 수 없습니다") from e
        return {"ok": True}
    finally:
        conn.close()


@app.get("/judge/next")
def judge_next(paths: Paths, now: Now) -> dict | None:
    conn = _open_rw(paths)
    try:
        return judge_queue.next_call(conn, now.date().isoformat())
    finally:
        conn.close()


class Judgment(BaseModel):
    call_uid: str
    labels: dict
    note: str | None = None
    part: str


@app.post("/judgments")
def save_judgment(body: Judgment, paths: Paths, now: Now) -> dict:
    conn = _open_rw(paths)
    try:
        return judge_queue.save(
            conn, body.call_uid, body.labels, body.note or None, part=body.part, at=_stamp(now)
        )
    except KeyError as e:
        raise HTTPException(404, "판정할 통화가 아닙니다") from e
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    finally:
        conn.close()


@app.post("/calls/{call_uid}/skip")
def skip_judgment(call_uid: str, paths: Paths, now: Now) -> dict:
    conn = _open_rw(paths)
    try:
        judge_queue.skip(conn, call_uid, _stamp(now))
        return {"ok": True}
    finally:
        conn.close()


@app.get("/blind/next")
def blind_next(paths: Paths, clinic: ClinicDep) -> dict | None:
    conn = _open_rw(paths)
    try:
        return blind.next_pair(conn, clinic)
    finally:
        conn.close()


class BlindVote(BaseModel):
    left_id: int
    right_id: int
    choice: str
    part: str


@app.post("/blind-votes")
def blind_vote(body: BlindVote, paths: Paths, clinic: ClinicDep, now: Now) -> dict:
    if body.choice not in PAIR_CHOICES:
        raise HTTPException(422, f"choice: {body.choice}")
    conn = _open_rw(paths)
    try:
        return blind.save_vote(
            conn,
            clinic,
            body.left_id,
            body.right_id,
            body.choice,
            part=body.part,
            at=_stamp(now),
        )
    except ValueError as e:
        raise HTTPException(422, "보여 준 비교 쌍이 아닙니다") from e
    except sqlite3.IntegrityError as e:
        raise HTTPException(409, "이미 투표한 쌍입니다") from e
    finally:
        conn.close()


@app.get("/metrics/accuracy")
def accuracy(paths: Paths, clinic: ClinicDep) -> dict:
    conn = _open_rw(paths)
    try:
        a, b = calls.active_versions(conn)
        versions = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT prompt_version FROM ax_call_classifications"
                " WHERE model_alias = 'classify' ORDER BY prompt_version"
            )
        ]
        pair = db.get_setting(conn, "blind_versions")
        return {
            "agreement": metrics.agreement(conn, clinic, versions, b) if b else {},
            "blind": metrics.blind_rates(conn, clinic, *pair) if pair else None,
            "human_share": metrics.human_share(conn, a, b),
            "versions": {"classify": versions, "active": a, "judge": b, "blind": pair},
        }
    finally:
        conn.close()
