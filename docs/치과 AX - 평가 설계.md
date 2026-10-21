---
title: 치과 AX - 평가 설계
related:
  - "[[치과 AX - 기능 정의]]"
  - "[[치과 AX - 가상 데이터 생성 설계]]"
  - "[[치과 AX - 앱 설계]]"
created: 2026-10-16
updated: 2026-10-21
tags:
  - 치과AX
  - 평가
---

> **뭘 어떻게 측정하나.** `eval.py`와 사람 판정 절차가 읽는 문서. 데이터가 어떻게 만들어지는지는 [[치과 AX - 가상 데이터 생성 설계]], 기능별 완료 기준은 [[치과 AX - 기능 정의]].
> 정답 저장소(`ground_truth/truth.sqlite`)의 테이블 정의와 채우는 쪽은 **이 문서 §6에만** 있다.

## 1. 원칙

1. **정답은 앱 DB 밖**(`CLAUDE.md` 절대 규칙). 평가 스크립트만 `truth.sqlite`를 `ATTACH`해 조인
2. **튜닝 셋 ≠ 보고 셋.** 모든 평가셋은 dev/test로 나눔. 튜닝·`make gate`·CI는 `--split dev`만. **test는 데모 직전 1회**, `truth_eval_runs`에 이력 강제 기록
3. **절대값보다 조건 간 차이.** 합성 데이터라 절대 수치는 높게 나옴. README의 주 지표는 "리랭커 on/off", "프롬프트 v1→v2" 같은 차이
4. **분수 병기.** 모수가 작아(dev 20·6·4문항) 1문항이 5~25%p를 움직임. 퍼센트만 쓰지 않음
5. **폴백 건 제외.** `ax_llm_calls.fallback_used`면 "A 결과"가 실제로는 B 출력 → 블라인드·정확도 집계에서 제외
6. **생성 분포는 현실대로.** 리포트가 재샘플링하지 않음. 치우침은 가중/단순 평균 병기로 드러냄

## 2. 통화 분류 (①·⑤)

### 2.1 평가 풀

크기는 `config` `calls_with_transcript`·`eval_pool_size`·`blind_pool_size`(small은 기간 비율로 축소). 아래 배분·분할은 전부 풀 크기에서 계산한다 — 고정 숫자를 두지 않는다.

- 전사 전체에 골드 라벨(`truth_call_labels`, 생성기 산출)
- **사람 판정 풀**: 생성기가 고정 시드로 층화 추출. 희귀 카테고리(불만·응급·모르겠음)는 각 카테고리 전사의 **60%를 뽑는다**(비례 배분보다 많이 뽑는 과표집. 전수를 뽑으면 비풀이 0건이라 60%에서 멈춤), 풀의 나머지는 남은 카테고리에 비례 배분. dev/test는 카테고리별 절반, 홀수 잔여는 카테고리를 돌며 dev/test에 번갈아 배정 → 합계 절반씩(풀이 홀수면 dev가 1건 더). 순번은 각 split 안에서 고정 셔플(층화 순서대로 두면 카테고리가 드러남)하고 **dev 전부 → test** 순으로 매긴다. 앱 DB에는 `in_eval_pool`·`eval_sort_order`만 — 카테고리 정보 없음
- **블라인드 풀**: 판정 풀 안에서 고정 시드로 추출, `in_blind_pool` — 분류 배치 대상에 이미 포함
- 비풀 = `silver` (골드만 있음, 변화 병기용)

### 2.2 지표

| 지표 | 모수 | 정의 |
|---|---|---|
| 항목별 정확도 | 9항목 × 모델 × `prompt_version` | 라벨 일치 비율 + **n 표시** |
| 목적 Strict | 전체 | `purpose == primary_purpose` — **헤드라인** |
| 목적 Relaxed | 카테고리별만 | `purpose == primary` OR (`purpose ∈ accepted_purposes` AND 대응 플래그 true). 플래그가 있는 목적(가격·불만·응급·예약 희망)에만 상승분이 몰리므로 헤드라인에 쓰지 않음 |
| 가중 / 단순 평균 | 전체 | 가중 = 현실 체감, 단순 = 카테고리 동등 |
| 판정 풀 정확도 | 풀 | **역확률 가중** w_c = N_c / n_c (층화 추출 보정). 가중 없이 전사 전체 골드와 비교하면 희귀 카테고리 비중 차이로 역전 |

**정확도 셋 분리 보고**
- 모델 vs 사람 (판정 풀) — 앱 화면의 "모델 일치율"은 이 중 **dev만** 집계(test 오염 방지). 붙여넣기 통화는 모수에서 제외
- 모델 vs 골드 (전사 전체)
- **사람 vs 골드 (판정 풀)** — 라벨 타당성. 낮으면 생성 라벨이 '의도'일 뿐 사실이 아니라는 뜻

**버전 비교**: 두 버전 모두 `status='ok'`인 call_id **교집합**으로만, n 표시. 리포트는 버전마다 `model_actual` 쌍을 표기(`prompt_version` 정의는 [[치과 AX - 앱 설계]] §1).

### 2.3 사람 판정 절차

- 화면은 전사 기본 노출, 요약은 접힘. 펼쳤으면 `shown_model` 기록 → 앵커링 사후 분석
- 제시 순서 `eval_sort_order` 고정. dev 먼저, test는 `judge_upto`를 풀 크기로 올려 개방([[치과 AX - 앱 설계]] §6). 건너뛰기는 끝으로 복귀(영구 누락 없음)
- 판정 큐는 **두 모델 모두 `ok`인 건만**(엇갈림 배지가 성립해야 함)
- 데이터셋 재생성에도 판정 보존: `make export-human` / `import-human`(키는 [[치과 AX - 앱 설계]] §7)
- 팀 합류 시 50건 이중 판정 → Cohen's κ 하나

### 2.4 블라인드 비교

- 요약 A·B를 모델명 가리고 제시 → `left / right / tie / both_wrong`
- **문체 누출 방지**: 요약 프롬프트에서 출력 형식 고정(마크다운·글머리 금지, 명사형 종결 '~함/~음', 공백 포함 80~110자 단일 문단) + 코드 후처리로 줄바꿈·기호 정규화
- **좌우 배치** `hash(transcript, seed) % 2` — 새로고침·재생성에도 불변, 50:50
- 집계는 `(model_actual_left, model_actual_right)` **쌍별**. 튜닝 중(경량 vs 경량) 투표와 최종(프론티어 vs 경량)이 섞이지 않음
- 리포트에 **위치 편향 지표**(left/right 선택 비율) 병기 — 50:50에서 벗어나면 문체 누출 의심
- 최종 모델 쌍의 투표는 데모 고정 직전에 새로 받음
- **블라인드 큐**(원본은 여기): `active_prompt_version`의 분류 ID 쌍에 대한 `ax_blind_votes`가 `NOT EXISTS`이고 `judge_status='done'`인 건(판정 전에 요약을 보는 앵커링 방지)

## 3. RAG 검색·챗 (③)

### 3.1 질문 60개

| 유형 | 수 | 예 | 정답 | 채점 |
|---|---|---|---|---|
| 매뉴얼형 | 18 | "노쇼 3회면 어떻게?", "실손 서류 발급 절차?" | 매뉴얼·FAQ 문단 | Hit@5, MRR |
| 공지형 | 10 | "스케일링 비용 언제 바뀌었어?", "추석 휴진?" | 최신 공지(옛 버전은 방해) | Hit@5 + **Top-1이 최신 버전** |
| 사례형 | 12 | "김○○ 지난번에 뭐 물어봤지?", "리마인드 문자 갔었나?" | 통화 전사·문자 스레드 | Hit@5 |
| 데이터형 | 12 | "내일 잔액 있는 환자 몇 명?", "오늘 취소 몇 건?" | `query_*` 도구 | 도구 선택 + `expected_args` + 숫자 일치 |
| 거절형 | 8 | "원장님 자택 주소?", "○○ 직원 연봉?" (행정 비공개 6) + 생성 시 금지한 상식 2 | 없음 | 마지막 호출이 `refuse`인지 |

**작성 시점·방식** (문서·전사 생성 **후**)
- 매뉴얼·공지·사례형: 생성된 문서·전사를 보고 질문과 정답 청크를 지정. 질문 핵심 어휘가 정답 문단에 그대로 있으면 바꿔 씀(어휘 일치 비율 < 30%, verify 경고)
- 데이터형: 생성된 DB에서 **답이 2건 이상인 질문**을 고름. 선행 심기 안 함. 재생성 시 12문항 재확인(10분)
- 거절형 상식 2개: **`make data` 전**에 금지 토픽으로 지정(`forbidden_topics.json`) → 정제 필터로 코퍼스에 없음을 보장
- 복합 조건·시간 계산 질문 포함 ("10월 이후 바뀐 비용 중 환자 부담이 늘어난 건?")

**dev 30 / test 30**: 유형 비율 동일. **정답 문서 단위로 격리** — 같은 공지·매뉴얼을 dev와 test가 공유하면 누출. 공지형은 공지마다 옛 버전이 있어야 최신성 채점이 된다(옛 버전 구성은 가상 데이터 생성 설계 §4.2).

### 3.2 채점 규칙

- **`expected_tools[]`**: 순서 있는 목록. "그 환자 7월에 뭐가 삭감됐나?" = `[query_patient, query_claims]` 2단 호출이 정상 경로. **감점은 목록 밖 도구 호출만**(무차별 호출 방지)
- **`expected_args`**: 자유 텍스트 `query` 제외, **반환값에서 답을 못 얻는 키만 필수**, 부분집합 일치. 예: "오늘 취소 몇 건" → `query_schedule`은 `by_status`를 돌려주므로 `status` 필터는 필수 아님, 날짜 범위만 필수. 로그의 `args`는 Pydantic 정규화 후 기록(키 순서·타입 차이로 거짓 음성 방지)
- 숫자 일치: 정답 숫자가 답변 텍스트에 존재(단위 정규화 후)
- 거절 판정: **마지막 도구 호출이 `refuse`**면 통과(먼저 검색해 보고 거절하는 정상 경로 허용). 거절형이 아닌 질문에서 `refuse`를 부르면 그 문항 실패 — 과잉 거절은 다른 유형 정답률에 반영
- 도구 호출 상한 6회/질문 초과 = 실패

### 3.3 지표

| 지표 | 모수 | 정의 |
|---|---|---|
| Hit@5 · MRR · Top-1 | 문서 검색형 **40**(매뉴얼 18 + 공지 10 + 사례 12; dev 20 / test 20) | 정답 청크 top-5 포함 / 순위 역수 / 1위 |
| 최신성 | 공지형 10 | Top-1이 최신 버전 (옛 버전이 top-5에 같이 있어도 됨) |
| 도구 선택 정확도 | 데이터형 12 | §3.2 규칙 통과 비율 |
| 환각 억제율 | 거절형 8 | 마지막 호출이 `refuse`인 비율 |

**`--retrieval-only`**: LLM 호출 없이 `retrieved_ids`만으로 Hit@5·MRR·Top-1·최신성 계산 — 리랭커 on/off, 하이브리드 vs 벡터만 비교는 전부 이 모드($0).

**조건 비교 표기**: 같은 문항에 두 조건을 적용한 paired 비교 → 분수(`14/20 → 17/20`) + 유의성 한 줄.
구제/손실 문항 목록과 원인(예: "옛 공지 배제는 리랭커가 구제")을 자동 출력 — **숫자보다 이 분석이 설득력의 본체.**

## 4. 준비표 (⑧)

- **집계**: ⑧ 착수 시 `prep_facts(date)` 첫 출력을 스냅샷으로 고정해 회귀 테스트
- **제안 후검증**(코드, 건별): `used_facts`의 모든 (key, value)가 입력 `facts`와 일치 **and** `text`에 등장하는 숫자가 전부 `used_facts.value`(단위 정규화 후)에 속함 → `suggestion_valid`. 실패율을 리포트에
- **사람 채점**: 30명분 `유용 / 무난 / 틀림`

## 5. EOB (슬롯 ②, 조건부)

판독 재현율·대조 정확도·자동 확정률(`truth_eob_lines`·`truth_claims` 대비). 지표 정의는 슬롯 착수 시.

## 6. 정답 저장소 `ground_truth/`

| 테이블 | 채우는 쪽 | 내용 |
|---|---|---|
| `truth_call_labels` | 생성기 | `call_uid` → `primary_purpose`, `accepted_purposes[]`, `counterpart`, `urgency`, flags 6, `noise_type`, `used_fact` json, `split`(dev/test/silver) |
| `truth_rag_qa` | 사람(생성 후) | `id, type, question, answer_text, answer_doc_ids[], answer_chunk_hint, expected_tools[], expected_args json, expect_refusal, split` |
| `truth_doc_meta` | 생성기 | `doc_id → is_distractor, planted_qa_ids[], supersedes_doc_id` |
| `truth_gen_meta` | 생성기 | `key → value`: `seed`, `scale`, `range_start`, `range_end`. verify가 규모별 기간을 읽음. 생성 시각은 넣지 않음(해시 비교) |
| `truth_eval_runs` | eval.py | `split, condition, started_at, report_path` — test 1회 규칙 검증 |
| `truth_claims` | 생성기(EOB 슬롯 착수 시, 조건부) | 청구 심사 결과 정답. 컬럼은 `ground_truth/schema.sql`, 생성 규칙은 가상 데이터 생성 설계 §2.4 |
| `truth_eob_lines` | 생성기(조건부) | 통보서 줄 정답. 슬롯 착수 시 정의 |

같은 내용의 JSON(`call_labels.json`, `rag_qa.json`, `doc_meta.json`)도 커밋 — git diff·리뷰용. `rag_qa.json`이 원본이고 `truth_rag_qa`는 적재 결과.

**앱 DB 쪽 누출 검사**는 생성기 `make verify`에 있음([[치과 AX - 가상 데이터 생성 설계]] §5.3 '누출').

## 7. `eval.py`

```
eval.py --split dev|test [--retrieval-only] [--prompt-version v2] [--condition reranker=off]
  → reports/eval_<split>_<condition>_<ts>.md  (+ truth_eval_runs 1행)
```
- 분류: §2.2 표 + 3종 정확도 + 버전 비교(교집합) + 블라인드 쌍별 승률·위치 편향
- RAG: §3.3 표 + 조건 비교(지정 시) + 구제/손실 문항 덤프
- 준비표: 회귀 통과 여부 + `suggestion_valid` 비율
- 평가 실행 시에만 게이트웨이 파일 캐시 `hash(model, prompt_version, messages, tools)` 활성 — 검색 파라미터만 바꾸는 실험에서 동일 컨텍스트 LLM 재과금 방지
- `is_eval=true`로 실사용 로그와 분리

`make gate`는 이 스크립트의 `--split dev` 출력을 읽음. 기준은 [[치과 AX - 기능 정의]] §6.

## 8. 비용

평가 구간별 비용·모델 운용 방침·상한은 [[치과 AX - 기능 정의]] §7.
