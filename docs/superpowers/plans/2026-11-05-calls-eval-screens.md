# 통화·② 판정 화면 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 통화 화면(`/calls` — 오늘·통화 기록·새 통화 추가·상세)과 AI 평가 화면(`/eval` — 판정·블라인드·정확도 표)을 만들고, 분류기 v2(할 일 종류)와 LLM 쌍 비교(블라인드 승률)를 붙인다.

**Architecture:** 백엔드는 조회·쓰기 로직을 화면 단위 모듈(`backend/calls.py`·`judge_queue.py`·`blind.py`·`metrics.py`)에 두고 `backend/api.py`는 연결·의존성·HTTP 변환만 한다. 분류기 v2는 요약 프롬프트 머리말 `todo_kinds: true`로 요약 스키마를 바꾸고, 일괄 실행은 기존 `classify_batch`를 쓴다. 프론트는 `frontend/components/calls/`·`components/eval/` 아래 화면 컴포넌트와 `lib/calls-api.ts`·`lib/eval-api.ts`.

**Tech Stack:** Python 3.13 · FastAPI · sqlite3(ORM 없음) · pytest · Next.js 16(App Router, cacheComponents) · TypeScript strict · Tailwind · shadcn/ui · Playwright(새로)

**Spec:** `docs/superpowers/specs/2026-11-05-calls-eval-screens-design.md`

## Global Constraints

- backend는 `ground_truth/`·`generator/`·`evaluation/`를 import하지 않는다(`tests/test_no_ground_truth_import.py`). 정답은 `data/`에 넣지 않는다
- 분류기·판정기 입력은 녹취록·방향뿐 — 새 통화의 전화번호·환자는 화면 표시에만
- 판정 풀(`in_eval_pool = 1`) 통화는 통화 화면(`/calls` API 전부)에 나오지 않는다. 블라인드 사람 투표·쌍 비교는 dev 블라인드 통화만: `in_blind_pool = 1 AND eval_sort_order <= ceil(eval_pool_size / 2)`(앱 설계 §6, `config/generator.yaml` `eval_pool_size`)
- "오늘"에 올라오는 생성 통화: 기준일(`shared/clinic.py` today) 전 **7일** 안(`started_at >= today - 7일`), 새로 추가한 통화는 날짜 무관
- 활성 버전: 분류 `ax_settings.active_prompt_version`(없으면 `"v1"`), 판정기 `ax_settings.prompt_versions.judge`, 블라인드 쌍 `ax_settings.blind_versions`
- 판정 큐 표본: 엇갈림 없는 대상 통화 중 `call_uid` 해시로 **5%**. 판정 키 매핑은 앱 설계 §9 표
- 화면 말: 영어 코드 대신 우리말(라벨 이름은 `shared/call_labels.py`의 `*_KO`), 방향 "걸려 온 전화 / 건 전화", 버튼 "＋ 새 통화 추가", 탭 "오늘"·"통화 기록", "판정"·"블라인드 비교"·"정확도 표"
- 배치 원칙: 하는 일·결과는 왼쪽, 녹취록은 오른쪽(상세 넓게 보기·판정·블라인드)
- 프론트 색·모서리는 `globals.css` 토큰만, 부품은 `components/ui`(필요하면 shadcn으로 그 폴더에 추가). 주석·독스트링은 한국어로 근거 문서 절
- 커밋 전 `make fmt` → `make lint` → `make test`. `git add`는 파일 이름으로. 커밋 형식 `<영역>: <무엇>` + 글머리 + 트레일러 두 줄
- LLM 실제 호출(분류기 v2, 쌍 비교)은 비용이 든다 — Task 3·7의 실행 단계만, 명령은 계획대로

## Review Focus

1. 녹취록이 빈 새 통화·방향 값이 이상한 요청 → 422, 통화를 만들지 않는다 (Task 6 테스트)
2. 분류·판정 호출이 실패한 새 통화 → 통화는 남고 상세가 `status: failed`를 보이며 다시 분류하면 ok가 된다 (Task 6 테스트)
3. 같은 할 일을 두 번 체크하거나 없는 번호를 체크 → 두 번째는 그대로(멱등), 없는 번호는 404 (Task 6 테스트)
4. 판정 큐가 비었을 때 / 블라인드 쌍의 한쪽 분류가 없을 때 → `next`가 `null`, 화면은 "남은 건 없음" (Task 5·7 테스트)
5. v1 할 일(문자열)만 있는 통화 → 화면이 깨지지 않고 전부 "안에서"로 (Task 2 `todo_items` 테스트, Task 4 상세 테스트)

---

### Task 1: 앱 테이블·dev 풀 조건·라벨 이름

**Files:**
- Modify: `shared/app_schema.sql`, `shared/clinic.py`, `shared/call_labels.py`
- Create: `backend/pools.py`
- Test: `tests/backend/test_pools.py`, `tests/backend/test_db.py`, `tests/generator/test_call_labels_sync.py`

**Interfaces:**
- Produces:
  - 테이블 `ax_pasted_calls(id INTEGER PK, call_uid TEXT UNIQUE NOT NULL, direction TEXT NOT NULL CHECK (direction IN ('inbound','outbound')), started_at TEXT NOT NULL, transcript TEXT NOT NULL, caller_phone TEXT, patient_id INTEGER, added_by TEXT NOT NULL, added_at TEXT NOT NULL)` — `call_uid`는 `p-0001`부터
  - `ax_judge_state(call_uid TEXT PK, state TEXT NOT NULL CHECK (state IN ('pending','skipped','done')), updated_at TEXT NOT NULL)`
  - `ax_todo_checks(classification_id INTEGER NOT NULL REFERENCES ax_call_classifications(id), item INTEGER NOT NULL, done_by TEXT NOT NULL, done_at TEXT NOT NULL, PRIMARY KEY (classification_id, item))`
  - `Clinic.eval_pool_size: int`(config `eval_pool_size`)
  - `backend/pools.py`: `dev_eval_sql(clinic, alias="a") -> str`(`a.in_eval_pool = 1 AND a.eval_sort_order <= N`), `dev_blind_sql(clinic, alias="a") -> str`(위 + `a.in_blind_pool = 1`)
  - `shared/call_labels.py`: `PURPOSE_KO`·`COUNTERPART_KO`·`URGENCY_KO`(`urgent` 급함·`normal` 보통·`low` 낮음)·`FLAG_KO`(`emergency` 응급·`complaint` 불만·`price_inquiry` 비용 문의·`wants_appt` 예약 희망·`appt_confirmed` 예약 잡힘·`has_todo` 할 일 있음) — PURPOSE·COUNTERPART 값은 `docs/wireframe/export.py`의 표 그대로

- [ ] **Step 1: 실패하는 테스트**
  - `test_pools.py::test_dev_pool_conditions` — source에 판정 풀 통화 4건(`eval_sort_order` 1~4, `eval_pool_size` 4로 만든 Clinic)과 블라인드 표시 2건(정렬 1·3)을 넣고 `dev_eval_sql`로 고른 uid가 정렬 1·2, `dev_blind_sql`이 정렬 1만
  - `test_db.py::test_new_tables_exist` — `init_schema` 뒤 세 테이블 존재, `ax_pasted_calls`에 `direction='sideways'` INSERT가 `sqlite3.IntegrityError`
  - `test_call_labels_sync.py::test_ko_names_cover_enums` — `set(PURPOSE_KO) == set(PURPOSES)`, 나머지 셋도 같음
- [ ] **Step 2:** `uv run pytest tests/backend/test_pools.py tests/backend/test_db.py tests/generator/test_call_labels_sync.py -q` → FAIL(import·테이블 없음)
- [ ] **Step 3:** 테이블·`Clinic` 필드·`pools.py`·`*_KO` 구현. `load_clinic`은 `cfg["eval_pool_size"]`를 읽는다
- [ ] **Step 4:** 같은 명령 → PASS, `make test` 전체 PASS
- [ ] **Step 5: Commit** `backend: 통화 화면 앱 테이블·dev 풀 조건·라벨 이름`

### Task 2: 분류기 v2 — 할 일 종류

**Files:**
- Create: `prompts/classify/v2/labels.md`(v1 `labels.md` 그대로 복사), `prompts/classify/v2/summary.md`
- Modify: `shared/call_labels.py`, `backend/prompts.py`, `backend/classify.py`
- Test: `tests/backend/test_prompts.py`, `tests/backend/test_classify.py`, `tests/shared/test_call_labels.py`

**Interfaces:**
- Consumes: `prompts.load("classify", v)`(기존)
- Produces:
  - `TODO_KINDS = ("contact", "internal")`
  - `SUMMARY_SCHEMA_KINDS` — `{"summary": str, "todo": [{"kind": enum TODO_KINDS, "text": str, "who": str, "after": int}]}`, 모든 칸 required, `additionalProperties: false`. `who`는 연락 상대(없으면 `""`), `after`는 먼저 끝내야 할 `internal` 항목의 0부터 번호(없으면 `-1`)
  - `todo_items(raw: list) -> list[dict]` — 문자열 항목은 `{"kind": "internal", "text": s, "who": "", "after": -1}`, dict는 그대로
  - `prompts.summary_kinds(version: str) -> bool` — `prompts/classify/<v>/summary.md` 머리말 `todo_kinds: true`
  - `classify.summary_request`가 `summary_kinds(version)`이면 `SUMMARY_SCHEMA_KINDS`를 쓴다. `classification_from`의 `todo`는 받은 목록 그대로 저장(v2는 dict 목록)

- [ ] **Step 1: 실패하는 테스트**
  - `test_call_labels.py::test_todo_items_reads_v1_strings` — `todo_items(["원장님께 전달"]) == [{"kind": "internal", "text": "원장님께 전달", "who": "", "after": -1}]`, dict 항목은 그대로
  - `test_prompts.py::test_summary_kinds_front_matter` — 임시 폴더 v9 `summary.md`에 머리말 `todo_kinds: true` → True, v1(저장소) → False
  - `test_classify.py::test_v2_summary_uses_kind_schema` — `Recording` 게이트웨이로 `cl.classify(..., "v2")` → summarize 호출의 `schema is SUMMARY_SCHEMA_KINDS`, v1이면 `SUMMARY_SCHEMA`; `prompts.load("classify","v2")["labels"] == prompts.load("classify","v1")["labels"]`
- [ ] **Step 2:** `uv run pytest tests/shared/test_call_labels.py tests/backend/test_prompts.py tests/backend/test_classify.py -q` → FAIL
- [ ] **Step 3:** 구현 + `prompts/classify/v2/summary.md` 작성 — v1 본문에서 todo 절만 바꾼다: 항목은 `kind`·`text`·`who`·`after`; 환자·보호자·외부에 전화·문자·회신하는 일은 `contact`(`who`에 상대 이름이나 "환자"), 원장님께 여쭙기·전달·일정·금액 확인·서류 준비는 `internal`; "원장님께 여쭤 확인해 환자에게 회신"처럼 섞인 일은 `internal` 하나 + `after`가 그 번호인 `contact` 하나로 쪼갠다; `text`는 "직원이" 없이 할 일만("원장님께 신경치료 후 통증 기간 여쭙기"). 나머지 규칙(통화 안에서 끝난 일 제외, 없으면 빈 목록)은 v1 그대로
- [ ] **Step 4:** 같은 명령 PASS, `make test` PASS
- [ ] **Step 5: Commit** `backend: 분류기 v2 — 할 일을 연락·안에서로`

### Task 3: 분류기 v2 실제 실행과 전환 (비용 약 $0.6)

**Files:**
- Modify: `docs/치과 AX - AI 실험 기록.md`(§12 새로)
- 데이터: `data/app.sqlite`(커밋 안 함)

- [ ] **Step 1:** `make classify V=v2` → `{'done': 1500, ...}`; failed가 있으면 같은 명령을 0이 될 때까지
- [ ] **Step 2:** `make disagreements A=v2 B=v2` → 행 수 기록
- [ ] **Step 3:** dev 채점 — 지난 브랜치처럼 `evaluation.eval.run(conn, reports_dir=REPORTS, app=None, split="dev", prompt_version=...)`로 분류 절만(CLI dev는 RAG까지 돌아 쓰지 않는다). v1·v2 목적 Strict·플래그 평균 차이 ≤ 2%p 확인. 넘으면 멈추고 사용자에게 보고
- [ ] **Step 4:** v2 할 일 점검 — 1,500건 모두 항목마다 `kind`가 있고, `contact`·`internal` 비율, `after` 쓴 항목 수를 센다
- [ ] **Step 5:** 설정 — `db.set_setting(conn, "active_prompt_version", "v2")`, `db.set_setting(conn, "blind_versions", ["v1", "v2"])` 후 commit
- [ ] **Step 6:** 실험 기록 §12(문제→원인→시도→결과→한계→비용) — v2 할 일 종류, 라벨 재호출 흔들림, 엇갈림 변화
- [ ] **Step 7: Commit** `docs: 분류기 v2 실행 결과`

### Task 4: 통화 조회 — 오늘·통화 기록·상세

**Files:**
- Create: `backend/calls.py`
- Test: `tests/backend/test_calls.py`(fixture: source에 생성 통화 — 7일 안 2건(그중 1건 판정 풀), 7일 밖 1건, 환자 1명 + app에 v2 분류·v2 판정·엇갈림 행)

**Interfaces:**
- Consumes: `pools.dev_*`(Task 1), `todo_items`(Task 2), `db.get_setting`
- Produces (`backend/calls.py`):
  - `TODAY_WINDOW_DAYS = 7`
  - `active_versions(conn) -> tuple[str, str]` — (분류, 판정기)
  - `today_board(conn, today: date) -> dict` — `{"internal": [Item], "contact": [Item], "done": [Item]}`. Item: `call_uid, classification_id, item, kind, text, who, after, at, name, chart_no, urgent, emergency, done_by, done_at`. 정렬: 응급 또는 급함 통화 먼저, 그다음 `at` 최근 순, 같은 통화 안은 항목 번호 순
  - `list_calls(conn, *, flt: str | None, q: str | None, before: str | None, limit: int = 50) -> dict` — `{"days": [{"date", "count", "calls": [Card]}], "next": str | None}`. `flt` ∈ `inbound`·`outbound`·`emergency`·`todo_left`·`disagree`, `q`는 이름·번호·차트번호 부분 일치, `before`는 그 시각보다 이전(다음 쪽), `next`는 마지막 카드 시각. Card: `call_uid, at, direction, name, chart_no, phone, purpose, urgency, emergency, disagree, todo_total, todo_left, summary, status`
  - `call_detail(conn, call_uid) -> dict | None` — `call`(uid·at·direction·name·chart_no·phone·duration_sec·transcript·source), `classification`(status·labels·summary·todo[Item 꼴 + done])·`judge`(labels 또는 None)·`disagree`(items 목록·is_core 또는 None)
  - 생성 통화와 `ax_pasted_calls`를 한 쿼리(UNION)로 합친다. 이름: 생성은 `emr_patients.name` 또는 `caller_name`, 새 통화는 연결 환자 이름 또는 None

- [ ] **Step 1: 실패하는 테스트**
  - `test_today_window_and_pool` — 7일 안·판정 풀 밖 통화의 할 일만 나오고, `contact`/`internal`이 칸에 맞게 갈림
  - `test_today_order_urgent_first`
  - `test_list_calls_groups_by_day_and_filters` — 날짜별 묶음·`count`, `flt="disagree"`, `q`=차트번호, 판정 풀 통화 없음, `limit=1`이면 `next`가 생기고 그 값으로 다음 쪽
  - `test_detail_has_judge_and_disagreement` / `test_detail_v1_strings_as_internal` / `test_detail_pool_call_is_none`
- [ ] **Step 2:** `uv run pytest tests/backend/test_calls.py -q` → FAIL
- [ ] **Step 3:** 구현(SQL 그대로, 활성 버전은 `active_versions`)
- [ ] **Step 4:** PASS, `make test` PASS
- [ ] **Step 5: Commit** `backend: 통화 조회 — 오늘·통화 기록·상세`

### Task 5: 판정 큐

**Files:**
- Create: `backend/judge_queue.py`
- Modify: `backend/classify_batch.py`(엇갈림 계산 뒤 `refresh` 호출)
- Test: `tests/backend/test_judge_queue.py`

**Interfaces:**
- Produces (`backend/judge_queue.py`):
  - `SAMPLE_SHARE = 0.05`, `is_sample(call_uid) -> bool`(sha256 앞 8자리 % 10000 < 500)
  - `refresh(conn, a, b) -> int` — 대상(판정 풀 밖 생성 + 새 통화, 활성 분류 ok, 엇갈림 행 있거나 표본) 중 상태 없는 것을 pending으로. 새로 넣은 수
  - `refresh_call(conn, call_uid) -> None`
  - `next_call(conn) -> dict | None` — 순서: pending 먼저 skipped 뒤 → 새 통화 → 핵심 엇갈림 → 나머지 엇갈림 → 표본, 같으면 `call_uid`. 반환 `call_uid, direction, at, transcript, left, done_today, done_total`(모델 라벨 없음)
  - `save(conn, call_uid, labels: dict, note: str | None, *, part, at) -> dict` — `ax_judgments` UPSERT, 상태 done, 반환 `{"classifier": labels, "judge": labels}`(저장 뒤 공개)
  - `skip(conn, call_uid, at) -> None`

- [ ] **Step 1: 실패하는 테스트** — `test_refresh_targets_disagreements_and_sample`(판정 풀 제외), `test_next_order`, `test_save_reveals_models_and_marks_done`, `test_skip_goes_last`, `test_empty_queue_is_none`(Review Focus 4)
- [ ] **Step 2:** FAIL
- [ ] **Step 3:** 구현. `classify_batch disagreements` 명령도 끝에 `refresh`
- [ ] **Step 4:** PASS, `make test` PASS
- [ ] **Step 5: Commit** `backend: 판정 큐`

### Task 6: 할 일 체크·새 통화 추가·다시 분류

**Files:**
- Modify: `backend/calls.py`, `backend/disagreements.py`
- Test: `tests/backend/test_calls.py`, `tests/backend/test_disagreements.py`

**Interfaces:**
- Consumes: `cl.classify`·`cl.judge`·`save_*`(기존), `judge_queue.refresh_call`(Task 5)
- Produces:
  - `disagreements.compute_call(conn, call_uid, a, b) -> bool` — 한 통화만 다시 계산(행이 생기면 True). `compute`와 같은 판정 함수를 쓴다
  - `calls.set_todo_done(conn, classification_id: int, item: int, *, part: str, at: str, done: bool) -> None` — 없는 항목이면 `KeyError`, 이미 같은 상태면 그대로
  - `calls.add_call(conn, gw, *, direction, transcript, phone, part, now: datetime, today: date) -> str` — 빈 녹취록·잘못된 방향은 `ValueError`. 시각 = `today` 날짜 + `now` 시각. 번호가 `emr_patients.phone`과 같으면 `patient_id`. 저장 뒤 `classify_one` 호출. uid 반환
  - `calls.classify_one(conn, gw, call_uid) -> None` — 활성 버전으로 분류·판정·저장, `compute_call`, `judge_queue.refresh_call`. 생성 통화와 새 통화 모두

- [ ] **Step 1: 실패하는 테스트**
  - `test_todo_check_moves_to_done_and_back` · `test_todo_check_idempotent_and_missing_item`(Review Focus 3)
  - `test_add_call_classifies_and_links_patient` — `FakeGateway`로 → `p-0001`, 분류·판정 `ok`, 시각 날짜가 `today`, 번호 맞는 환자 연결, 할 일이 `today_board`에 나옴
  - `test_add_call_rejects_empty`(Review Focus 1)
  - `test_add_call_failed_then_reclassify`(Review Focus 2) — 실패하는 게이트웨이 → 통화 남고 상세 `status == "failed"`, 정상 게이트웨이로 `classify_one` → ok
  - `test_disagreements.py::test_compute_call_matches_compute`
- [ ] **Step 2:** 해당 테스트 FAIL
- [ ] **Step 3:** 구현
- [ ] **Step 4:** PASS, `make test` PASS
- [ ] **Step 5: Commit** `backend: 할 일 체크·새 통화 추가·다시 분류`

### Task 7: 블라인드 — 사람 투표와 LLM 쌍 비교

**Files:**
- Create: `backend/blind.py`, `prompts/blind_judge/v1.md`
- Modify: `config/llm.yaml`(별칭 `blind_judge: [claude-haiku-5-5]`), `backend/classify_batch.py`(하위 명령 `blind`), `Makefile`(`blind-judge A= B= V=`), `shared/call_labels.py`(`PAIR_SCHEMA` — `{"choice": enum left·right·tie·both_wrong}`), `shared/app_schema.sql`(`ax_blind_votes` 주석)
- Test: `tests/backend/test_blind.py`

**Interfaces:**
- Produces (`backend/blind.py`):
  - `BLIND_SEED = "blind-v1"`, `first_on_left(transcript: str) -> bool` — `sha256(seed + transcript)` 짝수
  - `summary_text(summary: str, todo: list) -> str` — 요약 + "할 일: …"(없으면 "할 일: 없음")
  - `next_pair(conn, clinic) -> dict | None` — dev 블라인드 통화 중 `blind_versions` 두 분류가 모두 ok이고 투표 없는 것. 반환 `call_uid, transcript, direction, at, left{classification_id, text}, right{…}, left_count`(버전 이름 없음)
  - `save_vote(conn, left_id, right_id, choice, *, part, at) -> dict` — 반환 `{"left": version, "right": version}`
  - `pair_request(transcript, left_text, right_text, version) -> dict`(system·messages·schema) — 입력은 녹취록과 두 요약뿐
  - `combine(first: str, second_swapped: str) -> str` — 두 번째 답을 첫 배치로 되돌려 같으면 그 값, 다르면 `tie`
  - `run_pairs(conn, gw, clinic, a, b, version) -> dict` — dev 블라인드 통화마다 두 번 호출해 `ax_model_blind_votes` 저장(이미 있으면 건너뜀), `{"done", "failed"}`
- 프롬프트 `prompts/blind_judge/v1.md`: 녹취록을 기준으로 두 요약(할 일 포함) 중 통화를 더 정확하고 빠짐없이 옮긴 쪽을 고른다 — 녹취록에 없는 사실은 감점, 남은 할 일 누락은 감점, 길이·문체는 보지 않는다, 둘 다 사실이 틀리면 `both_wrong`, 차이가 없으면 `tie`

- [ ] **Step 1: 실패하는 테스트** — `test_next_pair_dev_only`(test 블라인드 통화 안 나옴), `test_next_pair_missing_side_is_none`(Review Focus 4), `test_left_right_stable`, `test_combine_tie_when_disagree`(`combine("left","right") == "tie"`, `combine("left","left") == "left"`), `test_pair_request_inputs_only`(messages에 녹취록·두 요약만, 판정기·골드 없음), `test_run_pairs_saves_and_skips_existing`(FakeGateway)
- [ ] **Step 2:** FAIL
- [ ] **Step 3:** 구현 + 프롬프트 + Makefile + 별칭
- [ ] **Step 4:** PASS, `make test` PASS
- [ ] **Step 5:** 실제 실행 `make blind-judge A=v1 B=v2 V=v1` → done 수(dev 블라인드 통화 수와 같음), 비용 기록
- [ ] **Step 6: Commit** `backend: 블라인드 사람 투표·LLM 쌍 비교`

### Task 8: 정확도 표 집계

**Files:**
- Create: `backend/metrics.py`
- Test: `tests/backend/test_metrics.py`

**Interfaces:**
- Produces:
  - `agreement(conn, clinic, versions: list[str], judge_version: str) -> dict` — `{version: {item: {"k", "n"}}}`, dev 판정 풀만, 양쪽 ok, 급함은 3값 그대로 비교(일치율 표는 정확도라 `differing_items`를 쓰지 않는다)
  - `blind_rates(conn, clinic, a, b) -> dict` — LLM: `{"b_better","a_better","tie","both_wrong","n","left","right"}`(좌우 선택 비율), 사람: 같은 꼴, `agree: {"k","n"}`(같은 쌍에서 LLM `choice`와 사람 `choice`가 같은 수)
  - `human_share(conn, a, b) -> dict` — `{"k": 엇갈림+표본 대상 수, "n": 판정 풀 밖 대상 수}`

- [ ] **Step 1: 실패하는 테스트** — 손으로 넣은 행으로 세 함수의 숫자를 확인(dev 풀 밖 행은 빠짐, 버전 방향 `b_better`가 b 쪽 선택)
- [ ] **Step 2:** FAIL → **Step 3:** 구현 → **Step 4:** PASS, `make test`
- [ ] **Step 5: Commit** `backend: 정확도 표·블라인드 승률 집계`

### Task 9: API

**Files:**
- Modify: `backend/api.py`
- Test: `tests/backend/test_api_calls.py`

**Interfaces:**
- Consumes: Task 4~8 함수
- Produces (모두 JSON):
  - 의존성 `get_gateway(conn)`(테스트는 `FakeGateway`로 덮어씀), `get_now()`(실제 시계, 테스트에서 덮어씀), 쓰기 연결 `_open_rw`(query_only 아님, `init_schema` 호출)
  - `GET /labels` → `{purpose, counterpart, urgency, flag}` 한국어 이름 표
  - `GET /calls/today` · `GET /calls?filter=&q=&before=` · `GET /calls/{uid}`(없으면 404)
  - `POST /calls` body `{direction, transcript, phone?, part}` → `{call_uid}`(ValueError → 422)
  - `POST /calls/{uid}/classify` · `POST /calls/{uid}/todos/{item}/done` body `{part, done: bool}`(KeyError → 404)
  - `GET /judge/next` → 통화 또는 `null` · `POST /judgments` body `{call_uid, labels, note?, part}` → 공개 라벨 · `POST /calls/{uid}/skip`
  - `GET /blind/next` → 쌍 또는 `null` · `POST /blind-votes` body `{left_id, right_id, choice, part}` → 버전 공개
  - `GET /metrics/accuracy` → `{agreement, blind, human_share, versions}`

- [ ] **Step 1: 실패하는 테스트** — 엔드포인트마다 정상 1건 + Review Focus 1·3·4의 HTTP 코드(TestClient, `source_paths` 확장 fixture)
- [ ] **Step 2:** FAIL → **Step 3:** 구현 → **Step 4:** PASS, `make test`
- [ ] **Step 5: Commit** `backend: 통화·판정·블라인드·정확도 API`

### Task 10: 통화 화면 — 오늘·상세

**Files:**
- Create: `frontend/app/(portal)/calls/page.tsx`, `frontend/lib/calls-api.ts`, `frontend/components/calls/calls-view.tsx`(탭·URL 상태 `?tab=&call=&wide=1`), `components/calls/today-board.tsx`, `components/calls/call-detail.tsx`, `components/calls/labels.ts`(`useLabels()` — `/labels` 한 번)
- Modify: `frontend/lib/api.ts`(`postJson<T>(path, body)` — `getJson`과 같은 오류 처리), `frontend/lib/nav.ts`(`BUILT`에 `/calls`)
- Add if needed: `frontend/components/ui/collapsible.tsx`(shadcn)

**동작(스펙 "통화 화면")**: "오늘" 두 칸 — 왼쪽 할 일·오른쪽 연락할 사람, 항목 체크(`postJson` → 다시 불러옴), 끝낸 일은 칸 아래 접힘, `after` ≥ 0이면 "← 왼쪽 ○○ 끝나면". 항목·카드를 누르면 `?call=`로 상세 패널. 패널 섹션 순서 머리 → 할 일 → 요약 → AI 두 개가 다르게 본 곳(엇갈림 있을 때만) → 분류 9항목(판정기 다른 값은 같은 줄 "판정기: ○○", 오류 색 토큰) → 녹취록 → 환자 정보(`PatientSummary` 재사용, 차트 없으면 "차트 없음"). 섹션은 펼친 채 시작·제목으로 접기. 패널 위 "넓게 보기"(`?wide=1`) — 왼쪽 정리·오른쪽 녹취록 말풍선, 두 칸 따로 스크롤, "패널로 돌아가기". `status: failed`면 "다시 분류" 버튼. 담당 파트는 헤더의 저장값(`ax.part`)을 `part`로.

- [ ] **Step 1:** 구현
- [ ] **Step 2:** `make lint` · `npm --prefix frontend run build` 통과
- [ ] **Step 3:** `make dev`로 띄워 확인 — 오늘 두 칸이 차고, 체크·되돌리기, 상세 패널·넓게 보기·접기, 박서준(c-0115) 통화의 엇갈림 빨간 줄이 보임
- [ ] **Step 4: Commit** `frontend: 통화 화면 — 오늘·상세`

### Task 11: 통화 화면 — 통화 기록·새 통화 추가

**Files:**
- Create: `components/calls/call-history.tsx`, `components/calls/add-call-dialog.tsx`
- Add if needed: `frontend/components/ui/dialog.tsx`, `textarea.tsx`(shadcn)

**동작**: 날짜별 카드("11월 30일 (일) · 14통"), 필터 칩 6개, 검색, 아래 "더 보기"(`next`). 새 통화 추가 창 — 방향(걸려 온 전화 기본)·전화번호(선택)·통화 내용·"추가하기" → 끝나면 그 통화 상세를 연다, 도는 동안 버튼 막고 "분류하는 중".

- [ ] **Step 1:** 구현 → **Step 2:** lint·build → **Step 3:** `make dev`에서 필터·검색·더 보기, 새 통화 추가(실제 호출 1회)가 상세·"오늘"에 나옴
- [ ] **Step 4: Commit** `frontend: 통화 기록·새 통화 추가`

### Task 12: AI 평가 화면 — 판정·블라인드·정확도 표

**Files:**
- Create: `frontend/app/(portal)/eval/page.tsx`, `frontend/lib/eval-api.ts`, `components/eval/eval-view.tsx`(탭 `?tab=judge|blind|acc`), `components/eval/judge-tab.tsx`, `components/eval/blind-tab.tsx`, `components/eval/accuracy-tab.tsx`
- Modify: `frontend/lib/nav.ts`(`COMING_SOON`에서 `eval` 빼기)

**동작**:
- 판정: 왼쪽 9항목 선택지(항목마다 버튼 전부 펼침, 키 매핑 앱 설계 §9 — 목적 2자리·나머지 1자리·입력 끝나면 다음 항목·↑↓ 이동·`/` 메모·Enter 저장·다음), 오른쪽 녹취록. 위 진행 한 줄, 아래 단축키 안내. 저장 뒤 모델 라벨(분류기·판정기)을 선택지 옆에 보이고 Enter로 다음 건. 건너뛰기. 큐가 비면 "남은 건 없음"
- 블라인드: 왼쪽 요약 1·2(할 일 포함)와 버튼 `1 / 2 / 3 비슷 / 4 둘 다 틀림`, 오른쪽 녹취록. 고른 뒤 버전 공개, Enter 다음. 쌍이 없으면 "남은 비교 없음"
- 정확도 표: 항목별 일치율 막대(버전별 열), 블라인드 승률(LLM·사람)·좌우 선택 비율·사람과 LLM 일치, 사람이 보는 몫

- [ ] **Step 1:** 구현 → **Step 2:** lint·build → **Step 3:** `make dev`에서 키보드만으로 판정 3건·블라인드 2건 저장 확인(그 행은 실데이터에 남으므로 확인 뒤 `ax_judgments`·`ax_blind_votes`에서 지운다)
- [ ] **Step 4: Commit** `frontend: AI 평가 — 판정·블라인드·정확도 표`

### Task 13: Playwright — 판정 10회·블라인드 5회

**Files:**
- Modify: `frontend/package.json`(devDependency `@playwright/test`, 스크립트 `e2e`), `backend/api.py`(`get_db_paths`가 환경 변수 `AX_DATA_DIR`이 있으면 그 폴더), `Makefile`(`e2e`)
- Create: `frontend/playwright.config.ts`, `frontend/e2e/eval.spec.ts`, `frontend/e2e/global-setup.ts`(`data/`를 임시 폴더로 복사하고 그 경로로 API 서버를 띄운다)

- [ ] **Step 1:** 테스트 작성 — 판정: 키 시퀀스(`01` `1` `2` `1` `1` `1` `1` `1` `1` Enter Enter)를 10번 → 복사본 `ax_judgments` 행 +10. 블라인드: `1` Enter를 5번 → `ax_blind_votes` +5
- [ ] **Step 2:** `make e2e` → 처음엔 FAIL(설정 전) 확인 후 설정 → PASS
- [ ] **Step 3:** `tests/backend/test_api.py`에 `AX_DATA_DIR` 경로 테스트 1개
- [ ] **Step 4:** CI에는 넣지 않는다(실데이터 필요) — Makefile 주석에 적는다
- [ ] **Step 5: Commit** `frontend: 판정·블라인드 Playwright`

### Task 14: 문서

**Files:**
- Modify: `docs/치과 AX - 기능 정의.md`(§4 ① 화면·API·완료 기준, §4 ② 화면·API·블라인드 사람 투표, §9 — 끝난 항목, "자동 개선 탭"·작업 API·fixture는 해당 착수 항목에), `docs/치과 AX - 앱 설계.md`(§1 테이블 `ax_pasted_calls`·`ax_judge_state`·할 일 체크·`ax_call_followups` 시점, §6 판정 큐 표본 `judge_queue.SAMPLE_SHARE`·"오늘" 범위 `calls.TODAY_WINDOW_DAYS`, §9 라우트·화면 공통 원칙(왼쪽 결과·오른쪽 근거)), `docs/치과 AX - 평가 설계.md`(§2.4 사람 투표 규칙), `CLAUDE.md`(명령에 `make blind-judge`·`make e2e`)

- [ ] **Step 1:** 고친 뒤 `uv run pytest tests/generator/test_doc_sync.py -q` PASS
- [ ] **Step 2: Commit** `docs: 통화·판정 화면 규칙 반영`
