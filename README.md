# dental-clinic-ai

동네 치과 AX(AI 전환) 프로젝트 — 가상 데이터로 치과 데스크·운영 업무의 AI 기능을 구현하고 평가한 포트폴리오. 설계는 `docs/`, 작업 규칙은 `CLAUDE.md`.

평가 데이터에는 실제로 있을 법한 헷갈리는 사례(바꿔 말하기·부정·약어 등)를 섞었고, 수치는 이 가상 데이터 기준입니다. 실제 병원 기록에서는 차이가 달라질 수 있습니다. 경위는 `docs/치과 AX - AI 실험 기록.md` §23~§25.

## 개발 방식

- **설계 → 계획 → 구현 → 리뷰**: 기능마다 설계 문서와 구현 계획을 먼저 쓰고, 구현 뒤 리뷰 에이전트·문서 점검을 거쳐 반영했다. 기록은 `docs/superpowers/`(입구는 그 폴더의 README)
- **테스트 먼저(TDD)**: 실패하는 테스트를 먼저 쓰고 실패를 확인한 뒤 구현해 통과시킨다. 리뷰 지적도 재현 테스트부터 쓰고 고쳤다. 가상 데이터는 검사(`make verify`)를 생성 코드보다 먼저 쓴다
- **결정적 테스트**: LLM 기능도 가짜 게이트웨이로 API 키 없이 같은 결과를 낸다. 테스트 약 2,700개(지금 수는 `make test`로 확인), CI가 PR마다 lint·test·프론트 빌드를 돌린다

**왜 TDD인가**: AI가 코드를 빠르게 쓰면서 테스트를 먼저 쓰는 비용은 거의 사라졌고, 대신 AI가 쓴 코드가 맞는지 확인하는 일이 개발의 중심이 됐다. 그래서 사람은 무엇이 맞는 동작인지를 테스트로 먼저 정하고, AI는 그 테스트를 통과하는 코드를 쓴다. 테스트도 AI가 쓰기 때문에, 구현 전에 실패하는 것을 먼저 확인하고 기대 동작은 설계 문서에서 가져온다. 코드는 테스트로, AI 출력의 품질은 평가로 잰다 — 정답 데이터(골드)로 채점하고, 프롬프트를 바꿀 때는 골드 기준으로 나아졌고 응급·불만을 새로 놓치지 않았을 때만 반영하는 규칙이며(지금은 ① 통화 분류), 규칙 기준선과 나란히 재서 기준선이 이기는 곳도 그대로 적는다.

## 환경 세팅

**필요한 것**
- macOS 또는 Linux
- [uv](https://docs.astral.sh/uv/) — Python 패키지·가상환경 관리. Python 3.11+는 uv가 자동으로 받는다
- make (macOS 기본 포함)
- [Node.js](https://nodejs.org/)와 npm — 버전은 `.nvmrc`. 프론트(`frontend/`)와 `make lint`가 쓴다(Claude Code 커밋 훅이 `make lint`를 돌린다)

**설치**
```bash
# 1. uv·Node 설치 (한 번만)
brew install uv node
# 또는: curl -LsSf https://astral.sh/uv/install.sh | sh  (Node는 nodejs.org나 nvm — `nvm use`가 .nvmrc를 읽는다)

# 2. 저장소 받기
git clone https://github.com/hhs1120/dental-clinic-ai.git
cd dental-clinic-ai

# 3. 의존성 설치 (.venv 생성, uv.lock 기준으로 버전 고정 / 프론트는 package-lock.json 기준)
uv sync
npm --prefix frontend ci

# 4. 확인
make test
```

**환경 변수** (LLM 단계·`make index`부터 필요)
```bash
cp .env.example .env   # API 키 입력. .env는 커밋되지 않는다
```

> conda와 함께 써도 충돌하지 않는다. 이 프로젝트는 uv의 `.venv`만 사용하므로 `pip install`·`conda install` 대신 `uv add --group <그룹> <패키지>`로 추가한다(그룹: 생성기 `generator`, 앱 `backend`, 테스트 `dev`).

## 명령

| 명령 | 설명 |
|---|---|
| `make data` | 가상 데이터 생성 (`data/source.sqlite`, 정답은 `ground_truth/truth.sqlite`) |
| `make verify` | 생성 데이터 검사 |
| `make test` | 테스트 실행 |
| `make index EMBEDDING=voyage-4` | 검색 색인 (`data/app.sqlite`, `VOYAGE_API_KEY` 필요) |
| `make dev` | 앱 실행 — API(8000) + 화면(http://localhost:3000), Ctrl+C로 둘 다 종료 |
| `make lint` / `make fmt` | 코드 검사 / 모양 정리 (파이썬 ruff, 프론트 ESLint·Prettier·tsc) |

## 구조

```
docs/          설계 문서 (기능 정의 · 가상 데이터 생성 설계 · 앱 설계 · 평가 설계 · 전환의 의미 · 프로젝트 소개 · 기술 소개 · AI 실험 기록)
config/        생성기 설정 (시드, 기간·건수, 공휴일, 금지 토픽), LLM 게이트웨이 설정 (llm.yaml — 별칭·단가·일일 상한)
generator/     데이터 생성기 + verify
shared/        스키마, LLM 게이트웨이
evaluation/    평가 스크립트
ground_truth/  정답지 (앱은 참조하지 않음)
data/          생성 결과 (source.sqlite — 생성기 출력, app.sqlite — 앱 테이블·FTS5·벡터)
backend/       에이전트 하네스, 검색 색인·검색, 조회 도구, FastAPI 앱
frontend/      Next.js (Tailwind + shadcn/ui)
tests/         generator/ 생성기·verify 테스트, shared/ 게이트웨이 테스트, evaluation/ 평가 테스트, backend/ 앱 테스트. 정답 격리 검사는 tests/ 바로 아래
```
