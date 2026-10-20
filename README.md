# dental-clinic-ai

동네 치과 AX(AI 전환) 프로젝트 — 가상 데이터로 치과 데스크·운영 업무의 AI 기능을 구현하고 평가한 포트폴리오. 설계는 `docs/`, 작업 규칙은 `CLAUDE.md`.

## 환경 세팅

**필요한 것**
- macOS 또는 Linux
- [uv](https://docs.astral.sh/uv/) — Python 패키지·가상환경 관리. Python 3.11+는 uv가 자동으로 받는다
- make (macOS 기본 포함)

**설치**
```bash
# 1. uv 설치 (한 번만)
brew install uv
# 또는: curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. 저장소 받기
git clone https://github.com/hhs1120/dental-clinic-ai.git
cd dental-clinic-ai

# 3. 의존성 설치 (.venv 생성, uv.lock 기준으로 버전 고정)
uv sync

# 4. 확인
make test
```

**환경 변수** (LLM 단계부터 필요)
```bash
cp .env.example .env   # API 키 입력. .env는 커밋되지 않는다
```

> conda와 함께 써도 충돌하지 않는다. 이 프로젝트는 uv의 `.venv`만 사용하므로 `pip install`·`conda install` 대신 `uv add <패키지>`로 추가한다.

## 명령

| 명령 | 설명 |
|---|---|
| `make data [SCALE=small\|medium\|large]` | 가상 데이터 생성 (기본 small) |
| `make verify` | 생성 데이터 검사 |
| `make test` | 테스트 실행 |

## 구조

```
docs/          설계 문서 (기능 정의 · 가상 데이터 생성 설계 · 앱 설계 · 평가 설계 · 전환의 의미 · 프로젝트 소개)
config/        생성기 설정 (시드, 규모, 공휴일, 금지 토픽)
generator/     데이터 생성기 + verify
shared/        스키마, LLM 게이트웨이
evaluation/    평가 스크립트
ground_truth/  정답지 (앱은 참조하지 않음)
data/          생성 결과 (app.sqlite, 벡터 색인)
backend/       FastAPI (예정)
frontend/      Next.js (예정)
tests/
```
