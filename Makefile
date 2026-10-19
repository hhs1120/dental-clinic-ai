SCALE ?= small

.PHONY: data verify test index fixture

data:
	uv run python -m generator --scale $(SCALE)

verify:
	uv run python -m generator.verify

test:
	uv run pytest

index fixture:
	@echo "TODO: $@"
