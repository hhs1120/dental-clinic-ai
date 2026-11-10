# ① 분류 프롬프트 관문 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `make prompt-gate A=<기준> B=<후보> ITEM=<목표 항목>`이 검증 구간 405건에서 두 버전을 3번씩 분류해 다수결로 비교하고 합격/불합격을 낸다.

**Architecture:** `evaluation/prompt_gate.py` 하나. 순수 함수(값 꺼내기·다수결·짝 비교·판정)와 입출력(모수·실행 저장·재사용·시도 수·정책 확인·CLI)을 나눈다. 게이트웨이는 주입받아 테스트는 `tests/backend/script.py` `Script`로 돈다.

**Tech Stack:** Python 3, sqlite3, `shared/llm.py` Gateway·JsonlLog, pytest.

**Spec:** `docs/superpowers/specs/2026-11-09-prompt-gate-design.md`

## Global Constraints

- `evaluation/`에서만 돈다. 앱 DB에 쓰지 않는다 — `ax_settings.auto_policy` 읽기만
- test 분할·test 판정을 읽지 않는다. 모수는 `truth_call_labels.split='silver'` 중 `silver_split` = `validation`
- 급함은 urgent 여부만 비교, 플래그는 0/1로 맞춰 비교
- 값은 `evaluation/metrics.py`에 이름으로: `GATE_RUNS = 3`, `GATE_P = 0.05`, `GATE_FALSE_ALARM_CAP = 2`, `GATE_REGRESSION_CAP = 3`, `GATE_ATTEMPTS = 2`
- 조건 enum: `target_up`, `no_new_miss`, `false_alarm_cap`, `no_regression` (실행 전 멈춤: `attempt_cap`, `auto_policy_on`, `holdout_in_population`)
- 출력·기록에 `call_uid`를 쓰지 않는다. 건별 실행은 `evaluation/runs/prompt-gate/<버전>/`(커밋 안 함), 판정 기록은 `reports/prompt_gate.jsonl`(커밋)
- 폴백 답·무효 라벨은 실패 → 통화당 3번까지 다시, 그래도 비면 판정 없이 멈춤(기록 없음)
- 실행 무효화 키 = `prompts/classify/<버전>/labels.md` sha256 + 첫 모델 이름(`prompts.model("classify", v)` 또는 `config/llm.yaml` `aliases.classify[0]`)
- 시도 수 = 기록 중 같은 기준·후보≠기준·같은 관문 값 해시의 서로 다른 후보 수. `A == B` 판정은 기록하되 세지 않는다
- 주석·독스트링은 한국어, 근거 문서 절을 가리킨다(CLAUDE.md)

## Review Focus

1. 폴백 모델(`claude-sonnet-5-5`)이 답한 라벨이 다수결에 섞임 → 실패로 다시 불러야 한다 (Task 2 `test_fallback_answer_is_retried`)
2. 프롬프트 파일을 고친 뒤 같은 버전 이름으로 다시 돌림 → 저장된 실행을 버리고 새로 돈다 (Task 2 `test_prompt_change_discards_runs`)
3. 이미 판정한 후보를 다시 돌림 → 저장된 실행으로 같은 판정, 시도 수는 늘지 않는다 (Task 3 `test_same_candidate_rerun_not_counted`)
4. 다수결에서 셋이 다 다른 목적 값 → 첫 실행 값, 결정적 (Task 1 `test_majority_three_way_takes_first`)
5. 터미널 출력이나 기록 행에 통화 id가 새어 나감 (Task 3 `test_output_and_record_have_no_call_uid`)

---

### Task 1: 순수 판정 함수와 관문 값

**Files:**
- Modify: `evaluation/metrics.py` (관문 상수 5개 — 평가 설계 §2.7 주석)
- Create: `evaluation/prompt_gate.py`
- Test: `tests/evaluation/test_prompt_gate.py`

**Interfaces:**
- Produces:
  - `ITEMS: tuple[str, ...]` = `evaluation.classification.ITEMS` (9항목, `urgency` 포함)
  - `value(labels: dict, item: str) -> str | int` — `urgency`는 `"urgent"`면 1 아니면 0, 플래그는 `int(bool)`, 그 밖은 값 그대로. 모델 라벨·골드 행에 같이 쓴다
  - `majority(runs: list[dict[str, dict]]) -> dict[str, dict]` — 통화 → 항목 → 다수결 값(2벌 이상 같으면 그 값, 셋이 다 다르면 첫 실행 값)
  - `compare(base: dict, cand: dict, gold: dict) -> dict[str, dict]` — 항목 → `{"base_wrong", "cand_wrong", "b", "c", "p"}` (b = 기준 틀림·후보 맞음, c = 기준 맞음·후보 틀림, p = `mcnemar_exact(b, c)`), 그리고 `"emergency"`·`"complaint"` 항목에 `"new_miss"`(골드 1, 기준 맞음, 후보 0)·`"false_alarm_delta"`(후보 오탐 − 기준 오탐)
  - `decide(stats: dict, item: str) -> list[str]` — 못 넘은 조건 이름 목록(빈 목록 = 합격)

- [ ] **Step 1: 실패하는 테스트 작성** — 골드·라벨은 작은 dict로 직접 만든다

```python
def test_value_urgency_and_flags():
    assert pg.value({"urgency": "low"}, "urgency") == 0
    assert pg.value({"urgency": "urgent"}, "urgency") == 1
    assert (
        pg.value({"has_todo": True}, "has_todo") == 1
        and pg.value({"has_todo": 0}, "has_todo") == 0
    )


def test_majority_two_to_one():
    runs = [{"c1": lab(has_todo=1)}, {"c1": lab(has_todo=0)}, {"c1": lab(has_todo=1)}]
    assert pg.majority(runs)["c1"]["has_todo"] == 1


def test_majority_three_way_takes_first():
    runs = [{"c1": lab(primary_purpose=p)} for p in ("book", "cancel", "other")]
    assert pg.majority(runs)["c1"]["primary_purpose"] == "book"


def test_compare_counts_b_c():
    # 기준 틀림·후보 맞음 6건, 반대 0건 → b=6, c=0, p=0.03125
    s = pg.compare(base, cand, gold)["has_todo"]
    assert (s["b"], s["c"]) == (6, 0) and s["p"] == pytest.approx(0.03125)


def test_decide_pass():
    assert pg.decide(stats_ok, "has_todo") == []


def test_decide_target_not_significant():
    assert "target_up" in pg.decide(stats_b5_c0, "has_todo")  # p=0.0625


def test_decide_new_emergency_miss():
    assert "no_new_miss" in pg.decide(stats_new_miss_1, "has_todo")


def test_decide_false_alarm_cap():
    assert "false_alarm_cap" in pg.decide(stats_fa_delta_3, "has_todo")  # 2는 통과


def test_decide_regression_cap():
    assert "no_regression" in pg.decide(stats_counterpart_c4_b0, "has_todo")  # 증가 4 > 3


def test_decide_regression_within_cap():
    assert pg.decide(stats_counterpart_c3_b0, "has_todo") == []


def test_decide_target_down_not_target_up():
    assert "target_up" in pg.decide(stats_b0_c6, "has_todo")
```

`lab(**kw)`는 9항목 기본값(`book`·`patient`·`normal`·플래그 0)에 kw를 덮는 테스트 도우미. `stats_*`는 `compare`가 내는 모양을 직접 적는다.

- [ ] **Step 2: 실패 확인** — Run: `uv run pytest tests/evaluation/test_prompt_gate.py -q` / Expected: FAIL (`prompt_gate` 없음)
- [ ] **Step 3: 구현** — `no_regression`은 목표 밖 항목마다 `c > b and p < GATE_P` 또는 `c - b > GATE_REGRESSION_CAP`이면 실패. `no_new_miss`·`false_alarm_cap`은 `emergency`·`complaint` 둘 다 본다(목표 항목이어도). 모듈 독스트링에 하는 일과 원본(스펙·평가 설계 §2.5·§2.7)
- [ ] **Step 4: 통과 확인** — 같은 명령 / Expected: PASS
- [ ] **Step 5: 커밋** — 마지막 Task에서 한 번에(영역 `eval`)

### Task 2: 모수와 실행 저장·재사용

**Files:**
- Modify: `evaluation/prompt_gate.py`
- Test: `tests/evaluation/test_prompt_gate.py`

**Interfaces:**
- Consumes: Task 1 `ITEMS`
- Produces:
  - `RUNS_DIR = Path("evaluation/runs/prompt-gate")`
  - `population(gold_rows: list[dict]) -> dict[str, dict]` — silver 행만 받아 `validation` 통화 → 골드 행. 입력에 `split != 'silver'` 행이 있으면 `SystemExit("holdout_in_population")`
  - `fingerprint(version: str, config: dict, root: Path = PROMPTS_DIR) -> dict` — `{"prompt_sha256", "model"}`
  - `load_runs(version: str, fp: dict, root: Path = RUNS_DIR) -> list[dict[str, dict]]` — `meta.json`이 `fp`와 다르면 폴더의 run 파일을 지우고 빈 3벌
  - `fill_runs(gw, version: str, calls: dict[str, tuple[str, str]], fp: dict, root: Path = RUNS_DIR) -> list[dict[str, dict]]` — 3벌에서 빠진 통화만 부르고 줄마다 덧붙임(`{"call_uid", "labels"}`), `meta.json` 기록. 결과가 `not r.ok`·`not valid_labels(r.data)`·`r.fallback_used`·`r.model != fp["model"]`면 실패로 같은 통화를 3번까지 다시. 그래도 빈 통화가 있으면 `SystemExit("빈 통화 N건 — 판정하지 않음")`. 호출은 `ThreadPoolExecutor(8)`, 인자는 `cl.label_request(transcript, direction, version)` + `max_tokens=cl.MAX_TOKENS`, LLM 로그는 `root / version / "llm.jsonl"`(게이트웨이는 호출하는 쪽이 만든다)

- [ ] **Step 1: 실패하는 테스트** (`tmp_path`를 root로, `Script`로 응답 고정)
  - `test_population_rejects_holdout` — dev 행이 섞이면 SystemExit
  - `test_population_is_validation_only` — `silver_split`의 validation 수와 같음
  - `test_fill_calls_only_missing` — run1에 2건이 이미 있으면 그 통화는 부르지 않는다(호출 수 = 3×N − 2)
  - `test_saved_runs_reused_without_calls` — 두 번째 `fill_runs`에서 `Script()` 빈 대본으로도 통과
  - `test_prompt_change_discards_runs` — `fp`의 `prompt_sha256`만 바꾸면 다시 모두 부른다
  - `test_fallback_answer_is_retried` — 첫 응답 `fallback_used=True`, 둘째 정상 → 저장 값은 둘째
  - `test_empty_after_three_failures_stops` — 같은 통화 3번 실패 → SystemExit, 그 통화 줄 없음
- [ ] **Step 2: 실패 확인** — Run: `uv run pytest tests/evaluation/test_prompt_gate.py -q` / Expected: 새 테스트 FAIL
- [ ] **Step 3: 구현**
- [ ] **Step 4: 통과 확인** — Expected: PASS

### Task 3: 판정 실행·시도 수·정책 확인·CLI

**Files:**
- Modify: `evaluation/prompt_gate.py`, `Makefile`(`prompt-gate` 타깃 — 주석 한 줄, `$(if $(and $(A),$(B),$(ITEM)),,$(error ...))`, `uv run --env-file .env python -m evaluation.prompt_gate --a $(A) --b $(B) --item $(ITEM)`)
- Test: `tests/evaluation/test_prompt_gate.py`

**Interfaces:**
- Consumes: Task 1·2 전부
- Produces:
  - `REPORT = Path("reports/prompt_gate.jsonl")`
  - `gate_hash() -> str` — 관문 상수 5개를 정렬 JSON으로 sha256 앞 12자
  - `attempts(base: str, report: Path = REPORT) -> set[str]` — 같은 기준·후보≠기준·`gate_hash` 같은 행의 후보 버전 집합
  - `run_gate(gw, a: str, b: str, item: str, *, gold_rows, calls, policy: dict, config: dict, runs_root: Path = RUNS_DIR, report: Path = REPORT) -> dict` — 순서: `item not in ITEMS`면 SystemExit → `policy`에 참 값이 있으면 `SystemExit("auto_policy_on")` → `b != a and b not in attempts(a) and len(attempts(a)) >= GATE_ATTEMPTS`면 `SystemExit("attempt_cap")` → 모수 → 두 버전 `fill_runs` → `majority`·`compare`·`decide` → 기록 1행 덧붙이고 같은 dict 반환. 행 칸: `at, base, candidate, item, gate, gate_hash, model, n, items(항목별 base_wrong·cand_wrong·b·c·p), failed(조건 목록), passed, cost_usd`
  - `main(argv=None)` — truth.sqlite·source.sqlite·app.sqlite를 읽기 전용(`?mode=ro`)으로 열어 `gold_rows`·`calls`·`policy`를 만들고 `Gateway(load_config(), JsonlLog(...), "runtime")`로 `run_gate`. 터미널에 항목별 표(기준 오답·후보 오답·b·c·p)와 조건별 통과·합격 여부

- [ ] **Step 1: 실패하는 테스트**
  - `test_gate_pass_records_row` — 후보가 has_todo를 6건 고치는 대본 → `passed=True`, 기록 1행, `failed == []`
  - `test_auto_policy_on_stops_before_calls` — `policy={"call_autoclose": True}` → SystemExit, 게이트웨이 호출 0, 기록 없음
  - `test_attempt_cap_stops_third_candidate` — 기록에 v3·v4 판정 행(같은 `gate_hash`) → `B=v5`는 SystemExit, 호출 0
  - `test_same_candidate_rerun_not_counted` — 기록에 v3·v4 → `B=v3`는 돈다(저장 실행 재사용, 호출 0)
  - `test_gate_hash_change_resets_count` — 기록 행의 `gate_hash`가 다르면 세지 않는다
  - `test_self_comparison_not_counted` — `A=v2 B=v2` 행은 `attempts`에 안 들어간다
  - `test_unknown_item_rejected` — `item="foo"` → SystemExit
  - `test_output_and_record_have_no_call_uid` — `main` 출력(capsys)과 기록 줄 어디에도 모수의 call_uid 문자열이 없다(`main`은 경로 인자를 주입할 수 있게 `argv`에 `--truth`·`--source`·`--app`·`--report`·`--runs` 기본값 옵션)
- [ ] **Step 2: 실패 확인** / Expected: 새 테스트 FAIL
- [ ] **Step 3: 구현**
- [ ] **Step 4: 통과 확인** — Run: `uv run pytest tests/evaluation/test_prompt_gate.py tests/test_no_ground_truth_import.py -q` / Expected: PASS
- [ ] **Step 5: `make fmt` → `make lint` → `make test`** / Expected: 모두 통과
- [ ] **Step 6: 커밋**

```bash
git add evaluation/metrics.py evaluation/prompt_gate.py tests/evaluation/test_prompt_gate.py Makefile
git commit -m "eval: ① 분류 프롬프트 관문 — 3번 다수결 짝 비교, 시도 2번"
```

### Task 4: v2 기준 실행 옮기기·실측·문서

**Files:**
- Modify: `docs/치과 AX - 평가 설계.md` §2.7, `docs/치과 AX - 기능 정의.md` §4 ②·§8 ① 행·§9, `docs/치과 AX - AI 실험 기록.md`(새 절), `CLAUDE.md` 명령 목록, 스펙 문서(실측 결과 한 줄)
- Create: `reports/prompt_gate.jsonl`(실측 1행)

- [ ] **Step 1: v2 3벌 옮기기** — `evaluation/runs/variance-v2/run{1,2,3}.jsonl`을 `evaluation/runs/prompt-gate/v2/`로 복사하고 `meta.json`에 `fingerprint("v2", load_config())` 기록(파이썬 한 줄, 코드로 남기지 않음). 확인: 각 파일 405줄, 모든 `labels`가 `valid_labels`
- [ ] **Step 2: 실측** — Run: `make prompt-gate A=v2 B=v2 ITEM=has_todo` / Expected: 게이트웨이 호출 0(저장 재사용), `target_up` 실패(b = c = 0), `no_new_miss`·`no_regression`·`false_alarm_cap` 통과, 기록 1행(`attempts`에 안 셈)
- [ ] **Step 3: 문서** — 스펙 "문서 반영" 절대로. 평가 설계 §2.7은 관문 규칙을 값 이름(`evaluation/metrics.py` `GATE_*`)으로 가리키고 숫자를 옮겨 적지 않는다. 미룬 자동 개선은 한 묶음 문단으로. 기능 정의 §9의 "자동 개선 착수 시" 줄은 "자동 개선(후보 작성 에이전트) 착수 시 — 대상·재료·누출 차단·원칙 9 자동 처리 조건"으로, 착수 순서 첫 항목은 `make gate`로. AI 실험 기록 새 절(문제 → 원인 → 시도 → 결과 → 한계: 흔들림 표, 다수결·시도 2번을 고른 근거, 자동 개선을 미룬 근거, 한계 = 다수결이 흔들림을 얼마나 줄이는지는 재지 않음)
- [ ] **Step 4: 문서 훑기** — 바뀐 절에 날짜·경위·중복이 없는지(문서에는 규칙만, 경위는 실험 기록)
- [ ] **Step 5: 커밋**

```bash
git add reports/prompt_gate.jsonl CLAUDE.md docs/superpowers/specs/2026-11-09-prompt-gate-design.md docs/superpowers/plans/2026-11-09-prompt-gate.md "docs/치과 AX - 평가 설계.md" "docs/치과 AX - 기능 정의.md" "docs/치과 AX - AI 실험 기록.md"
git commit -m "docs: 프롬프트 관문 규칙·흔들림 실측, 자동 개선 미룸"
```
