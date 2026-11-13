# 출력 경로는 generator/paths.py

.PHONY: data verify test lint fmt eval index fixture llm-import llm-fill-api llm-prune wireframe dev classify judge disagreements blind-judge gold-arbitrate gold-audit hard-label hard-review autoclose auto-policy action-label action-audit followups followup-eval e2e prompt-gate gate prep claim-check claim-omission claim-evidence claim-appeal-dev claim-qa-draft claim-qa-review claim-omission-cases claim-omission-synth claim-check-sample claim-check-dev recon expense-ai

data:
	PYTHONHASHSEED=0 uv run python -m generator $(if $(FAKE_LLM),--fake-llm)

# LLM 단계 채우기 (generator/llm_jobs.py, 가상 데이터 생성 설계 §5.1). Claude Code는 /fill-llm-jobs
llm-import:
	uv run python -m generator.llm_jobs import

# 이번 생성이 쓰지 않은 LLM 캐시 줄(프롬프트·입력이 바뀐 옛 시도) 지우기 — 빈 건이 없을 때만
llm-prune:
	PYTHONHASHSEED=0 uv run python -m generator --prune-llm-cache

llm-fill-api:
	$(if $(TASK),,$(error TASK=transcript·document·chart·hard_transcript 중 하나))
	uv run $(if $(wildcard .env),--env-file .env) python -m generator.llm_jobs fill-api --task $(TASK) $(if $(LIMIT),--limit $(LIMIT)) $(if $(ITEMS),--items $(ITEMS)) $(if $(NO_BATCH),--no-batch)

verify:
	uv run python -m generator.verify

# ① 분류·판정 일괄(backend/classify_batch.py) — 생성 통화 전체, 이미 ok인 건은 건너뜀. NO_BATCH=1이면 일반 호출만
classify:
	$(if $(V),,$(error V=프롬프트 버전 필요))
	uv run $(if $(wildcard .env),--env-file .env) python -m backend.classify_batch classify --version $(V) $(if $(LIMIT),--limit $(LIMIT)) $(if $(NO_BATCH),--no-batch)

judge:
	$(if $(V),,$(error V=프롬프트 버전 필요))
	uv run --group finetune $(if $(wildcard .env),--env-file .env) python -m backend.classify_batch judge --version $(V) $(if $(LIMIT),--limit $(LIMIT)) $(if $(NO_BATCH),--no-batch)

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

# 어려운 통화 정답 라벨(어려운 통화 설계 §4) — Opus 독립 라벨(과금, Batch, 이어 돎) / 사람 검토 → 최종 JSON, 그 뒤 make data
hard-label:
	uv run $(if $(wildcard .env),--env-file .env) python -m evaluation.hard_labels label

hard-review:
	uv run python -m evaluation.hard_labels review

# dev 평가 리포트(evaluation/eval.py) — 저장된 결과로 채점, 검색 절은 질문 임베딩 API를 부른다(색인 모델 키). test는 사람: python -m evaluation.eval --split test --section …
eval:
	uv run $(if $(wildcard .env),--env-file .env) python -m evaluation.eval --split dev

# 코어 합격 기준(기능 정의 §6) — 무료 검사 먼저, 과금하는 dev 평가(evaluation/gate.py)는 마지막. make dev를 끄고 돌린다
gate: frontend/node_modules
	$(MAKE) test
	npm --prefix frontend run -s build
	$(MAKE) e2e
	uv run $(if $(wildcard .env),--env-file .env) python -m evaluation.gate

# ① 분류 프롬프트 관문(평가 설계 §2.7) — 검증 구간에서 기준 A·후보 B를 GATE_RUNS번씩 다수결 비교(과금, 저장된 실행은 다시 씀)
prompt-gate:
	$(if $(and $(A),$(B),$(ITEM)),,$(error A=기준 버전 B=후보 버전 ITEM=목표 항목 필요))
	uv run $(if $(wildcard .env),--env-file .env) python -m evaluation.prompt_gate --a $(A) --b $(B) --item $(ITEM)

# ① 처리 정답(설계 "PR ② 처리 정답") — Opus 라벨(과금, 빠진 건만, Batch)·사람 표본 터미널 화면
action-label:
	uv run $(if $(wildcard .env),--env-file .env) python -m evaluation.call_actions label

action-audit:
	uv run python -m evaluation.call_actions audit

# ① 후속 에이전트(backend/followup.py) — 오늘 할 일에 오른 통화 중 처리안 없는 것(일반 호출, 과금). REDO=1이면 옛 프롬프트 버전 처리안을 지우고 다시
followups:
	uv run $(if $(wildcard .env),--env-file .env) python -m backend.followup run $(if $(REDO),--redo)

# ① 후속 처리 평가 실행(evaluation/followup.py) — 채점 모수 통화에 app.sqlite 사본(evaluation/runs/)에서 라운드 배치(과금, 빠진 건만). NO_BATCH=1이면 일반 호출만
followup-eval:
	uv run $(if $(wildcard .env),--env-file .env) python -m evaluation.followup run $(if $(NO_BATCH),--no-batch)

# ④ 진료 준비표(backend/prep.py) — 기준일 포함 최근 8일(과금), Batch(NO_BATCH=1이면 일반 호출). 사실·버전·모델이 같고 통과한 행은 건너뜀
prep:
	uv run $(if $(wildcard .env),--env-file .env) python -m backend.prep run $(if $(DATE),--date $(DATE)) $(if $(NO_BATCH),--no-batch)

# ⑤-2 청구 AI 점검(기능 정의 §5 ⑤, 과금·이어 돎) — SCOPE=all이면 전 기간(평가), 기본은 화면 범위
claim-check:
	uv run $(if $(wildcard .env),--env-file .env) python -m backend.claim_check run $(if $(SCOPE),--scope $(SCOPE)) $(if $(NO_BATCH),--no-batch)

# ⑤-2b 추가 처치 누락(설계 §2b, 과금·이어 돎) — SCOPE=all이면 제출·대기 줄(평가), 기본은 대기 줄(화면)
claim-omission:
	uv run $(if $(wildcard .env),--env-file .env) python -m backend.claim_omission run $(if $(SCOPE),--scope $(SCOPE)) $(if $(NO_BATCH),--no-batch)

# ⑤-3 서류 사유 조정 건의 차트 문장(설계 §3, 과금·이어 돎) — SCOPE=all이면 서류 사유 전부(평가), 기본은 기한 남은 건(화면)
claim-evidence:
	uv run $(if $(wildcard .env),--env-file .env) python -m backend.claim_evidence run $(if $(SCOPE),--scope $(SCOPE)) $(if $(NO_BATCH),--no-batch)

# ⑤-3 청구 기준 질문 초안(과금, 이어 돎) → 검토 페이지(사람, 승인 JSON) → make data(설계 §3)
claim-qa-draft:
	uv run $(if $(wildcard .env),--env-file .env) python -m evaluation.claim_qa draft
claim-qa-review:
	uv run python -m evaluation.claim_qa review

# ⑤-4 재심 사유서 — 정답 다툴 만함 dev 건에 초안(과금, 이어 돎, 설계 §4). test는 사람: python -m evaluation.claim_appeal run --split test
claim-appeal-dev:
	uv run $(if $(wildcard .env),--env-file .env) python -m evaluation.claim_appeal run --split dev

# ⑤-2b 평가 전용 합성 사례 — 코드가 정한 명세로 LLM이 차트 문장만(과금, 이어 돎, 설계 §2b)
claim-omission-cases:
	uv run $(if $(wildcard .env),--env-file .env) python -m evaluation.claim_omission cases

# ⑤-2b 합성 사례 dev 점검·채점(과금, 이어 돎). test는 사람: python -m evaluation.claim_omission synth·real --split test
claim-omission-synth:
	uv run $(if $(wildcard .env),--env-file .env) python -m evaluation.claim_omission synth --split dev

# ⑤-2 프롬프트 고칠 때 — dev 고정 표본만 점검하고 그 표본으로 채점(과금, 일반 호출 — BATCH=1이면 Batch 반값)
claim-check-sample:
	uv run $(if $(wildcard .env),--env-file .env) python -m evaluation.claim_check sample $(if $(N),--n $(N)) $(if $(BATCH),--batch)

# ⑤-2 버전을 정한 뒤 dev 전부(과금, Batch). test는 사람: python -m evaluation.claim_check run --split test
claim-check-dev:
	uv run $(if $(wildcard .env),--env-file .env) python -m evaluation.claim_check run --split dev $(if $(NO_BATCH),--no-batch)

# ① 자동 종결(기능 정의 §4 ①) — 활성 버전 쌍으로 다시 계산. auto_policy가 꺼져 있으면 아무것도 하지 않는다
autoclose:
	uv run python -m backend.autoclose run

auto-policy:
	$(if $(and $(KIND),$(ON)),,$(error KIND=작업 종류 ON=1|0 필요))
	uv run python -m backend.autoclose policy $(KIND) $(if $(filter 1,$(ON)),on,off)

# 와이어프레임 데이터(docs/wireframe/data.json — 정답 라벨 예시가 들어 있어 커밋하지 않음). make data 뒤에
wireframe:
	uv run python docs/wireframe/export.py

# 테스트는 병렬(pytest-xdist) — 작업자 수 JOBS(기본값은 make dev 몫을 남긴 수, 한 줄로 보려면 JOBS=0).
# worksteal은 먼저 끝난 작업자가 남은 테스트를 가져가 무거운 생성 테스트가 한 작업자에 몰려도 꼬리가 짧다
# TESTS는 일부만(예: TESTS=tests/generator — CI가 러너 둘에 나눠 돌린다)
JOBS ?= 8
test:
	uv run pytest -n $(JOBS) --dist worksteal $(TESTS)

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
# Qwen 판정기(새 통화 판정)는 finetune 그룹(mlx-lm)이 있어야 돈다 — Qwen 판정기 설계 "게이트웨이 로컬 갈래" 의존성
dev: frontend/node_modules
	trap 'kill 0' INT TERM EXIT; \
	uv run --group finetune $(if $(wildcard .env),--env-file .env) uvicorn backend.api:app --port 8000 & \
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

# ⑧-1 조사 에이전트(backend/recon_agent.py) — 지금 프롬프트 버전으로 조사 안 한 불일치 건(과금, 이어 돎). MONTHS="2026-08 2026-09"로 달 제한, NO_BATCH=1이면 일반 호출만
recon:
	uv run $(if $(wildcard .env),--env-file .env) python -m backend.recon_agent run $(foreach m,$(MONTHS),--month $(m)) $(if $(NO_BATCH),--no-batch)

# ⑧-2 증빙 탐색 에이전트(backend/expense_agent.py) — 지금 프롬프트 버전으로 조사 안 한 출금(과금, 이어 돎). MONTHS="2026-08 2026-09"로 달 제한, NO_BATCH=1이면 일반 호출만
expense-ai:
	uv run $(if $(wildcard .env),--env-file .env) python -m backend.expense_agent run $(foreach m,$(MONTHS),--month $(m)) $(if $(NO_BATCH),--no-batch)
