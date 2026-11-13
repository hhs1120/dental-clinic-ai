# ⑤-3 근거 찾기 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 조정 건을 펼치면 그 사유의 청구 기준 절 전문과(서류 사유 건은) AI가 고른 차트 문장을 보이고, 화면 맨 위 검색칸으로 청구 기준 절을 찾으며, 코드 없이 묻는 질문 평가셋(AI 초안 → 브라우저 검토)으로 Hit@5·MRR을 잰다.

**Architecture:** 2b(`backend/claim_omission.py`)의 저장·재사용·Batch 흐름을 공용 모듈 `backend/claim_store.py`로 옮기고 2b와 새 서류 사유 차트 문장(`backend/claim_evidence.py`)이 함께 쓴다. 검색은 ③ `Searcher`에 `doc_type` 필터를 더해 청구 기준 절만(`backend/claim_search.py`). 평가셋은 `evaluation/claim_qa.py`가 초안·베끼기 검사·로컬 검토 페이지·채점을 맡고, 승인 JSON은 `make data`가 `truth_claim_qa`로 적재한다.

**Tech Stack:** Python(sqlite3, `shared/llm.py` 게이트웨이 — 차트 문장 별칭 `chat`, 질문 초안 `gen`), 표준 라이브러리 `http.server`(검토 페이지), FastAPI, Next.js, pytest, Playwright.

**Spec:** `docs/superpowers/specs/2026-11-10-claim-review-design.md` §3

## Global Constraints

- `backend/`는 `ground_truth/`·`generator/`·`evaluation/`을 참조하지 않는다. 정답(`truth_claim_qa`)은 앱으로 가지 않는다 — 검토 페이지는 `evaluation/` 안에서만
- 차트 문장 입력: 시술·치아·차트 서술 + 그 사유의 기준 절 본문. 청구 금액·조정 결과·판단 기록은 넣지 않는다. 출력 인용 0~3개, 인용은 차트에 실제로 있어야(2a `claim_check.found`)
- 차트 문장 범위: `scope "app"` = 재심 신청 기한이 남은 서류 사유(`adjust_note_types` = `documentation`) 조정·불능, `scope "all"` = 서류 사유 조정·불능 전부
- 화면 문구: 조항 카드 머리 `문서 제목 › 절 제목`, 차트 문장 머리 `차트에서 옮겨 적을 문장(AI)`, 빈 답 `차트에 옮겨 적을 문장 없음`, 검색칸 placeholder `청구 기준 찾기 — 예: 치석제거 다음 달로 넘어가면 왜 깎여?`, 결과 상자 제목 `찾은 조항`
- 검색 대상: `doc_type` claim_standard 청크 중 절 청크만(문서 머리 청크 — 첫 줄에 ` > `가 없는 것 — 빼기), 결과 5개, AI 답 문장 없음
- 평가셋: 사유 11개 × 2문항, 정답 = 절 제목(`adjust_note_headings`의 값), 맞음은 절 단위, dev/test는 사유 키 정렬 순서로 번갈아(짝수 번째 dev)
- 베끼기 경고(평가 설계 §3.1): 정답 절 본문과 연속 4어절 겹침, 절 제목, 사유 코드(R01 등)·상세 사유 문구 — 초안은 경고가 없을 때만 검토에 올리고, 사람이 고친 문장은 경고를 보이되 막지 않는다
- 과금 명령은 실행 전에 건수·예상 금액을 보이고 승인받는다. test 채점은 사람만
- 주석·독스트링 한국어, 근거 문서 절. 색은 `globals.css` 토큰, 부품은 `components/ui/`

## Review Focus

1. 색인이 없을 때 검색칸 — 503 안내 한 줄, 화면의 나머지(목록·펼침)는 그대로
2. 서류 사유 건인데 차트 문장 결과가 없음(아직 안 돌림) vs 빈 답 — 앞은 아무 줄도 안 보이고, 뒤는 "차트에 옮겨 적을 문장 없음"
3. 2b를 공용 모듈로 옮긴 뒤 저장된 `ax_claim_omissions` 행이 그대로 다시 쓰인다(해시 계산이 바뀌지 않아 재호출 0)
4. 갱신 버튼 — 삭감 대응 건수는 2a 할 일 ∪ 서류 사유 차트 문장 할 일(같은 건 두 번 세지 않음)
5. 검토 페이지에서 고친 문장에 베끼기 경고가 있어도 저장은 되고 JSON에 경고가 남는다

---

### Task 1: 공용 저장·실행 모듈로 2b 옮기기 (동작 그대로)

**Files:**
- Create: `backend/claim_store.py`
- Modify: `backend/claim_omission.py`
- Test: `tests/backend/test_claim_store.py`, 기존 `tests/backend/test_claim_omission.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class Kind: table: str; task: str; alias: str; version: str; facts: Callable[[dict], dict]; schema: Callable[[sqlite3.Connection], dict]; check: Callable[[dict, dict], bool]; empty: dict; text_key: str = "note"` — 서술 칸(비면 부르지 않고 `empty` 저장)
  - `save(conn, kind, claim_id, facts, h, result, model) -> None`, `plan(conn, kind, rows, model) -> (todo, skipped, empty)`, `todo_ids(conn, kind, rows, model) -> set[int]`, `build(conn, gw, kind, rows, *, use_batch, poll_seconds=30) -> dict`(2b `build`와 같은 키), `saved(conn, kind, claim_ids=None) -> dict[int, sqlite3.Row]`(claim_id, facts, result, created_at, prompt_version)
  - `claim_omission.KIND`, `claim_omission.build/todo_ids/results`는 시그니처 그대로(안에서 `claim_store` 호출)
- [ ] **Step 1: Write the failing test** — `tests/backend/test_claim_store.py`: 작은 가짜 `Kind`(표는 `ax_claim_omissions`를 빌려 씀)로 `test_build_saves_reuses_and_retries`(첫 호출 1·두 번째 0·두 번 실패면 result NULL), `test_empty_text_saves_empty_result`, `test_hash_matches_claim_check`(`claim_store`의 해시가 `claim_check._hash`와 같다 — Review Focus 3)
- [ ] **Step 2: Run** `uv run pytest tests/backend/test_claim_store.py -q` — Expected: FAIL(모듈 없음)
- [ ] **Step 3: Implement** — `claim_omission.py`의 `_save`·`_plan`·`build`·`todo_ids` 본문을 `claim_store`로 옮기고 2b는 `KIND`로 부른다
- [ ] **Step 4: Run** `uv run pytest tests/backend/test_claim_store.py tests/backend/test_claim_omission.py tests/backend/test_claims.py tests/backend/test_claim_check.py -q` — Expected: PASS
- [ ] **Step 5: Commit** · `backend: ⑤ AI 점검 저장·실행을 claim_store로 — 2b 동작 그대로`

### Task 2: 서류 사유 차트 문장 (`backend/claim_evidence.py`)

**Files:**
- Create: `backend/claim_evidence.py`, `prompts/claim_evidence/v1.md`
- Modify: `shared/app_schema.sql`(`ax_claim_evidence` — 칸은 `ax_claim_omissions`와 같음), `Makefile`(`claim-evidence`)
- Test: `tests/backend/test_claim_evidence.py`

**Interfaces:**
- Consumes: `claim_store.Kind/build/todo_ids/saved`, `claim_check.found`, `claim_check.sections`, `backend.prep.master_values`
- Produces:
  - `TASK, ALIAS, PROMPT_VERSION = "claim_evidence", "chat", "v1"`, `KIND`
  - `SCHEMA = {quotes: array[string], maxItems 3}`
  - `evidence_facts(row, mv, secs) -> {procedure, tooth, note, standard: {heading, body}}`
  - `check_result(facts, r) -> bool` — 모든 인용이 `found(q, note)`
  - `targets(conn, today, *, scope) -> list[dict]`(행: `claim_id, procedure_code, procedure_name, tooth_no, note, adjust_note, status, reviewed_at`)
  - `results(conn, claim_ids=None) -> dict[int, {quotes: list[str] | None, valid, at, prompt_version}]`
  - `todo_ids(conn, today, model=None) -> set[int]`, `main` — `python -m backend.claim_evidence run [--scope app|all] [--no-batch]`, Makefile `claim-evidence [SCOPE=all] [NO_BATCH=1]`
- [ ] **Step 1: Write the failing tests** — `test_facts_hide_amount_and_judgment`(facts 키가 `{procedure, tooth, note, standard}`뿐, 조정 문구·금액 없음), `test_targets_documentation_only`(서류 사유 건만, app은 기한 남은 것만, 요건·상병 사유는 빠짐), `test_build_and_results`(대본 답 인용 2개 → `quotes` 2개), `test_quote_not_in_chart_retried_then_none`, `test_empty_answer_kept`(빈 목록 답은 통과 — `quotes == []`)
- [ ] **Step 2: Run** `uv run pytest tests/backend/test_claim_evidence.py -q` — Expected: FAIL
- [ ] **Step 3: Implement** — 프롬프트 v1 요지: "청구 기준 절이 진료기록·내역설명에 적으라고 요구하는 내용에 해당하는 문장을 차트에서 글자 그대로 0~3개 고른다, 해당 문장이 없으면 빈 목록, 지어내지 않는다"
- [ ] **Step 4: Run** 같은 명령 — Expected: PASS
- [ ] **Step 5: Commit** · `backend: ⑤-3 서류 사유 차트 문장 — ax_claim_evidence·make claim-evidence`

### Task 3: 조정 건 API — 조항 카드·차트 문장·갱신 버튼

**Files:**
- Modify: `backend/claims.py`(`adjusted` 행, `_ai_pending`), `backend/api.py`(`_run_claim_check_job`·`claim_check_run`)
- Test: `tests/backend/test_claims.py`, `tests/backend/test_claim_check.py`

**Interfaces:**
- Produces: `adjusted` 행에 `standard: {doc_title, heading, body} | None`(사유 → 절, 본문 없으면 None), `chart_quotes: list[str] | None`(서류 사유이고 통과한 결과가 있을 때만 목록, 아니면 None). `_ai_pending.adjust` = (2a 할 일 ∪ 차트 문장 할 일) 중 대기 아닌 줄. 작업은 2a → 2b → 차트 문장, `total`·`done`은 세 대상의 합집합
- [ ] **Step 1: Write the failing tests** — `test_adjusted_standard_card`(모든 유형 행에 `doc_title`·`heading`·`body`, 머리 문서는 그 절이 든 문서), `test_adjusted_chart_quotes_documentation_only`(서류 사유 + 결과 → 목록, 빈 답 → `[]`, 결과 없음 → None, 요건 사유 → None — Review Focus 2), `test_pending_adjust_union`(2a·차트 문장 둘 다 할 일인 건 없음 — 요건 건 1 + 서류 건 1 → `adjust == 2`), `test_job_runs_evidence`, `test_every_reason_card_and_rule`(상세 사유 11개 모두 카드 절 제목 = `CLAIM_HEADINGS[key]`, 헷갈리기 쉬운 쌍 — '내역설명 미기재'는 보완 후 재청구·채울 항목, '상병 착오'는 삭감 수용만 — 설계 §3 채점)
- [ ] **Step 2: Run** `uv run pytest tests/backend/test_claims.py tests/backend/test_claim_check.py -q` — Expected: 새 테스트 FAIL
- [ ] **Step 3: Implement** — 절 본문·문서 제목은 `ax_documents` claim_standard에서 한 번 읽는다(`claim_check.sections`를 넓혀 제목도 돌려주거나 같은 꼴의 함수)
- [ ] **Step 4: Run** 같은 명령 — Expected: PASS
- [ ] **Step 5: Commit** · `backend: ⑤-3 조정 건에 기준 조항 카드·서류 사유 차트 문장, 갱신 작업에 포함`

### Task 4: 청구 기준 검색

**Files:**
- Create: `backend/claim_search.py`
- Modify: `backend/rag_search.py`(`Searcher.search`·`_where`에 `doc_type=None`), `backend/api.py`(`GET /claims/standards?q=`), `backend/eval_hooks.py`(`App.claim_search`)
- Test: `tests/backend/test_claim_search.py`

**Interfaces:**
- Produces:
  - `Searcher.search(..., doc_type: str | None = None)` — 주면 `c.doc_type = ?` 조건
  - `claim_search.search(searcher, query, k=5) -> list[{chunk_id, doc_title, heading, text}]` — `source_types=["document"]`, `doc_type="claim_standard"`, `top_k=CANDIDATES`로 받은 뒤 머리 청크를 빼고 k개. `heading`은 청크 첫 줄 `제목 > 절`의 뒤, `text`는 첫 줄을 뺀 본문
  - `GET /claims/standards?q=` → `{"results": [...]}`, 색인 없으면 503 `NO_INDEX`(GET /search와 같은 처리·Voyage 사용 기록)
  - `App.claim_search(question, condition=None) -> list[str]` — 절 제목 순위(평가용, `drain_usage`)
- [ ] **Step 1: Write the failing tests** — `test_only_claim_sections`(다른 문서·머리 청크가 섞인 색인에서 결과가 claim_standard 절뿐, 5개 이하), `test_heading_parsed`, `test_api_no_index_503`(Review Focus 1), `test_api_results`
- [ ] **Step 2: Run** `uv run pytest tests/backend/test_claim_search.py -q` — Expected: FAIL
- [ ] **Step 3: Implement**(가짜 임베더는 `backend.embeddings.FakeEmbedder`, 색인 꼴은 `tests/backend/test_rag_search.py`)
- [ ] **Step 4: Run** 같은 명령 + `uv run pytest tests/backend/test_rag_search.py -q` — Expected: PASS
- [ ] **Step 5: Commit** · `backend: ⑤-3 청구 기준 찾기 — 청구 기준 절만 검색, GET /claims/standards`

### Task 5: 화면 — 조항 카드·차트 문장·검색칸

**Files:**
- Create: `frontend/components/claims/standard-search.tsx`
- Modify: `frontend/lib/claims-api.ts`, `frontend/components/claims/adjust-table.tsx`(펼친 곳 ①·②), 청구 점검 화면 페이지(검색칸을 탭 위에), `frontend/e2e/claims.spec.ts`
- [ ] **Step 1: e2e 먼저** — 가짜 응답으로: 펼친 곳 ①에 `공통 분야 청구 기준 › 내역설명 기재`와 본문, 서류 사유 행 ②에 `차트에서 옮겨 적을 문장(AI)`과 인용, 빈 답 행에 `차트에 옮겨 적을 문장 없음`, 검색칸에 입력 → `찾은 조항` 상자에 결과, 닫기 버튼으로 닫힘, 503이면 안내 한 줄
- [ ] **Step 2: Run** `npm --prefix frontend run -s e2e -- e2e/claims.spec.ts` — Expected: 새 테스트 FAIL
- [ ] **Step 3: Implement** — 조항 본문은 줄바꿈 유지(`whitespace-pre-line`), 차트 문장 상자는 AI 표시 토큰(2a 딱지와 같은 variant)
- [ ] **Step 4: Run** 같은 명령 + `make lint` — Expected: PASS
- [ ] **Step 5: Commit** · `frontend: ⑤-3 조항 카드·서류 사유 차트 문장·청구 기준 찾기`

### Task 6: 정답 표 `truth_claim_qa`와 적재

**Files:**
- Modify: `ground_truth/schema.sql`, `generator/paths.py`(`claim_qa`: `ground_truth/claim_qa.json`), `generator/build.py`(있으면 적재), `generator/__main__.py`(경로 넘김)
- Test: `tests/generator/test_claim_qa_load.py`

**Interfaces:**
- Produces: `CREATE TABLE truth_claim_qa (id TEXT PRIMARY KEY, reason TEXT NOT NULL, question TEXT NOT NULL, answer_heading TEXT NOT NULL, warnings TEXT NOT NULL, split TEXT NOT NULL CHECK (split IN ('dev','test')))`; `load_claim_qa(conn, rows: list[dict]) -> int` — JSON 행(`id, reason, question, answer_heading, warnings[], split`)을 넣고 수를 돌려줌, `answer_heading`이 `CLAIM_HEADINGS` 값이 아니면 ValueError
- [ ] **Step 1: Write the failing tests** — `test_load_rows`, `test_unknown_heading_rejected`, `test_missing_file_skips`(build가 파일 없으면 표만 빈 채)
- [ ] **Step 2: Run** `uv run pytest tests/generator/test_claim_qa_load.py -q` — Expected: FAIL
- [ ] **Step 3: Implement**(적재는 생성 마지막, `emr_*`·LLM 캐시는 건드리지 않는다)
- [ ] **Step 4: Run** 같은 명령 + `uv run pytest tests/generator/test_doc_sync.py -q` — Expected: PASS
- [ ] **Step 5: Commit** · `generator: truth_claim_qa 표와 승인 JSON 적재`

### Task 7: 질문 초안·베끼기 검사·분할

**Files:**
- Create: `evaluation/claim_qa.py`, `prompts/gen_claim_qa.md`
- Test: `tests/evaluation/test_claim_qa.py`

**Interfaces:**
- Produces:
  - `REASONS -> list[dict]` — `generator.doc_catalog.CLAIM_HEADINGS`·`generator.master.ADJUST_REASONS`에서 `{reason, heading, note, body}`(body는 `generator/doc_catalog`가 만든 문서에서 그 절 — 또는 source DB `ax_documents`)
  - `split_of(reason: str) -> "dev"|"test"` — 사유 키 정렬 순서 짝수 번째 dev
  - `copy_warnings(question: str, reason: dict) -> list[str]` — 연속 4어절 겹침(본문), 절 제목 포함, 사유 코드·상세 사유 문구 포함
  - `draft(gw, path=DRAFTS, *, per_reason=2, attempts=3) -> dict` — 별칭 `gen`, 사유마다 경고 없는 문항이 2개 될 때까지 최대 3번, 이어 돎(이미 2개 있으면 건너뜀), `evaluation/claim_qa_drafts.jsonl`에 `{id: "cq-R01-1", reason, question}`
- [ ] **Step 1: Write the failing tests** — `test_split_alternates`(11개 중 dev 6), `test_copy_warnings`(본문 4어절 그대로 → 경고, 절 제목 → 경고, "R02" → 경고, 직원 말투 질문 → `[]`), `test_draft_keeps_clean_and_resumes`(가짜 게이트웨이: 첫 답에 베끼기 문항 섞임 → 깨끗한 것만 저장, 다시 부르면 호출 0)
- [ ] **Step 2: Run** `uv run pytest tests/evaluation/test_claim_qa.py -q` — Expected: FAIL
- [ ] **Step 3: Implement** — 프롬프트 요지: 치과 직원이 심사 결과를 보고 묻는 말투, 사유 코드·절 제목·기준 문장을 옮기지 않음, 상황을 구체적으로(예: "치석제거 다음 달로 넘어가면 왜 깎여?")
- [ ] **Step 4: Run** 같은 명령 — Expected: PASS
- [ ] **Step 5: Commit** · `eval: ⑤-3 청구 기준 질문 초안·베끼기 검사`

### Task 8: 검토 페이지

**Files:**
- Modify: `evaluation/claim_qa.py`(`review` 서버·`finalize`)
- Create: `evaluation/claim_qa_review.html`
- Test: `tests/evaluation/test_claim_qa.py`

**Interfaces:**
- Produces:
  - `decide(drafts, decisions: list[{id, status: "approved"|"edited"|"dropped", question}]) -> list[dict]` — 승인·수정 문항만, 수정은 `copy_warnings`를 다시 돌려 `warnings`에 남김(막지 않음), `answer_heading`·`split`을 붙여 정답 JSON 행
  - `review(port=0)` — `http.server`로 `127.0.0.1`에 HTML 한 장 + `GET /drafts`(문항·사유 문구·절 제목·본문) + `POST /check`(문장 → 경고) + `POST /save`(결정 → `ground_truth/claim_qa.json` 쓰고 요약), 주소를 출력하고 브라우저를 연다
  - `python -m evaluation.claim_qa draft`(과금, 이어 돎) · `review`(사람) — Makefile `claim-qa-draft`·`claim-qa-review`
- [ ] **Step 1: Write the failing tests** — `test_decide_edit_rechecks`(고친 문장에 절 제목 → 행은 남고 `warnings`에 경고 — Review Focus 5), `test_decide_drops`, `test_review_server_roundtrip`(포트 0으로 띄워 `GET /drafts`·`POST /save` → 파일 내용)
- [ ] **Step 2: Run** `uv run pytest tests/evaluation/test_claim_qa.py -q` — Expected: 새 테스트 FAIL
- [ ] **Step 3: Implement** — HTML은 단독(외부 스크립트 없음), 문항마다 사유·절 제목·본문 접기, 질문 입력칸, [승인][버림], 고치면 `/check` 경고 표시, 맨 아래 [저장]
- [ ] **Step 4: Run** 같은 명령 — Expected: PASS
- [ ] **Step 5: Commit** · `eval: ⑤-3 질문 검토 페이지(로컬, 앱과 따로)`

### Task 9: 채점 — Hit@5·MRR·차트 문장 실재율, make eval

**Files:**
- Modify: `evaluation/claim_qa.py`(`score`), `evaluation/eval.py`, `Makefile`
- Create: `evaluation/claim_evidence.py`(`score`)
- Test: `tests/evaluation/test_claim_qa.py`, `tests/evaluation/test_claim_evidence_eval.py`

**Interfaces:**
- Produces:
  - `claim_qa.score(conn, app, split) -> {n, candidates, hit5, mrr, by_reason, rows(dev만)}` — `app.claim_search`의 절 제목 순위에서 `answer_heading`의 첫 순위(5 안), `candidates` = 청구 기준 절 수
  - `claim_evidence.score(conn) -> {n, checked, valid, answered, empty}` — 서류 사유 전부, 지금 버전만
  - `eval.py` dev: `truth_claim_qa`가 있고 색인이 있으면 ⑤-3 검색, 저장된 차트 문장이 있으면 실재율 — `_render_claim_basis`; test는 `python -m evaluation.claim_qa score --split test`(사람, 버전 없음 — 실행마다 `reports/claim_qa_runs.jsonl`에 남김)
- [ ] **Step 1: Write the failing tests** — `test_score_section_level`(같은 문서 다른 절이 1위면 맞음 아님 — 문서 단위가 아니다), `test_score_mrr`, `test_evidence_score`, `test_render`
- [ ] **Step 2: Run** `uv run pytest tests/evaluation/test_claim_qa.py tests/evaluation/test_claim_evidence_eval.py -q` — Expected: FAIL
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run** 같은 명령 + `uv run pytest tests/evaluation -q` — Expected: PASS
- [ ] **Step 5: Commit** · `eval: ⑤-3 채점 — 절 단위 Hit@5·MRR, 서류 사유 차트 문장 실재율`

### Task 10: 문서

**Files:** 평가 설계 §5 ⑤-3·§6 표(`truth_claim_qa` 칸), 앱 설계(⑤ 문단 — `ax_claim_evidence`·`GET /claims/standards`, §9 `/claims` 행), 기능 정의 §5 ⑤-3·§9(남은 것 → test 채점), 가상 데이터 생성 설계(정답 JSON 적재 목록에 `claim_qa.json`), `CLAUDE.md` 명령 ⑤ 줄
- [ ] **Step 1:** 고친 뒤 `uv run pytest tests/generator/test_doc_sync.py -q` — Expected: PASS
- [ ] **Step 2: Commit** 설계·계획과 함께 · `docs: ⑤-3 근거 찾기 — 조항 카드·차트 문장·청구 기준 찾기·질문 평가셋`

### Task 11: 실행(과금·사람 — 단계마다 승인)

- [ ] **Step 1:** `make claim-evidence SCOPE=all` — 서류 사유 조정·불능 전부(약 180건) Batch, 예상 ~$0.4
- [ ] **Step 2:** `make claim-qa-draft` — 사유 11개 × 최대 3번, 예상 ~$0.05. 그다음 `make claim-qa-review`로 사용자가 브라우저에서 검토 → `ground_truth/claim_qa.json` → `make data`(LLM 작업 0건인지 먼저 확인)
- [ ] **Step 3:** `make eval`로 dev Hit@5·MRR(질문 임베딩·리랭커 호출 — 금액 작음)과 차트 문장 실재율
- [ ] **Step 4:** AI 실험 기록 §29, 커밋
