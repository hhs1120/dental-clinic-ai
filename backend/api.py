"""앱 API (앱 설계 §9, 앱 뼈대 설계안 '백엔드'). 기록 검색은 평가에 쓴 Searcher를 그대로 부른다.
sqlite3 연결은 만든 스레드에 묶이고 의존성과 핸들러는 다른 워커 스레드에서 돌 수 있어, 핸들러를 동기 def로 쓰고
연결·Searcher는 핸들러 안에서 만들어 닫는다. 임베더·리랭커만 프로세스에 하나 둔다."""

import datetime as dt
import json
import os
import queue
import sqlite3
import threading
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, ValidationError, field_validator

from backend import (
    autoclose,
    blind,
    calls,
    chat,
    claim_appeal,
    claim_check,
    claim_evidence,
    claim_omission,
    claim_search,
    claims,
    db,
    documents,
    emr,
    emr_bridge,
    expense,
    expense_auto,
    judge_queue,
    metrics,
    missed,
    prep,
    recon,
    recon_agent,
    recon_auto,
    recon_match,
    report,
    search_summary,
    today,
)
from backend.embeddings import make_embedder
from backend.harness.auto import revert_auto_action
from backend.harness.llm import make_gateway
from backend.harness.store import RunLocked
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
from backend.usage import record_voyage
from shared.call_labels import (
    COUNTERPART_KO,
    FLAG_KO,
    PAIR_CHOICES,
    PURPOSE_KO,
    URGENCY_KO,
)
from shared.clinic import Clinic, load_clinic
from shared.followup import ACTION_KO

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
        if hasattr(inner, "embed_with_usage"):  # 사용량을 돌려주는 임베더만 — 검색기가 기록한다
            self.embed_with_usage = self._guard(inner.embed_with_usage)

    @staticmethod
    def _guard(fn):
        def run(*args):
            try:
                return fn(*args)
            except Exception as e:
                raise EmbeddingFailed(str(e)) from e

        return run

    def embed(self, texts: list[str], kind: str):
        try:
            return self.inner.embed(texts, kind)
        except Exception as e:
            raise EmbeddingFailed(str(e)) from e


def get_db_paths() -> tuple[Path, Path]:
    """환경 변수 AX_DATA_DIR이 있으면 그 폴더 — Playwright가 data/ 복사본으로 띄운다(make e2e)"""
    data = Path(os.environ.get("AX_DATA_DIR") or ROOT / "data")
    return data / "app.sqlite", data / "source.sqlite"


def get_improve_report() -> Path:
    """자동 개선 이력 — 자동 개선 루프가 쓰는 라벨 없는 집계 파일(평가 설계 §2.7, 앱은 파일만 읽는다)"""
    return ROOT / "reports" / "improve_runs.jsonl"


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
        out = {
            bucket: [card(conn, c, searcher.replaced_by) for c in chunks]
            for bucket, chunks in found.items()
        }
    finally:
        conn.close()
    if usage := searcher.drain_usage():  # 읽기 전용 연결이라 기록은 따로 연다(하루 상한이 센다)
        log = db.connect(*paths)
        try:
            record_voyage(log, usage, is_eval=False, ref=None)
        finally:
            log.close()
    return out


@app.get("/claims/standards")
def claim_standards(
    q: str,
    paths: Annotated[tuple[Path, Path], Depends(get_db_paths)],
    models: Annotated[tuple, Depends(get_models)],
) -> dict:
    """⑤-3 청구 기준 찾기 — 청구 기준 절 5개 원문(backend/claim_search.py), 색인이 없으면 503(GET /search와 같음)"""
    if not q.strip():
        raise HTTPException(422, "q: 질문이 비었습니다")
    conn = _open(paths, NO_INDEX)
    try:
        if not conn.execute("SELECT 1 FROM ax_chunks LIMIT 1").fetchone():
            raise HTTPException(503, NO_INDEX)
        try:
            searcher = Searcher(conn, _clinic(), *models)
            results = claim_search.search(searcher, q.strip())
        except MissingVectors as e:
            raise HTTPException(503, NO_INDEX) from e
        except EmbeddingFailed as e:
            raise HTTPException(503, f"임베딩 호출 실패: {e}") from e
    finally:
        conn.close()
    if usage := searcher.drain_usage():  # 읽기 전용 연결이라 기록은 따로 연다(하루 상한이 센다)
        log = db.connect(*paths)
        try:
            record_voyage(log, usage, is_eval=False, ref=None)
        finally:
            log.close()
    return {"results": results}


CHAT_MAX_QUESTION = 500  # 글자 — 질문 1개(기능 정의 §4 ③ 멀티턴 없음)


class ChatBody(BaseModel):
    question: str = Field(min_length=1, max_length=CHAT_MAX_QUESTION)

    @field_validator("question", mode="before")
    @classmethod
    def strip(cls, v):
        return v.strip() if isinstance(v, str) else v


def get_chat_gateway():
    """연결 → 게이트웨이. 테스트가 가짜로 바꾼다"""
    return make_gateway


def get_recon_gateway():
    """⑧-1 조사 에이전트 — 연결 → 게이트웨이. 테스트가 대본 게이트웨이로 바꾼다"""
    return make_gateway


class SummaryBody(BaseModel):
    """GET /search 쿼리와 같은 이름·형식 — 화면이 같은 인자를 그대로 보낸다"""

    q: str
    source_types: str | None = None
    date_from: str | None = None
    date_to: str | None = None
    chart_no: str | None = None


@app.post("/search/summary")
def post_search_summary(
    body: SummaryBody,
    paths: Annotated[tuple[Path, Path], Depends(get_db_paths)],
    models: Annotated[tuple, Depends(get_models)],
    gateway: Annotated[object, Depends(get_chat_gateway)],
) -> dict:
    """기록 검색 AI 요약 — 같은 조건으로 다시 검색해 그 결과만 입력·인용 기준으로 쓴다
    (설계 2026-11-09 검색 요약 "요약 API"). sources는 cited_ids 순서 그대로(화면 번호 = 순번)"""
    try:
        args = to_search_args(
            body.q, body.source_types, body.date_from, body.date_to, body.chart_no
        )
    except ValidationError as e:
        err = e.errors()[0]
        raise HTTPException(422, f"{'.'.join(map(str, err['loc']))}: {err['msg']}") from e
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    if not all(Path(p).exists() for p in paths):
        raise HTTPException(503, NO_INDEX)
    conn = db.connect(*paths)  # 기록을 쓰므로 읽기 전용이 아니다
    try:
        if not conn.execute("SELECT 1 FROM ax_chunks LIMIT 1").fetchone():
            raise HTTPException(503, NO_INDEX)
        try:
            searcher = Searcher(conn, _clinic(), *models)
            out = search_summary.summarize(conn, gateway(conn), searcher, args)
        except MissingVectors as e:
            raise HTTPException(503, NO_INDEX) from e
        except EmbeddingFailed as e:
            raise HTTPException(503, f"임베딩 호출 실패: {e}") from e
        cited = out["cited_ids"]
        rows = {
            r["id"]: dict(r)
            for r in conn.execute(
                f"SELECT * FROM ax_chunks WHERE id IN ({','.join('?' * len(cited))})", cited
            )
        }
        sources = [card(conn, rows[i], searcher.replaced_by) for i in cited if i in rows]
    finally:
        conn.close()
    return {"status": out["status"], "text": out["text"], "sources": sources}


def _sse(event: dict) -> str:
    kind = event.pop("type")
    return f"event: {kind}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


def _chat_sources(conn, cited_ids: list[str], replaced_by: dict) -> list[dict]:
    """인용 id → 화면 카드(인용 순서). 색인 청크가 아닌 인용(조회 결과 차트 id)은 카드가 없다"""
    rows = {
        r["id"]: dict(r)
        for r in conn.execute(
            f"SELECT * FROM ax_chunks WHERE id IN ({','.join('?' * len(cited_ids))})", cited_ids
        )
    }
    return [card(conn, rows[i], replaced_by) for i in cited_ids if i in rows]


def _chat_worker(paths, models, gateway, question: str, events: queue.Queue) -> None:
    """작업 스레드 — 자기 연결로 챗을 끝까지 돌린다. 스트림이 끊겨도 기록이 남게"""
    conn = db.connect(*paths)
    try:
        searcher = Searcher(conn, _clinic(), *models)
        out = chat.chat(conn, gateway(conn), searcher, _clinic(), question, on_event=events.put)
        events.put(
            {
                "type": "answer",
                "log_id": out["log_id"],
                "status": out["status"],
                "text": out["answer_text"],
                "sources": _chat_sources(conn, out["cited_ids"], searcher.replaced_by),
            }
        )
    except MissingVectors:
        events.put({"type": "error", "message": NO_INDEX})
    except Exception as e:  # 화면에는 짧게, 실행 기록은 하네스가 남긴다
        events.put({"type": "error", "message": f"답변 중 오류: {type(e).__name__}"})
    finally:
        conn.close()
        events.put(None)


@app.post("/chat")
def post_chat(
    body: ChatBody,
    paths: Annotated[tuple[Path, Path], Depends(get_db_paths)],
    models: Annotated[tuple, Depends(get_models)],
    gateway: Annotated[object, Depends(get_chat_gateway)],
) -> StreamingResponse:
    """질문 1개 → 진행 이벤트(step)·답(answer)·오류(error) SSE (기능 정의 §4 ③)"""
    conn = _open(paths, NO_INDEX)
    try:
        if not conn.execute("SELECT 1 FROM ax_chunks LIMIT 1").fetchone():
            raise HTTPException(503, NO_INDEX)
    finally:
        conn.close()
    events: queue.Queue = queue.Queue()
    threading.Thread(
        target=_chat_worker, args=(paths, models, gateway, body.question, events)
    ).start()

    def stream():
        while (event := events.get()) is not None:
            yield _sse(event)

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/chat/{log_id}")
def get_chat(log_id: int, paths: Annotated[tuple[Path, Path], Depends(get_db_paths)]) -> dict:
    """저장된 질문 1개를 POST /chat의 answer 이벤트와 같은 모양으로 — 모델을 다시 부르지 않는다
    (기능 정의 §4 앱 구성 "물어보기 창", 앱 설계 §9). 평가 실행·검색 요약·아직 도는 질문은 404"""
    conn = _open(paths, NO_INDEX)
    try:
        row = conn.execute(
            "SELECT * FROM ax_search_logs WHERE id = ? AND kind = 'chat' AND is_eval = 0"
            " AND status != 'running'",
            (log_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "저장된 질문이 없습니다")
        sources = _chat_sources(conn, json.loads(row["cited_ids"]), documents.replacements(conn))
        # 진행 표시와 같게 — 진행 이벤트는 도구가 실제로 돈 단계만 낸다(backend/chat.py registry):
        # 통과·도구 오류·실행 뒤 막힘(raw_result 있음)은 돌았고, 실행 전 막힘은 안 돌았다
        steps = [
            r[0]
            for r in conn.execute(
                "SELECT s.tool FROM ax_agent_steps s JOIN ax_agent_runs r ON r.id = s.run_id"
                " WHERE r.agent = ? AND r.target = ? AND s.kind = 'tool' AND s.tool NOT IN (?, ?)"
                " AND (s.hook_status = 'pass' OR s.raw_result IS NOT NULL) ORDER BY s.seq",
                (chat.AGENT, str(log_id), chat.ANSWER, chat.REFUSE),
            )
        ]
    finally:
        conn.close()
    return {
        "log_id": row["id"],
        "question": row["question"],
        "steps": steps,
        "status": row["status"],
        "text": row["answer_text"] or chat.FAILED_TEXT,
        "sources": sources,
    }


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


def _ax_appointments(paths: tuple[Path, Path], date_from: dt.date, date_to: dt.date) -> list[dict]:
    """승인된 AX 예약(emr_bridge) — 앱 DB가 없으면 빈 목록(색인 없이도 예약표·EMR은 나온다)"""
    if not Path(paths[0]).exists():
        return []
    app_conn = _open_rw(paths)
    try:
        return emr_bridge.ax_appointments(app_conn, date_from, date_to)
    finally:
        app_conn.close()


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
        # 승인된 AX 예약은 앱 DB에 있다 — 앱 DB가 없으면 원본만(AI 비서 설계 §5.3)
        extra = _ax_appointments(paths, day, day)
        out = emr.schedule_day(conn, day, clinic.today, clinic.range_start, extra)
    finally:
        conn.close()
    # 잡은 통화의 응급 여부 — ① 분류는 앱 DB에 있다(emr.py는 원본만). 앱 DB가 없으면 분류 전
    booked = [a["call"] for a in out["appointments"] if a["call"] and not a["call"]["pool"]]
    flags = {}
    if booked and Path(paths[0]).exists():
        app_conn = _open_rw(paths)
        try:
            flags = calls.call_emergency(app_conn, [c["uid"] for c in booked])
        finally:
            app_conn.close()
    for a in out["appointments"]:
        if a["call"]:
            a["call"]["emergency"] = flags.get(a["call"]["uid"])
    return out


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
        extra = _ax_appointments(paths, dt.date.min, dt.date.max)
        record = emr.patient_record(conn, chart_no.strip(), _clinic().today, extra)
    finally:
        conn.close()
    if record is None:
        raise HTTPException(404, "환자를 찾을 수 없습니다")
    return record


@app.get("/documents")
def list_documents(paths: Annotated[tuple[Path, Path], Depends(get_db_paths)]) -> dict:
    """문서함 목록 — 공지는 최신 버전만(기능 정의 §4 ③). 색인 없이 source.sqlite만 읽는다"""
    conn = _open_source(paths)
    try:
        return {"documents": documents.list_documents(conn, "main")}
    finally:
        conn.close()


@app.get("/documents/{doc_id}")
def get_document(doc_id: int, paths: Annotated[tuple[Path, Path], Depends(get_db_paths)]) -> dict:
    conn = _open_source(paths)
    try:
        doc = documents.get_document(conn, doc_id, "main")
    finally:
        conn.close()
    if doc is None:
        raise HTTPException(404, "문서가 없습니다")
    return doc


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
        "action": ACTION_KO,
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


@app.get("/calls/patient/{chart_no}")
def calls_of_patient(chart_no: str, paths: Paths, exclude: str | None = None) -> dict:
    conn = _open_rw(paths)
    try:
        return calls.patient_calls(conn, chart_no, exclude=exclude)
    finally:
        conn.close()


@app.get("/calls/{call_uid}")
def call_detail(call_uid: str, paths: Paths, clinic: ClinicDep) -> dict:
    conn = _open_rw(paths)
    try:
        d = calls.call_detail(conn, call_uid, clinic.today)
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
def reclassify(call_uid: str, paths: Paths, clinic: ClinicDep, gw: GatewayFactory) -> dict:
    conn = _open_rw(paths)
    try:
        if calls.call_detail(conn, call_uid) is None:
            raise HTTPException(404, "통화를 찾을 수 없습니다")
        calls.classify_one(conn, gw(conn), call_uid)
        return calls.call_detail(conn, call_uid, clinic.today)
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


class Part(BaseModel):
    part: str


@app.post("/calls/{call_uid}/check")
def check_call(call_uid: str, body: Part, paths: Paths, clinic: ClinicDep, now: Now) -> dict:
    """확인할 통화의 확인함(기능 정의 §4 오늘 화면) — 지금 대상이 아니면 404"""
    conn = _open_rw(paths)
    try:
        if not calls.check_call(conn, call_uid, today=clinic.today, by=body.part, at=_stamp(now)):
            raise HTTPException(404, "확인할 통화가 아닙니다")
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


# ── ⑨ 미예약 환자 (설계 docs/superpowers/specs/2026-11-10-missed-patients-design.md) ─────────


@app.get("/missed")
def missed_sheet(paths: Paths, clinic: ClinicDep, now: Now) -> dict:
    """미예약 환자 목록(backend/missed.py) — 사실은 앱 기준일 아침, 연락 기록 기간은 실제 시계"""
    conn = _open_rw(paths)
    try:
        return missed.sheet(conn, clinic.today, now)
    finally:
        conn.close()


class MissedContact(BaseModel):
    result: str
    channel: str
    part: str


@app.post("/missed/{patient_id}/contacts")
def missed_contact(
    patient_id: int, body: MissedContact, paths: Paths, clinic: ClinicDep, now: Now
) -> dict:
    """연락 결과 한 줄 — 모르는 결과·수단이나 미예약 환자가 아니면 400"""
    conn = _open_rw(paths)
    try:
        missed.record_contact(
            conn,
            patient_id,
            body.result,
            body.channel,
            by=body.part,
            at=_stamp(now),
            day=clinic.today,
        )
        conn.commit()
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    finally:
        conn.close()


# ── ⑤ 청구 점검 (설계 docs/superpowers/specs/2026-11-10-claim-review-design.md §1) ─────────


@app.get("/claims")
def claims_adjusted(paths: Paths, clinic: ClinicDep) -> dict:
    """삭감 대응 — 조정·불능 청구 전부와 칩 집계(backend/claims.py). 기한·남은 날은 앱 기준일, 화면이 행 칩으로 거른다"""
    conn = _open_rw(paths)
    try:
        return claims.adjusted(conn, clinic.today)
    finally:
        conn.close()


@app.get("/claims/pre")
def claims_pre(paths: Paths, clinic: ClinicDep) -> dict:
    """청구 전 점검 — 대기 청구 줄과 상병 확인 경고. 지난 조정은 앱 기준일 전 통보"""
    conn = _open_rw(paths)
    try:
        return claims.pre_claim(conn, clinic.today)
    finally:
        conn.close()


class ClaimJudgment(BaseModel):
    judgment: str
    part: str


@app.post("/claims/{claim_id}/judgment")
def claim_judgment(
    claim_id: int, body: ClaimJudgment, paths: Paths, clinic: ClinicDep, now: Now
) -> dict:
    """사람 판단 기록(덮어씀) — 기한은 앱 기준일, 기록 시각은 실제 시계. 모르는 값·기한 지남·상병 착오에 삭감 수용 외는 400"""
    conn = _open_rw(paths)
    try:
        claims.record_judgment(
            conn, claim_id, body.judgment, by=body.part, at=_stamp(now), today=clinic.today
        )
        conn.commit()
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    finally:
        conn.close()


class AppealReview(BaseModel):
    part: str
    checked: bool


class AppealSubmit(BaseModel):
    part: str


@app.post("/claims/{claim_id}/appeal")
def claim_appeal_draft(claim_id: int, paths: Paths, clinic: ClinicDep, gw: GatewayFactory) -> dict:
    """⑤-4 재심 사유서 초안 쓰기·다시 쓰기 — 일반 호출(사람이 기다림, 설계 §4). 재심 신청이 아니거나 제출 결정한 건은 400"""
    conn = _open_rw(paths)
    try:
        return claim_appeal.draft(conn, gw(conn), claim_id, clinic.today)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    finally:
        conn.close()


@app.post("/claims/{claim_id}/appeal/review")
def claim_appeal_review(
    claim_id: int, body: AppealReview, paths: Paths, clinic: ClinicDep, now: Now
) -> dict:
    """검토 확인 체크·해제 — 통과한 초안만, 제출 결정 뒤에는 400"""
    conn = _open_rw(paths)
    try:
        claim_appeal.review(conn, claim_id, body.part, _stamp(now), body.checked, clinic.today)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    finally:
        conn.close()


@app.post("/claims/{claim_id}/appeal/submit")
def claim_appeal_submit(
    claim_id: int, body: AppealSubmit, paths: Paths, clinic: ClinicDep, now: Now
) -> dict:
    """제출 결정 기록(AI는 제출하지 않는다) — 검토 확인이 없으면 400"""
    conn = _open_rw(paths)
    try:
        claim_appeal.submit(conn, claim_id, body.part, _stamp(now), clinic.today)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    finally:
        conn.close()


# ── ⑧ 재무 대조 (설계 docs/superpowers/specs/2026-11-11-finance-recon-design.md §1·§2) ─────────


@app.get("/finance/recon")
def finance_recon(paths: Paths, clinic: ClinicDep, month: str | None = None) -> dict:
    """일계표 — 그 달(없으면 가장 최근 달) 날짜별 대조와 불일치 건(backend/recon.py). 대조 경계는 앱 기준일, 모르는 달 400"""
    conn = _open_rw(paths)
    try:
        return recon.ledger(conn, clinic.today, month)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    finally:
        conn.close()


class ReconConfirm(BaseModel):
    cause: str
    part: str


@app.post("/finance/recon/{key}/confirm")
def finance_recon_confirm(
    key: str, body: ReconConfirm, paths: Paths, clinic: ClinicDep, now: Now
) -> dict:
    """불일치 건 사람 확정(덮어씀) — 기록 시각은 실제 시계. 지금 탐지에 없는 건·모르는 원인은 400"""
    conn = _open_rw(paths)
    try:
        recon.confirm(conn, key, body.cause, by=body.part, at=_stamp(now), today=clinic.today)
        conn.commit()
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    finally:
        conn.close()


def _recon_worker(paths, gateway, key: str, today: dt.date, events: queue.Queue) -> None:
    """작업 스레드 — 자기 연결로 조사를 끝까지 돌린다. 스트림이 끊겨도 기록이 남게. 끝나면 자동 마감 다시 계산"""
    conn = db.connect(*paths)
    try:
        agent = recon_agent.investigate(
            conn,
            gateway(conn),
            key,
            today,
            on_line=lambda line: events.put({"type": "line", "line": line}),
        )
        recon_auto.refresh(conn, today)
        conn.commit()
        if agent is None:  # 실패로 끝남(게이트웨이 오류·거절) — 결과가 없어 다시 조사할 수 있다
            events.put({"type": "error", "message": "조사 실패 — 다시 시작할 수 있습니다"})
        else:
            events.put({"type": "done", "agent": agent})
    except RunLocked:  # 사전 확인 뒤 다른 탭이 먼저 시작
        events.put({"type": "error", "message": "다른 곳에서 조사 중입니다"})
    except Exception as e:  # 화면에는 짧게, 실행 기록은 하네스가 남긴다
        events.put({"type": "error", "message": f"조사 중 오류: {type(e).__name__}"})
    finally:
        conn.close()
        events.put(None)


@app.post("/finance/recon/{key}/investigate")
def finance_recon_investigate(
    key: str,
    paths: Paths,
    clinic: ClinicDep,
    gateway: Annotated[object, Depends(get_recon_gateway)],
) -> StreamingResponse:
    """불일치 건 조사(설계 §3) — 조사 과정 줄(line)·결과(done)·오류(error) SSE. 지금 탐지에 없는 건 400,
    지금 버전으로 이미 조사했거나 조사 중(잠금이 살아 있음)이면 409(다시 조사하지 않는다). 끊긴 조사·실패는 다시 조사"""
    conn = _open_rw(paths)
    try:
        keys = {i["key"] for i in recon_match.detect(recon_match.load(conn, clinic.today))}
        if key not in keys:
            raise HTTPException(400, f"불일치 건이 아님: {key}")
        if r := recon_agent.result(conn, key):
            raise HTTPException(
                409, "조사 중인 건" if r["status"] == "running" else "이미 조사한 건"
            )
    finally:
        conn.close()
    events: queue.Queue = queue.Queue()
    threading.Thread(
        target=_recon_worker, args=(paths, gateway, key, clinic.today, events)
    ).start()

    def stream():
        while (event := events.get()) is not None:
            yield _sse(event)

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/finance/expenses")
def finance_expenses(paths: Paths, clinic: ClinicDep, month: str | None = None) -> dict:
    """지출 증빙 — 그 달(없으면 가장 최근 달) 출금별 증빙·과목(backend/expense.py, 설계 §2). 모르는 달 400"""
    conn = _open_rw(paths)
    try:
        return expense.sheet(conn, clinic.today, month)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    finally:
        conn.close()


@app.get("/finance/expenses/{table}/{outflow_id}/candidates")
def finance_expense_candidates(table: str, outflow_id: int, paths: Paths) -> dict:
    """증빙 바꾸기 후보 — 출금일 근처 증빙(금액 무관), 날짜 가까운 순. 모르는 표·없는 출금 400"""
    conn = _open_rw(paths)
    try:
        return {"docs": expense.candidates(conn, table, outflow_id)}
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    finally:
        conn.close()


class ExpenseConfirm(BaseModel):
    document_ids: list[int]
    account: str
    part: str


@app.post("/finance/expenses/{table}/{outflow_id}/confirm")
def finance_expense_confirm(
    table: str, outflow_id: int, body: ExpenseConfirm, paths: Paths, clinic: ClinicDep, now: Now
) -> dict:
    """출금 사람 확정(덮어씀) — 기록 시각은 실제 시계. 없는 출금·모르는 과목·후보 밖 증빙·기준일 이후 출금·
    다른 출금에 이미 확정된 증빙은 400(설계 §4)"""
    conn = _open_rw(paths)
    try:
        expense.confirm(
            conn,
            table,
            outflow_id,
            body.document_ids,
            body.account,
            by=body.part,
            at=_stamp(now),
            today=clinic.today,
        )
        conn.commit()
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    finally:
        conn.close()


@app.get("/today")
def today_sheet(paths: Paths, clinic: ClinicDep, now: Now) -> dict:
    """오늘 화면 한 장(backend/today.py) — 앱 기준일 기준, 근거 없는 질문만 실제 시계"""
    conn = _open_rw(paths)
    try:
        return today.sheet(conn, clinic.today, now)
    finally:
        conn.close()


# 되돌릴 수 있는 자동 처리 — ① 자동 종결·⑧-1 일계표 자동 마감·⑧-2 지출 증빙 자동 확정
AUTO_KINDS = autoclose.KINDS | recon_auto.KINDS | expense_auto.KINDS


@app.post("/auto-actions/{action_id}/revert")
def revert_auto(action_id: int, body: Part, paths: Paths, now: Now) -> dict:
    """자동 처리 되돌리기(앱 설계 §3b) — 앱 상태만 복원하고 해당 큐로 돌려보낸다"""
    conn = _open_rw(paths)
    try:
        row = conn.execute(
            "SELECT kind FROM ax_auto_actions WHERE id = ?", (action_id,)
        ).fetchone()
        if row is None or row["kind"] not in AUTO_KINDS:
            raise HTTPException(404, "자동 처리를 찾을 수 없습니다")
        if not revert_auto_action(conn, AUTO_KINDS, action_id, body.part, clock=lambda: now):
            raise HTTPException(409, "이미 되돌린 자동 처리입니다")
        conn.commit()
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


@app.get("/improve/runs")
def improve_runs(paths: Paths, report: Annotated[Path, Depends(get_improve_report)]) -> dict:
    """자동 개선 실행 이력(최신 먼저)·대기 버전·활성 버전 — 읽기 전용(기능 정의 §4 ② "자동 개선")"""
    rows = [json.loads(x) for x in report.read_text().splitlines()] if report.exists() else []
    conn = _open(paths, NO_DATA)
    try:
        return {
            "runs": sorted(rows, key=lambda r: r["at"], reverse=True),
            "pending": db.get_setting(conn, "pending_prompt_version"),
            "active": calls.active_versions(conn)[0],
        }
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


# ── ⑥ 경영 리포트 (기능 정의 §5 ⑥) ──────────────────────────────────


@app.get("/report")
def report_sheet(
    paths: Paths, clinic: ClinicDep, scope: str = "monthly", period: str | None = None
) -> dict:
    """기간 숫자·변화 분해·저장된 해석(backend/report.py sheet, 설계 2026-11-13 리포트 §5). 모르는 기간 400"""
    conn = _open_rw(paths)
    try:
        return report.sheet(conn, clinic.today, scope, period)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    finally:
        conn.close()


# ── ④ 진료 준비표 (기능 정의 §4 ④) ──────────────────────────────────

JOB_STALE = dt.timedelta(minutes=30)  # 이보다 오래 running인 작업은 죽은 것으로 보고 막지 않는다


def _tab_day(date: str | None, clinic: Clinic) -> dt.date:
    try:
        day = dt.date.fromisoformat(date) if date else clinic.today
    except ValueError as e:
        raise HTTPException(422, f"date: {e}") from e
    if day not in prep.tab_days(clinic.today):
        raise HTTPException(400, "준비표는 기준일 포함 최근 8일만")
    return day


@app.get("/prep")
def prep_sheet(paths: Paths, clinic: ClinicDep, date: str | None = None) -> dict:
    day = _tab_day(date, clinic)
    conn = _open_rw(paths)
    try:
        return prep.sheet(conn, day, clinic.today, include_ax=True)
    finally:
        conn.close()


def _run_prep_job(paths, gw_factory, job_id: int, day: dt.date, today: dt.date) -> None:
    """작업 스레드 — 연결을 따로 열고 일반 호출로 만든다(사람이 기다린다). 어디서 실패해도 작업 행을 끝낸다"""
    status, error, done = "failed", None, 0
    conn = None
    try:
        conn = db.connect(*paths)
        out = prep.build(conn, gw_factory(conn), [day], today, use_batch=False, include_ax=True)
        status, done = "done", out["rows"]
    except Exception as e:  # noqa: BLE001 — 실패를 작업 행에 남겨 화면이 보인다
        error = f"{type(e).__name__}: {e}"
    finally:
        if conn is not None:
            conn.close()
        _end_job(paths, job_id, status, error, done)


def _end_job(paths, job_id: int, status: str, error: str | None, done: int) -> None:
    end = db.connect(*paths)  # 작업 연결이 깨졌어도 끝 표시는 새 연결로
    try:
        with end:
            end.execute(
                "UPDATE ax_jobs SET status = ?, error = ?, done = ?, finished_at = ? WHERE id = ?",
                (status, error, done, dt.datetime.now().isoformat(timespec="seconds"), job_id),
            )
    finally:
        end.close()


@app.post("/jobs/prep-regenerate")
def prep_regenerate(paths: Paths, clinic: ClinicDep, gw: GatewayFactory, date: str) -> dict:
    day = _tab_day(date, clinic)
    conn = _open_rw(paths)
    try:
        conn.execute(
            "BEGIN IMMEDIATE"
        )  # 같은 날짜를 두 번 누른 요청이 둘 다 새 작업을 만들지 않게
        since = (dt.datetime.now() - JOB_STALE).isoformat(timespec="seconds")
        running = conn.execute(
            "SELECT id FROM ax_jobs WHERE kind = 'prep' AND arg = ? AND status = 'running'"
            " AND replace(started_at, ' ', 'T') >= ? ORDER BY id DESC",
            (day.isoformat(), since),
        ).fetchone()
        if running:
            conn.commit()
            return {"job_id": running[0]}
        total = len(prep.prep_facts(conn, day, clinic.today, include_ax=True))
        job_id = conn.execute(
            "INSERT INTO ax_jobs (kind, arg, status, total, started_at)"
            " VALUES ('prep', ?, 'running', ?, ?)",
            (day.isoformat(), total, dt.datetime.now().isoformat(timespec="seconds")),
        ).lastrowid
        conn.commit()
    finally:
        conn.close()
    threading.Thread(
        target=_run_prep_job, args=(paths, gw, job_id, day, clinic.today), daemon=True
    ).start()
    return {"job_id": job_id}


# ── ⑤-2 청구 AI 점검 (기능 정의 §5 ⑤) ───────────────────────────────


def _run_claim_check_job(paths, gw_factory, job_id: int, today: dt.date) -> None:
    """작업 스레드 — 앱 범위 줄을 2a·2b·⑤-3 차례로 일반 호출로 점검(사람이 기다린다). 어디서 실패해도 작업 행을 끝낸다"""
    status, error, done = "failed", None, 0
    conn = None
    try:
        conn = db.connect(*paths)
        gw = gw_factory(conn)
        rows = claim_check.targets(conn, today, scope="app")
        claim_check.build(conn, gw, rows, use_batch=False)
        more = claim_omission.targets(conn, today, scope="app")  # 2b(설계 §2b) — 같은 버튼으로
        claim_omission.build(conn, gw, more, use_batch=False)
        docs = claim_evidence.targets(conn, today, scope="app")  # ⑤-3 서류 사유 차트 문장(설계 §3)
        claim_evidence.build(conn, gw, docs, use_batch=False)
        status, done = "done", len({r["claim_id"] for r in rows + more + docs})
    except Exception as e:  # noqa: BLE001 — 실패를 작업 행에 남겨 화면이 보인다
        error = f"{type(e).__name__}: {e}"
    finally:
        if conn is not None:
            conn.close()
        _end_job(paths, job_id, status, error, done)


@app.post("/jobs/claim-check")
def claim_check_run(paths: Paths, clinic: ClinicDep, gw: GatewayFactory) -> dict:
    """청구 전 점검의 "AI 점검 갱신하기" — 이미 도는 작업이 있으면 그 id(prep-regenerate와 같은 꼴)"""
    conn = _open_rw(paths)
    try:
        conn.execute("BEGIN IMMEDIATE")
        since = (dt.datetime.now() - JOB_STALE).isoformat(timespec="seconds")
        running = conn.execute(
            "SELECT id FROM ax_jobs WHERE kind = 'claim_check' AND status = 'running'"
            " AND replace(started_at, ' ', 'T') >= ? ORDER BY id DESC",
            (since,),
        ).fetchone()
        if running:
            conn.commit()
            return {"job_id": running[0]}
        total = len(
            {r["claim_id"] for r in claim_check.targets(conn, clinic.today, scope="app")}
            | {r["claim_id"] for r in claim_omission.targets(conn, clinic.today, scope="app")}
            | {r["claim_id"] for r in claim_evidence.targets(conn, clinic.today, scope="app")}
        )
        job_id = conn.execute(
            "INSERT INTO ax_jobs (kind, arg, status, total, started_at)"
            " VALUES ('claim_check', '', 'running', ?, ?)",
            (total, dt.datetime.now().isoformat(timespec="seconds")),
        ).lastrowid
        conn.commit()
    finally:
        conn.close()
    threading.Thread(
        target=_run_claim_check_job, args=(paths, gw, job_id, clinic.today), daemon=True
    ).start()
    return {"job_id": job_id}


@app.get("/jobs/{job_id}")
def job(job_id: int, paths: Paths) -> dict:
    conn = _open_rw(paths)
    try:
        row = conn.execute("SELECT * FROM ax_jobs WHERE id = ?", (job_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(404, "작업 없음")
    return dict(row)
