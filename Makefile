# 출력 경로는 generator/paths.py

.PHONY: data verify test lint fmt index fixture llm-import llm-fill-api

data:
	PYTHONHASHSEED=0 uv run python -m generator $(if $(FAKE_LLM),--fake-llm)

# LLM 단계 채우기 (generator/llm_jobs.py, 가상 데이터 생성 설계 §5.1). Claude Code는 /fill-llm-jobs
llm-import:
	uv run python -m generator.llm_jobs import

llm-fill-api:
	$(if $(TASK),,$(error TASK=transcript·document·chart 중 하나))
	uv run $(if $(wildcard .env),--env-file .env) python -m generator.llm_jobs fill-api --task $(TASK) $(if $(LIMIT),--limit $(LIMIT))

verify:
	uv run python -m generator.verify

test:
	uv run pytest

# 코드 컨벤션(PEP 8, 설정은 pyproject.toml [tool.ruff]) — lint는 검사만, fmt는 모양 정리 + 안전한 자동 수정
lint:
	uv run ruff check .
	uv run ruff format --check .

fmt:
	uv run ruff format .
	uv run ruff check --fix .

index fixture:
	@echo "TODO: $@"
