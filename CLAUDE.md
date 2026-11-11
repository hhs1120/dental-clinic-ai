# dental-clinic-ai (치과 AX)

동네 치과 AX 포트폴리오 프로젝트. 가상 데이터(미니 EMR + 통화·문서·문자 + 정답)로 통화 분류·판정과 자동 개선·RAG 챗·진료 준비표와 업무 에이전트를 구현한다.

## 설계 문서
규칙·절차의 원본 — 코드와 어긋나면 문서를 먼저 확인한다. 단 **값**(숫자·enum·컬럼·인덱스)은 `generator/master.py`·`shared/source_schema.sql`·`shared/app_schema.sql`·`ground_truth/schema.sql`·`config/generator.yaml`이 원본이고(verify 허용치·기준값은 `generator/verify/`, 채점 상수는 `evaluation/metrics.py`, 검색 상수는 `backend/rag_index.py`·`backend/rag_search.py`) 문서는 이름으로 가리킨다.

- `docs/치과 AX - 기능 정의.md` — 무엇을 만드나, `make gate` 기준
- `docs/치과 AX - 가상 데이터 생성 설계.md` — `emr_*`·`fin_*`·생성기 적재 `ax_*` 스키마, 생성 규칙, `make verify` 검사 목록 (생성기가 읽음)
- `docs/치과 AX - 앱 설계.md` — 앱 `ax_*` 테이블, 도구 시그니처, 고정 쿼리, 앱 기술 선택, 화면 (backend·frontend가 읽음)
- `docs/치과 AX - 평가 설계.md` — 지표, 채점, 정답 저장소(`truth_*`) 테이블 정의
- `docs/치과 AX - 전환의 의미.md` — 전환 전후 업무 변화 (배경, 구현 스펙 아님)
- `docs/치과 AX - AI 실험 기록.md` — AI 품질 실험의 문제·원인·시도·수치·한계 (구현 스펙 아님, 설계 문서에 경위를 넣지 않는 대신 여기에)
- `docs/치과 AX - 프로젝트 소개.md` — 비개발자용 한 장 소개 (구현 스펙 아님)
- `docs/치과 AX - 개발자 소개.md` — 팀원 모집용: 기술 스택·데이터 규모·기능별 AI·독립 과제·포트폴리오에 남는 것 (구현 스펙 아님)
- `docs/wireframe/` — 앱 화면 와이어프레임(클릭 HTML). 프론트 화면의 레이아웃·동작 기준은 앱 설계 §9(와이어프레임 `index-v2.html`과 그 예외), `index.html`은 비교용 이전 판. 데이터는 `make wireframe`

## 폴더
- `generator/` 데이터 생성기 + verify (테스트는 `tests/generator/`, 게이트웨이는 `tests/shared/`, 평가는 `tests/evaluation/`, 앱 테스트는 `tests/backend/`에) · `shared/` 스키마·LLM 게이트웨이(`shared/llm.py`, 설정 `config/llm.yaml`)·앱 기준 날짜(`shared/clinic.py`) · `evaluation/` eval.py·튜닝 비교 tune.py·프롬프트 관문 prompt_gate.py·(착수 때) 자동 개선·시나리오 DB
- `ground_truth/` 정답(truth.sqlite + JSON) · `data/` source.sqlite(생성기 출력)·app.sqlite(앱 테이블·FTS5·벡터) · `config/` 생성기·LLM 설정 · `generator/llm/` LLM 단계 캐시·작업(라벨이 들어 있어 `data/` 밖) · `prompts/` 프롬프트(앱은 버전 파일, 생성기는 작업당 파일 하나)
- `backend/` 에이전트 하네스(`backend/harness/`)·검색(`rag_index`·`rag_search`)·조회 도구(`query_tools`)·FastAPI 앱(`backend/api.py`) · `frontend/` Next.js(Tailwind + shadcn/ui)
- `.claude/` Claude Code 설정 — `agents/`(리뷰어·문서 점검, 읽기 전용 훅), `commands/`(`/fill-llm-jobs`), 훅 `settings.json` → `hooks/guard.py`(막기·확인 창·알림 목록은 그 모듈 독스트링, 테스트 `tests/test_claude_hooks.py`)·권한 허용 목록

## 절대 규칙
- `backend/`는 `ground_truth/`·`generator/`·`evaluation/`를 참조하지 않는다 (`tests/test_no_ground_truth_import.py`가 검사)
- 정답은 `data/`(source.sqlite·app.sqlite)에 한 줄도 넣지 않는다
- 프롬프트 관문과 자동 개선 루프는 `evaluation/`에서만 돌고 test 분할을 읽지 않는다. 개선 에이전트(착수 전)의 실행 기록은 `app.sqlite`에 쓰지 않고 앱에는 라벨 없는 집계·실수 후보만 넘긴다. 관문 기준 수정과 `--split test` 실행은 사람만(평가 설계 §2.7)
- 앱의 도구 설명·권한·훅·정책·입력 제한 변경은 개선 에이전트가 후보·재현 테스트까지만, 반영은 사람이 승인한다. 안전장치를 푸는 변경은 자동으로 만들지 않는다(평가 설계 §2.7)
- 테이블 접두어는 소속 시스템 기준: `emr_` 전자차트 원본 · `fin_` 재무 원본 · `ax_` AX 시스템 · `truth_` 정답(별도 DB). `emr_`·`fin_`은 backend가 쓰지 않는다(가상 데이터 생성 설계 §0)
- 시드 고정(`config/generator.yaml`), 생성기는 단계마다 `seed`와 단계 이름으로 따로 만든 난수 생성기를 쓴다 — 단계를 더해도 기존 결과가 그대로(가상 데이터 생성 설계 §5.1)
- 데이터 상태(가상 데이터 생성 설계 §0)가 **고정**이면 기존 `emr_*`·`fin_*`를 수정하지 않고 추가 데이터는 새 테이블로. **재설계**로 여는 것은 사람이 정하고, 다시 고정하기 전에 전체 재생성·`make verify`·RAG 평가셋 재확인
- 정형(코드) 단계를 고치면 그 뒤 LLM 입력이 바뀐 건의 캐시가 다시 나간다(앞 단계일수록 넓게) — 고치기 전에 `make data`로 나갈 작업 수를 확인(단계 순서는 가상 데이터 생성 설계 §5.1)
- verify 검사를 생성 코드보다 먼저 작성한다 (TDD)
- 분류기의 통화 입력은 `transcript`·`direction`만(`backend/classify.py` `classify`)
- 병원 상수(시술·영업시간·스케일링 규칙)는 `generator/master.py`에만(하루 상한만 `config/generator.yaml` `visits_per_day`). 생성기·verify·테스트 모두 여기서 읽는다, backend는 생성기가 적재한 `source.ax_master_values`로 읽는다
- 문서에만 있는 생성 값을 코드로 옮기면 문서에서 지우고 코드 이름으로 가리키거나, 남기면 `tests/generator/test_doc_sync.py`에 비교를 더한다

## 명령
과금하는 명령은 (과금), 실패·빠진 건만 다시 도는 명령은 (이어 돎). 인자·동작은 Makefile 주석과 원본 모듈 독스트링.
- `uv sync` — lock 커밋 필수(Faker 버전이 이름 결정성에 영향), 파이썬은 uv 관리 빌드만(sqlite-vec이 확장을 연다). 그룹: `generator`·`backend`·`dev`, Qwen 파인튜닝은 `finetune`(기본 아님 — `uv run --group finetune`)
- `make data` / `make verify` / `make test` — 테스트는 같은 생성을 임시 폴더에 돌린다(병렬, 작업자 수 `JOBS=`). `make data FAKE_LLM=1`은 가짜 게이트웨이
- LLM 단계: `make data`가 캐시에 없는 건을 `generator/llm/jobs/`로 내보내고 멈춤 → `make llm-fill-api TASK=…`(과금) 또는 `/fill-llm-jobs` → `make llm-import` → `make data`. 옛 캐시 줄은 `make llm-prune`
- `make index EMBEDDING=<모델>` — 검색 색인(과금, 이어 돎, 앱 설계 §3), `make data`와 따로
- `make dev` — 앱(API 8000 + 화면 3000, 검색은 색인이 있어야). `make e2e` — 화면 Playwright(로컬 전용, 켜 둔 `make dev`와 따로 `.next-e2e`로 띄운다)
- `make fmt` / `make lint` — 파이썬 ruff, 프론트 ESLint·Prettier·tsc
- `make gate` — 코어 합격 기준(기능 정의 §6), 마지막 dev 평가는 (과금), `make dev`를 끄고
- ① `make classify V=`(과금, 이어 돎) / `judge V=`(로컬 Qwen, 이어 돎) / `disagreements A= B=`(`backend/classify_batch.py`) · `make autoclose` / `auto-policy KIND= ON=1|0`(`backend/autoclose.py`)
- ① `make followups`(과금, `backend/followup.py`) · `make action-label`(과금, 이어 돎) / `action-audit`(`evaluation/call_actions.py`) · `make followup-eval`(과금, 이어 돎, `evaluation/followup.py`)
- ② `make blind-judge A= B= V=`(과금, 이어 돎, 평가 설계 §2.4) · `make gold-arbitrate`(과금, 이어 돎) / `gold-audit`(평가 설계 §2.3) · `make prompt-gate A= B= ITEM=`(과금, 이어 돎, 평가 설계 §2.7 — 같은 기준에 후보 `metrics.GATE_ATTEMPTS`개까지)
- ④ `make prep [DATE=] [NO_BATCH=1]`(과금, 이어 돎, `backend/prep.py`)
- ⑤ `make claim-check [SCOPE=all] [NO_BATCH=1]`(과금, 이어 돎, `backend/claim_check.py`) · `make claim-check-sample [N=]`(과금, dev 고정 표본 — 프롬프트 고칠 때) / `claim-check-dev`(과금, dev 전부 — 버전을 정한 뒤, `evaluation/claim_check.py`)

## 코드 컨벤션
- Python은 **PEP 8**을 ruff로 지킨다(설정 `pyproject.toml` `[tool.ruff]` — 코드 99자, 한국어 독스트링·주석은 줄 길이 검사 제외). 코드를 고치면 커밋 전 `make fmt` → `make lint` → `make test`. 동작 없는 일괄 정리 커밋은 `.git-blame-ignore-revs`에 넣는다(로컬은 `git config blame.ignoreRevsFile .git-blame-ignore-revs`)
- 표준에 없는 이 프로젝트 관례:
  - 주석·독스트링은 한국어로, 규칙의 근거 문서 절을 가리킨다(예: `평가 설계 §3.2`). 모듈 첫 독스트링에 하는 일과 원본 문서
  - 값은 직접 쓰지 않고 `generator/master.py`·`config`·스키마의 이름으로 읽는다(위 절대 규칙)
  - verify 검사는 `check_*(conn, ctx) → 위반 행 목록`, 독스트링이 실패·경고 때 출력되는 규칙 설명
  - DB는 sqlite3에 SQL 그대로(ORM 없음)
- Next.js: ESLint `next/core-web-vitals`, TypeScript strict, Prettier. 컴포넌트 `PascalCase`, 훅 `useX`. 색·모서리·글꼴·그림자는 `frontend/app/globals.css` 토큰으로만 쓰고(직접 hex·임의 값·Tailwind 기본 색 이름 금지), 부품은 `frontend/components/ui/`(shadcn)만 쓴다 — 모양이 더 필요하면 그 파일에 variant를 더한다(예외: 전환 전 EMR 창 안은 옛 프로그램 부품 `components/emr/emr-ui.tsx`, 색은 `--emr-*` 토큰)

## 깃
- 브랜치는 작업 묶음마다 `feat/<묶음>`(예: `feat/eval-set`) — main에서 따고, PR 머지 뒤 main에서 다음 브랜치
- 커밋 메시지는 `<영역>: <무엇>` + 본문 글머리. 영역: `generator` · `eval` · `backend` · `frontend` · `docs` · `claude`(`.claude/` 설정) · `chore`(도구·의존성) · `style`(동작 없는 정리)
- `git add`는 파일 이름으로 — 폴더 단위·`-A` 금지(임시 파일이 섞이지 않게)
- PR은 `.github/pull_request_template.md`, CI(`.github/workflows/ci.yml`)가 `make lint`(파이썬·프론트)·`make test`·프론트 빌드를 따로 동시에 돌린다

## 현재 단계
진행 순서와 다음 할 일은 `docs/치과 AX - 기능 정의.md` §9.

---

## Coding Guidelines

Behavioral guidelines to reduce common LLM coding mistakes.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

### 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

### 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

### 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

### 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.
