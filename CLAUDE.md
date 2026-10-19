# dental-clinic-ai (치과 AX)

동네 치과 AX 포트폴리오 프로젝트. 가짜 데이터(미니 EMR + 통화·문서·문자 + 정답)로 통화 분류·사람 판정·RAG 챗·진료 준비표를 구현한다.

## 설계 문서 (구현의 원본 스펙 — 코드와 어긋나면 문서를 먼저 확인)
- `docs/치과 AX - 기능 정의.md` — 무엇을 만드나, `make gate` 기준
- `docs/치과 AX - 데이터 설계.md` — 스키마, 생성 규칙, `make verify` 검사 목록
- `docs/치과 AX - 평가 설계.md` — 지표, 채점, 정답 저장소(`truth_*`) 테이블 정의

## 폴더
- `generator/` 데이터 생성기 + verify · `shared/` 스키마·LLM 게이트웨이 · `evaluation/` eval.py
- `ground_truth/` 정답(truth.sqlite + JSON) · `data/` app.sqlite, chroma · `config/` 생성기 설정
- `backend/` FastAPI · `frontend/` Next.js (개발 단계에서 세팅)

## 절대 규칙
- `backend/`는 `ground_truth/`·`generator/`를 import하지 않는다 (`tests/test_no_ground_truth_import.py`가 검사)
- 정답은 `app.sqlite`에 한 줄도 넣지 않는다
- 시드 고정(`config/generator.yaml`). 같은 시드 → 같은 결과
- 정형 데이터(1~6단계) 완성 + `make verify` 통과 → LLM 생성(7~9단계) 1회. 정형 로직을 고치면 LLM 캐시가 전량 미스
- verify 검사를 생성 코드보다 먼저 작성한다 (TDD)
- 분류기 입력은 `classify(transcript, direction)`만

## 명령
- `uv sync` — 의존성 설치 (lock 파일 커밋 필수: Faker 버전이 이름 결정성에 영향)
- `make data [SCALE=small|medium|large]` / `make verify` / `make test`

## 현재 단계
준비: 생성기 + verify. 앱(backend/frontend)은 아직 손대지 않는다.

---

## Coding Guidelines

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

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
