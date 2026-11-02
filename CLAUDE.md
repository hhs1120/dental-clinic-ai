# dental-clinic-ai (치과 AX)

동네 치과 AX 포트폴리오 프로젝트. 가상 데이터(미니 EMR + 통화·문서·문자 + 정답)로 통화 분류·판정과 자동 개선·RAG 챗·진료 준비표와 업무 에이전트를 구현한다.

## 설계 문서
규칙·절차의 원본 — 코드와 어긋나면 문서를 먼저 확인한다. 단 **값**(숫자·enum·컬럼·인덱스)은 `generator/master.py`·`shared/source_schema.sql`·`shared/app_schema.sql`·`ground_truth/schema.sql`·`config/generator.yaml`이 원본이고(verify 허용치·기준값은 `generator/verify/`, 채점 상수는 `evaluation/metrics.py`) 문서는 이름으로 가리킨다.

- `docs/치과 AX - 기능 정의.md` — 무엇을 만드나, `make gate` 기준
- `docs/치과 AX - 가상 데이터 생성 설계.md` — `emr_*`·`fin_*`·생성기 적재 `ax_*` 스키마, 생성 규칙, `make verify` 검사 목록 (생성기가 읽음)
- `docs/치과 AX - 앱 설계.md` — 앱 `ax_*` 테이블, 도구 시그니처, 고정 쿼리, 앱 기술 선택 (backend가 읽음)
- `docs/치과 AX - 평가 설계.md` — 지표, 채점, 정답 저장소(`truth_*`) 테이블 정의
- `docs/치과 AX - 전환의 의미.md` — 전환 전후 업무 변화 (배경, 구현 스펙 아님)
- `docs/치과 AX - 프로젝트 소개.md` — 비개발자용 한 장 소개 (구현 스펙 아님)
- `docs/치과 AX - 개발자 소개.md` — 팀원 모집용: 기술 스택·데이터 규모·기능별 AI·독립 과제·포트폴리오에 남는 것 (구현 스펙 아님)
- `docs/wireframe/` — 앱 화면 와이어프레임(클릭 HTML). 프론트 화면의 기준은 `index-v2.html`(앱 설계 §9), `index.html`은 비교용 이전 판. 데이터는 `make wireframe`

## 폴더
- `generator/` 데이터 생성기 + verify (테스트는 `tests/generator/`, 게이트웨이는 `tests/shared/`, 평가는 `tests/evaluation/`, 앱 테스트는 `tests/backend/`에) · `shared/` 스키마·LLM 게이트웨이(`shared/llm.py`, 설정 `config/llm.yaml`) · `evaluation/` eval.py·자동 개선·시나리오 DB
- `ground_truth/` 정답(truth.sqlite + JSON) · `data/` source.sqlite(생성기 출력)·app.sqlite(앱 테이블·FTS5·벡터) · `config/` 생성기 설정 · `generator/llm/` LLM 단계 캐시·작업(라벨이 들어 있어 `data/` 밖) · `prompts/` 프롬프트(앱은 버전 파일, 생성기는 작업당 파일 하나)
- `backend/` FastAPI·에이전트 하네스(`backend/harness/`) · `frontend/` Next.js (개발 단계에서 세팅)
- `.claude/` Claude Code 설정 — `agents/`(리뷰어·문서 점검, 읽기 전용 훅), `commands/`(`/fill-llm-jobs`), 훅 `settings.json` → `hooks/guard.py`(막기·확인 창·알림 목록은 그 모듈 독스트링, 테스트 `tests/test_claude_hooks.py`)·권한 허용 목록

## 절대 규칙
- `backend/`는 `ground_truth/`·`generator/`·`evaluation/`를 참조하지 않는다 (`tests/test_no_ground_truth_import.py`가 검사)
- 정답은 `data/`(source.sqlite·app.sqlite)에 한 줄도 넣지 않는다
- 자동 개선 루프는 `evaluation/`에서만 돌고 test 분할을 읽지 않는다. 개선 에이전트의 실행 기록은 골드가 들어 있어 `app.sqlite`에 쓰지 않고, 앱에는 라벨 없는 집계·실수 후보만 넘긴다. 관문 기준 수정과 `--split test` 실행은 사람만 한다(평가 설계 §2.7)
- 앱의 도구 설명·도구 권한·훅·정책·입력 제한 변경은 개선 에이전트가 후보와 재현 테스트까지만 만들고 반영은 사람이 승인한다. 안전장치를 푸는 변경은 자동으로 만들지 않는다(평가 설계 §2.7)
- 테이블 접두어는 현실의 소속 시스템 기준: `emr_` 전자차트 원본(진료·수납·청구·차트·문자) · `fin_` 재무 원본(금융기관 내역·회계 증빙) · `ax_` AX 시스템 · `truth_` 정답(별도 DB). `emr_`·`fin_`은 backend가 쓰지 않는다. 상세는 가상 데이터 생성 설계 §0
- 시드 고정(`config/generator.yaml`) — 같은 시드 → 같은 결과. 생성기는 **단계별 독립 시드**: 단계마다 `seed`와 단계 이름으로 난수 생성기를 따로 만든다(전역 난수 생성기 하나를 이어 쓰지 않는다 — 단계를 추가해도 기존 결과가 바뀌지 않게, 가상 데이터 생성 설계 §5.1)
- 데이터 상태(가상 데이터 생성 설계 §0)가 **고정**이면 기존 `emr_*`·`fin_*`를 수정하지 않는다. 추가 데이터는 새 테이블로. 상태를 **재설계**로 여는 것은 사람이 정하고, 다시 고정하기 전에 전체 재생성·`make verify`·RAG 평가셋 재확인을 마친다
- 정형(코드) 단계를 고치면 그 뒤 LLM 입력이 바뀐 건의 캐시가 미스 나 다시 채워야 한다(앞 단계일수록 넓게). 고치기 전에 `make data`로 다시 나갈 작업 수를 확인한다. 단계 목록·순서는 가상 데이터 생성 설계 §5.1
- verify 검사를 생성 코드보다 먼저 작성한다 (TDD)
- 분류기 입력은 `classify(transcript, direction)`만
- 병원 상수(시술·영업시간·스케일링 규칙)는 `generator/master.py`에만 둔다(하루 상한 값만 `config/generator.yaml`의 `visits_per_day`). 생성기·verify·테스트 모두 여기서 읽는다
- 문서에만 있는 생성 값(시술 가중표, 심사 결과 분포, 통화 분포 등)을 코드로 옮길 때는 반드시 둘 중 하나: 문서에서 지우고 코드 이름으로 가리키거나, 문서에 남기면 `tests/generator/test_doc_sync.py`에 비교를 추가한다

## 명령
- `uv sync` — 의존성 설치 (lock 파일 커밋 필수: Faker 버전이 이름 결정성에 영향). 의존성은 `pyproject.toml`의 그룹으로 나눈다 — 생성기는 `generator`, 앱은 `backend`, 테스트·린트(pytest·ruff)는 `dev`
- `make data` / `make verify` / `make test` — 규모는 하나(`config/generator.yaml`). 테스트는 같은 생성을 임시 폴더에 돌린다
- `make fmt`(모양 정리 + 안전한 자동 수정) / `make lint`(검사만) — ruff, 아래 코드 컨벤션
- LLM 단계(7·9·9b): `make data`가 캐시(`generator/llm/cache/`, 커밋)에 없는 건을 `generator/llm/jobs/`로 내보내고 멈춤 → `make llm-fill-api TASK=…` 또는 `/fill-llm-jobs` → `make llm-import` → `make data`. `make data FAKE_LLM=1`은 가짜 게이트웨이(과금 없음). 프롬프트·입력이 바뀐 옛 시도 줄은 `make llm-prune`으로 지운다(빈 건이 없을 때만)

## 코드 컨벤션
- Python은 **PEP 8**을 ruff로 지킨다(설정 `pyproject.toml` `[tool.ruff]` — 코드 99자, 한국어 독스트링·주석은 줄 길이 검사 제외). 코드를 고치면 커밋 전 `make fmt` → `make lint` → `make test`. 동작 없는 일괄 정리 커밋은 `.git-blame-ignore-revs`에 넣는다(로컬은 `git config blame.ignoreRevsFile .git-blame-ignore-revs`)
- 표준에 없는 이 프로젝트 관례:
  - 주석·독스트링은 한국어로, 규칙의 근거 문서 절을 가리킨다(예: `평가 설계 §3.2`). 모듈 첫 독스트링에 하는 일과 원본 문서
  - 값은 직접 쓰지 않고 `generator/master.py`·`config`·스키마의 이름으로 읽는다(위 절대 규칙)
  - verify 검사는 `check_*(conn, ctx) → 위반 행 목록`, 독스트링이 실패·경고 때 출력되는 규칙 설명
  - DB는 sqlite3에 SQL 그대로(ORM 없음)
- Next.js(frontend 착수 때 켠다): ESLint `next/core-web-vitals`, TypeScript strict, Prettier. 컴포넌트 `PascalCase`, 훅 `useX`

## 깃
- 브랜치는 작업 묶음마다 `feat/<묶음>`(예: `feat/eval-set`) — main에서 따고, PR 머지 뒤 main에서 다음 브랜치
- 커밋 메시지는 `<영역>: <무엇>` + 본문 글머리. 영역: `generator` · `eval` · `docs` · `claude`(`.claude/` 설정) · `chore`(도구·의존성) · `style`(동작 없는 정리)
- `git add`는 파일 이름으로 — 폴더 단위·`-A` 금지(임시 파일이 섞이지 않게)
- PR은 `.github/pull_request_template.md`, CI(`.github/workflows/ci.yml`)가 `make lint`·`make test`를 돌린다

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
