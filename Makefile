# 출력 경로는 generator/paths.py

.PHONY: data verify test index fixture llm-import llm-fill-api

data:
	PYTHONHASHSEED=0 uv run python -m generator $(if $(FAKE_LLM),--fake-llm)

# LLM 단계 채우기 (generator/llm_jobs.py, 가상 데이터 생성 설계 §5.1). Claude Code는 /fill-llm-jobs
llm-import:
	uv run python -m generator.llm_jobs import

llm-fill-api:
	$(if $(TASK),,$(error TASK=transcript·document·chart 중 하나))
	uv run python -m generator.llm_jobs fill-api --task $(TASK)

verify:
	uv run python -m generator.verify

test:
	uv run pytest

index fixture:
	@echo "TODO: $@"
