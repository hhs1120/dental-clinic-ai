SCALE ?= small

.PHONY: data verify test index fixture

data:
	PYTHONHASHSEED=0 uv run python -m generator --scale $(SCALE) $(if $(REGEN_LLM),--regen-llm)

verify:
	uv run python -m generator.verify

test:
	uv run pytest

index fixture:
	@echo "TODO: $@"
