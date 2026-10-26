# 출력 경로는 generator/paths.py

.PHONY: data verify test index fixture

data:
	PYTHONHASHSEED=0 uv run python -m generator $(if $(REGEN_LLM),--regen-llm)

verify:
	uv run python -m generator.verify

test:
	uv run pytest

index fixture:
	@echo "TODO: $@"
