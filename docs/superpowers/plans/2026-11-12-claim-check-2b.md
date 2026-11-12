# ⑤-2 추가 처치 누락 (2b) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 제출·대기 청구 줄마다 "차트에 그날 했다고 적혔는데 청구에 없는 급여 처치"를 AI에 짧게 묻고(급여 판정은 코드), 청구 전 점검 화면에 보이며, 실데이터(test 전용)와 평가 전용 합성 사례로 키워드 기준선과 나란히 채점한다.

**Architecture:** 2a(`backend/claim_check.py`)와 같은 틀의 새 모듈 `backend/claim_omission.py` — 입력(사실) → 해시 → Batch/일반 호출 → 인용 후검증 → 한 번 더 → 저장(`ax_claim_omissions`). 원답(처치 이름·치아·인용)을 저장하고, 읽을 때 코드가 시술 표로 맞춰 비급여·나이 밖·청구된 것을 거른다. 평가는 `evaluation/claim_omission.py` — 합성 사례 명세(코드) → LLM이 차트 문장만 → 코드 검사 → 같은 프롬프트로 점검 → 항목 단위 채점.

**Tech Stack:** Python(sqlite3, `shared/llm.py` 게이트웨이 — 점검 별칭 `chat`, 합성 문장 별칭 `gen`), FastAPI, Next.js, pytest, Playwright.

**Spec:** `docs/superpowers/specs/2026-11-10-claim-review-design.md` §2b (위 §2 공통 규칙 포함)

## Global Constraints

- 입력에 청구 상태·조정 결과·사유를 넣지 않는다. 차트는 `emr_chart_notes.note`만(2a와 같음)
- `backend/`는 `ground_truth/`·`generator/`·`evaluation/`을 import하지 않는다. 나이 범위는 `master_values(conn)["age_group_range"]`, 시술 표는 `source.emr_procedures`
- 묻는 범위: `scope "app"` = 대기(`pending`) 줄, `scope "all"` = 제출·대기(`submitted`·`pending`) 줄 — 요건 대상 여부와 무관
- 급여 판정은 코드: 처치 이름이 `emr_procedures.name`과 같거나 별칭(`aliases`)이 시술 하나에만 맞으면 그 시술, 아니면 버림. 남기는 조건 = `is_insured` ≠ `no` · `is_tooth_level` = 1 · 치아가 `[1-4][1-8]` · 그날 나이가 `age_group_range[age_group]` 안 · (시술, 치아)가 청구된 (시술, 치아)와 다름
- 2a의 `omissions`는 그대로 두되 화면·채점은 2b만 쓴다
- 맞음 = (시술 코드, 치아)가 정답과 같음, **항목 단위**(처치만 맞고 치아가 틀리면 잘못 잡음 + 놓침). 키워드 기준선도 같은 거름·같은 규칙
- 실데이터 누락 줄(`truth_claim_errors` kind `omitted_extra`, 23줄)은 test에서만 센다. dev에서는 누락 정답이 없는 줄의 잘못 잡은 수만. 합성 사례는 사례 id 해시 반반, 프롬프트는 합성 dev로만 고친다. test 채점은 사람만(guard 훅이 `--split test`에 확인 창)
- 합성 사례의 실행 기록은 `evaluation/` 파일에만(`app.sqlite`에 쓰지 않음)
- 화면 딱지 문구: `차트에 있는데 청구에 없음(AI)`, 뒤에 `{시술} {치아}번: “{인용}”`
- 과금 명령은 실행 전에 건수·예상 금액을 사용자에게 보이고 승인받는다
- 주석·독스트링 한국어, 근거 문서 절을 가리킨다. 색은 `globals.css` 토큰, 부품은 `components/ui/`

## Review Focus

1. 별칭이 시술 둘에 걸리는 이름("임플란트" — 1차·2차) — 버린다(급여 판정을 추측하지 않음)
2. 청구된 시술과 같은 시술·같은 치아를 AI가 누락으로 답함 — 걸러져 화면·채점에 안 나온다. 같은 시술·다른 치아는 남는다
3. 대기 줄 중 2a 대상이 아닌 줄(스케일링 등)에만 2b 결과가 있음 — 화면 행의 `ai.requirement`가 null이어도 깨지지 않고, AI 칩 건수·`ai_checked`에 든다
4. 2a·2b 둘 다 다시 불러야 하는 줄 — 갱신 버튼 건수가 두 번 세지 않는다(줄 합집합)
5. 합성 문장이 검사에 계속 실패 — 3번까지 다시 쓰고 실패한 사례는 실패로 보고하고 저장하지 않는다(다음 실행에 이어서)

---

### Task 1: 입력·후검증·급여 거름 (순수 함수)

**Files:**
- Create: `backend/claim_omission.py`
- Test: `tests/backend/test_claim_omission.py`

**Interfaces:**
- Consumes: `backend.claim_check.found(quote, text) -> bool`, `MIN_QUOTE`
- Produces:
  - `TASK, ALIAS, PROMPT_VERSION = "claim_omission", "chat", "v1"`
  - `schema(names: list[str]) -> dict` — `{omissions: [{procedure: enum[names], tooth: str|null, quote: str}]}`, `additionalProperties: False`
  - `omission_facts(row: dict) -> dict` — `{procedure, tooth, age, note}`(row 키 `procedure_name`·`tooth_no`·`age`·`note`, 치아 "00"은 None)
  - `check_result(facts: dict, r: dict) -> bool` — 모든 `quote`가 `found(quote, note)`
  - `procedure_index(rows: list[dict]) -> dict[str, dict]` — 이름·별칭 → 시술 행(별칭이 둘 이상 시술에 걸리면 그 별칭은 빼고, 이름이 우선)
  - `insured_items(items: list[dict], claimed_code: str, facts: dict, index: dict, age_range: dict) -> list[dict]` — `[{procedure_code, procedure, tooth, quote}]`(Global Constraints의 거름)

- [ ] **Step 1: Write the failing tests**

```python
def test_facts_only_line_and_note():
    f = co.omission_facts(ROW | {"status": "submitted", "adjust_note": "x"})
    assert set(f) == {"procedure", "tooth", "age", "note"} and f["tooth"] is None  # 스케일링 00


def test_check_quote_in_note():
    assert co.check_result(
        F,
        {
            "omissions": [
                {"procedure": "아말감/GI", "tooth": "12", "quote": "12번 치아 아말감/GI 함께 진행"}
            ]
        },
    )
    assert not co.check_result(
        F, {"omissions": [{"procedure": "아말감/GI", "tooth": "12", "quote": "없는 문장입니다"}]}
    )
    assert co.check_result(F, {"omissions": []})


def test_index_drops_ambiguous_alias():
    idx = co.procedure_index(PROCS)
    assert idx["아말감"]["code"] == "U0006" and idx["아말감/GI"]["code"] == "U0006"
    assert "임플란트" not in idx and idx["임플란트 1차"]["code"] == "U0013"


def test_insured_items_filters():
    items = [
        it("아말감/GI", "12"),  # 남음
        it("레진", "14"),  # 비급여
        it("실란트", "36"),  # 성인 — 나이 밖
        it("구강검진", None),  # 치아 단위 아님
        it("스케일링", "00"),  # 청구된 것·치아 아님
        it("없는처치", "21"),  # 표에 없음
    ]
    got = co.insured_items(items, "U0001", F | {"age": 40}, co.procedure_index(PROCS), AGE_RANGE)
    assert [(x["procedure_code"], x["tooth"]) for x in got] == [("U0006", "12")]


def test_insured_items_same_procedure_other_tooth_kept():
    got = co.insured_items(
        [it("아말감/GI", "16"), it("아말감/GI", "15")],
        "U0006",
        F | {"tooth": "15", "age": 40},
        IDX,
        AGE_RANGE,
    )
    assert [x["tooth"] for x in got] == ["16"]


def test_child_sealant_kept():
    got = co.insured_items([it("홈메우기", "36")], "U0002", F | {"age": 9}, IDX, AGE_RANGE)
    assert got[0]["procedure_code"] == "U0004"
```

`ROW`는 스케일링 줄(`procedure_code` U0001·`tooth_no` "00"·나이 40·노트 "스케일링 시행하고 이어서 12번 치아 아말감/GI 함께 진행함."), `PROCS`는 `source.emr_procedures` 열 이름 그대로의 dict 목록(`aliases`는 JSON 문자열 — 생성기 `generator.master.PROCEDURES`에서 만든다), `AGE_RANGE`는 `generator.clinic.master_values_rows()`의 `age_group_range`.

- [ ] **Step 2: Run** `uv run pytest tests/backend/test_claim_omission.py -q` — Expected: FAIL(`ModuleNotFoundError: backend.claim_omission`)
- [ ] **Step 3: Implement** 위 Produces를 `backend/claim_omission.py`에(모듈 독스트링: 하는 일·설계 §2b). 나이 범위 상한 None은 상한 없음
- [ ] **Step 4: Run** 같은 명령 — Expected: PASS
- [ ] **Step 5: Commit** `git add backend/claim_omission.py tests/backend/test_claim_omission.py` · `backend: ⑤-2b 추가 처치 누락 입력·후검증·급여 거름`

### Task 2: 저장·대상·실행·명령

**Files:**
- Modify: `shared/app_schema.sql`(2a 표 아래에 `ax_claim_omissions`, 칸은 `ax_claim_checks`와 같음, 주석에 result 모양), `backend/claim_omission.py`, `Makefile`
- Create: `prompts/claim_omission/v1.md`
- Test: `tests/backend/test_claim_omission.py`

**Interfaces:**
- Produces:
  - `targets(conn, today, *, scope: "app"|"all", claim_ids=None) -> list[dict]` — 행: `claim_id, procedure_code, procedure_name, tooth_no, status, note, age`(2a `targets`처럼 `emr_claims`·`emr_procedures`·`emr_patients`·`emr_chart_notes` 조인)
  - `todo_ids(conn, today, model=None) -> set[int]` — 앱 범위에서 다시 부를 줄(서술 없는 줄 제외)
  - `build(conn, gw, rows, *, use_batch, poll_seconds=30) -> dict` — 2a `build`와 같은 흐름·같은 반환 키. 서술 없는 줄은 부르지 않고 `{"omissions": []}`로 저장
  - `results(conn, claim_ids=None) -> dict[int, dict]` — `{claim_id: {omissions: insured_items(...) | None, valid, at, prompt_version}}`(거름은 읽을 때, 시술 표·나이 범위는 여기서 한 번 읽음)
  - `main(argv)` — `python -m backend.claim_omission run [--scope app|all] [--no-batch]`
  - Makefile `claim-omission` — `make claim-omission [SCOPE=all] [NO_BATCH=1]`(과금, 이어 돎)
- 2a와 공유: `claim_check._hash`·`_norm`·`found`, `backend.prep._age`·`_batchable`·`master_values`(`_batchable`은 별칭 `chat`이 같아 그대로)

- [ ] **Step 1: Write the failing tests**(2a `tests/backend/test_claim_check.py`의 `FakeGw`·`src`·`claim` 꼴, 대본 답은 `{"omissions": [...]}`)
  - `test_targets_scope` — 대기·제출·확정 줄 하나씩: `app` → 대기만, `all` → 대기·제출, 스케일링 줄도 든다
  - `test_build_saves_and_reuses` — 첫 실행 호출 1, 같은 입력 두 번째 호출 0
  - `test_build_retries_once_then_null` — 인용이 없는 답 둘 → `valid` False·`omissions` None
  - `test_empty_note_not_called` — 호출 0, 결과 `omissions == []`
  - `test_results_filtered` — 대본 답에 비급여 레진 + 아말감/GI → `results`에는 아말감/GI만
  - `test_request_has_no_status` — 게이트웨이가 받은 메시지에 `submitted`·조정 문구가 없고, 스키마 `procedure` enum이 시술 이름 19개
  - `test_todo_ids_app_only` — 제출 줄은 세지 않는다
- [ ] **Step 2: Run** `uv run pytest tests/backend/test_claim_omission.py -q` — Expected: 새 테스트 FAIL(`targets` 없음)
- [ ] **Step 3: Implement** 표·함수·프롬프트. 프롬프트 v1 요지: 입력은 청구 줄(시술·치아·나이)과 차트 서술, "그날 이 진료에서 **시행했다고** 적힌 처치 중 청구 줄의 시술·치아가 아닌 것"만, 계획·권유·거절·다른 병원·지난 내원은 넣지 않음, 처치 이름은 보기(enum)에서, 치아는 차트에 적힌 번호, 인용은 글자 그대로 한 구절~한 문장, 없으면 빈 목록. 급여 여부는 묻지 않는다
- [ ] **Step 4: Run** 같은 명령 + `uv run pytest tests/backend/test_claim_check.py -q` — Expected: PASS
- [ ] **Step 5: Commit** 표·모듈·프롬프트·Makefile·테스트 · `backend: ⑤-2b 추가 처치 누락 저장·실행·make claim-omission`

### Task 3: 화면 — 청구 전 점검에 붙이기

**Files:**
- Modify: `backend/claim_check.py`(`pending`을 `todo_ids` 위로 — 반환 모양 그대로), `backend/claims.py`(`pre_claim`), `backend/api.py`(`_run_claim_check_job`·`claim_check_run`), `frontend/lib/claims-api.ts`, `frontend/components/claims/pre-claim-table.tsx`, `frontend/e2e/claims.spec.ts`
- Test: `tests/backend/test_claims.py`(또는 2a 화면 테스트가 있는 파일), `tests/backend/test_claim_check.py`

**Interfaces:**
- Consumes: `claim_omission.results`, `claim_omission.todo_ids`, `claim_omission.targets`, `claim_omission.build`
- Produces:
  - `claim_check.todo_ids(conn, today, model=None) -> set[int]`
  - `pre_claim` 행 `ai = {requirement: 2a 통과면 값 아니면 None, evidence: 2a 통과면 값 아니면 [], omissions: 2b 통과면 거른 목록 아니면 []} | None`(둘 다 없거나 둘 다 실패면 None). `counts.ai` = `requirement == "missing"` 또는 `omissions`가 있는 줄, `counts.ai_checked` = 2a나 2b가 저장된 대기 줄 수, `ai_at` = 둘 중 가장 늦은 시각, `ai_pending.pre` = 대기 줄 중 (2a 할 일 ∪ 2b 할 일) 수, `ai_pending.adjust`는 지금 그대로
  - 작업: 2a `build` 뒤 2b `build`(둘 다 `scope "app"`, 일반 호출), `total`·`done` = 두 대상 줄의 합집합 수
  - TS `ClaimCheck = {requirement: "met"|"missing"|"na"|null; evidence: Evidence[]; omissions: {procedure_code: string; procedure: string; tooth: string; quote: string}[]}`

- [ ] **Step 1: Write the failing tests**
  - `test_pre_claim_uses_2b_omissions` — 2a 결과 omissions `[아말감]`, 2b 결과 `[]` → 행 `ai.omissions == []`·`counts.ai == 0`; 2b에 아말감/GI 12 → `counts.ai == 1`
  - `test_pre_claim_2b_only_line` — 스케일링 대기 줄에 2b만 → `ai.requirement is None`, `counts.ai_checked == 1`
  - `test_pending_union` — 2a·2b 둘 다 할 일인 대기 줄 하나 → `ai_pending.pre == 1`
  - `test_job_runs_both` — 작업 뒤 `claim_check.pending == {"pre": 0, "adjust": 0}`이고 `claim_omission.todo_ids == set()`
- [ ] **Step 2: Run** `uv run pytest tests/backend/test_claims.py tests/backend/test_claim_check.py -q` — Expected: 새 테스트 FAIL
- [ ] **Step 3: Implement** 백엔드, 그다음 화면: `AiFinding`의 누락 줄을 `<Badge variant="warn">차트에 있는데 청구에 없음(AI)</Badge>` + `{o.procedure} {o.tooth}번: “{o.quote}”`로, `requirement`가 null이면 요건 딱지 없음. e2e 가짜 응답의 누락 항목에 `procedure_code`·`tooth`를 넣고 딱지·문구를 확인
- [ ] **Step 4: Run** 위 pytest + `make lint` + `cd frontend && npx playwright test e2e/claims.spec.ts`(로컬, `make e2e`와 같은 설정) — Expected: PASS
- [ ] **Step 5: Commit** · `backend·frontend: ⑤-2b 누락을 청구 전 점검 AI 칩에` (영역은 `backend`, 프론트는 같은 커밋 본문에 적거나 `frontend:` 커밋으로 나눔)

### Task 4: 합성 사례 — 명세·검사·문장 쓰기

**Files:**
- Create: `evaluation/claim_omission.py`, `prompts/gen_claim_omission.md`
- Test: `tests/evaluation/test_claim_omission_eval.py`

**Interfaces:**
- Produces:
  - `SHAPES_HIT = ("plain", "other_name", "separated", "tooth_first", "child_sealant")`, `SHAPES_DECOY = ("planned", "declined", "elsewhere", "past_visit", "claimed_again", "adult_sealant", "uninsured")`
  - `case_specs() -> list[dict]` — 누락 모양마다 4건, 디코이마다 3건(41건), `random.Random(f"{seed}-claim_omission")`(`config/generator.yaml` seed). 사례: `{id: "om-01"…, shape, claimed: {procedure_code, procedure, tooth, age}, extra: {procedure_code, procedure, tooth}, truth: [(code, tooth)] | []}`. 청구 시술은 급여 줄(스케일링·신경치료·단순발치·잇몸치료, 소아는 구강검진), 추가 처치는 누락 모양이 아말감/GI(`child_sealant`만 실란트, 나이 6~12), 디코이 `adult_sealant`는 실란트·나이 19 이상(했다고 적혀도 정답 없음), `uninsured`는 레진, `claimed_again`은 청구 시술·치아 그대로. 추가 처치 치아는 청구 치아와 다름
  - `split_of(case_id: str) -> "dev"|"test"` — sha256 반반
  - `check_note(spec, note) -> list[str]` — 문제 목록(빈 목록 = 통과): `extra.tooth`의 `NN번`과 청구 시술 이름(또는 별칭)이 있음, 모양 표지(`other_name`: "GI"·"글라스아이오노머" 중 하나 있고 "아말감"·"때움" 없음 / `planned`: "예정"·"다음" / `declined`: "거절"·"원치"·"보류"·"미루" / `elsewhere`: "다른 병원"·"타 병원"·"다른 치과" / `past_visit`: "지난"·"이전" / `uninsured`: "레진" / 실란트 모양: "실란트"·"홈메우기"·"홈 메우기"), `generator.master.CHART_FORBIDDEN_WORDS` 없음
  - `write_cases(gw, path, *, attempts=3) -> dict` — 명세 해시가 같은 줄이 `path`(기본 `evaluation/claim_omission_cases.jsonl`)에 있으면 건너뜀, 아니면 별칭 `gen`으로 한 단락을 쓰고 `check_note` 통과할 때까지 최대 3번(다시 쓸 때 문제 목록을 알려 줌), 통과만 한 줄씩 덧붙임. 반환 `{written, skipped, failed: [id]}`
  - `load_cases(path) -> list[dict]` — 명세 + `note`, 지금 명세 해시와 맞는 줄만
- 문장 프롬프트(`prompts/gen_claim_omission.md`) 요지: 실데이터 말투(소견·환자 말·시행·다음 계획 1~4문장), 모양 설명은 코드가 사례마다 한국어 한 줄로 넘김, "삭감·요건·청구" 같은 말 금지

- [ ] **Step 1: Write the failing tests**
  - `test_specs_fixed_and_counts` — 두 번 부른 결과가 같고 41건, 누락 모양 20건만 `truth`가 있음, 모든 `extra.tooth != claimed.tooth`
  - `test_specs_age_rules` — `child_sealant` 나이 6~12, `adult_sealant` 19 이상
  - `test_split_half` — 41건 중 dev 15~26건
  - `test_check_note_markers` — `other_name`에 "아말감"·"때움"이 있으면 문제, `planned`에 "예정" 없으면 문제, 금지어 있으면 문제, 맞는 문장은 `[]`
  - `test_write_cases_retries_and_resumes` — 가짜 게이트웨이가 첫 답은 검사 실패·둘째 통과 → 파일에 1줄, 다시 부르면 호출 0
- [ ] **Step 2: Run** `uv run pytest tests/evaluation/test_claim_omission_eval.py -q` — Expected: FAIL(모듈 없음)
- [ ] **Step 3: Implement**(모듈 독스트링: 하는 일·설계 §2b·정답이 붙은 입력이라 기록은 `evaluation/`에만)
- [ ] **Step 4: Run** 같은 명령 — Expected: PASS
- [ ] **Step 5: Commit** · `eval: ⑤-2b 합성 사례 명세·문장 검사·쓰기`

### Task 5: 채점 — 키워드 기준선·합성·실데이터·make eval

**Files:**
- Modify: `evaluation/claim_omission.py`, `evaluation/eval.py`, `Makefile`
- Test: `tests/evaluation/test_claim_omission_eval.py`

**Interfaces:**
- Consumes: Task 1 `insured_items`·`procedure_index`, Task 2 `targets`·`results`·`build`의 요청 꼴(`claim_omission._request`)
- Produces:
  - `match(pred: list[tuple], truth: list[tuple]) -> dict` — `{tp, fp, fn}`(항목 단위, 중복 없는 (코드, 치아))
  - `keyword_items(note: str, claimed_code: str, claimed_tooth: str|None, age: int, index: dict, age_range: dict) -> list[tuple]` — 문장(마침표·줄바꿈)마다 `NN번` 치아(청구 치아가 아닌 것)와 시술 이름·별칭이 같이 있으면 (코드, 치아), 그 뒤 `insured_items`와 같은 거름. 계획·부정을 가리지 않는다
  - `run_synth(gw, split, cases_path, runs_path) -> dict` — 그 분할 사례를 `claim_omission`과 같은 요청·후검증·한 번 더로 점검, 기록 `evaluation/claim_omission_runs.jsonl`(`{id, note_hash, prompt_version, model, result}`, 같은 키면 다시 부르지 않음)
  - `score_synth(cases, runs, split) -> dict` — `{n, ai: pr, keyword: pr, by_shape: {shape: {n, ai_ok, keyword_ok}}, failed}`(pr = 2a `_pr`과 같은 키, 후검증 실패는 빈 답)
  - `score_real(conn, split) -> dict` — 지금 버전으로 점검한 제출·대기 줄. dev: 누락 정답 줄을 빼고 `{lines, ai_fp_lines, ai_fp_items, keyword_fp_lines}`, test: 그에 더해 `{hits: {found, n}}`(정답 23줄 중 항목이 맞은 수)과 `ai`·`keyword` pr
  - `main` — `cases`(과금, 이어 돎) · `synth --split dev|test`(과금, 이어 돎 — test는 사람, 버전마다 한 번) · `real --split test`(사람, 과금 없음 — 저장 결과로 채점, 버전마다 한 번, `reports/claim_omission_runs.jsonl`)
  - `eval.py`: dev에서 `claim_omission` 결과(합성 dev 기록이 있으면 `score_synth`, 저장된 2b가 있으면 `score_real(dev)`), `_render_claim_omission`
  - Makefile `claim-omission-cases`, `claim-omission-synth`(dev)
- [ ] **Step 1: Write the failing tests**
  - `test_match_items` — 맞는 것 + 다른 것 하나 → `{tp:1, fp:1, fn:0}`, 치아만 틀림 → `{tp:0, fp:1, fn:1}`
  - `test_keyword_catches_plan_sentence` — "남은 치료는 46번 치아 아말감/GI" → `[("U0006","46")]`(기준선의 약점을 그대로), "레진" 문장은 거름
  - `test_score_synth` — 사례 둘(누락·디코이)과 기록 → AI·키워드 pr과 `by_shape`
  - `test_score_real_dev_hides_positives` — 누락 정답 줄이 dev에 있어도 dev 결과에 `hits`가 없고 그 줄은 `lines`에 안 든다
  - `test_score_real_test_hits` — 정답 항목과 같은 AI 결과 → `hits == {found: 1, n: 1}`
  - `test_test_runs_once` — 2a `check_test` 꼴, 같은 버전 두 번째 `real --split test`는 `AlreadyRun`
  - `test_render` — `_render_claim_omission`에 "⑤-2b"·"키워드"가 있음
- [ ] **Step 2: Run** `uv run pytest tests/evaluation/test_claim_omission_eval.py -q` — Expected: 새 테스트 FAIL
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run** 같은 명령 + `uv run pytest tests/evaluation -q` — Expected: PASS
- [ ] **Step 5: Commit** · `eval: ⑤-2b 채점 — 항목 단위·키워드 기준선·합성·실데이터, make eval`

### Task 6: 문서

**Files:**
- Modify: `docs/치과 AX - 평가 설계.md` §5 ⑤-2(추가 처치 누락 범위 "급여 청구 줄 전부" → 제출·대기 월 급여 줄, 항목 단위·키워드 기준선·합성 dev/test·실데이터 dev는 잘못 잡은 수만), `docs/치과 AX - 앱 설계.md`(`ax_claim_omissions` 표 이름·청구 전 점검 화면 딱지), `docs/치과 AX - 기능 정의.md` §5 ⑤-2 완료 기준 문구와 §9 ⑤ 항목(2b 남은 것 → test 채점·합성 test는 사람), `CLAUDE.md` 명령 ⑤ 줄(`make claim-omission`·`claim-omission-cases`·`claim-omission-synth`)
- [ ] **Step 1:** 고친 뒤 `uv run pytest tests/generator/test_doc_sync.py -q` — Expected: PASS
- [ ] **Step 2: Commit** 설계 문서(spec)와 함께 · `docs: ⑤-2b 추가 처치 누락 — 범위·채점·명령`

### Task 7: 실행(과금 — 단계마다 사용자 승인)

- [ ] **Step 1:** `make claim-omission-cases` — 41건 × 최대 3번, 예상 ~$0.3. 결과 파일을 훑고(모양별 한 건씩 사용자에게 보임) 커밋 `evaluation/claim_omission_cases.jsonl`
- [ ] **Step 2:** `make claim-omission-synth` — 합성 dev ~20건 일반 호출, 예상 ~$0.1. AI·키워드 비교를 보고 프롬프트를 고치면 v2(합성 dev로만)
- [ ] **Step 3:** `make claim-omission SCOPE=all` — 678줄 Batch, 예상 ~$2. 그다음 `make eval`로 실데이터 dev 잘못 잡은 수
- [ ] **Step 4:** `docs/치과 AX - AI 실험 기록.md` §28 — 문제·원인·시도·결과(합성 dev 표·실데이터 dev 잘못 잡은 수)·한계·비용. 커밋 `docs: AI 실험 기록 §28 ⑤-2b 추가 처치 누락`
- test(`synth --split test`·`real --split test`)는 사람
