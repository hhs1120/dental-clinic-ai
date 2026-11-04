# 출력 경로는 generator/paths.py

.PHONY: data verify test lint fmt index fixture llm-import llm-fill-api llm-prune wireframe dev

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
	trap 'kill 0' EXIT; \
	uv run $(if $(wildcard .env),--env-file .env) uvicorn backend.api:app --port 8000 & \
	npm --prefix frontend run dev

# 검색 색인 (앱 설계 §3). 벡터는 모델마다 한 벌이고 없는 청크만 계산한다
index:
	$(if $(EMBEDDING),,$(error EMBEDDING=모델 이름 필요))
	uv run $(if $(wildcard .env),--env-file .env) python -m backend.rag_index --embedding $(EMBEDDING)

fixture:
	@echo "TODO: $@"
