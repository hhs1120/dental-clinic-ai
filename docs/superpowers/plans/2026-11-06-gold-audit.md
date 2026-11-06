# 골드 점검 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 판정기와 골드가 다른 통화를 Sonnet 중재 판정기와 사람 표본 50통화로 점검해, 골드 오류 추정과 중재·사람 일치율을 `eval.py` 리포트에 낸다.

**Architecture:** 모듈 하나 `evaluation/gold_audit.py`가 대상 선정·가림·중재 호출·표본·터미널 화면·집계를 맡고, 기록은 `evaluation/gold_audit.jsonl`(한 줄 = 통화·판정기 버전). `eval.py`는 그 기록을 읽어 판정 섹션에 붙이고, dev에서만 오류 추정 (통화, 항목)을 뺀 골드 수치를 함께 낸다. 골드가 든 호출 기록은 `JsonlLog`로 `evaluation/`에만 쓴다.

**Tech Stack:** Python 3 · sqlite3 · `shared/llm.py` Gateway/FakeGateway/JsonlLog · pytest

**Spec:** `docs/superpowers/specs/2026-11-06-gold-audit-design.md` (원본 규칙: `docs/치과 AX - 평가 설계.md` §2.3)

## Global Constraints

- 브랜치 `feat/gold-audit`(main에서). 커밋 `<영역>: <무엇>` + 본문 글머리 + 트레일러 두 줄(`Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`, `Claude-Session: https://claude.ai/code/session_01G62cGwhYixc5LpBLhj8iGp`). `git add`는 파일 이름으로. 커밋 전 `make fmt` → `make lint` → `make test`
- `backend/`는 이 모듈을 import하지 않는다. 골드·중재 입력·결과는 `data/`(app.sqlite 포함)에 쓰지 않는다 — DB는 `?mode=ro`로 연다
- 대상 = test 판정 풀이 아닌(`truth_call_labels.split` ≠ `test`) 생성 통화 중, 활성 판정기 버전(`classification.judge_version(conn)`)의 `ok`·폴백 아닌 판정과 골드가 `shared.call_labels.differing_items` 기준으로 하나라도 다른 통화
- 선택지: `primary_purpose`·`counterpart`는 `가 | 나 | 둘 다 아님`, 나머지 7항목은 `가 | 나`. 저장값은 `gold | judge | neither`
- 급함은 화면·중재 입력에서 `급함 / 급하지 않음` 두 값으로만 보인다
- 가/나 배치: `sha256(f"{call_uid}:{item}")` 첫 바이트가 짝수면 골드가 `가`
- 중재 모델은 게이트웨이 별칭 `gold_audit: [claude-sonnet-5-5]`(폴백 없음), 프롬프트 `prompts/gold_audit/v1.md`, 호출 기록 `evaluation/gold_audit_llm.jsonl`
- 표본 크기 `GOLD_AUDIT_SAMPLE = 50`(`evaluation/metrics.py`)
- 골드 오류 추정은 **중재 선택만**으로(`judge`·`neither`). 사람 답은 일치율과 참고 건수에만
- 주석·독스트링 한국어, 근거 절 표기(예: `평가 설계 §2.3`)

## Review Focus

1. 녹취록이 재생성으로 바뀐 기록 — `transcript_sha`가 지금 녹취록과 다르면 리포트·표본·dev 제외에서 빠지고 "녹취록이 바뀐 기록 N"으로만 보여야 한다 → Task 4 `test_stale_records_are_skipped`
2. 중재가 일부 실패한 상태 — 실패한 통화는 기록에 남지 않고 다음 실행에서 그것만 다시 호출, 중재가 다 끝나기 전에는 `audit`이 표본을 뽑지 않고 거절해야 한다 → Task 2 `test_failed_calls_retry_only_missing`, Task 3 `test_audit_refuses_until_arbitration_complete`
3. 사람 화면에서 잘못된 키(빈 줄·4·`가`) — 같은 항목을 다시 묻고 저장하지 않아야 한다 → Task 3 `test_audit_reprompts_on_bad_key`
4. 중재가 스키마 밖 값을 돌려줌(이진 항목에 `둘 다 아님`) — 그 통화는 실패로 보고 기록하지 않는다 → Task 2 `test_invalid_choice_is_failure`
5. 판정기 버전이 바뀐 뒤 — 옛 버전 줄은 리포트·표본에서 무시되고 새 버전 대상만 호출된다 → Task 2 `test_new_judge_version_gets_new_lines`

---

### Task 1: 대상·가림·중재 입력

**Files:**
- Create: `evaluation/gold_audit.py`, `prompts/gold_audit/v1.md`, `tests/evaluation/test_gold_audit.py`
- Modify: `evaluation/metrics.py`(상수), `config/llm.yaml`(별칭 `gold_audit`)

**Interfaces:**
- Produces:
  - `GOLD_AUDIT_SAMPLE = 50` in `evaluation/metrics.py`
  - `targets(conn, judge_version: str) -> list[dict]` — 각 `{call_uid, direction, transcript, transcript_sha, items: {item: {"gold": v, "judge": v}}}`, `call_uid` 순. `transcript_sha = sha256(transcript).hexdigest()`
  - `gold_side(call_uid: str, item: str) -> str` — `"가" | "나"`
  - `choices(item: str) -> tuple[str, ...]` — `("가","나","둘 다 아님")` 또는 `("가","나")`
  - `show(item: str, value) -> str` — 우리말 값(`shared.call_labels`의 `*_KO`, 플래그 `예/아니오`, 급함 `급함/급하지 않음`)
  - `arbiter_request(target: dict) -> tuple[str, dict]` — (user 메시지 문자열, 구조화 출력 스키마 `{type: object, properties: {item: {enum: choices(item)}}, required: [...], additionalProperties: false}`)
  - `decode(target: dict, answer: dict) -> dict[str, str]` — 가/나/둘 다 아님 → `gold|judge|neither`, 스키마 밖이면 `ValueError`
- 테스트 DB는 `tests/evaluation/eval_db.make_db`(판정기 v1: 통화 2 dev가 목적 book≠complaint·complaint 0≠1, 통화 3 test가 다름)

- [ ] **Step 1: 실패하는 테스트**

```python
def test_targets_skip_test_pool(conn):
    uids = [t["call_uid"] for t in gold_audit.targets(conn, "v1")]
    assert uids == ["c-0002"]  # c-0003은 test 판정 풀 — 평가 설계 §1 원칙 2

def test_targets_compare_urgency_as_urgent_or_not(conn):
    # 판정기 v1 통화 4의 urgency를 low로(골드 normal) → 대상 아님, urgent로 → 대상
    ...

def test_request_hides_gold():
    t = {"call_uid": "c-0002", "direction": "inbound", "transcript": "직원: 네\n환자: 불만이에요",
         "items": {"primary_purpose": {"gold": "complaint", "judge": "book"},
                   "complaint": {"gold": 1, "judge": 0}}}
    msg, schema = gold_audit.arbiter_request(t)
    for word in ("골드", "gold", "정답", "판정기", "judge"):
        assert word not in msg
    assert schema["properties"]["complaint"]["enum"] == ["가", "나"]
    assert schema["properties"]["primary_purpose"]["enum"] == ["가", "나", "둘 다 아님"]

def test_sides_are_mixed():
    sides = {gold_audit.gold_side(f"c-{i:04d}", "has_todo") for i in range(20)}
    assert sides == {"가", "나"}

def test_prompt_file_hides_gold():
    text = (ROOT / "prompts/gold_audit/v1.md").read_text()
    for word in ("골드", "gold", "정답", "판정기"):
        assert word not in text

def test_decode_maps_back():
    # gold_side가 "가"인 항목에서 "가" → "gold", "나" → "judge", "둘 다 아님" → "neither"; 이진 항목 "둘 다 아님" → ValueError
```

`test_targets_compare_urgency_as_urgent_or_not`는 fixture DB에 `UPDATE ax_model_judgments SET labels = json_set(labels, '$.urgency', ?)`로 값을 바꿔 두 경우를 확인한다.

- [ ] **Step 2: 실행해 실패 확인** — `uv run pytest tests/evaluation/test_gold_audit.py -q` → `ModuleNotFoundError: evaluation.gold_audit`

- [ ] **Step 3: 구현** — `targets`는 `classification._calls`·`_judgments`를 재사용(같은 모듈 비공개 함수지만 같은 패키지라 허용; 폴백·실패 판정 제외는 `_usable`과 같게). 프롬프트 `v1.md`는 `prompts/judge/v2.md`의 항목 정의 절을 옮기고 앞에 "두 답 중 녹취록에 맞는 쪽을 고른다, 목적·상대는 둘 다 틀리면 둘 다 아님"을 둔다(골드·판정기라는 말 없이 "두 답"). `config/llm.yaml`에 `gold_audit: [claude-sonnet-5-5]  # 골드 점검 중재(평가 설계 §2.3) — 폴백 없음`

- [ ] **Step 4: 통과 확인** — 같은 명령 → PASS

- [ ] **Step 5: 커밋** — `eval: 골드 점검 대상·가림·중재 입력`

### Task 2: 중재 실행·기록

**Files:**
- Modify: `evaluation/gold_audit.py`, `Makefile`(`gold-arbitrate`, `.PHONY`), `tests/evaluation/test_gold_audit.py`

**Interfaces:**
- Consumes: Task 1 전부
- Produces:
  - `AUDIT_PATH = ROOT / "evaluation" / "gold_audit.jsonl"`, `LLM_LOG = ROOT / "evaluation" / "gold_audit_llm.jsonl"`, `PROMPT_VERSION = "v1"`
  - `load(path) -> list[dict]` / `save(path, records: list[dict]) -> None`(통째로 다시 씀, `call_uid`·`judge_version` 순)
  - 기록 한 줄: `{call_uid, judge_version, transcript_sha, prompt_version, arbiter_model, items: {item: {gold, judge, gold_side, arbiter, human: None}}}`
  - `arbitrate(gw, conn, path, judge_version: str, workers: int = 4) -> dict` — 그 버전 줄이 없는 대상만 호출, 돌려주는 값 `{"done": n, "failed": n, "cost_usd": x}`
  - CLI `python -m evaluation.gold_audit arbitrate [--judge-version V]` — 버전 생략이면 `judge_version(conn)`. app·source·truth를 `?mode=ro`로 열고 `Gateway(load_config(), JsonlLog(LLM_LOG), "runtime")`
  - `make gold-arbitrate` → `uv run --env-file .env python -m evaluation.gold_audit arbitrate`

- [ ] **Step 1: 실패하는 테스트** (FakeGateway를 쓰는 `Recording`류 대역을 테스트 파일에 둔다 — `tests/backend/test_classify.py` `Recording` 참고)

```python
def test_arbitrate_writes_records(conn, tmp_path):
    p = tmp_path / "a.jsonl"
    r = gold_audit.arbitrate(
        FakeGateway(JsonlLog(tmp_path / "log.jsonl"), scope="runtime"), conn, p, "v1"
    )
    assert r["done"] == 1 and r["failed"] == 0
    [rec] = gold_audit.load(p)
    assert rec["call_uid"] == "c-0002" and rec["judge_version"] == "v1"
    assert set(rec["items"]) == {"primary_purpose", "complaint"}
    assert all(
        v["arbiter"] in ("gold", "judge", "neither") and v["human"] is None
        for v in rec["items"].values()
    )


def test_failed_calls_retry_only_missing(
    conn, tmp_path
): ...  # 첫 실행 실패 → 기록 없음, 둘째 실행에서 그 통화만 호출
def test_invalid_choice_is_failure(
    conn, tmp_path
): ...  # complaint에 "둘 다 아님" → failed 1, 기록 없음
def test_new_judge_version_gets_new_lines(
    conn, tmp_path
): ...  # v1 기록 뒤 판정기 v2 행을 넣고 arbitrate(v2) → v2 줄만 새로, v1 줄 그대로
def test_arbitrate_does_not_write_app_db(conn, tmp_path): ...  # 실행 전후 ax_llm_calls 행 수 같음
```

- [ ] **Step 2: 실패 확인** — `uv run pytest tests/evaluation/test_gold_audit.py -q` → `AttributeError: arbitrate`
- [ ] **Step 3: 구현** — `ThreadPoolExecutor(workers)`, `gw.complete("gold_audit", task="gold-audit", system=프롬프트, schema=..., prompt_version=PROMPT_VERSION, messages=[{"role":"user","content":msg}])`. 결과는 모은 뒤 한 번에 `save`(기존 줄 + 새 줄). `r.ok`가 아니거나 `decode`가 `ValueError`면 failed
- [ ] **Step 4: 통과 확인** → PASS
- [ ] **Step 5: 커밋** — `eval: 골드 점검 중재 실행·기록(make gold-arbitrate)`

### Task 3: 표본·사람 화면

**Files:**
- Modify: `evaluation/gold_audit.py`, `Makefile`(`gold-audit`), `tests/evaluation/test_gold_audit.py`

**Interfaces:**
- Consumes: Task 2 `load`·`save`·기록 형식
- Produces:
  - `group(record) -> str` — 항목 중 `arbiter == "judge"`가 있으면 `"judge"`, 없고 `"neither"`가 있으면 `"neither"`, 그 밖 `"gold"`
  - `sample(records: list[dict], n: int = GOLD_AUDIT_SAMPLE) -> list[str]` — `call_uid` 목록. 묶음(`judge`·`neither`·`gold` 순)마다 `n // 3`(나머지는 앞 묶음부터 1씩)을 `sha256(call_uid)` 순으로, 모자란 묶음의 몫은 남은 묶음들에서 해시 순으로 채운다. 전체가 n 이하면 전부
  - `audit(conn, path, judge_version: str, read=input, write=print) -> None` — 표본 중 `human`이 빈 통화부터 한 통화씩
  - CLI `python -m evaluation.gold_audit audit`, `make gold-audit`
- 화면(한 통화): `[i/n] call_uid · 방향` → 녹취록(줄마다 `직원:`·`환자:` 그대로) → 항목마다 `항목 우리말 이름 — 가: 값 / 나: 값` 다음 줄에 `1 가 · 2 나`(목적·상대는 `· 3 둘 다 아님`) 입력. 통화의 항목을 다 고르면 저장하고 그때 처음으로 `중재: 가|나|둘 다 아님` 결과를 항목별로 보인다. 저장 전 출력에 골드·판정기 쪽 표시 없음

- [ ] **Step 1: 실패하는 테스트**

```python
def test_sample_balanced_and_deterministic():
    recs = (
        [_rec(f"c-{i:04d}", "judge") for i in range(5)]
        + [_rec(f"c-1{i:03d}", "neither") for i in range(40)]
        + [_rec(f"c-2{i:03d}", "gold") for i in range(40)]
    )
    s = gold_audit.sample(recs, 50)
    assert len(s) == 50 and s == gold_audit.sample(list(reversed(recs)), 50)
    # judge 묶음은 5건뿐 → 5 전부, 남은 45를 neither·gold가 나눔(23·22 또는 22·23)
    assert sum(u.startswith("c-0") for u in s) == 5


def test_sample_takes_all_when_few(): ...  # 기록 7건, n=50 → 7건
def test_audit_saves_per_call_and_resumes(
    conn, tmp_path
): ...  # 입력을 리스트로 흘려 첫 통화 저장 뒤 EOFError로 끊고 다시 audit → 저장된 human 유지, 다음 통화부터
def test_audit_hides_gold_until_saved(
    conn, tmp_path
): ...  # 마지막 입력 전까지 write에 넘어간 문자열에 "골드"·"판정기"·"중재" 없음
def test_audit_reprompts_on_bad_key(
    conn, tmp_path
): ...  # 입력 ["", "4", "가", "1", ...] → 같은 항목을 다시 묻고 human은 "1"의 값
def test_audit_refuses_until_arbitration_complete(
    conn, tmp_path
): ...  # 대상 중 기록 없는 통화가 있으면 SystemExit("중재가 끝나지 않음 — make gold-arbitrate")
```

- [ ] **Step 2: 실패 확인** → `AttributeError: sample`
- [ ] **Step 3: 구현** — 사람 선택도 `decode`로 `gold|judge|neither`로 풀어 저장. 표본은 활성 판정기 버전·`transcript_sha`가 맞는 줄에서만 뽑는다(Task 4 `current` 사용 — 이 태스크에서 먼저 만들고 Task 4가 재사용해도 된다)
- [ ] **Step 4: 통과 확인** → PASS
- [ ] **Step 5: 커밋** — `eval: 골드 점검 사람 표본 화면(make gold-audit)`

### Task 4: 집계와 eval.py 리포트

**Files:**
- Modify: `evaluation/gold_audit.py`, `evaluation/classification.py`(`_score`·`evaluate`·`evaluate_judge`에 제외 인자), `evaluation/eval.py`(`run` 인자·결과·`render`), `tests/evaluation/test_gold_audit.py`, `tests/evaluation/test_eval_cli.py`

**Interfaces:**
- Consumes: Task 2 기록 형식, Task 3 `sample`
- Produces:
  - `current(records, conn, judge_version) -> tuple[list[dict], int]` — 그 버전이고 `transcript_sha`가 지금 녹취록과 같은 줄, 그리고 녹취록이 바뀐 줄 수
  - `errors(records) -> set[tuple[str, str]]` — 중재가 `judge`·`neither`인 (call_uid, item)
  - `summarize(records) -> dict` — `{"calls": n, "arbiter": {item: {"gold": k, "judge": k, "neither": k}}, "agreement": {item: {"k","n"}}, "agreement_total": {"k","n"}, "human_errors": {"judge": k, "neither": k}, "human_pending": n}`(일치 = 사람이 고른 항목 중 중재와 같은 선택)
  - `classification._score(..., drop: set[tuple[str, str]] | None = None)` — 항목별로 (통화, 항목)이 drop에 있으면 그 항목 모수에서 뺀다(목적 가중·카테고리 평균도 목적 항목 drop 기준)
  - `classification.evaluate(..., gold_drop=None)` → 버전마다 `model_vs_gold_audited`(gold_drop이 있을 때만), `evaluate_judge(..., gold_drop=None)` → `judge_vs_gold_audited`
  - `eval.run(..., gold_audit_path: Path | None = None)` — 기본 `gold_audit.AUDIT_PATH`. 결과 `result["gold_audit"] = {"summary":…, "stale": n} | None`(dev 전체·test judge 섹션). `gold_drop`은 **dev에서만** 넘긴다
  - 렌더: 판정 섹션 뒤 `## 골드 점검` — 대상 통화 수, 항목별 중재 선택 수, 중재 vs 사람 일치(분수), 표본 안 사람 기준 오류 추정(참고), 사람 미판정 수, 녹취록이 바뀐 기록 수. 파일이 없으면 `골드 점검 전`. dev의 모델·판정기 vs 골드 줄 옆에 `오류 추정 제외 목적 k/n · 가중 x%`

- [ ] **Step 1: 실패하는 테스트**

```python
def test_errors_from_arbiter_only():
    rec = _rec(
        "c-0002", items={"primary_purpose": ("judge", "gold"), "complaint": ("gold", None)}
    )  # (arbiter, human)
    assert gold_audit.errors([rec]) == {("c-0002", "primary_purpose")}


def test_summarize_agreement(): ...  # 사람이 고른 2항목 중 1항목 일치 → agreement_total {"k":1,"n":2}, human_errors judge 0·neither 0 등
def test_stale_records_are_skipped(
    conn, tmp_path
): ...  # transcript_sha를 바꾼 기록 → current가 빼고 stale 1


# tests/evaluation/test_eval_cli.py
def test_dev_report_drops_gold_errors(conn, reports, tmp_path):
    ...
    # c-0002 목적을 arbiter "judge"로 둔 기록 → dev 결과 judge v1 judge_vs_gold_audited 목적 n이 원래보다 1 적고, 리포트 md에 "골드 점검" 절


def test_test_report_keeps_original_gold(conn, reports, tmp_path):
    ...
    # 같은 기록으로 --split test --section judge → judge_vs_gold_audited 없음, gold_audit 요약은 있음


def test_report_without_audit_file(conn, reports, tmp_path): ...  # "골드 점검 전"
```

- [ ] **Step 2: 실패 확인** → `AttributeError: errors` 등
- [ ] **Step 3: 구현** — `gold_audit`가 `classification`을 import하므로 `classification`은 `gold_audit`를 import하지 않는다(제외 집합은 eval.py가 만들어 넘긴다)
- [ ] **Step 4: 통과 확인** — `uv run pytest tests/evaluation -q` → PASS
- [ ] **Step 5: 커밋** — `eval: 골드 점검 결과를 리포트에 — 일치율·오류 추정·dev 제외 수치`

### Task 5: 문서와 실제 중재 실행

**Files:**
- Modify: `docs/치과 AX - 평가 설계.md`(§2.3 골드 점검, §7 판정 줄), `CLAUDE.md`(명령), `docs/치과 AX - 기능 정의.md`(§9 착수 순서 줄)
- Create(실행 결과): `evaluation/gold_audit.jsonl`, `evaluation/gold_audit_llm.jsonl`

- [ ] **Step 1: 문서** — 평가 설계 §2.3: 대상 비교는 급함 여부만(`differing_items`), 화면·중재의 급함 두 값, `둘 다 아님`은 목적·상대에만, 기록 키 (통화, 판정기 버전), 중재 모델은 `config/llm.yaml` `gold_audit`, 명령 `make gold-arbitrate`·`make gold-audit`, 호출 기록 `evaluation/gold_audit_llm.jsonl`, 표본 크기는 `evaluation/metrics.py` `GOLD_AUDIT_SAMPLE`(문서의 "50건" 값은 이름으로 바꿈). §7 "골드 점검 결과는 `make gold-audit`가 생긴 뒤 붙인다" → 판정 섹션에 붙는다는 현재형. `CLAUDE.md` 명령에 `make gold-arbitrate` / `make gold-audit` 한 줄. 기능 정의 §9 착수 순서에서 "골드 점검 →" 를 뺀다
- [ ] **Step 2: 검사** — `make fmt` → `make lint` → `make test` 통과
- [ ] **Step 3: 커밋** — `docs: 골드 점검 규칙 반영(급함 여부·둘 다 아님 범위·명령)`
- [ ] **Step 4: 실제 중재** — 사용자에게 예상 비용(대상 250통화, Sonnet, 어림 $2~3)을 알리고 승인 뒤 `make gold-arbitrate`. Expected: `done 250 failed 0`(실패가 있으면 한 번 더 실행)
- [ ] **Step 5: 확인** — `uv run python -m evaluation.eval --split dev` 리포트의 `## 골드 점검` 절에 항목별 중재 선택 수가 나오고 사람 미판정 50
- [ ] **Step 6: 커밋** — `eval: 골드 점검 중재 결과(판정기 v2)` — `evaluation/gold_audit.jsonl`·`evaluation/gold_audit_llm.jsonl`
- [ ] **Step 7: 사용자에게 `make gold-audit` 안내** — 사람 표본 50통화는 사용자가 직접(끊었다 이어 하기 가능). 끝나면 기록 커밋은 사용자 요청 때
