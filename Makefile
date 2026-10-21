SCALE ?= large
# 규모별 출력 경로는 generator/paths.py — large는 앱·평가가 보는 기본 경로, small은 따로

.PHONY: data verify test index fixture

data:
	PYTHONHASHSEED=0 uv run python -m generator --scale $(SCALE) $(if $(REGEN_LLM),--regen-llm)

verify:
	uv run python -m generator.verify --scale $(SCALE)

test:
	uv run pytest

index fixture:
	@echo "TODO: $@"
