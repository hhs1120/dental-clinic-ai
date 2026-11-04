# 앱 뼈대·기록 검색 화면 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `make dev` 하나로 FastAPI + Next.js 앱을 띄우고, 평가에 쓴 `Searcher` 그대로 실데이터 기록 검색을 해 볼 수 있게 한다. 뒤 화면이 붙을 공통 레이아웃과 스타일 토큰도 이번에 깐다.

**Architecture:** `backend/api.py`의 FastAPI 앱 하나가 `GET /search`·`GET /clinic`을 준다. 핸들러는 동기 `def`이고, 요청마다 DB 연결과 `Searcher`를 만든다. 임베더·리랭커만 프로세스에 하나 두고 의존성으로 넘긴다. 검색 결과 카드의 칸은 `backend/search_cards.py`가 청크 prefix를 떼어 만든다. 프론트는 `frontend/`의 Next.js App Router이고, `/api/*`를 rewrites로 8000번에 넘긴다. 검색 상태는 URL 쿼리로 둔다.

**Tech Stack:** FastAPI, uvicorn, httpx(테스트) / Next.js 16 (App Router), TypeScript strict, Tailwind CSS v4, shadcn/ui, ESLint(`eslint-config-next` core-web-vitals), Prettier, npm.

**Spec:** `docs/superpowers/specs/2026-11-03-app-skeleton-design.md`. 실행하는 사람은 이 계획과 설계안을 함께 읽는다. 표시 규칙의 원본은 `docs/치과 AX - 앱 설계.md` §3·§9, 화면 레이아웃·동작의 기준은 `docs/wireframe/index-v2.html`(시작 화면 `R.home` 1470행, 머리 띠 388행, 물어보기 `R.ask` 984행)이다.

## Global Constraints

- `backend/`의 모든 텍스트 파일(주석 포함)에 `ground_truth`·`generator`·`evaluation`·`truth.sqlite`·`truth_`를 쓰지 않는다. 타입 힌트에 `typing.Generator`를 쓰지 않고 `Iterator`를 쓴다. 검사는 `tests/test_no_ground_truth_import.py`가 한다
- `frontend/`에 `ground_truth`·`truth.sqlite`·`truth_`·`call_labels`·`doc_meta`·`rag_qa`를 쓰지 않는다
- `SearchArgs`와 `search_docs` 도구 시그니처는 바꾸지 않는다. `backend/rag_index.py`의 prefix 형식도 바꾸지 않는다(바꾸면 청크 텍스트가 달라져 다시 색인해야 한다)
- 테스트는 API 키 없이 돈다. `FakeEmbedder`를 쓰고 리랭커는 끈다
- 의존성 그룹: `fastapi`·`uvicorn`은 `backend`, `httpx`는 `dev`. `uv.lock`도 커밋한다
- 색·모서리·글꼴·그림자는 `frontend/app/globals.css`의 토큰으로만 쓴다. 화면 코드에 hex, `rounded-[…]` 같은 임의 값, Tailwind 기본 색 이름(`teal-700` 등)을 쓰지 않는다
- 버튼·입력칸·체크박스·탭·카드·배지·선택 상자는 `frontend/components/ui/`(shadcn CLI가 만든 것)만 쓴다. 모양이 더 필요하면 그 파일에 variant를 더한다
- 와이어프레임의 직원 이름, 색·CSS, 클라이언트 측 단어 일치 검색, HTML 문자열 강조는 옮기지 않는다. 지금 사실이 아닌 머리말 문구("무엇을 물었는지 기록됩니다", "색인 갱신 04:00", "08:50")도 옮기지 않는다
- 파이썬 주석·독스트링은 한국어로 쓰고 근거 문서 절을 가리킨다. 모듈 첫 독스트링에는 하는 일과 원본 문서를 쓴다
- 커밋 전에 `make fmt` → `make lint` → `make test`. `git add`는 파일 이름으로 한다. 커밋 메시지는 `<영역>: <무엇>` + 본문 글머리이고 끝에 아래 두 줄을 붙인다
  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01G62cGwhYixc5LpBLhj8iGp
  ```

## Review Focus

설계안이 직접 다루지 않았지만 쓰다 보면 부딪히기 쉬운 입력이다. 각 줄의 확인은 담당 Task에 들어 있다.

1. 외부 API가 느리거나 재시도할 때 프록시가 먼저 끊어 "서버에 연결하지 못했습니다"가 뜨면 안 된다. 기다렸다가 결과나 503 문구를 보여야 한다 → Task 3의 `proxyTimeout`, Task 5 수동 확인
2. FTS 문법 문자가 든 검색어(`"크라운*(`, `OR`, `-`)가 500이 나면 안 된다 → Task 2 `test_search_query_with_fts_syntax`
3. 정규식 특수문자가 든 검색어(`(`, `+`, `?`)로 강조를 만들 때 화면이 깨지면 안 된다 → Task 5 `highlight` 이스케이프와 수동 확인
4. 시작일이 종료일보다 늦어도 오류가 아니라 빈 결과로 답해야 한다 → Task 2 `test_search_reversed_dates_is_empty_not_error`
5. 브라우저 뒤로/앞으로 이동 때 URL에 맞춰 검색어·필터·결과가 바뀌어야 한다 → Task 5 수동 확인

## 파일 구조

| 파일 | 하는 일 |
|---|---|
| `backend/search_cards.py` | 청크 → 화면 카드 칸(prefix 떼기, 차트 서술 원문, 문서 버전·옛 공지) |
| `backend/api.py` | FastAPI 앱, 의존성(DB 경로·모델), `/search`·`/clinic`, 인자 변환, 503 처리 |
| `backend/rag_search.py` | `SEARCH_TOP_K = 10` 상수만 더한다 |
| `tests/backend/conftest.py` | 실제 `shared/source_schema.sql`로 만든 source 픽스처 `source_paths` |
| `tests/backend/test_search_cards.py` | 카드 칸 테스트 |
| `tests/backend/test_api.py` | API 테스트, 실제 uvicorn 동시 요청 테스트 |
| `frontend/` | Next.js 앱(아래 Task 3~5) |
| `Makefile`, `.github/workflows/ci.yml`, `.gitignore`, `pyproject.toml` | 실행·검사·CI |

프론트 안쪽:

| 파일 | 하는 일 |
|---|---|
| `frontend/app/globals.css` | Tailwind 진입점 + 토큰(라이트·다크) + `@theme inline` 연결 |
| `frontend/app/layout.tsx` | `<html lang="ko">`, 글꼴, globals.css |
| `frontend/app/page.tsx` | 시작 화면 |
| `frontend/app/(portal)/layout.tsx` | 공통 머리 띠 + 본문 |
| `frontend/app/(portal)/[screen]/page.tsx` | 아직 없는 화면들의 "준비 중" |
| `frontend/app/(portal)/ask/page.tsx` | 물어보기(Suspense로 감싼 `AskView`) |
| `frontend/lib/api.ts` | API 타입, `getJson`, `ApiError` |
| `frontend/lib/nav.ts` | 메뉴·별도 공간 목록 |
| `frontend/lib/search-query.ts` | 검색 상태 ↔ URL 쿼리 |
| `frontend/lib/highlight.tsx` | 검색어 강조(React 노드) |
| `frontend/components/app-header.tsx` | 로고·메뉴(또는 공간 이름+돌아가기)·담당 파트·기준일 |
| `frontend/components/coming-soon.tsx` | "준비 중" 판 |
| `frontend/components/ask/ask-view.tsx` | 탭, 입력 상태 보관 |
| `frontend/components/ask/search-form.tsx` | 검색어·출처·기간·환자번호 |
| `frontend/components/ask/search-results.tsx` | 2단 결과, 건수, 상태 문구, 매뉴얼 칸 빈 이유 |
| `frontend/components/ask/result-card.tsx` | 카드 한 장 |
| `frontend/components/ui/*` | shadcn: button, input, checkbox, tabs, card, badge, select, label |

---

### Task 1: 검색 카드 칸 (`backend/search_cards.py`)

**Files:**
- Create: `backend/search_cards.py`
- Modify: `backend/rag_search.py` (상수 `SEARCH_TOP_K = 10`을 `CANDIDATES` 옆에. 주석: 화면 결과 수 — 도구 기본값과 별개, 화면은 훑어보는 용도)
- Modify: `tests/backend/conftest.py` (픽스처 `source_paths` 추가, 기존 `paths`는 그대로 둔다)
- Test: `tests/backend/test_search_cards.py`

**Interfaces:**
- Produces:
  - `card(conn: sqlite3.Connection, chunk: dict, replaced_by: dict[int, int]) -> dict` — `chunk`는 `ax_chunks` 한 줄(dict). `conn`에는 `source`가 붙어 있어야 한다. 돌려주는 키는 언제나 12개다: `id, source_type, title, body, event_date, chart_no, patient_name, caller_name, direction, doc_type, version, is_outdated`. 그 출처에 해당하지 않는 칸은 `None`이다(`is_outdated`도 문서가 아니면 `None`)
  - 픽스처 `source_paths(tmp_path) -> tuple[Path, Path]` — `(app.sqlite 경로(아직 없음), source.sqlite 경로)`
  - `backend.rag_search.SEARCH_TOP_K: int = 10`

출처마다 만드는 방법은 하나다(앱 설계 §3, `backend/rag_index.py`의 prefix):

| 출처 | `text` 모양 | 화면 칸 |
|---|---|---|
| document | `제목` 또는 `제목 > 절` + `\n` + 조각 | `title` = 첫 줄, `body` = 나머지. `version`은 `source.ax_documents.version`(id는 `doc:<id>:<n>`의 가운데), `is_outdated` = `doc_id in replaced_by` |
| call | `[환자: 이름(차트번호) \| YYYY-MM-DD \| 수신]` 또는 `[상대: 이름 \| … \| 발신]` + `\n` + 조각 | `patient_name`·`caller_name`·`direction`을 prefix에서 읽는다. source 조인을 하지 않는다 |
| message_thread | `[환자: 이름(차트번호) \| YYYY-MM] ` + 문자들 | `patient_name`은 prefix, `body`는 `] ` 뒤 |
| chart_note | `[차트 … \| … \| 시술] 서술 (검색용 말)` | `body` = `source.emr_chart_notes.note`(`chart:<treatment_id>`). `text`를 잘라 만들지 않는다 |

`chart_no`·`event_date`·`doc_type`은 청크 칸 그대로 쓴다. prefix 모양이 맞지 않으면 `ValueError`를 낸다(색인 형식이 바뀐 것이므로 조용히 넘기지 않는다).

- [ ] **Step 1: `source_paths` 픽스처를 만든다**

`tmp_path/"source.sqlite"`에 `shared/source_schema.sql`을 `executescript`하고 아래 행을 넣는다. NOT NULL 칸은 아무 값이나 채운다. 날짜는 모두 `load_clinic().today`(2026-12-01) 이전이어야 한다.
- `emr_clinic`: `('클로드치과', '{}')`
- `emr_patients`: id 1 `C-00001` 김철수, id 2 `C-00002` 박영희, id 3 `C-00003`
- `emr_procedures`: `scaling` 스케일링, aliases `["치석제거"]`
- `emr_treatments`: id 7, 환자 3, `scaling`, 치아 `16`, `performed_at` `2026-07-03 10:00:00`
- `emr_chart_notes`: treatment 7, note `통증 호소(좌측) 치석 제거 6개월 후 재내원` — 서술 안에 괄호가 있고, 치아 번호와 `6개월` 때문에 검색용 꼬리 괄호가 붙는다
- `ax_documents`: 1 notice `미백 비용` v1 `2026-01-01` 본문 `# 변경 내용\n미백 30만원`, 2 notice `미백 비용` v2 `2026-06-01` 본문 `# 변경 내용\n미백 35만원`, 3 manual `틀니 수리` v1 시행일 없음, 헤더 없는 본문 `접수 후 기공소로 보낸다`
- `ax_calls`: `c-0001` 환자 1 inbound `2026-07-03 09:00:00` 녹취 `틀니 수리 문의`, `c-0002` 환자 없음 caller_name `김미영` outbound `2026-07-04 09:00:00` 녹취 `거래처 납품 확인`. 둘 다 `has_transcript = 1`
- `emr_messages`: 환자 2, out `2026-07-03 09:00:00` `예약 확인 부탁드립니다`, in `2026-07-03 10:00:00` `네`

- [ ] **Step 2: 실패하는 테스트를 쓴다**

```python
"""검색 카드 칸 (앱 설계 §3 prefix, 설계안 '백엔드')."""

import pytest

from backend import db
from backend.embeddings import FakeEmbedder
from backend.rag_index import build_index
from backend.rag_search import Searcher
from backend.search_cards import card
from shared.clinic import load_clinic


@pytest.fixture
def indexed(source_paths):
    conn = db.connect(*source_paths)
    db.init_schema(conn)
    build_index(conn, FakeEmbedder())
    yield conn
    conn.close()


@pytest.fixture
def cards(indexed):
    replaced = Searcher(indexed, load_clinic(), FakeEmbedder()).replaced_by
    rows = [dict(r) for r in indexed.execute("SELECT * FROM ax_chunks")]
    return {r["id"]: card(indexed, r, replaced) for r in rows}


def test_every_card_has_same_keys_and_no_prefix(cards):
    keys = {
        "id",
        "source_type",
        "title",
        "body",
        "event_date",
        "chart_no",
        "patient_name",
        "caller_name",
        "direction",
        "doc_type",
        "version",
        "is_outdated",
    }
    for c in cards.values():
        assert set(c) == keys
        assert not c["body"].startswith("[")


def test_document_title_and_version(cards):
    # 첫 헤더 앞 빈 절은 조각이 없어 번호를 쓰지 않는다(rag_index._documents)
    old, new, manual = cards["doc:1:0"], cards["doc:2:0"], cards["doc:3:0"]
    assert old["title"] == "미백 비용 > 변경 내용" and old["body"] == "미백 30만원"
    assert (old["version"], old["is_outdated"]) == ("v1", True)
    assert (new["version"], new["is_outdated"]) == ("v2", False)
    assert manual["title"] == "틀니 수리" and manual["body"] == "접수 후 기공소로 보낸다"
    assert manual["doc_type"] == "manual" and manual["event_date"] is None


def test_call_with_patient(cards):
    c = cards["call:c-0001:0"]
    assert (c["patient_name"], c["chart_no"], c["direction"]) == ("김철수", "C-00001", "수신")
    assert c["caller_name"] is None and c["body"] == "틀니 수리 문의"
    assert c["title"] is None and c["version"] is None and c["is_outdated"] is None


def test_call_without_patient(cards):
    c = cards["call:c-0002:0"]
    assert (c["caller_name"], c["patient_name"], c["direction"]) == ("김미영", None, "발신")


def test_thread(cards):
    c = cards["thread:C-00002:2026-07"]
    assert c["patient_name"] == "박영희"
    assert c["body"] == "07-03 예약 확인 부탁드립니다 07-03 답장: 네"


def test_chart_body_is_note_without_search_tail(cards, indexed):
    text = indexed.execute("SELECT text FROM ax_chunks WHERE id = 'chart:7'").fetchone()[0]
    assert text.endswith(")")  # 검색용 꼬리가 실제로 붙어 있어야 이 테스트가 뜻이 있다
    c = cards["chart:7"]
    assert c["body"] == "통증 호소(좌측) 치석 제거 6개월 후 재내원"
    assert c["patient_name"] is None and c["chart_no"] == "C-00003"


def test_unknown_prefix_raises(source_paths):
    conn = db.connect(*source_paths)
    with pytest.raises(ValueError):
        card(
            conn,
            {
                "id": "call:x:0",
                "source_type": "call",
                "text": "형식 없음",
                "chart_no": None,
                "event_date": None,
                "doc_type": None,
            },
            {},
        )
```

- [ ] **Step 3: 실패를 확인한다**

Run: `uv run pytest tests/backend/test_search_cards.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'backend.search_cards'`

- [ ] **Step 4: `card`를 구현하고 `SEARCH_TOP_K`를 더한다**

prefix는 출처별 정규식 하나로 읽는다. 차트 서술은 `SELECT note FROM source.emr_chart_notes WHERE treatment_id = ?`, 문서 버전은 `SELECT version FROM source.ax_documents WHERE id = ?`로 읽는다.

- [ ] **Step 5: 통과를 확인한다**

Run: `uv run pytest tests/backend -v`
Expected: 새 테스트를 포함해 모두 PASS(기존 `paths` 픽스처를 쓰는 테스트도 그대로 통과)

- [ ] **Step 6: 커밋한다**

```bash
make fmt && make lint && make test
git add backend/search_cards.py backend/rag_search.py tests/backend/conftest.py tests/backend/test_search_cards.py
git commit  # backend: 검색 결과 카드 칸 — prefix 떼기·차트 서술 원문·옛 공지
```

---

### Task 2: FastAPI 앱 (`backend/api.py`)

**Files:**
- Create: `backend/api.py`
- Modify: `pyproject.toml` (`backend` 그룹에 `fastapi`, `uvicorn`, `dev` 그룹에 `httpx`), `uv.lock` (`uv sync`로 갱신)
- Test: `tests/backend/test_api.py`

**Interfaces:**
- Consumes: Task 1의 `card`, `SEARCH_TOP_K`, `source_paths`
- Produces (Task 3의 `make dev`, Task 4·5의 프론트가 쓴다):
  - `backend.api.app: FastAPI`
  - `get_db_paths() -> tuple[Path, Path]` — 기본 `ROOT/"data"/"app.sqlite"`, `ROOT/"data"/"source.sqlite"`. 테스트가 `app.dependency_overrides`로 바꾼다
  - `get_models() -> tuple[embedder, reranker | None]` — 안에서 `@lru_cache`된 `_models()`가 `GuardedEmbedder(make_embedder(DEFAULT_EMBEDDING))`, `voyage_rerank(DEFAULT_RERANKER)`를 한 번만 만든다. 만들다 실패하면 503. 테스트는 `(GuardedEmbedder(FakeEmbedder()), None)`으로 바꾼다
  - `GuardedEmbedder(inner)` — `model` 속성을 그대로 두고, `embed`의 예외를 `EmbeddingFailed(RuntimeError)`로 바꾼다
  - `to_search_args(q, source_types, date_from, date_to, chart_no) -> SearchArgs` — 잘못된 값이면 `ValueError`를 낸다(pydantic `ValidationError`도 `ValueError`다)
  - `GET /search?q=&source_types=&date_from=&date_to=&chart_no=` → `{"manual_results": [card…], "case_results": [card…]}`
  - `GET /clinic` → `{"name": str, "today": "YYYY-MM-DD"}`
  - 오류는 FastAPI 기본 모양 `{"detail": str}`, 상태 422 또는 503

규칙(설계안 '백엔드'):
- 핸들러는 동기 `def`로 쓴다. `db.connect`와 `Searcher`는 핸들러 안에서 만들고 `try/finally`로 닫는다. 연결 직후 `PRAGMA query_only = ON`을 실행한다
- 연결 전에 두 DB 파일이 있는지 확인한다. 없으면 503이고, `connect`가 빈 파일을 만들게 두지 않는다
- 청크가 0개이면 503이다. `MissingVectors`와 `EmbeddingFailed`도 503이다. 리랭커 실패는 `Searcher`가 이미 처리한다
- 503 문구: 파일 없음·청크 0개·벡터 없음은 `검색 색인이 없습니다 — make index EMBEDDING={DEFAULT_EMBEDDING}을 먼저 실행하세요`, 임베딩 실패는 `임베딩 호출 실패: {원인}`, 모델 준비 실패는 `임베딩 준비 실패: {원인}`. `/clinic`에서 파일이 없으면 `데이터가 없습니다 — make data를 먼저 실행하세요`
- 인자 변환은 설계안의 변환 표 그대로다. `source_types`는 쉼표로 나누고 앞뒤 공백과 빈 조각을 버린다. 그 결과가 비면 `ValueError`이다(`SearchArgs`는 빈 목록을 '전체'로 받아 버리므로 직접 막는다). `top_k=SEARCH_TOP_K`로 하고, `Searcher.search(**args.model_dump(mode="json"))`로 부른다
- 응답의 각 결과는 `card(conn, chunk, searcher.replaced_by)`로 만든다. 순서는 `Searcher.search` 그대로이고 묶지 않는다

- [ ] **Step 1: 의존성을 더한다**

`pyproject.toml`에 더하고 `uv sync`를 실행한다. Expected: `uv.lock`이 바뀌고 `uv run python -c "import fastapi, uvicorn, httpx"`가 성공한다.

- [ ] **Step 2: 실패하는 테스트를 쓴다**

픽스처(모두 `tests/backend/test_api.py` 안):
- `indexed_paths(source_paths)` — `source_paths`에 `init_schema` + `build_index(conn, FakeEmbedder())`를 하고 연결을 닫은 뒤 경로를 돌려준다
- `client_with(indexed_paths)` — 공장 함수 `make(paths=indexed_paths, models=(GuardedEmbedder(FakeEmbedder()), None)) -> TestClient`. `get_db_paths`·`get_models`를 바꿔 끼운다. 정리할 때 `app.dependency_overrides.clear()`
- `client(client_with)` — `client_with()`

```python
def ids(body):
    return {k: [c["id"] for c in v] for k, v in body.items()}


def test_search_matches_searcher(client, indexed_paths):
    r = client.get("/search", params={"q": "틀니 수리"})
    assert r.status_code == 200
    conn = db.connect(*indexed_paths)
    want = Searcher(conn, load_clinic(), FakeEmbedder()).search("틀니 수리", top_k=SEARCH_TOP_K)
    assert ids(r.json()) == {k: [c["id"] for c in v] for k, v in want.items()}


def test_buckets_split(client):
    body = client.get("/search", params={"q": "틀니"}).json()
    assert {c["source_type"] for c in body["manual_results"]} <= {"document"}
    assert {c["source_type"] for c in body["case_results"]} <= {
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
    body = client.get(
        "/search", params={"q": "미백", "date_from": "2026-05-01", "date_to": ""}
    ).json()
    assert [c["id"] for c in body["manual_results"]] == [
        "doc:2:0"
    ]  # v1 공지·시행일 없는 매뉴얼은 빠진다


def test_card_fields(client):
    body = client.get("/search", params={"q": "틀니 거래처 미백"}).json()
    by_id = {c["id"]: c for c in body["manual_results"] + body["case_results"]}
    assert by_id["call:c-0001:0"]["patient_name"] == "김철수"
    assert by_id["call:c-0001:0"]["direction"] == "수신"
    assert by_id["call:c-0002:0"]["caller_name"] == "김미영"
    assert by_id["doc:1:0"]["is_outdated"] is True


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
    r = client.get(
        "/search", params={"q": "틀니", "date_from": "2026-12-01", "date_to": "2026-01-01"}
    )
    assert r.status_code == 200
    assert r.json() == {"manual_results": [], "case_results": []}


def test_missing_db_files_503_without_creating(client_with, tmp_path):
    app_db, source_db = tmp_path / "none" / "app.sqlite", tmp_path / "none" / "source.sqlite"
    r = client_with(paths=(app_db, source_db)).get("/search", params={"q": "틀니"})
    assert r.status_code == 503 and "make index" in r.json()["detail"]
    assert not app_db.exists()


def test_empty_index_503(client_with, source_paths):
    db.init_schema(db.connect(*source_paths))  # 청크 없는 app.sqlite
    r = client_with(paths=source_paths).get("/search", params={"q": "틀니"})
    assert r.status_code == 503 and "make index" in r.json()["detail"]


def test_missing_vectors_503(client_with):
    r = client_with(models=(GuardedEmbedder(FakeEmbedder(model="other")), None)).get(
        "/search", params={"q": "틀니"}
    )
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
```

- [ ] **Step 3: 실제 uvicorn 동시 요청 테스트를 쓴다**

```python
def test_concurrent_requests_on_real_server(indexed_paths):
    # 연결을 핸들러 밖(의존성)에서 열면 스레드가 달라 sqlite3가 막는다 — TestClient로는 재현되지 않는다(설계안 '연결')
    ...
    with ThreadPoolExecutor(16) as pool:
        codes = list(
            pool.map(
                lambda _: httpx.get(f"{base}/search", params={"q": "틀니"}).status_code, range(48)
            )
        )
    assert codes == [200] * 48
```

빈 포트는 `socket.bind(("127.0.0.1", 0))`으로 얻는다. `uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))`를 스레드에서 `run()`하고, `server.started`가 참이 될 때까지 기다린다(상한 10초). 끝나면 `server.should_exit = True` 후 `join`하고 `dependency_overrides`를 비운다. 이 테스트가 CI에서 불안정하면 완료 기준 4번의 수동 확인으로 돌리고 그 사실을 보고한다.

- [ ] **Step 4: 실패를 확인한다**

Run: `uv run pytest tests/backend/test_api.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'backend.api'`

- [ ] **Step 5: `backend/api.py`를 구현한다**

`load_clinic()`도 `@lru_cache`로 한 번만 읽는다. 모듈 독스트링: 앱 API(앱 설계 §9, 설계안), 핸들러가 동기 `def`이고 연결을 안에서 여는 이유 한 줄.

- [ ] **Step 6: 통과를 확인한다**

Run: `uv run pytest tests/backend/test_api.py tests/test_no_ground_truth_import.py -v`
Expected: 모두 PASS

- [ ] **Step 7: 커밋한다**

```bash
make fmt && make lint && make test
git add pyproject.toml uv.lock backend/api.py tests/backend/test_api.py
git commit  # backend: FastAPI 앱 — GET /search·/clinic, 인자 변환·503
```

---

### Task 3: 프론트 세팅과 도구 연결

**Files:**
- Create: `frontend/` (create-next-app), `frontend/.prettierrc`, `frontend/.prettierignore`, `.nvmrc`
- Create (shadcn CLI): `frontend/components.json`, `frontend/lib/utils.ts`, `frontend/components/ui/{button,input,checkbox,tabs,card,badge,select,label}.tsx`
- Modify: `frontend/app/globals.css`, `frontend/app/layout.tsx`, `frontend/next.config.ts`, `frontend/package.json`, `Makefile`, `.github/workflows/ci.yml`, `.gitignore`

**Interfaces:**
- Consumes: Task 2의 `backend.api:app`
- Produces:
  - Tailwind 클래스로 쓰는 토큰: `bg-background`, `text-foreground`, `bg-card`, `bg-header`, `bg-secondary`, `text-muted-foreground`, `border-border`, `bg-primary`, `text-primary-foreground`, `bg-accent`, `text-accent-foreground`, `ring-ring`, `text-link`, `bg-mark`, `shadow-card`, `shadow-control`, `rounded-md`(5px), `rounded-xl`(11px), 상태 `bg-status-{warn,danger,ok,info}-bg`·`text-status-…-fg`·`border-status-…-border`·`bg-status-…-dot`
  - `Badge` variant: 기본 shadcn 것 + `kind`(accent 배경, 종류 표시), `muted`(secondary 배경), `outdated`(`muted-foreground` 점선 테두리), `warn`·`danger`·`ok`·`info`(배경+테두리+굵은 글자, 앞에 점 `<span>`). `Badge`에 `dot?: boolean`을 더하는 대신, 상태 variant면 점을 자동으로 그린다
  - npm 스크립트: `dev`, `build`, `lint`(`eslint .`), `typecheck`(`tsc --noEmit`), `format`(`prettier --write .`), `format:check`(`prettier --check .`)
  - make 타깃: `frontend/node_modules`, `dev`; `lint`·`fmt`에 프론트 검사

- [ ] **Step 1: Next.js 앱을 만든다**

```bash
npx create-next-app@16.4.5 frontend --ts --eslint --tailwind --app --no-src-dir --import-alias "@/*" --use-npm
```

`.nvmrc`는 저장소 루트에 `26`(이 PC의 Node 메이저). `tsconfig.json`의 `"strict": true`를 확인한다. `eslint.config.mjs`에는 `next/core-web-vitals`와 `next/typescript`가 들어 있어야 한다. Prettier를 dev 의존성으로 설치하고, `eslint-config-prettier`를 ESLint 설정 끝에 둔다. `.prettierignore`에는 `.next`, `node_modules`, `next-env.d.ts`, `package-lock.json`을 넣는다. create-next-app이 만든 예시 SVG(`public/*.svg`)와 기본 페이지 내용은 지운다.

- [ ] **Step 2: shadcn을 초기화하고 부품을 더한다**

```bash
npx shadcn@4.23.0 init      # base color: neutral, CSS variables: yes
npx shadcn@4.23.0 add button input checkbox tabs card badge select label
```

- [ ] **Step 3: `globals.css`를 토큰으로 바꾼다**

설계안 '스타일'의 두 표 값을 그대로 넣는다. 라이트는 `:root`, 다크는 `@media (prefers-color-scheme: dark) { :root { … } }`에 둔다. shadcn이 넣은 `.dark { … }` 블록과 `@custom-variant dark (&:is(.dark *));` 줄은 지운다. 그러면 Tailwind의 `dark:`가 OS 설정을 따른다. 전환 버튼은 두지 않는다.
- shadcn 기본 토큰은 `card`/`popover`(+`-foreground` = foreground 값), `secondary`/`muted`/`input`(+`secondary-foreground` = foreground 값), `destructive` = 상태 `danger`의 점 색이다. `sidebar*`, `chart-*` 토큰은 지운다
- 새 토큰: `--header`(머리 띠), `--link`, `--mark`, `--shadow-card`, `--shadow-control`, 상태 16개(`--status-warn-bg` 등)
- `--radius: 0.4375rem`으로 둔다. `@theme inline`에서 `--radius-md: calc(var(--radius) - 2px)`, `--radius-xl: calc(var(--radius) + 4px)`인지 확인한다
- `@theme inline`에 새 토큰을 연결한다(`--color-header: var(--header)`, `--color-status-warn-bg: var(--status-warn-bg)`, `--shadow-card: var(--shadow-card)` …). `--font-sans: var(--font-inter), var(--font-noto-kr), sans-serif`
- shadcn 부품의 `shadow-xs`·`shadow-sm`은 `shadow-control`·`shadow-card`로 바꾸고, 카드는 `rounded-xl`인지 확인한다

`layout.tsx`: `<html lang="ko">`, `next/font/google`의 `Inter`(`variable: "--font-inter"`, `subsets: ["latin"]`)와 `Noto_Sans_KR`(`variable: "--font-noto-kr"`, weight `400·500·700`, `preload: false`). `<body className="bg-background text-foreground font-sans antialiased">`. `metadata.title`은 `치과 AX`.

- [ ] **Step 4: API 프록시를 둔다**

`next.config.ts`에 `rewrites: [{ source: "/api/:path*", destination: "http://localhost:8000/:path*" }]`와 `experimental.proxyTimeout: 300_000`을 둔다. 주석으로 근거를 남긴다: 임베딩 1번과 버킷별 리랭크 2번이 각각 최대 30초씩 3번 시도한다(`backend/embeddings.py` `API_TIMEOUT`·`API_RETRIES`). 그래서 4.5분까지 기다려야 백엔드의 503 문구가 화면에 온다.

- [ ] **Step 5: Makefile·CI·.gitignore를 고친다**

```make
frontend/node_modules: frontend/package-lock.json
	npm --prefix frontend ci
	touch $@

dev: frontend/node_modules
	trap 'kill 0' EXIT; \
	uv run $(if $(wildcard .env),--env-file .env) uvicorn backend.api:app --port 8000 & \
	npm --prefix frontend run dev
```

`lint`와 `fmt`는 `frontend/node_modules`를 선행 조건으로 둔다. `lint`에는 `npm --prefix frontend run lint`, `format:check`, `typecheck`를 더하고, `fmt`에는 `npm --prefix frontend run format`을 더한다. `.PHONY`에 `dev`를 더한다. 커밋 훅이 `make lint`를 돌리므로, 새 worktree에서도 `npm ci`가 먼저 돈다.

`ci.yml`: `uv sync` 다음, `make lint` 앞에 `actions/setup-node@v4`(`node-version-file: .nvmrc`, `cache: npm`, `cache-dependency-path: frontend/package-lock.json`)를 둔다. `make test` 뒤에는 `npm --prefix frontend run build` 단계를 둔다. 머리 주석의 "next build까지 도는 make ci는…" 문장은 이번 상태에 맞게 고친다: next build는 지금 돌고, fixture·Playwright가 생기면 `make ci`.

`.gitignore`: `frontend/node_modules`, `frontend/.next`, `frontend/next-env.d.ts`, `*.tsbuildinfo`.

- [ ] **Step 6: 확인한다**

Run: `make lint && npm --prefix frontend run build && uv run pytest tests/test_no_ground_truth_import.py`
Expected: 모두 성공. `grep -rn "#[0-9A-Fa-f]\{3,6\}" frontend/app frontend/components --include=*.tsx`의 결과가 없다.

- [ ] **Step 7: 커밋한다**

`frontend/` 아래는 `git status`로 목록을 보고 파일 이름으로 add한다(`node_modules`·`.next`가 빠졌는지 확인).

```bash
git commit  # chore: 프론트 세팅 — Next.js·Tailwind·shadcn·토큰, make dev, CI 빌드
```

---

### Task 4: 시작 화면·공통 레이아웃·준비 중 화면

**Files:**
- Create: `frontend/lib/api.ts`, `frontend/lib/nav.ts`, `frontend/components/app-header.tsx`, `frontend/components/coming-soon.tsx`, `frontend/app/(portal)/layout.tsx`, `frontend/app/(portal)/[screen]/page.tsx`
- Modify: `frontend/app/page.tsx`

**Interfaces:**
- Consumes: Task 2의 `GET /clinic`, Task 3의 토큰·부품
- Produces:
  ```ts
  // lib/api.ts
  export type SourceType = "document" | "call" | "message_thread" | "chart_note";
  export interface SearchCard { id: string; source_type: SourceType; title: string | null; body: string;
    event_date: string | null; chart_no: string | null; patient_name: string | null; caller_name: string | null;
    direction: "수신" | "발신" | null; doc_type: string | null; version: string | null; is_outdated: boolean | null }
  export interface SearchResponse { manual_results: SearchCard[]; case_results: SearchCard[] }
  export interface Clinic { name: string; today: string }
  export class ApiError extends Error {}
  export async function getJson<T>(path: string, params?: URLSearchParams): Promise<T>
  export function useClinic(): Clinic | null | "error"   // 한 번만 가져온다(모듈 안 Promise 캐시)
  // lib/nav.ts
  export const MENU: { href: string; label: string }[]
  export const SPACES: Record<"eval" | "emr", string>   // { eval: "AI 평가", emr: "전환 전 EMR" }
  export const COMING_SOON: Record<string, string>      // 라우트 조각 → 화면 이름
  ```
- `getJson`은 `/api${path}`로 요청한다. 응답이 `ok`가 아니면, 본문이 JSON이고 `detail`이 문자열일 때 그 문자열로 `ApiError`를 던진다. `detail`이 배열(FastAPI 422)이면 각 항목의 `msg`를 `, `로 이어 붙인다. JSON이 아니거나 `fetch`가 실패하면 `ApiError("서버에 연결하지 못했습니다")`를 던진다

화면 규칙:
- `MENU` 순서(기능 정의 §4 앱 구성): 오늘 `/today`, 통화 `/calls`, 예약 `/schedule`, 준비표 `/prep`, 물어보기 `/ask`, 청구 점검 `/claims`, 경영 리포트 `/report`, 재무 대조 `/finance`, 후속 관리 `/followup`
- `COMING_SOON`은 `MENU`에서 `/ask`를 뺀 것과 `eval`·`emr`이다. `[screen]/page.tsx`는 `generateStaticParams`로 이 목록만 만들고 `dynamicParams = false`로 둔다. 그래서 다른 경로는 404다
- 머리 띠(`bg-header`, 아래 테두리): 왼쪽에 로고(`AX` 사각형 + 병원 이름, `/`로 링크). 가운데에 메뉴(현재 항목은 `bg-accent text-accent-foreground font-bold`, `aria-current="page"`). 경로가 `/eval`·`/emr`이면 메뉴 대신 공간 이름과 "← 돌아가기" 버튼을 둔다. 버튼은 `history.length > 1`이면 `router.back()`, 아니면 `/`로 간다. 오른쪽에는 담당 파트 `Select`(원장/데스크/상담실장)와 `기준일 YYYY-MM-DD`를 둔다
- 담당 파트는 `localStorage` 키 `ax.part`에 둔다. 읽기·쓰기 모두 `try/catch`로 감싼다. 첫 렌더에는 값 없이 그리고 `useEffect`에서 읽는다(하이드레이션 불일치 방지). 기본값은 `데스크`
- 기준일·병원 이름은 `useClinic()`으로 가져온다. 불러오는 중이면 빈칸, 실패하면 `—`
- 시작 화면(`/`, 머리 띠 없음): 와이어프레임 `R.home`의 배치를 따른다. 제목은 병원 이름(실패하면 `—`), 그 밑에 `Dental · 사내 시스템`. `Card` 타일 4개(2열, 600px 미만 1열)는 와이어프레임의 제목·설명·대상 문구 그대로이고, 링크는 `/today`·`/ask`·`/eval`·`/emr`이다
- `ComingSoon`: 화면 이름 제목과 `준비 중입니다.` 한 줄

- [ ] **Step 1: 위 파일들을 만든다**

- [ ] **Step 2: 검사한다**

Run: `make lint && npm --prefix frontend run build`
Expected: 성공. 빌드 출력에 `/`, `/ask`, `/today` … `/emr`이 보인다(`/ask`는 Task 5 전까지 `[screen]`에 없으므로 404여도 된다).

- [ ] **Step 3: 화면을 직접 확인한다**

`make dev`로 띄운다(`data/app.sqlite`·`data/source.sqlite`가 있어야 한다). 확인할 것:
- 시작 화면 제목이 병원 이름이고, 타일 4개가 각 경로로 간다
- 메뉴가 9개이고 현재 항목이 표시된다
- `/eval`에서 메뉴 대신 "AI 평가"와 돌아가기가 나오고, 돌아가기가 들어오기 전 화면으로 간다
- 담당 파트를 바꾸고 새로고침하면 유지된다
- 백엔드를 끄고 새로고침하면 기준일이 `—`이다
- OS 다크 모드를 켜면 다크 토큰으로 바뀐다

Ctrl+C로 두 프로세스가 모두 끝나는지도 확인한다(`lsof -i :8000 -i :3000`이 비어 있다).

- [ ] **Step 4: 커밋한다**

```bash
git commit  # frontend: 시작 화면·공통 머리 띠·준비 중 화면
```

---

### Task 5: 기록 검색 화면 (`/ask`)

**Files:**
- Create: `frontend/app/(portal)/ask/page.tsx`, `frontend/lib/search-query.ts`, `frontend/lib/highlight.tsx`, `frontend/components/ask/{ask-view,search-form,search-results,result-card}.tsx`

**Interfaces:**
- Consumes: Task 4의 `getJson`, `ApiError`, `SearchResponse`, `SearchCard`, `SourceType`
- Produces:
  ```ts
  // lib/search-query.ts
  export const ALL_TYPES: SourceType[]  // ["document", "call", "message_thread", "chart_note"]
  export interface SearchState { q: string; types: SourceType[]; from: string; to: string; chart: string }
  export function readSearch(p: URLSearchParams): SearchState   // 없는 값은 빈 문자열, types 없으면 ALL_TYPES
  export function writeSearch(s: SearchState): URLSearchParams  // tab=search, 빈 값 생략, types가 4개 전부면 생략
  export function toApiParams(s: SearchState): URLSearchParams  // q·source_types·date_from·date_to·chart_no, 빈 값 생략
  export function manualEmptyReason(s: SearchState): string | null
  // lib/highlight.tsx
  export function highlight(text: string, q: string): React.ReactNode[]
  ```

화면 규칙(설계안 '화면'):
- URL 모양: `/ask?tab=search&q=…&types=document,call&from=…&to=…&chart=…`. `tab`이 없으면 기록 검색이다. 탭은 `질문하기 | 기록 검색 | 문서함`이고, 탭을 바꾸면 `tab`만 바꿔 `router.replace`한다. 질문하기·문서함은 `준비 중입니다.`를 보인다
- 입력 상태(`SearchState` 초안)는 `AskView`가 들고 있어 탭을 바꿔도 남는다. 찾기를 누르면 `router.push`로 URL을 바꾼다. 검색 실행은 URL(`useSearchParams`)의 `q`가 바뀔 때만 일어나고, 이때 입력 초안도 URL 값으로 다시 맞춘다. 그래서 새로고침·뒤로 가기·공유 링크가 모두 같은 경로를 탄다. 늦게 도착한 옛 응답은 버린다(요청마다 `AbortController`)
- `page.tsx`는 `useSearchParams` 때문에 `<Suspense>`로 `AskView`를 감싼다(그렇지 않으면 `next build`가 실패한다)
- 머리말: 제목 `물어보기`, 설명 `매뉴얼·공지와 지난 통화·문자·차트 기록을 찾습니다.`
- 검색 조건 `Card`: 첫 줄에 검색어 `Input`(type search)과 찾기 `Button`. 아랫줄에 출처 `Checkbox` 4개(문서·통화·문자·차트 기록), 기간 날짜 입력 2개, 환자번호 `Input`(placeholder `C-00000`). 출처가 하나도 없거나 검색어가 공백뿐이면 찾기를 막는다. Enter로도 찾는다
- 상태 문구:
  - 처음: `검색어를 넣고 찾기를 누르세요.`
  - 로딩: `찾는 중…`
  - 두 칸이 모두 비면: `결과가 없습니다.`
  - 오류: `ApiError` 메시지를 `danger` 안내 띠(점 + 굵은 글자, `role="alert"`)로 보인다
- 결과 머리: `관련도 상위 {manual+case}건`, 옆에 `"{q}" · 출처 {전체 | 고른 출처 이름}`(작은 회색 글자). 그 아래 2단(`md` 이상 2열): `매뉴얼 결과 {n}` / `실제 사례 결과 {n}`
- 매뉴얼 칸이 비면 `manualEmptyReason`의 문구를 보인다. 우선순위는 아래 순서다. 해당 사항이 없으면 `결과가 없습니다.`
  1. 출처에 문서 없음 → `문서가 출처에서 빠졌습니다`
  2. 환자번호 있음 → `환자번호 필터에서는 문서를 찾지 않습니다`
  3. 기간 있음 → `기간 필터에서는 시행일 있는 공지만 찾습니다`
- 결과 카드(`Card`, 조각 하나 = 카드 하나, 묶지 않는다):
  - 윗줄 작은 글자: 종류 배지 / 날짜 / 차트 번호
    - 종류 배지: 문서는 `kind`, 나머지는 `muted`
    - 문서 종류 이름: notice 공지, manual 매뉴얼, faq FAQ, insurance_guide 보험 안내, claim_standard 청구 기준, education 교육 자료, external 외부 자료
    - 통화는 `통화 · 수신`, 문자는 `문자`, 차트는 `차트 기록`
  - 통화·문자는 `이름(차트번호)`, 환자 없는 통화는 `상대: {caller_name}`. 공지는 `{version} · 시행 {event_date}`. 옛 공지는 `outdated` 배지 `옛 버전`을 달고 제목을 `text-muted-foreground`로 둔다
  - 문서는 `title`을 굵게 쓴다
  - 본문은 `line-clamp-3`으로 접는다. `펼치기`/`접기`(`text-link`) 버튼으로 그 조각 전체를 연다. 본문이 짧아 잘리지 않으면 버튼을 두지 않는다(`scrollHeight > clientHeight`로 판단)
  - 제목과 본문에 `highlight`를 쓴다
- `highlight`: `q`를 공백으로 나눈 어절 중 빈 것을 빼고, 긴 것부터 정규식 이스케이프해 `|`로 잇는다. 대소문자는 구분하지 않는다. `split`으로 나눈 조각 중 맞은 것만 `<mark className="bg-mark rounded-sm px-0.5 text-inherit">`로 감싼다. `dangerouslySetInnerHTML`을 쓰지 않는다

- [ ] **Step 1: `search-query.ts`와 `highlight.tsx`를 만든다**

- [ ] **Step 2: 컴포넌트와 페이지를 만든다**

- [ ] **Step 3: 검사한다**

Run: `make lint && npm --prefix frontend run build && make test`
Expected: 모두 성공

- [ ] **Step 4: 실데이터로 직접 확인한다(설계안 완료 기준 2·3·5와 Review Focus)**

`make index EMBEDDING=voyage-4`가 끝난 `data/`와 `.env`의 키로 `make dev`를 띄운다. 확인할 것:
1. 시작 화면 → 물어보기 → 기록 검색으로 이동된다. Ctrl+C로 둘 다 끝난다
2. "크라운 가격"이 매뉴얼·사례 두 칸으로 나뉘어 나온다
3. 출처를 차트 기록만 고르면 사례 칸에 차트만 나오고 차트 번호가 보인다. 매뉴얼 칸에는 `문서가 출처에서 빠졌습니다`가 나온다
4. 환자번호를 주면 그 환자의 기록만 나온다(소문자 `c-…`로도)
5. 차트 카드 본문 끝에 검색용 괄호가 없다
6. 옛 공지 카드에 `옛 버전`이 보인다
7. 새로고침해도 검색이 복원되고 담당 파트가 유지된다. 뒤로/앞으로 이동하면 검색어·필터·결과가 URL을 따른다
8. `미백 (비용`, `c++`, `a?`로 검색해도 화면이 깨지지 않고 강조가 맞다
9. 백엔드를 끄면 `서버에 연결하지 못했습니다`가 나온다. `data/app.sqlite`를 잠시 다른 이름으로 바꾸면 `make index` 문구가 나온다. 화면은 깨지지 않는다
10. 처음 검색(외부 API 첫 호출)이 느려도 프록시 오류 없이 결과가 온다

- [ ] **Step 5: 커밋한다**

```bash
git commit  # frontend: 기록 검색 화면 — URL 상태·2단 결과·카드·강조
```

---

### Task 6: 문서 반영과 마무리 확인

**Files:**
- Modify: `CLAUDE.md`, `README.md`, `docs/치과 AX - 앱 설계.md`, `docs/치과 AX - 기능 정의.md`

- [ ] **Step 1: 설계안 '문서 변경'대로 고친다**

- `CLAUDE.md`
  - 폴더: `backend/`에 FastAPI(`backend/api.py`), `frontend/`는 Next.js(Tailwind + shadcn)로
  - 명령: `make dev` 한 줄
  - 코드 컨벤션 Next.js 줄의 "(frontend 착수 때 켠다)"를 지우고 규칙을 더한다: 색·모서리·글꼴·그림자는 `frontend/app/globals.css` 토큰으로만, 부품은 `components/ui`만
  - 깃 절: 커밋 영역에 `backend`·`frontend`를 더하고, CI 설명에 프론트 검사·빌드를 더한다
  - 설계 문서 절의 검색 상수 위치(`backend/rag_search.py`)가 맞는지 확인한다
- `README.md`: `frontend/ Next.js (예정)` 줄을 지금 상태로 고친다
- 앱 설계
  - §7 '스택' 행: `Next.js + TypeScript`(Tailwind CSS + shadcn/ui)
  - §7 '실행' 행: `make dev`를 앞에 더한다
  - §9 첫 문단: "와이어프레임은 레이아웃·동작의 기준이고, 색·모양은 `frontend/app/globals.css` 토큰과 `components/ui`"
  - §9 공통 레이아웃: 병원 이름·기준일은 `GET /clinic`
- 기능 정의 §9
  - "프론트 착수 시 — `CLAUDE.md` 코드 컨벤션의 Next.js 규칙…" 항목을 지운다
  - 착수 순서 줄에서 `앱 뼈대·기록 검색 화면 →`를 지운다

문서에는 규칙만 쓴다. 결정 날짜·경위는 넣지 않는다.

- [ ] **Step 2: 남은 참조를 훑는다**

Run: `grep -rn "예정\|CSS Modules\|tokens.css\|frontend 착수" CLAUDE.md README.md docs/*.md .github/workflows/ci.yml`
Expected: 이번 작업과 관련된 낡은 문구가 없다

- [ ] **Step 3: 전체를 확인한다**

Run: `make fmt && make lint && make test && npm --prefix frontend run build`
Expected: 모두 성공. 테스트 수가 이 브랜치 시작 때(1426)보다 늘어난다

- [ ] **Step 4: 커밋한다**

```bash
git add CLAUDE.md README.md "docs/치과 AX - 앱 설계.md" "docs/치과 AX - 기능 정의.md"
git commit  # docs: 앱 뼈대 반영 — make dev·프론트 스타일 규칙·완료한 다음 할 일 정리
```

푸시와 PR은 사용자가 요청할 때 한다. PR 전에는 `pj2-doc-tidy`를 한 번 돌린다(메모리 규칙).
