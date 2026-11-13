# 청구 기준 실제 고시 발췌 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 청구 기준 문서 5개 중 8개 절을 실제 고시 원문 발췌로, 3개 절은 "병원 내부 점검 기준" 표시로 바꾸고, R05 사랑니
차트를 X-선 근거 모양으로 다시 만든 뒤 ⑤ AI를 다시 잰다.

**Architecture:** 원문·출처·보충 줄은 새 모듈 `generator/claim_standards.py`에 상수로 둔다. 청구 기준 문서 본문은 9단계 계획
단계(`documents.draft`)가 이 상수로 쓰고, 본문 단계(`stage_bodies`)는 `claim_standard`를 LLM에 보내지 않는다. R05 차트는
`generator/chart.py`의 요건 사실·초안·근거 말만 바꾼다. 차트 생성 프롬프트는 건드리지 않는다. 화면은 절 카드를 문서함의
마크다운 렌더러로 그린다.

**Tech Stack:** Python 3(uv), sqlite3, pytest, ruff · Next.js(TypeScript, react-markdown), Playwright

**Spec:** `docs/superpowers/specs/2026-11-13-real-claim-standards-design.md`

## Global Constraints

- 원문 인용은 스펙 "절별 본문"의 글자 그대로 옮긴다. 띄어쓰기는 대조 이미지 기준이며, 스펙 표기가 그 기준이다. 문단 하나는 `> ` 한 줄이다(줄을 나누지 않는다 — Review Focus 1).
- 값은 `generator/master.py` 이름으로 읽는다: `SCALING_MIN_AGE`, `CONDITIONAL_INSURED_MIN_AGE`, `PERIO_LINK_DAYS`, `PERIO_POCKET_MIN_MM`, `PERIO_NEAR_MISS_SHARE`, `EVIDENCE_NEGATED_SHARE`, `EVIDENCE_PARAPHRASE_SHARE`. 원문 인용 안의 숫자(19세·65세)는 f-string으로 만들지 않는다. 대신 테스트로 master 값과 같은지 본다.
- `prompts/gen_chart.md`를 고치지 않는다. 프롬프트 해시가 캐시 키라, 고치면 차트 1,953건 전부가 다시 나간다(`generator/llm_jobs.py` `_hash`).
- `generator/chart.py`의 난수 소비 횟수를 바꾸지 않는다. 새로 고르는 값은 `_form`(진료 번호 해시)으로 고른다. 난수가 밀리면 다른 줄 결과가 바뀐다(CLAUDE.md 시드 규칙).
- `emr_*` 정형 행·`fin_*`·`truth_claim_errors`는 바꾸지 않는다. `truth_chart_notes`의 R05 요건 줄 `facts`와 `emr_chart_notes`의 그 줄 서술만 바뀐다.
- 과금 명령은 실행할 때마다 사용자 승인을 받는다. `--split test`는 돌리지 않는다(사람만).
- 커밋 전 `make fmt` → `make lint` → `make test`. `git add`는 파일 이름으로. 커밋 메시지는 `<영역>: <무엇>` + 본문 글머리 + 세션 첨부 줄.
- 문서에는 규칙만 적는다(경위·날짜 금지). 경위·수치는 AI 실험 기록에만 적는다.

## Review Focus

1. 기준 인용이 두 `> ` 줄에 걸치면 `found()`(공백만 접음)가 `>` 때문에 실패한다. ⑤-2·⑤-4가 실제 원문 문장을 인용할 때 후검증을 통과해야 한다 → Task 1 `test_found_quote_inside_real_section`.
2. R05 새 모양을 고를 때 난수를 하나라도 더 쓰면 R05가 아닌 줄의 차트까지 다시 나간다. 다른 줄의 사실은 그대로여야 한다 → Task 2 `test_r05_style_draws_same_rng`.
3. 바꿔 말하기 줄에 "X-선"·"방사선"·"파노라마"가 남으면 키워드 기준선이 맞혀 버려 모양이 무너진다. 이 줄에는 근거 말이 없어야 한다 → Task 2 `test_r05_paraphrase_forbids_xray_words`(9b 검증 기준과 verify가 같은 함수를 쓴다).
4. 문서 머리말이 절로 잘못 읽히면 절 카드가 하나 더 생기거나 사유당 절 수가 1이 아니게 된다 → Task 1에서 verify `check_claim_sections`를 생성 DB에 돌린다(기존 테스트 `tests/generator/test_documents.py`).
5. 화면 절 카드에 `>`·`**` 기호가 그대로 보이면 안 된다. 인용은 인용 블록으로 그려져야 한다 → Task 3 e2e 단언.

---

### Task 1: 청구 기준 본문을 코드로 — 실제 절 8개·가상 절 3개

**Files:**
- Create: `generator/claim_standards.py`
- Modify: `generator/doc_catalog.py` (`_CLAIM_RULE`·`_claim_doc` — 사실 문장·꼭 넣을 말을 새 모듈에서, R04 `same` 뗌)
- Modify: `generator/documents.py` (`draft`의 `claim_standard` 분기, `stage_bodies`가 `claim_standard` 건너뜀, `payload`·`checks`의 청구 분기 정리)
- Test: `tests/generator/test_claim_standards.py`(새), `tests/generator/test_documents.py`·`tests/generator/test_doc_catalog.py`(기존 기대값 갱신)

**Interfaces:**
- Produces:
  - `generator/claim_standards.py`
    - `REAL: dict[str, Real]` — `Real(quote: tuple[str, ...], source: str, supplement: str | None, words: tuple[str, ...])`. 키는 E01·R04·R05·R06·D01·D02·D03·C01
    - `VIRTUAL: dict[str, tuple[str, ...]]` — R01·R02·R03 → 목록 줄들
    - `VIRTUAL_MARK = "병원 내부 점검 기준 — 고시에 같은 문장 없음"`
    - `INTRO_NOTE = "이 문서의 절은 실제 고시 발췌(출처 표기)와 병원 내부 점검 기준(표시)으로 나뉜다."`
    - `section_body(reason: str) -> str` — 제목 줄을 뺀 절 본문 마크다운
  - `documents.draft(d)`: `claim_standard`면 최종 본문
- Consumes: `CLAIM_HEADINGS`, `CLAIM_DOCS`(그대로)

**본문 값:**
- `quote`: 스펙의 인용 블록 줄 그대로. 빈 `>` 줄로 나뉜 문단은 튜플 원소 하나씩이다. R06은 줄마다 하나(`- 다 음 -` 줄 포함).
- `source`: 스펙 "출처:" 뒤 문자열.
- `supplement`: 스펙 "병원 내부 보충:" 뒤 문자열. R06·D03은 None.
- `VIRTUAL`: 지금 `data/source.sqlite` 해당 절 목록 줄 그대로. 90·4는 `PERIO_LINK_DAYS`·`PERIO_POCKET_MIN_MM`으로 넣는다.
- `words`(꼭 넣을 말, 절 안에 있어야 함):
  - E01 (`f"{SCALING_MIN_AGE}세 이상"`, "연(매년 1월∼12월) 1회") · R04 ("순수 건전치아", "제1큰어금니") · R05 ("X-선 촬영",) · R06 (`f"{CONDITIONAL_INSURED_MIN_AGE}세 이상"`, "부분 무치악") · D01 ("특정내역기재란", "JX999") · D02 ("X-Ray 촬영", "방사선 영상자료") · D03 ("치식",) · C01 ("의무기록",)
  - 가상 절은 지금 `_CLAIM_RULE` words 그대로(R01 "다시 하는 사유", R02 `f"{PERIO_LINK_DAYS}일"`, R03 `f"{PERIO_POCKET_MIN_MM}mm"`·"출혈")

`section_body` 모양(실제):

```
> {quote[0]}
>
> {quote[1]} …

출처: {source}

병원 내부 보충: {supplement}
```

`section_body` 모양(가상): 첫 줄 `VIRTUAL_MARK`, 빈 줄, `- {줄}` 목록.

문서 본문: `# {title}` / 빈 줄 / `{topic}. {INTRO_NOTE}` / 사유마다 빈 줄 + `## {heading}` + 빈 줄 + `section_body(r)`.

- [ ] **Step 1: 실패하는 테스트 쓰기** — `tests/generator/test_claim_standards.py`

```python
def test_every_reason_has_one_body():
    assert set(REAL) | set(VIRTUAL) == set(ADJUST_REASONS) and not set(REAL) & set(VIRTUAL)


def test_real_section_shape():
    body = section_body("R05")
    assert body.startswith("> 영구치나 유치의 난발치 및 매복발치는 X-선 촬영 후")
    assert "\n\n출처: 세부사항 2025 p.939, 고시 제2007-46호('07.6.1. 시행)" in body
    assert body.endswith(
        "병원 내부 보충: 촬영으로 확인한 매복 정도와 골삭제 여부를 차트에 함께 적는다."
    )
    assert "병원 내부 보충" not in section_body("D03")


def test_virtual_section_marked():
    assert section_body("R03").startswith(VIRTUAL_MARK + "\n\n- ")
    assert f"{PERIO_POCKET_MIN_MM}mm" in section_body("R03")


def test_quote_ages_match_master():
    assert f"{SCALING_MIN_AGE}세 이상" in REAL["E01"].quote[-1]
    assert f"{CONDITIONAL_INSURED_MIN_AGE}세 이상" in REAL["R06"].quote[0]


def test_words_inside_section():
    for r, x in REAL.items():
        assert all(w in section_body(r) for w in x.words), r


def test_found_quote_inside_real_section():
    # Review Focus 1 — 한 문단 = 한 줄이라 문단 안 문장 인용이 후검증을 통과한다
    from backend.claim_check import found

    assert found(
        "X-선 촬영 없이 일률적으로 산정된 난발치의 경우는 해당 발치로", section_body("R05")
    )
    assert found("청구된 상병분류기호는 의무기록 내용과 일치하여야 한다", section_body("C01"))


def test_claim_doc_body_is_code():
    d = BY_KEY["claim-외과임플란트"]
    body = documents.draft(d)
    assert INTRO_NOTE in body.split("\n## ")[0]
    assert "## 발치 난이도 산정 근거\n\n" + section_body("R05") in body
```

`tests/generator/test_documents.py`에 더한다: `stage_bodies`가 `claim_standard`를 resolve하지 않는다. 가짜 ctx로 resolve 호출 목록에 청구 문서 id가 없는지 본다.

- [ ] **Step 2: 실패 확인** — `uv run pytest tests/generator/test_claim_standards.py -q` → ModuleNotFoundError
- [ ] **Step 3: `generator/claim_standards.py` 구현**, `doc_catalog._CLAIM_RULE`을 이 모듈의 words로(사실 `text`는 실제 절은 supplement가 있으면 그것, 없으면 첫 인용 문단, 가상 절은 지금 문장). R04 `same` 뗌. `documents.draft`·`stage_bodies` 고침.
- [ ] **Step 4: 통과 확인** — `uv run pytest tests/generator -q -k "claim or document or doc_catalog or doc_sync"` → PASS. 기존 기대값이 바뀐 테스트(`test_checks`의 청구 must_include 등)는 새 본문 기준으로 고친다.
- [ ] **Step 5: 커밋** — `generator: 청구 기준 문서를 실제 고시 발췌·병원 내부 기준 절로 — 본문은 코드가 쓴다`

### Task 2: R05 사랑니 차트 — X-선 근거 모양 (verify 먼저)

**Files:**
- Modify: `generator/chart.py` (`EVIDENCE_WORDS["R05"]`, `PARAPHRASE`, `NEGATION["R05"]`, `_requirement` R05 분기, `_style` R05 부분 소견, `draft_note` R05 줄, `_plain_requirement_words` R05)
- Modify: `generator/master.py` — `R05_NEAR_MISS_SHARE = PERIO_NEAR_MISS_SHARE` 다음 줄에 주석(추정값, R05 부분 소견 = 매복·골삭제는 적고 촬영 없음)
- Modify: `generator/verify/chart.py` `check_chart_evidence_words` 독스트링(R05 부분 소견 포함 — 검사는 `evidence_forbidden`이 맡는다)
- Test: `tests/generator/test_chart_redesign.py`, `tests/generator/test_chart.py`(R05 기대값)

**Interfaces:**
- Produces: R05 요건 사실 `{"key": "R05", "evidence", "impaction", "bone_removal", "xray"}`
  - `xray`: 근거 있음이면 "X-선", 없으면 None. 부분 소견은 impaction·bone_removal이 있고 xray가 None
  - style ∈ explicit·paraphrase·negated·plain·near_miss
- 값:
  - `EVIDENCE_WORDS["R05"] = ("X-선", "방사선", "파노라마")`
  - `PARAPHRASE["X-선"] = "엑스레이"`
  - `NEGATION["R05"] = "X-선 촬영 없이 발치"`
  - 초안 근거 줄
    - 근거 있음: `f"X-선 촬영으로 {impaction} 확인, 골삭제 후 발치."`
    - 부분 소견: `f"{impaction} 상태로 골삭제 후 발치."`
  - `_plain_requirement_words`
    - 근거 있음: `["X-선", impaction, "골삭제"]`
    - 부분 소견: `[impaction, "골삭제"]`
    - 그 밖(plain·negated): `[]`
- 난수(Review Focus 2):
  - 근거 있음 impaction은 지금처럼 `_pick(rng, …)`(근거 있는 줄만 뽑던 그대로)
  - 부분 소견 여부와 그 impaction은 `_form(str(treatment_id), "r05_near", …)` 해시로 고른다
    - 부분 소견 비율: `int(sha256(...),16) % 1000 < R05_NEAR_MISS_SHARE * 1000`
    - 부정으로 뽑힌 줄은 부분 소견이 아니다
  - `_style`의 `rng.random()` 호출 횟수는 그대로다
- `xray`는 요건 사실이라 `narrative_payload`가 다른 요건 필드처럼 `history`로 넘긴다. 프롬프트의 `history` 설명에는 없지만, 초안 문장과 꼭 넣을 말("X-선")이 함께 가고 프롬프트의 "`must_mention`의 말 그대로" 규칙이 지킨다. 프롬프트는 고치지 않는다(해시 유지).

- [ ] **Step 1: 실패하는 테스트**(`test_chart_redesign.py`)

```python
def test_r05_evidence_needs_xray(conn):  # 근거 있는 R05 줄의 꼭 넣을 말에 "X-선"
def test_r05_near_miss_has_impaction_no_xray(conn):  # style near_miss인 R05 줄: impaction·bone_removal 있고 xray None, evidence False
def test_r05_paraphrase_forbids_xray_words():  # Review Focus 3 — paraphrase 사실의 evidence_forbidden ⊇ {"X-선","방사선","파노라마"}, must에 "엑스레이"
def test_r05_style_draws_same_rng():  # Review Focus 2 — 세는 rng로 _style을 R05 근거 없음/있음 사실에 돌려 random() 호출이 각 1번
def test_r05_styles_present(conn):  # 생성 DB R05 요건 줄 style에 explicit·near_miss·negated·plain이 모두 있음(paraphrase는 비율이 작아 없을 수 있음 — 단언하지 않음)
```

- [ ] **Step 2: 실패 확인** — `uv run pytest tests/generator/test_chart_redesign.py -q -k r05` → FAIL
- [ ] **Step 3: 구현**(위 값). 키워드 기준선 `evaluation/claim_check.py`는 `EVIDENCE_WORDS`를 그대로 읽어 코드 수정이 없다. 독스트링의 R05 설명만 맞춘다.
- [ ] **Step 4: 통과 확인** — `uv run pytest tests/generator tests/evaluation -q` → PASS
- [ ] **Step 5: 커밋** — `generator: R05 사랑니 차트 근거를 X-선 촬영으로 — 부분 소견은 매복·골삭제만`

### Task 3: 화면 — 절 카드를 마크다운으로

**Files:**
- Modify: `frontend/components/ask/doc-body.tsx` — `blockquote` 컴포넌트(토큰만: `border-l-2 border-border pl-3 text-muted-foreground` 등 globals 토큰)
- Modify: `frontend/components/claims/adjust-table.tsx` `StandardBox` — `<p className="whitespace-pre-line">` 대신 `<DocBody markdown={card.body} />`. DocBody의 `max-w-prose`가 카드에서 넓이를 제한하면 `className` 인자를 더한다.
- Test: `frontend/e2e/claims.spec.ts`

- [ ] **Step 1: e2e 단언 추가** — 사랑니 R05 조정 건을 펼치면, 절 카드 안에 `blockquote`가 있고 "출처:" 텍스트가 보이며 카드 텍스트에 "> "가 없다. 가상 절(R03 건)은 "병원 내부 점검 기준 — 고시에 같은 문장 없음"이 보인다.
- [ ] **Step 2: 실패 확인** — Task 4 데이터 뒤에 `make e2e`(로컬). 데이터 전이면 이 단언만 FAIL.
- [ ] **Step 3: 구현**
- [ ] **Step 4: `make lint` + `make e2e` 통과**
- [ ] **Step 5: 커밋** — `frontend: 청구 기준 절 카드를 마크다운으로 — 인용 블록·출처 줄`

### Task 4: 데이터 다시 만들기 (재설계 범위 둘)

- [ ] **Step 1: 나갈 작업 수 확인** — `make data`. 멈춘 출력에서 작업 수를 확인한다.
  - Expected: `chart` 작업은 R05 요건 줄 수(최대 195), `document` 작업 0
  - 다른 task 작업이 생기면 멈추고 원인을 찾는다(난수 밀림 또는 프롬프트 변경)
- [ ] **Step 2: 차트 채우기** — `/fill-llm-jobs`(과금 없음) → `make llm-import` → `make data`
- [ ] **Step 3: `make verify`** — 위반 0
- [ ] **Step 4: 정답 숨긴 판독**
  - R05 요건 줄 표본 30개를 무작위로 고른다(근거 있음·부분 소견·부정·plain 섞어서).
  - 보조 에이전트에게 R05 절 본문과 차트만 주고 근거 있음·없음을 고르게 한다. 결과를 정답과 맞춰 본다.
  - 기대: 대부분 맞힘. 틀린 줄은 모양을 고칠지 기록만 할지 정한다.
- [ ] **Step 5: `make llm-prune`**(옛 문서·차트 캐시 줄 정리), `make wireframe`이 청구 기준 문서를 읽으면 다시 뽑는다
- [ ] **Step 6: 커밋**
  - 캐시·생성 산출물 중 저장소에 들어가는 파일만 이름으로
  - 메시지: `generator: 청구 기준·R05 차트 재생성`

### Task 5: ⑤ 다시 재기 (과금 — 명령마다 승인)

- [ ] **Step 1: 이전 수치 확보** — AI 실험 기록 §22·§24·§28~§30의 dev 수치를 표로 옮겨 둔다(새로 돌리지 않음)
- [ ] **Step 2:** `make index EMBEDDING=<지금 모델>` (약 $0.03)
- [ ] **Step 3:** `make claim-check SCOPE=all` (약 $4.3)
- [ ] **Step 4:** `make claim-omission SCOPE=all` (바뀐 R05 줄만)
- [ ] **Step 5:** `make claim-evidence SCOPE=all`
- [ ] **Step 6:** `make claim-appeal-dev` (약 $1.6)
- [ ] **Step 7:** `make eval` — ⑤-2a·2b·⑤-3(Hit@5·MRR·인용 실재율)·⑤-4 dev 수치. 키워드 기준선 R05 결과를 따로 본다(부정·바꿔 말하기에 속고 부분 소견은 맞힘)
- [ ] **Step 8:** `python -m evaluation.claim_qa` 베끼기 경고를 새 본문으로 다시 본다(과금 없음, 명령은 `evaluation/claim_qa.py` 독스트링)

### Task 6: 문서

**Files:**
- `docs/치과 AX - 가상 데이터 생성 설계.md`
  - 청구 기준 문서 규칙: 코드가 쓰는 본문, 실제 절·가상 절·보충 줄·출처 줄, `generator/claim_standards.py`
  - R05 근거 모양
  - 알려진 흠: 실란트 나이 12/18, 급여 임플란트 2개 초과 환자, 청구방법 고시 출처 판
- `docs/치과 AX - 앱 설계.md`: 절 카드 마크다운
- `docs/치과 AX - 기능 정의.md` §9
  - "실제 청구 기준" 항목을 B안 남은 것으로 줄인다
  - 새 할 일: ⑤-4가 출처 없는 절을 기준 인용으로 쓰지 않게(프롬프트 개정 때)
- `docs/치과 AX - AI 실험 기록.md`: 새 절
  - 문제·원인·시도·결과(Task 5 수치를 이전과 나란히)·한계(R01~R03, `cq-R04-1`, 2016 전체본)
- `docs/치과 AX - 개발자 소개.md`·`프로젝트 소개.md`에 청구 기준이 "가상"이라고 적힌 곳이 있으면 고친다

- [ ] **Step 1: 고치고 훑기**
- [ ] **Step 2: pj2-doc-tidy로 점검**(PR 전)
- [ ] **Step 3: 커밋** — `docs: 청구 기준 실제 고시 발췌 — 규칙·실험 기록·남은 할 일`
