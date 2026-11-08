# 출력 경로는 generator/paths.py

.PHONY: data verify test lint fmt index fixture llm-import llm-fill-api llm-prune wireframe dev classify judge disagreements blind-judge gold-arbitrate gold-audit autoclose auto-policy action-label action-audit e2e

data:
	PYTHONHASHSEED=0 uv run python -m generator $(if $(FAKE_LLM),--fake-llm)

# LLM 단계 채우기 (generator/llm_jobs.py, 가상 데이터 생성 설계 §5.1). Claude Code는 /fill-llm-jobs
llm-import:
	uv run python -m generator.llm_jobs import

# 이번 생성이 쓰지 않은 LLM 캐시 줄(프롬프트·입력이 바뀐 옛 시도) 지우기 — 빈 건이 없을 때만
llm-prune:
	PYTHONHASHSEED=0 uv run python -m generator --prune-llm-cache

llm-fill-api:
	$(if $(TASK),,$(error TASK=transcript·document·chart 중 하나))
	uv run $(if $(wildcard .env),--env-file .env) python -m generator.llm_jobs fill-api --task $(TASK) $(if $(LIMIT),--limit $(LIMIT))

verify:
	uv run python -m generator.verify

# ① 분류·판정 일괄(backend/classify_batch.py) — 생성 통화 전체, 이미 ok인 건은 건너뜀. NO_BATCH=1이면 일반 호출만
classify:
	$(if $(V),,$(error V=프롬프트 버전 필요))
	uv run $(if $(wildcard .env),--env-file .env) python -m backend.classify_batch classify --version $(V) $(if $(LIMIT),--limit $(LIMIT)) $(if $(NO_BATCH),--no-batch)

judge:
	$(if $(V),,$(error V=프롬프트 버전 필요))
	uv run $(if $(wildcard .env),--env-file .env) python -m backend.classify_batch judge --version $(V) $(if $(LIMIT),--limit $(LIMIT)) $(if $(NO_BATCH),--no-batch)

disagreements:
	$(if $(and $(A),$(B)),,$(error A=분류기 버전 B=판정기 버전 필요))
	uv run python -m backend.classify_batch disagreements --a $(A) --b $(B)

# 블라인드 LLM 쌍 비교(평가 설계 §2.4) — dev 블라인드 통화, 분류 버전 A vs B, 쌍 비교 프롬프트 버전 V
blind-judge:
	$(if $(and $(A),$(B),$(V)),,$(error A=분류 버전 B=분류 버전 V=쌍 비교 프롬프트 버전 필요))
	uv run $(if $(wildcard .env),--env-file .env) python -m backend.classify_batch blind --a $(A) --b $(B) --version $(V)

# 골드 점검(평가 설계 §2.3) — 중재(과금, 빠진 건만)·사람 표본 터미널 화면. 골드를 보므로 evaluation/에서
gold-arbitrate:
	uv run $(if $(wildcard .env),--env-file .env) python -m evaluation.gold_audit arbitrate $(if $(BATCH),--batch)

gold-audit:
	uv run python -m evaluation.gold_audit audit

# ① 처리 정답(설계 "PR ② 처리 정답") — Opus 라벨(과금, 빠진 건만, Batch)·사람 표본 터미널 화면
action-label:
	uv run $(if $(wildcard .env),--env-file .env) python -m evaluation.call_actions label

action-audit:
	uv run python -m evaluation.call_actions audit

# ① 자동 종결(기능 정의 §4 ①) — 활성 버전 쌍으로 다시 계산. auto_policy가 꺼져 있으면 아무것도 하지 않는다
autoclose:
	uv run python -m backend.autoclose run

auto-policy:
	$(if $(and $(KIND),$(ON)),,$(error KIND=작업 종류 ON=1|0 필요))
	uv run python -m backend.autoclose policy $(KIND) $(if $(filter 1,$(ON)),on,off)

# 와이어프레임 데이터(docs/wireframe/data.json — 정답 라벨 예시가 들어 있어 커밋하지 않음). make data 뒤에
wireframe:
	uv run python docs/wireframe/export.py

test:
	uv run pytest

# 코드 컨벤션(파이썬 PEP 8은 pyproject.toml [tool.ruff], 프론트는 ESLint·Prettier·tsc) — lint는 검사만, fmt는 모양 정리 + 안전한 자동 수정.
# 커밋 훅이 make lint를 돌리므로 새 worktree에서도 npm ci가 먼저 돈다
lint: frontend/node_modules
	uv run ruff check .
	uv run ruff format --check .
	npm --prefix frontend run -s lint
	npm --prefix frontend run -s format:check
	npm --prefix frontend run -s typecheck

fmt: frontend/node_modules
	uv run ruff format .
	uv run ruff check --fix .
	npm --prefix frontend run -s format

frontend/node_modules: frontend/package-lock.json
	npm --prefix frontend ci
	touch $@

# 앱 실행 — API(8000)와 화면(3000)을 같이 띄우고 Ctrl+C로 둘 다 끝낸다
dev: frontend/node_modules
	trap 'kill 0' INT TERM EXIT; \
	uv run $(if $(wildcard .env),--env-file .env) uvicorn backend.api:app --port 8000 & \
	npm --prefix frontend run dev

# 화면 E2E(Playwright) — data/ 복사본으로 API(8100)·화면(3100)을 띄운다. 실데이터가 필요해 CI에는 넣지 않는다
e2e: frontend/node_modules
	npm --prefix frontend run -s e2e

# 검색 색인 (앱 설계 §3). 벡터는 모델마다 한 벌이고 없는 청크만 계산한다
index:
	$(if $(EMBEDDING),,$(error EMBEDDING=모델 이름 필요))
	uv run $(if $(wildcard .env),--env-file .env) python -m backend.rag_index --embedding $(EMBEDDING)

fixture:
	@echo "TODO: $@"
