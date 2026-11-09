# ③ 기록 검색 요약·문서함 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 기록 검색에 출처 번호가 붙은 AI 요약을 붙이고, 질문하기↔기록 검색 연결과 종류 칩이 있는 문서함을 만든다.

**Architecture:** 요약은 `backend/search_summary.py`의 게이트웨이 호출 한 번(검사 실패 시 한 번 재시도)이고, `POST /search/summary`가 같은 조건으로 다시 검색해 그 결과만 입력·인용 기준으로 쓴다. 같은 검색·결과·프롬프트 버전이면 `ax_search_logs`의 끝난 행을 다시 쓴다. 문서함은 `source.sqlite`만 읽는 `backend/documents.py` + `GET /documents`·`/documents/{id}`와 `/ask?tab=docs&doc=<id>` 화면이다.

**Tech Stack:** Python 3 · FastAPI · sqlite3 · pytest / Next.js 16 · React 19 · Tailwind · shadcn/ui · Playwright · `react-markdown` + `remark-gfm`(새로 추가)

**Spec:** `docs/superpowers/specs/2026-11-09-search-summary-docs-design.md` (앞 설계 `docs/superpowers/specs/2026-11-09-chat-agent-design.md`)

## Global Constraints

- 브랜치 `feat/search-summary-docs`(이미 main에서 땄다). 커밋은 `<영역>: <무엇>` + 본문 글머리, `git add`는 파일 이름으로
- `backend/`는 `ground_truth/`·`generator/`·`evaluation/`를 import하지 않는다. `emr_*`·`fin_*`에 쓰지 않는다
- 주석·독스트링은 한국어, 근거 절을 가리킨다(예: `설계 2026-11-09 검색 요약 "요약 입력과 검사"`)
- 화면은 `frontend/app/globals.css` 토큰과 `frontend/components/ui/` 부품만 쓴다(직접 hex·임의 값·Tailwind 기본 색 이름 금지)
- 요약 별칭 `chat`, 작업 이름 `search_summary`, 프롬프트 `prompts/search_summary/v1.md`, 출력 `{text, cited_ids[], refused}`
- 고정 문구: `refused` → `검색 결과에서 정리할 내용을 찾지 못했습니다`, 그 밖 실패 → `요약을 만들지 못했습니다`
- 규칙 id: `search_summary.cite_outside`, `search_summary.cite_missing`, `search_summary.number_unverified`
- 과금 실행(실제 Sonnet·Voyage 호출)은 돌리기 전에 사람에게 묻는다
- 커밋 전 `make fmt` → `make lint` → `make test`

## Review Focus

1. **출처 표지가 숫자 후검증에 걸리는 것** — 요약 `"…2시간 무료[2]."`에서 `2`가 표지로만 있을 때 숫자 검사가 통과해야 하고, 표지를 뗀 본문 숫자는 그대로 검사돼야 한다 → Task 2 `test_marker_digits_are_not_numbers`
2. **결과가 옛 공지뿐인 검색** — 입력이 비면 모델을 부르지 않고 행도 남기지 않으며 `refused` 문구 → Task 2 `test_only_outdated_notices_skips_model`
3. **결과 0건 검색에서 화면이 요약을 부르는 것** — 결과가 없으면 요약 카드도 요청도 없다 → Task 5 e2e `빈 결과면 요약을 부르지 않는다`
4. **옛 버전 공지를 문서함에서 직접 여는 것**(검색 결과의 "옛 버전" 카드) — 목록은 최신만이지만 `/ask?tab=docs&doc=<옛 id>`는 본문과 "옛 버전 — 최신 v○ 보기" 줄을 보인다 → Task 1 `test_document_old_version_points_to_latest`, Task 4 e2e
5. **하루 상한에 닿은 뒤의 요약** — `BudgetExceeded`면 `budget` 상태와 실패 문구, 검색 결과는 그대로 → Task 2 `test_budget_exceeded_is_budget`

---

### Task 1: 문서함 API

**Files:**
- Create: `backend/documents.py`
- Modify: `backend/rag_search.py:145-157` (`Searcher._replacements` → `documents.replacements` 호출)
- Modify: `backend/api.py` (`GET /documents`, `GET /documents/{doc_id}`)
- Test: `tests/backend/test_documents.py`

**Interfaces:**
- Produces:
  - `replacements(conn, schema: str = "source") -> dict[int, int]` — 옛 공지 id → 같은 제목 최신 공지 id. 최신 = `effective_date DESC, id DESC` 첫 행(지금 `Searcher._replacements`와 같은 규칙, 그 본문을 옮긴다). `schema`는 `"source"`(앱 연결 ATTACH) 또는 `"main"`(`_open_source` 연결)
  - `list_documents(conn, schema="source") -> list[dict]` — `[{id, type, title, version, effective_date}]`, 옛 공지 제외. 정렬: 공지는 `effective_date DESC`, 나머지는 `title`
  - `get_document(conn, doc_id: int, schema="source") -> dict | None` — `{id, type, title, version, effective_date, body, versions: [{id, version, effective_date}], latest_id}`. `versions`는 같은 제목 공지 전부, 최신 먼저(공지가 아니면 `[]`). `latest_id`는 공지면 최신 id, 아니면 자기 id
  - `GET /documents` → `{"documents": list_documents(...)}`, `GET /documents/{doc_id}` → `get_document(...)` 또는 404 `"문서가 없습니다"`. 둘 다 `_open_source(paths)`(schema `"main"`)

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/backend/test_documents.py`, `source_paths` 픽스처(공지 1·2 "미백 비용" v1/v2, 매뉴얼 3)와 `client_with`(test_api.py의 픽스처를 conftest로 옮기거나 import)

```python
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


def test_documents_work_without_index(client_with, source_paths):
    # app.sqlite 없이(색인 전)도 열린다
    assert client_with(paths=source_paths).get("/documents").status_code == 200
```

- [ ] **Step 2: 실패 확인** — Run: `uv run pytest tests/backend/test_documents.py -v` / Expected: FAIL (404 Not Found 경로)
- [ ] **Step 3: `backend/documents.py` 구현, `Searcher._replacements`가 `documents.replacements(self.conn)`를 부르게 바꾸고, `api.py`에 두 경로 추가.** 모듈 독스트링에 원본(기능 정의 §4 ③ 문서함, 설계 2026-11-09 검색 요약 "문서함 API")
- [ ] **Step 4: 통과 확인** — Run: `uv run pytest tests/backend/test_documents.py tests/backend/test_rag_search.py -v` / Expected: PASS (검색의 옛 공지 정렬 테스트도 그대로 통과)
- [ ] **Step 5: 커밋** — `git add backend/documents.py backend/rag_search.py backend/api.py tests/backend/test_documents.py` (픽스처를 옮겼으면 `tests/backend/conftest.py`·`tests/backend/test_api.py`도) → `git commit -m "backend: 문서함 API — 최신 공지만, 버전 목록"`

### Task 2: 요약 모듈

**Files:**
- Create: `backend/search_summary.py`, `prompts/search_summary/v1.md`
- Modify: `backend/chat.py:61-71` (`_chart_plain`을 `backend/search_cards.py`의 `chart_plain`으로 옮기고 chat은 그걸 import)
- Test: `tests/backend/test_search_summary.py`

**Interfaces:**
- Consumes: `Searcher.search(**SearchArgs.model_dump(mode="json"))`, `searcher.replaced_by`, `searcher.drain_usage()`, `backend.usage.record_voyage(conn, usage, is_eval=, ref=None)`, `backend.verify.allowed_numbers/numbers_ok`, `backend.harness.store.count_block(conn, rule_id)`, `backend.harness.loop.external(id, text)`, `backend.prompts.load("search_summary", "v1")["system"]`, `gw.complete(ALIAS, task=, system=, messages=, schema=, prompt_version=) -> LLMResult`(`ok`, `data`, `cost_usd`, `fallback_used`), `shared.llm.BudgetExceeded`
- Produces:
  - `backend.search_cards.chart_plain(conn, chunk: dict) -> dict` (지금 `chat._chart_plain` 그대로)
  - 상수 `TASK = "search_summary"`, `ALIAS = "chat"`, `PROMPT_VERSION = "v1"`, `REFUSED_TEXT`, `FAILED_TEXT`, `CITE_OUTSIDE`, `CITE_MISSING`, `NUMBER`(규칙 id 셋, Global Constraints 값)
  - `strip_marks(text: str) -> str` — `[n]` 표지를 뗀 본문
  - `check(text: str, cited: list[str], input_ids: list[str], sources: list[str]) -> str | None` — 어긋난 규칙 id 또는 None. 순서: ① `cited` ⊆ `input_ids`, 모든 `[n]`이 1..len(cited), 모든 cited가 표지로 한 번 이상 쓰임, cited 비어 있음 → `CITE_OUTSIDE` ② 문장마다 표지 → `CITE_MISSING` ③ `numbers_ok(strip_marks(text), allowed_numbers(sources))` → `NUMBER`. (`refused`면 부르지 않는다)
  - `summarize(conn, gw, searcher, args: SearchArgs, *, is_eval: bool = False) -> dict` — `{status, text, cited_ids, retrieved_ids, log_id, reused, cost_usd, fallback_used}`. `text`는 `answered`면 모델 본문, `refused`면 `REFUSED_TEXT`, 그 밖은 `FAILED_TEXT`. 실패 상태의 `cited_ids`는 `[]`

문장 나누기(검사 ②) — 문서가 정하지 않는 규칙이라 적는다:

```python
SENTENCES = re.compile(r"\n+|(?<=[.?!\]])\s+")  # 줄바꿈, 또는 . ? ! ] 뒤 공백에서 나눈다
MARK = re.compile(r"\[(\d+)\]")
# 빈 조각은 건너뛰고, 남은 조각마다 MARK가 하나 이상 있어야 한다
```

흐름(`summarize`):
1. `found = searcher.search(...)`; 입력 = 두 버킷 청크 중 문서 id(`doc:<id>:<n>`)가 `searcher.replaced_by`에 없는 것, 차트는 `chart_plain`. `input_ids` = 그 id 순서 목록
2. Voyage 사용량을 `record_voyage`로 남긴다(입력이 비어도)
3. 입력이 비면 → `{status: "refused", text: REFUSED_TEXT, log_id: None, reused: False, ...}` (모델·행 없음)
4. 캐시 키 `tool_calls = json.dumps([{"name": "search", "args": args.model_dump(mode="json"), "prompt_version": PROMPT_VERSION}], ensure_ascii=False, sort_keys=True)`, `retrieved_ids = json.dumps(input_ids)`. `kind='search_summary' AND is_eval=0 AND status IN ('answered','refused') AND tool_calls=? AND retrieved_ids=?` 최신 행이 있으면(그리고 `is_eval`이 거짓이면) 그 행으로 돌려준다(`reused: True`, 모델 호출 없음)
5. 사용자 메시지 = `검색어: {q}\n\n검색 결과:\n` + 입력마다 `external(id, text)`. 첫 호출 → `refused`면 끝, 아니면 `check`. 어긋나면 `count_block` 후 assistant(첫 출력 JSON) + user(이유 + 재시도 안내) 를 붙여 한 번 더. 둘째도 어긋나면 `count_block` 후 상태 `cite_failed`(CITE_*) / `number_failed`(NUMBER)
6. `r.ok`가 거짓이거나 `data`가 없으면 `failed`, `BudgetExceeded`면 `budget`
7. `ax_search_logs`에 끝난 행 1개(`kind='search_summary'`, `question=args.query`, 위 `tool_calls`·`retrieved_ids`, `answer_text`(answered면 모델 본문), `cited_ids`, `status`, `fallback_used`, `is_eval`, `created_at`은 chat과 같은 형식) — 커밋

숫자 근거 `sources` = `[args.query]` + `date_from`·`date_to`·`patient_chart_no` 중 값 있는 것 + 입력 청크 `text`.

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/backend/test_search_summary.py`. 픽스처는 `tests/backend/test_chat.py` `conn`처럼 만들되 청크에 옛 공지·최신 공지를 더한다(`ax_documents` 1·2 "주차 안내" v1/v2, 청크 `doc:1:0` "주차 안내\n진료 당일 1시간 무료", `doc:2:0` "주차 안내\n진료 당일 2시간 무료", `doc:3:0` 매뉴얼, `chart:102`). 게이트웨이는 `tests/backend/script.py` `Script(*LLMResult)`, 응답은 `LLMResult(True, "", {"text":…, "cited_ids":[…], "refused":False}, "m", False, 1, 1, 0.01, 1)`

```python
def test_answer_with_numbered_citations(conn): # "진료 당일 2시간 무료[1]." cited ["doc:2:0"] → answered, 행 1개, reused False
def test_outdated_notice_not_in_input(conn):   # 보낸 메시지에 "doc:1:0"이 없고 retrieved_ids에도 없다
def test_marker_digits_are_not_numbers():  # check("무료입니다[1][2][3].", ["a","b","c"], ["a","b","c"], ["무료"]) is None, 그리고 check("7일 걸립니다[1].", ["a"], ["a"], ["무료"]) == NUMBER
def test_unverified_number_retries_then_number_failed(conn):  # "3시간 무료[1]." 두 번 → number_failed, text == FAILED_TEXT, cited_ids == [], 규칙 카운트 2
def test_cite_outside_retries_once_then_answers(conn):  # 첫 응답 cited ["doc:9:0"], 둘째 정상 → answered, gw 호출 2
def test_sentence_without_marker_is_cite_missing(conn):  # "주차 2시간 무료[1]. 예약은 전화로." 두 번 → cite_failed, 규칙 cite_missing 2
def test_marker_out_of_range_is_cite_outside(conn):   # "[2]" 인데 cited 1개 → 재시도
def test_unused_citation_is_cite_outside(conn):       # cited 2개인데 본문에 [1]만
def test_refused_shows_fixed_text(conn):              # refused True → status refused, text == REFUSED_TEXT
def test_only_outdated_notices_skips_model(conn):     # source_types=["document"], 검색어로 옛 공지만 걸리게(또는 searcher를 가짜로) → gw 호출 0, 행 0, status refused
def test_budget_exceeded_is_budget(conn):              # Script(BudgetExceeded("x")) → status budget, text == FAILED_TEXT
def test_gateway_failure_is_failed(conn):             # LLMResult(False, …) → failed
def test_same_search_reuses_row(conn):                # 두 번 부름 → 둘째 reused True, gw 호출 합 1, 행 1개
def test_prompt_version_change_makes_new_row(conn, monkeypatch):  # PROMPT_VERSION을 "v2"로 → 새 호출
def test_failed_row_is_not_reused(conn):              # 첫 결과 failed → 둘째는 다시 호출
def test_eval_run_does_not_reuse(conn):               # is_eval=True면 캐시를 보지 않고 행은 is_eval=1
def test_chart_input_has_no_search_brackets(conn):    # 보낸 메시지에 차트 괄호 꼬리 "(잇몸치료 · 사흘)"가 없다
```

각 테스트는 위 주석의 상태·`text`·`cited_ids`·`len(gw.calls)`·`ax_search_logs`/`ax_rule_counts` 행을 단언한다.

- [ ] **Step 2: 실패 확인** — Run: `uv run pytest tests/backend/test_search_summary.py -v` / Expected: FAIL (`ModuleNotFoundError: backend.search_summary`)
- [ ] **Step 3: `chart_plain`을 옮기고 `backend/search_summary.py` 구현, `prompts/search_summary/v1.md` 작성.** 프롬프트에 넣을 규칙: 검색 결과가 무엇을 말하는지 3~5문장으로 정리, 검색어에 답하거나 결과를 세지 않음, 모든 문장 끝에 `[n]`(n은 `cited_ids` 순번), 숫자는 결과 표기 그대로, 정리할 관련 내용이 없으면 `refused: true`·빈 `text`, 환자 이름을 쓰지 않음(통화·문자 결과는 "지난 통화에서"처럼)
- [ ] **Step 4: 통과 확인** — Run: `uv run pytest tests/backend/test_search_summary.py tests/backend/test_chat.py tests/backend/test_prompts.py -v` / Expected: PASS
- [ ] **Step 5: 커밋** — `git add backend/search_summary.py backend/search_cards.py backend/chat.py prompts/search_summary/v1.md tests/backend/test_search_summary.py` → `git commit -m "backend: 기록 검색 요약 — 문장별 출처 번호·숫자 후검증·재사용"`

### Task 3: 요약 API

**Files:**
- Modify: `backend/api.py` (`POST /search/summary`)
- Test: `tests/backend/test_api.py`

**Interfaces:**
- Consumes: `search_summary.summarize(...)` (Task 2), `to_search_args`, `get_chat_gateway`(연결 → 게이트웨이), `card(conn, chunk, replaced_by)`
- Produces: `POST /search/summary` 본문 `SummaryBody {q: str, source_types: str | None, date_from: str | None, date_to: str | None, chart_no: str | None}`(`GET /search` 쿼리와 같은 이름·형식 — 화면이 `toApiParams` 결과를 그대로 보낸다) → `{status: str, text: str, sources: SearchCard[]}`. `sources`는 `cited_ids` 순서 그대로(화면 번호 = 순번). 오류: 인자 422(`/search`와 같은 문구), 색인 없음·`MissingVectors` 503 `NO_INDEX`, `EmbeddingFailed` 503. 쓰기 연결은 `db.connect(*paths)`(query_only 아님)

- [ ] **Step 1: 실패하는 테스트 작성** — `chat_client`와 같은 방식으로 `get_chat_gateway`를 `lambda: lambda conn: Script(LLMResult…)`(tests/backend/script.py)로 바꾼 `summary_client` 픽스처

```python
def test_summary_returns_numbered_sources(summary_client):
    c = summary_client(ok_result("접수 후 기공소로 보낸다[1].", ["doc:3:0"]))
    r = c.post("/search/summary", json={"q": "틀니 수리"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "answered" and [s["id"] for s in body["sources"]] == ["doc:3:0"]

def test_summary_same_filters_as_search(summary_client):  # source_types="call" → 입력에 문서 청크 없음(gw.calls[0] 메시지)
def test_summary_bad_args_is_422(summary_client):          # source_types="" → 422
def test_summary_without_index_is_503(client_with, source_paths):  # /chat의 503 테스트와 같은 방식
def test_summary_logs_voyage_usage(...):                    # test_search_records_voyage_usage와 같은 방식으로 ax_llm_calls에 voyage 행
```

- [ ] **Step 2: 실패 확인** — Run: `uv run pytest tests/backend/test_api.py -k summary -v` / Expected: FAIL (404/405)
- [ ] **Step 3: `api.py`에 `SummaryBody`·`POST /search/summary` 구현** — 핸들러는 동기 def, 연결·Searcher를 핸들러 안에서 만들고 닫는다(api.py 머리 독스트링 규칙)
- [ ] **Step 4: 통과 확인** — Run: `uv run pytest tests/backend -v` / Expected: PASS
- [ ] **Step 5: 커밋** — `git add backend/api.py tests/backend/test_api.py` → `git commit -m "backend: POST /search/summary"`

### Task 4: 문서함 탭

**Files:**
- Create: `frontend/lib/doc-types.ts`, `frontend/components/ask/docs-panel.tsx`, `frontend/components/ask/doc-body.tsx`, `frontend/e2e/ask.spec.ts`
- Modify: `frontend/package.json`·`package-lock.json`(`react-markdown`, `remark-gfm`), `frontend/lib/api.ts`(타입), `frontend/components/ask/result-card.tsx`(`DOC_TYPE` 이동 + "문서함에서 보기"), `frontend/components/ask/ask-view.tsx`(docs 탭)

**Interfaces:**
- Consumes: `GET /documents`, `GET /documents/{id}` (Task 1)
- Produces:
  - `lib/doc-types.ts`: `DOC_TYPE: Record<DocType, string>`(지금 `result-card.tsx` 값 그대로), `DOC_TYPE_ORDER: DocType[] = ["notice","manual","faq","insurance_guide","claim_standard","education","external"]`, `docHref(id: number): string` → `/ask?tab=docs&doc=${id}`, `docIdOf(chunkId: string): number | null`(`doc:<id>:<n>` → id)
  - `lib/api.ts`: `DocType`, `DocumentItem {id, type, title, version, effective_date}`, `DocumentDetail extends DocumentItem {body, versions: {id, version, effective_date}[], latest_id}`
  - `<DocsPanel />` — URL `doc`를 읽는다. 선택 종류 = 열린 문서의 종류, 없으면 `notice`. 칩 = `DOC_TYPE_ORDER` 순, 라벨 `DOC_TYPE[t]` + 그 종류 문서 수. 칩을 누르면 그 종류 첫 문서로 `router.replace(docHref(id))`. 왼쪽 목록(그 종류만, 공지는 시행일 표시), 오른쪽 본문(`<DocBody markdown />`, 종류·버전·시행일 딱지, "읽기 전용"). 공지면 아래에 다른 버전 링크, 열린 문서가 `latest_id`와 다르면 본문 위에 `옛 버전 — 최신 v{최신 version} 보기` 링크
  - `<DocBody markdown: string />` — `react-markdown` + `remark-gfm`, 원시 HTML 끔(기본값), 제목·목록·표 스타일은 토큰 클래스로

- [ ] **Step 1: 실패하는 e2e 작성** — `frontend/e2e/ask.spec.ts` 문서함 부분(실데이터 복사본의 `source.sqlite`를 읽는다 — 문서 API는 키가 필요 없다)

```ts
test("문서함은 공지 칩으로 열리고 칩을 바꾸면 그 종류만 보인다", async ({ page }) => {
  await page.goto("/ask?tab=docs");
  await expect(page.getByRole("button", { name: /^공지 \d+$/ })).toHaveAttribute("aria-pressed", "true");
  await page.getByRole("button", { name: /^매뉴얼 \d+$/ }).click();
  await expect(page).toHaveURL(/tab=docs&doc=\d+/);
  // 목록 항목이 모두 매뉴얼 — 열린 본문 딱지가 "매뉴얼"
  await expect(page.getByRole("article").getByText("매뉴얼", { exact: true })).toBeVisible();
});

test("옛 버전 공지를 열면 최신 보기 링크가 있다", async ({ page, request }) => {
  // /api/documents에서 공지 하나의 versions[1]을 골라 그 id로 연다
  // → "옛 버전 — 최신 v" 링크를 누르면 URL doc가 latest_id
});
```

- [ ] **Step 2: 실패 확인** — `make dev`를 끄고 Run: `make e2e` / Expected: 문서함 두 테스트 FAIL("준비 중입니다")
- [ ] **Step 3: 의존성 추가(`npm --prefix frontend install react-markdown remark-gfm`)와 위 Produces 구현.** `ResultCard`는 `source_type === "document"`일 때 메타 줄 끝에 `next/link` "문서함에서 보기"(`docHref(docIdOf(card.id))`). `AskView`의 docs 탭 자리표시를 `<DocsPanel />`로 바꾼다
- [ ] **Step 4: 통과 확인** — Run: `make e2e` 그리고 `make lint` / Expected: PASS, 린트·타입 오류 없음
- [ ] **Step 5: 커밋** — 바뀐 파일을 이름으로 `git add` → `git commit -m "frontend: 문서함 — 종류 칩·본문·옛 버전 링크, 검색 카드에서 열기"`

### Task 5: 요약 카드와 탭 연결

**Files:**
- Create: `frontend/components/ask/search-summary.tsx`
- Modify: `frontend/lib/api.ts`(`SummaryResponse`), `frontend/components/ask/ask-view.tsx`, `frontend/components/ask/chat-panel.tsx`, `frontend/e2e/ask.spec.ts`

**Interfaces:**
- Consumes: `POST /search/summary` (Task 3), `toApiParams`·`writeSearch`(`lib/search-query.ts`), `docHref`·`docIdOf`·`DOC_TYPE` (Task 4)
- Produces:
  - `SummaryResponse {status: string; text: string; sources: SearchCard[]}`
  - `<SearchSummary search: SearchState, requestKey: string, onAsk: (q: string) => void />` — 결과가 1건 이상인 `done` 상태에서만 `AskView`가 그린다. `requestKey`가 바뀌면 `postJson("/search/summary", Object.fromEntries(toApiParams(search)))`, 늦게 온 옛 응답은 버린다. 상태: 만드는 중 / `answered`(본문의 `[n]`을 작은 번호 표지로, 아래에 번호 칩 — `n · 종류 · 제목`(문서) 또는 `n · 날짜 · 차트번호/이름`(사례), 문서 칩은 `docHref`로) / 그 밖은 서버가 준 `text`를 그대로 / 요청 오류는 `errorText(e)`. AI가 만든 칸 표시(기존 화면의 AI 배지 방식)를 붙이고, 카드 아래 "이걸로 질문하기"(항상 보임)가 `onAsk(search.q)`
  - `ChatPanel` props `{draft: string; onDraftChange: (v: string) => void}` — 입력 상태를 `AskView`로 올린다(탭을 바꿔도 남는 동작 유지). 답 카드마다(답·거절·실패 모두) "근거 더 보기" 링크 = `/ask?${writeSearch({q: question, types: ALL_TYPES, from: "", to: "", chart: ""})}`
  - `AskView`: 기록 검색 탭 폼 아래·결과 위에 `<SearchSummary>`. `onAsk(q)` = 탭을 `chat`으로 바꾸고 `draft`를 q로(보내지 않는다)

- [ ] **Step 1: 실패하는 e2e 작성** — `frontend/e2e/ask.spec.ts`에 더한다. `/api/search`·`/api/search/summary`·`/api/chat`은 `page.route`로 가짜 응답(과금·키 없이)

```ts
test("요약 본문 번호와 출처 칩 번호가 맞는다", async ({ page }) => {
  // /api/search → manual_results 1장(doc:3:0), /api/search/summary → {status:"answered", text:"접수 후 기공소로 보낸다[1].", sources:[같은 카드]}
  await page.goto("/ask?tab=search&q=틀니");
  await expect(page.getByText("접수 후 기공소로 보낸다")).toBeVisible();
  await expect(page.getByRole("link", { name: /^1 · / })).toHaveAttribute("href", /tab=docs&doc=3/);
});
test("빈 결과면 요약을 부르지 않는다", async ({ page }) => {
  // /api/search → 두 버킷 빈 목록, /api/search/summary 호출 수를 세어 0
});
test("요약 실패 문구에도 이걸로 질문하기가 있고 누르면 질문 입력에 들어간다", async ({ page }) => {
  // summary → {status:"failed", text:"요약을 만들지 못했습니다", sources:[]}
  // "이걸로 질문하기" → 질문하기 탭, textarea 값이 "틀니", 아직 보내지 않음(/api/chat 호출 0)
});
test("근거 더 보기는 같은 질문으로 기록 검색을 연다", async ({ page }) => {
  // /api/chat → SSE "event: answer\ndata: {status:'refused', text:'문서함에 없음', sources:[]}\n\n"
  // 질문 보내고 "근거 더 보기" → URL tab=search&q=<질문>
});
```

- [ ] **Step 2: 실패 확인** — Run: `make e2e` / Expected: 새 네 테스트 FAIL
- [ ] **Step 3: 위 Produces 구현**
- [ ] **Step 4: 통과 확인** — Run: `make e2e`·`make lint`·`npm --prefix frontend run build` / Expected: PASS
- [ ] **Step 5: 커밋** — 파일 이름으로 `git add` → `git commit -m "frontend: 기록 검색 AI 요약 카드, 이걸로 질문하기·근거 더 보기"`

### Task 6: 문서 반영과 실측

**Files:**
- Modify: `docs/치과 AX - 기능 정의.md`(§4 ③ API에 `POST /search/summary`, §9 "③ 검색 요약·문서함 착수 시" 줄 삭제, 착수 순서 줄 맨 앞 항목 삭제), `docs/치과 AX - 앱 설계.md`(§3 요약 입력·검사·상태·캐시·규칙 id 셋 — 값은 `backend/search_summary.py`를 이름으로 가리킨다, §9 `/ask` 문서함 칩·URL·문서 카드 링크·요약 카드), `docs/superpowers/specs/2026-11-09-chat-agent-design.md`("브랜치 2" 절을 새 설계 문서를 가리키는 한 줄로), `docs/wireframe/index-v2.html`(`askSearch` 요약 카드 예시 문구를 "결과가 말하는 바 + 문장 끝 [n]"으로), `docs/치과 AX - AI 실험 기록.md`(요약 비용 실측 한 절 — 문제·결과 형식)

- [ ] **Step 1: 사람에게 묻고** 실제 키로 `make dev`를 띄워 기록 검색 3~5회(문서형·사례형·필터) 실행 → `SELECT task, cost_usd FROM ax_llm_calls WHERE task='search_summary'`로 회당 비용, 요약의 번호·문구를 눈으로 확인
- [ ] **Step 2: 위 문서 고치기**, 와이어프레임은 같은 아티팩트 링크(https://claude.ai/artifact/23GciX8FDbxtTeYektjBgV)에 다시 올린다
- [ ] **Step 3: 확인** — Run: `make test` / Expected: PASS(`tests/generator/test_doc_sync.py` 포함)
- [ ] **Step 4: 커밋** — `git add` 문서 파일 이름으로 → `git commit -m "docs: 기록 검색 요약·문서함 규칙, 요약 비용 실측"`
- [ ] **Step 5: PR 전** — `pj2-doc-tidy` 에이전트로 바뀐 코드·문서 점검(메모리 규칙), 리뷰는 사용자가 요청할 때만
