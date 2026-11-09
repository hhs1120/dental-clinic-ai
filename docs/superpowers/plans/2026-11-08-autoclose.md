# 자동 종결 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 확인할 것이 없는 통화(두 모델 모두 할 일 없음·응급·불만·급함·엇갈림·폴백 없음)를 `auto_policy`가 켜졌을 때 자동 종결하고, 되돌리기·사람 판정·버전 전환으로 풀며, `eval.py`가 골드로 오류율을 잰다.

**Architecture:** 조건은 순수 함수 `backend/autoclose.py` `autoclose_reason` 하나 — 앱과 `eval.py`가 같이 쓴다. 종결은 기존 하네스 `apply_auto_action(kind="call_autoclose")`로 `ax_auto_actions`에 남기고(앱 상태는 그 행 자체), 보드·목록·상세는 되돌리지 않은 종결 행으로 판단한다. 시스템 풀기는 행 삭제, 사람 되돌림은 `revert_auto_action`.

**Tech Stack:** Python 3 · sqlite3 · FastAPI · Next.js(TypeScript, shadcn) · pytest

**Spec:** `docs/superpowers/specs/2026-11-08-followup-autoclose-design.md` "PR ① 자동 종결" (원본 규칙: 기능 정의 §4 ①·§3 원칙 1, 앱 설계 §3b·§7.3, 평가 설계 §5)

## Global Constraints

- 브랜치 `feat/autoclose`(main에서). 커밋 `<영역>: <무엇>` + 본문 글머리 + 트레일러 두 줄(`Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`, `Claude-Session: https://claude.ai/code/session_01G62cGwhYixc5LpBLhj8iGp`). `git add`는 파일 이름으로. 커밋 전 `make fmt` → `make lint` → `make test`
- `backend/`는 `evaluation/`·`generator/`·`ground_truth/`를 import하지 않는다. `evaluation/`이 `backend.autoclose`를 import하는 방향은 된다
- 작업 종류 이름 `call_autoclose`. `auto_policy` 기본 끔 — 꺼져 있으면 아무것도 종결하지 않는다
- 조건(전부 참일 때만 종결): 분류·판정기 결과 모두 `ok`이고 폴백 아님, 두 라벨 모두 `has_todo` 거짓, 두 라벨 모두 `emergency`·`complaint` 거짓, 두 라벨 모두 `urgency` ≠ `urgent`, `shared.call_labels.differing_items(cls, judge)`가 빈 목록. 판정 풀 통화는 대상이 아니다(앱은 `CALLS_CTE`가 이미 뺀다)
- 사람이 되돌린 통화는 다시 종결하지 않는다(`auto.py`가 `REVERTED`로 막음). 시스템 풀기(조건을 더는 못 넘음)는 되돌리지 않은 종결 행을 지운다
- 화면 색·부품은 `globals.css` 토큰과 `components/ui`만

## Review Focus

1. `auto_policy`가 꺼진 상태에서 `make autoclose`를 돌림 — 아무것도 종결되지 않고 "꺼져 있음"만 출력, 거절 집계(`ax_rule_counts`)를 통화 수만큼 쌓지 않는다 → Task 2 `test_run_policy_off_does_nothing`
2. 폴백으로 답한 분류(또는 판정) — 라벨이 깨끗해도 종결하지 않는다 → Task 1 `test_fallback_blocks`
3. 사람이 되돌린 뒤 `make autoclose`를 다시 돌림 — 다시 종결되지 않고, 행도 지워지지 않는다 → Task 2 `test_human_revert_sticks`
4. 판정기 결과가 아직 없는 새 통화 — 종결하지 않고, 판정이 들어온 뒤 조건을 넘으면 그때 종결 → Task 2 `test_classify_one_closes_after_both`
5. 새로 추가한 통화(`ax_pasted_calls`)도 같은 조건으로 종결되고 상세에서 되돌릴 수 있다 → Task 3 `test_detail_and_revert_pasted`

---

### Task 1: 조건 함수

**Files:**
- Create: `backend/autoclose.py`, `tests/backend/test_autoclose.py`

**Interfaces:**
- Produces:
  - `KIND = "call_autoclose"`
  - `autoclose_reason(cls: dict | None, judge: dict | None) -> str | None` — 인자는 `{"labels": dict, "ok": bool, "fallback": bool}` 또는 None. 조건을 다 넘으면 종결 이유 문자열(고정 문구 `"두 모델 모두 할 일 없음 · 응급·불만·급함·엇갈림 없음"`), 하나라도 못 넘으면 None

- [ ] **Step 1: 실패하는 테스트** — 기본 라벨(`tests/evaluation/eval_db.labels`와 같은 모양: 할 일·응급·불만 0, 급함 normal)로

```python
def test_clean_pair_closes():
    assert autoclose_reason(_r(), _r()) is not None


def test_todo_blocks():
    assert (
        autoclose_reason(_r(has_todo=1), _r()) is None
        and autoclose_reason(_r(), _r(has_todo=1)) is None
    )


def test_emergency_complaint_urgent_block(): ...  # 각각 한쪽만 켜도 None
def test_disagreement_blocks(): ...  # 목적만 다름(book vs confirm) → None; 급함 normal vs low → 종결(urgent 여부만 비교)
def test_missing_failed_fallback_block(): ...  # judge None, cls ok=False, fallback=True 각각 None
def test_fallback_blocks():
    assert autoclose_reason(_r(), _r(fallback=True)) is None
```

- [ ] **Step 2: 실패 확인** — `uv run pytest tests/backend/test_autoclose.py -q` → ModuleNotFoundError
- [ ] **Step 3: 구현** — 모듈 독스트링에 원본 절(기능 정의 §4 ①, 앱 설계 §3b)
- [ ] **Step 4: 통과 확인** → PASS
- [ ] **Step 5: 커밋** — `backend: 자동 종결 조건 함수`

### Task 2: 종결 실행·풀기 (`make autoclose`, 새 통화, 사람 판정)

**Files:**
- Modify: `backend/autoclose.py`, `backend/calls.py`(`classify_one` 끝), `backend/judge_queue.py`(`save` 뒤), `Makefile`, `tests/backend/test_autoclose.py`

**Interfaces:**
- Consumes: Task 1 `autoclose_reason`, `KIND`; `backend.harness.auto` `AutoKind`·`apply_auto_action`·`revert_auto_action`; `calls.active_versions`, `calls.CALLS_CTE`
- Produces:
  - `KINDS = {KIND: AutoKind(condition=..., apply=no-op, undo=no-op)}` — condition은 DB에서 활성 버전 결과를 다시 읽어 `autoclose_reason`이 None이면 막는 이유 `"조건 안 맞음"`
  - `pair(conn, call_uid) -> tuple[dict | None, dict | None]` — 활성 분류·판정기 행과 `ax_llm_calls.fallback_used`를 `autoclose_reason` 인자 모양으로
  - `refresh_call(conn, call_uid, clock=datetime.now) -> str` — `"closed" | "opened" | "kept" | "off"`. 정책이 꺼져 있으면 `"off"`(아무것도 안 함). 조건을 넘고 행이 없으면 `apply_auto_action`, 못 넘고 되돌리지 않은 행이 있으면 그 행을 지움(`"opened"`). 사람이 되돌린 행은 그대로
  - `run(conn) -> dict` — 정책이 꺼져 있으면 `{"off": True}`만(통화를 돌지 않음). 켜져 있으면 `CALLS_CTE`의 모든 통화에 `refresh_call`, `{"closed": n, "opened": n, "kept": n}`
  - `unclose_by_human(conn, call_uid, by: str) -> bool` — 되돌리지 않은 종결 행이 있으면 `revert_auto_action`
  - `set_policy(conn, kind: str, on: bool) -> None` — `ax_settings.auto_policy[kind]`
  - CLI `python -m backend.autoclose run` / `python -m backend.autoclose policy <kind> on|off`, `make autoclose`, `make auto-policy KIND=… ON=1|0`
  - `calls.classify_one` 끝에서 `autoclose.refresh_call(conn, call_uid)`; `judge_queue.save`에서 사람 라벨이 `has_todo`·`emergency`·`complaint` 참 또는 `urgency == "urgent"`이면 `autoclose.unclose_by_human(conn, call_uid, part)`

- [ ] **Step 1: 실패하는 테스트** — 앱 테스트 DB는 `tests/backend/test_api_calls.py`가 쓰는 fixture를 재사용(분류·판정기 행과 `ax_llm_calls`가 있는 DB)

```python
def test_run_policy_off_does_nothing(conn): assert autoclose.run(conn) == {"off": True}; assert _closed(conn) == set(); assert _blocks(conn) == 0
def test_run_closes_clean_calls(conn): set_policy(conn, KIND, True); r = autoclose.run(conn); assert _closed(conn) == {clean uids}; assert r["closed"] == len(...)
def test_rerun_is_idempotent(conn): ...  # 두 번째 run은 closed 0, kept n
def test_system_unclose_when_condition_breaks(conn): ...  # 종결 뒤 분류 라벨 has_todo=1로 바꾸고 run → 행 삭제, opened 1; 다시 깨끗하게 → 다시 closed
def test_human_revert_sticks(conn): ...  # revert_auto_action 뒤 run → 다시 종결 안 함, 행 남고 reverted_at 유지
def test_classify_one_closes_after_both(conn): ...  # FakeGateway로 판정기 버전 없이 classify_one → 종결 없음; 판정기 버전 켜고 다시 → 조건 맞으면 종결
def test_human_judgment_unclose(conn): ...  # 종결된 통화를 판정 큐에 넣고 judge_queue.save(labels has_todo=True) → reverted_by == part
```

- [ ] **Step 2: 실패 확인** → AttributeError
- [ ] **Step 3: 구현** — `run`과 `refresh_call`은 통화마다 커밋하지 않고 끝에 한 번(`auto.py`는 부르는 쪽 트랜잭션). Makefile: `autoclose:` `uv run python -m backend.autoclose run`, `auto-policy:`(KIND·ON 없으면 에러)
- [ ] **Step 4: 통과 확인** → PASS(그리고 `tests/backend` 전체)
- [ ] **Step 5: 커밋** — `backend: 자동 종결 실행·풀기 — make autoclose, 새 통화, 사람 판정`

### Task 3: 보드·목록·상세·되돌리기 API

**Files:**
- Modify: `backend/calls.py`(`ROWS_SQL`·`today_board`·`_card`·`call_detail`), `backend/api.py`, `tests/backend/test_api_calls.py`

**Interfaces:**
- Consumes: Task 2 `KINDS`, `revert_auto_action`
- Produces:
  - `ROWS_SQL`에 되돌리지 않은 `call_autoclose` 행 LEFT JOIN — `auto.id AS close_id, auto.reason AS close_reason, auto.created_at AS closed_at`
  - `today_board`: `close_id`가 있는 통화의 할 일은 빼고, "끝낸 일"에도 넣지 않는다
  - `_card`: `"closed": bool`
  - `call_detail`: `"autoclose": {"id", "reason", "at"} | None`
  - `POST /auto-actions/{id}/revert` body `{part}` → `{"ok": True}`; 없는 id 404, 이미 되돌림 409. `kind`가 `call_autoclose`가 아닌 행도 같은 엔드포인트(앱 설계 §3b 공용) — 지금 등록된 종류만 `KINDS`로

- [ ] **Step 1: 실패하는 테스트**

```python
def test_board_hides_closed(client, ...): ...  # 종결된 통화의 할 일이 internal·contact·done 어디에도 없음
def test_list_and_detail_show_closed(client, ...): ...  # 목록 카드 closed True, 상세 autoclose.reason
def test_revert_returns_to_board(client, ...): ...  # POST /auto-actions/{id}/revert → 보드에 다시, 두 번째 409
def test_detail_and_revert_pasted(client, ...): ...  # 새로 추가한 통화도 같은 흐름
```

- [ ] **Step 2: 실패 확인** → FAIL
- [ ] **Step 3: 구현**
- [ ] **Step 4: 통과 확인** → PASS
- [ ] **Step 5: 커밋** — `backend: 자동 종결 통화를 오늘 할 일에서 빼고 상세·되돌리기 API`

### Task 4: 화면 — 상세의 종결 줄·되돌리기, 목록 딱지

**Files:**
- Modify: `frontend/lib/calls-api.ts`(`CallCard.closed`, `CallDetail.autoclose`, `revertAuto(id, part)`), `frontend/components/calls/call-detail.tsx`, `frontend/components/calls/call-history.tsx`, `frontend/components/calls/calls-view.tsx`(되돌린 뒤 보드·상세 새로 고침)

**Interfaces:**
- Consumes: Task 3 API 모양
- 화면: 상세 맨 위(분류 표 위)에 한 줄 — `Badge`(muted 계열) "자동 종결" + 이유 + 시각 + `Button`(outline, sm) "되돌리기". 누르면 `part`(현재 파트, `lib/part.ts`)로 되돌리고 줄이 사라지며 오늘 할 일에 다시 오른다. 통화 기록 카드에는 이름 옆 "자동 종결" 딱지

- [ ] **Step 1: 구현**(화면은 타입 검사·빌드로 확인 — 기존 e2e에 자동 종결 시나리오는 넣지 않는다, 실데이터에 정책이 꺼져 있어서)
- [ ] **Step 2: 확인** — `make lint`(tsc 포함) 통과, 개발 서버에서 `make auto-policy KIND=call_autoclose ON=1` → `make autoclose` → 통화 기록에 딱지, 상세에서 되돌리면 오늘 할 일로 — 확인 뒤 정책을 다시 끈다(`ON=0`)
- [ ] **Step 3: 커밋** — `frontend: 통화 상세에 자동 종결 줄·되돌리기, 통화 기록에 딱지`

### Task 5: 오류율 `eval.py --section auto`

**Files:**
- Create: `evaluation/autoclose.py`, `tests/evaluation/test_autoclose_eval.py`
- Modify: `evaluation/metrics.py`(`wilson`), `evaluation/eval.py`(SECTIONS·키·렌더), `tests/evaluation/test_eval_cli.py`

**Interfaces:**
- Consumes: `backend.autoclose.autoclose_reason`; `evaluation.splits.silver_split`; `evaluation.classification._calls`·`_classifications`·`_judgments`
- Produces:
  - `metrics.wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None` — n=0이면 None
  - `autoclose.evaluate(conn, split: str, a: str, b: str) -> dict` — 모수: dev = silver 검증 구간(`silver_split`이 `validation`), test = test 판정 풀. 두 결과의 `ok`·`fallback`으로 `autoclose_reason` → `{"pair": f"{a}×{b}", "n": 모수, "closed": k, "rate": closed/n, "errors": {"k", "n": closed, "ci": wilson}, "by_item": {"has_todo": k, "emergency": k, "complaint": k, "urgent": k}}` — 오류 = 종결된 통화 중 골드가 할 일·응급·불만 참 또는 급함 urgent
  - `eval.py`: SECTIONS에 `"auto"`, test 키는 교차검증과 같게 `--prompt-version`(분류) × 활성 판정기(`f"{a}×{b}"`). dev는 활성 분류 × 활성 판정기 한 쌍을 기본 포함. 렌더 "## ① 자동 종결" — 대상 n, 종결 k(비율), 오류 k/n(95% 구간), 항목별

- [ ] **Step 1: 실패하는 테스트** — `tests/evaluation/eval_db` fixture에 판정기 v2·분류 v2 행을 더해 깨끗한 쌍·할 일 숨은 쌍을 만든다

```python
def test_wilson():
    assert metrics.wilson(0, 0) is None
    lo, hi = metrics.wilson(1, 10)
    assert 0 < lo < 0.1 < hi < 0.5


def test_error_counts_gold_todo(
    conn,
): ...  # 두 모델이 할 일 없음이라 종결됐는데 골드 has_todo=1 → errors.k 1, by_item.has_todo 1
def test_test_split_is_test_pool_only(conn): ...
def test_dev_is_silver_validation(conn): ...  # material 구간 통화는 모수에 없음
# test_eval_cli.py
def test_auto_section_test_once(
    conn, reports
): ...  # --split test --section auto --prompt-version v1 → 한 번, 두 번째 AlreadyRun, prompt_version "v1×v1"
def test_dev_report_has_autoclose(conn, reports): ...  # "## ① 자동 종결"
```

- [ ] **Step 2: 실패 확인** → FAIL
- [ ] **Step 3: 구현**
- [ ] **Step 4: 통과 확인** — `uv run pytest tests/evaluation -q` → PASS
- [ ] **Step 5: 커밋** — `eval: 자동 종결 오류율 — eval.py --section auto`

### Task 6: 문서

**Files:**
- Modify: `docs/치과 AX - 앱 설계.md`(§1 정확도 뷰 목록 — `backend/metrics.py`의 `agreement`·`blind_rates`를 이름·모수·컬럼으로, §3b 종결 풀기 규칙: 사람 되돌림은 영구, 시스템 풀기는 행 삭제), `docs/치과 AX - 기능 정의.md`(§4 ① 되돌리기 위치 — 오늘 화면 전에는 통화 상세, §4 ② 완료 기준 "정확도 뷰 2개 SQL이 행 ≥ 9"를 정확도 뷰 정의에 맞춤, §9 "자동 종결 착수 시" 줄 삭제·착수 순서 줄 고침), `docs/치과 AX - 평가 설계.md`(§5 자동 종결 오류율: dev 모수가 silver 검증 구간, 키 분류×판정기, Wilson 구간), `docs/치과 AX - 평가 설계.md` §7 `--section` 목록에 auto, `CLAUDE.md` 명령(`make autoclose` / `make auto-policy KIND=… ON=1|0`)

- [ ] **Step 1: 문서 고침**
- [ ] **Step 2: 검사** — `make fmt` → `make lint` → `make test` 통과
- [ ] **Step 3: 커밋** — `docs: 자동 종결 규칙·명령·정확도 뷰 반영`
