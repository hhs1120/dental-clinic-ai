SCALE ?= large
# 규모별 출력 경로 — large는 앱·평가가 보는 기본 경로, small은 따로 둬서 서로 덮어쓰지 않는다
SMALL_PATHS = --db data/small/app.sqlite --truth ground_truth/small/truth.sqlite

.PHONY: data verify test index fixture

data:
	PYTHONHASHSEED=0 uv run python -m generator --scale $(SCALE) $(if $(REGEN_LLM),--regen-llm)

verify:
	uv run python -m generator.verify $(if $(filter small,$(SCALE)),$(SMALL_PATHS))

test:
	uv run pytest

index fixture:
	@echo "TODO: $@"
