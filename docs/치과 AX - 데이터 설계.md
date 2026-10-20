---
title: 치과 AX - 데이터 설계
related:
  - "[[치과 AX - 기능 정의]]"
  - "[[치과 AX - 평가 설계]]"
created: 2026-10-16
updated: 2026-10-19
status: v2.2 (2026-10-19. 중복 규칙 단일화, 일정·마감 날짜 삭제, 검수 반영 — 통화 enum·급함·플래그 규칙, 캐시 키 attempt, EOB 청구 측을 정형 5단계로, `refuse` 도구, 병원 상수는 `generator/master.py`, 입력 파일은 `config/`. 이전 판은 프로젝트 밖에 별도 보관)
tags:
  - 치과AX
  - 데이터
---

> **어떤 데이터를 어떻게 만드나.** 생성기 코드가 읽는 문서. 뭘 측정하나(지표·채점·정답 저장소 정의)는 [[치과 AX - 평가 설계]], 구현 디테일(WAL·워커·헤더·에러 처리)은 `CLAUDE.md`.
> **범위 밖(프로젝트 후 확장)**: 연쇄 치료계획, 리콜 큐, 임플란트 평생 한도, 다회차 시술의 회차 추적. 환자는 전부 **단발 내원** 모델.

## 0. 전제

| 항목 | 결정 |
|---|---|
| 규모 | 환자 1,000 / 2026-02-01~12-15 / 평일 26명·토 13명 / 통화 전사 1,000 + 메타 5,000 / 문서 50 + 방해 27 / 문자 ~1.5만 / (조건부) EOB 월별 9 + 건별 파생 (= large) |
| 축소 규모 | `--scale small\|medium`은 **하루 밀도(평일 26명) 유지**, 끝(12-15) 고정·시작일만 늦춤(small 08-01 / medium 05-01) → TODAY 기준 구간(확정월·제출월 9~10·전월 11·미래 12/2~15)이 모두 남음. 환자 수·통화 전사·메타·판정/블라인드 풀은 기간 비율(달력 일수)로 축소·반올림. **문서는 모든 규모에서 77개(core 50 + distractor 27) 고정**. 판정 풀 층화는 고정 숫자가 아니라 비율 규칙(희귀 60% 상한 + 나머지 비례, 평가 설계 §2.1)으로 계산 — 문서의 숫자는 large 기준 결과값 |
| 평가 규모 | **사람 판정·RAG 질문 작성·`eval.py` 보고·`make gate`는 모두 large 하나에서만**. small·medium은 개발·테스트용 |
| 기준일 | `TODAY = 2026-12-01 (화)` 고정. 앱은 설정값을 읽음(환경변수로 변경 = 타임머신) |
| 보험 | 건강보험 단일. 월 1회 일괄 청구 + 소량 보완 청구. 실손은 환자 플래그 + 통화 소재만 |
| 스키마 | `emr_*` = 미니 EMR(생성기가 만들고 앱은 읽기 전용) / `ax_*` = 앱이 쓰는 것 |
| 재현성 | 시드 고정, `make data` 한 번에 전체. LLM 생성물은 파일 캐시·커밋, `--regen-llm`으로만 재생성 |
| 단순화 | 수가·심사는 흉내만. 급여 청구액 = fee × 0.7. 소아 급여 특례 없음. '구강검진'은 진찰·파노라마 대체 가상 코드 |
| 생성 순서 | 사람 판정 시작 후 데이터셋 교체 금지(판정이 call_id FK). 정형 로직을 고치면 LLM 캐시 전량 미스 → 정형 완성·verify **뒤에** LLM 1회 |

```yaml
seed: 20261201
today: 2026-12-01
range: { start: 2026-02-01, end: 2026-12-15 }
patients: 1000
visits_per_day: 26          # 평일. 토요일 ×0.5
reschedule_rate: 0.05       # 예약 변경 쌍 → 변경 통화 150건 모수
noshow_rate: 0.06
sameday_cancel_rate: 0.04
confirm_rate: 0.85          # 전날 리마인드 응답
unpaid_rate: 0.15           # 내원당 미수 발생률
settle_next_visit: 0.80     # 미수가 다음 내원에서 정산되는 비율
calls_with_transcript: 1000
calls_meta_only: 5000
eval_pool_size: 300         # 층화 규칙은 평가 설계 §2.1
blind_pool_size: 150
documents: { core: 50, distractor: 27 }
messages: { appt_confirm: 1.0, reminder: 1.0, noshow: 1.0, balance: 0.5, reply_rate: 0.2 }
eob: { monthly_forms: 9, supplementary_ratio: 0.10 }   # supplementary_ratio는 정형 5단계, monthly_forms는 PDF(조건부)
holidays: config/holidays_2026.json
forbidden_topics: config/forbidden_topics.json
scale_presets:                                          # range.end 고정, 시작일만 늦춤
  small:  { start: 2026-08-01 }                         # 137/318 ≈ 0.43 — 테스트 fixture
  medium: { start: 2026-05-01 }                         # 229/318 ≈ 0.72
  large:  { start: 2026-02-01 }                         # 1.0 (= range)
```

## 1. 병원 프로필

영업시간·동시성 한도·시술 마스터의 코드상 원본은 `generator/master.py` (이 절과 어긋나면 둘 다 고친다).

- 원장 1, 위생사 3, 데스크 2, 상담실장 1. 체어 4
- 평일 09:30–18:30(점심 13–14), 토 09:30–13:30, **목·일·공휴일 휴진**. 30분 슬롯
- 내원 구성: 진료 70% / 위생(스케일링·검진) 25% / 상담 5%. 피크 10–11시·15–17시
- **병목은 원장 시간**: 동시 2체어 × 16슬롯 = 32슬롯/일. 진료 18건 × 평균 40분 = 24슬롯. 60분 시술은 비피크 우선 배치
- 동시성: 원장 진료 체어마다 위생사 1명 어시스트 → 동시 (hygiene 수 + treatment 수) ≤ 3. 상담은 상담실(체어·위생사 불필요)
- 신환 월 내원의 12%. 노쇼 6%, 당일 취소 4%, 변경 5%, 미확인 15%, 미수 발생 15%

## 2. `emr_*`

> 슬롯 기능의 추가 데이터(⑪ 통장·카드 거래, 지출 등)는 기존 `emr_*`를 수정하지 않는 **새 테이블**로만 만든다. 기존 테이블이 바뀌면 정형 데이터 → LLM 캐시 → 전사 → 사람 판정까지 연쇄로 무효가 된다.

### 2.1 마스터

**`emr_clinic`**(1행): `name, chairs, hours_json`
**`emr_staff`**: `id, name, role(doctor/hygienist/desk/consultant), active`

**`emr_procedures`** — 19개 고정
| 컬럼 | 설명 |
|---|---|
| code PK | 수가코드 형식 흉내(`U2232`) |
| name, category | `preventive/restorative/endo/prosth/surgery/implant/ortho/cosmetic/perio/consult` |
| is_insured | `yes/no/conditional` |
| fee_total, patient_share_rate | 급여 0.3, 비급여 1.0 |
| duration_min | 내원 1회 소요(아래 표) |
| typical_sessions | 보통 몇 회(전사 소재용. 회차 추적은 안 함) |
| insured_limit, limit_scope | 스케일링 1/`year` |
| age_group | `child/adult/senior/null` |
| is_tooth_level | 치아 단위 → `tooth_no` 필수 |
| aliases | 구어체 별칭 json. 전사 fact 검증·검색 동의어 |

코드·값의 원본은 `generator/master.py`(`U0001`~`U0019` = 표 순번, 스케일링 `U0001`).

| # | 분류 | 시술 | 급여 | 비용 | 소요 | 비고 |
|---|---|---|---|---|---|---|
| 1 | preventive | 스케일링 | yes | 60,000 | 30 | adult 연 1회 급여. 미성년 비급여 |
| 2 | preventive | 구강검진 | yes | 15,000 | 30 | 가상 코드 |
| 3 | preventive | 불소도포 | no | 30,000 | 30 | child |
| 4 | preventive | 실란트 | yes | 40,000 | 30 | child, 치아 |
| 5 | restorative | 레진 | no | 80,000 | 30 | 치아 |
| 6 | restorative | 아말감/GI | yes | 25,000 | 30 | 치아 |
| 7 | restorative | 인레이 | no | 350,000 | 60 | 치아 |
| 8 | endo | 신경치료 | yes | 120,000 | 60 | 치아 |
| 9 | prosth | 크라운 | no | 550,000 | 60 | 치아 |
| 10 | prosth | 브릿지 | no | 1,500,000 | 60 | |
| 11 | surgery | 단순발치 | yes | 30,000 | 30 | 치아 |
| 12 | surgery | 사랑니 발치 | yes | 90,000 | 60 | 치아 |
| 13 | implant | 임플란트 1차 | conditional | 1,300,000 | 90 | senior, 치아 (한도 추적은 범위 밖 → 생성 시 환자당 ≤ 2개로만 제한) |
| 14 | implant | 임플란트 2차 | **no** | 0 | 60 | 청구 대상 아님 |
| 15 | ortho | 교정 상담 | no | 0 | 30 | |
| 16 | ortho | 교정 월 체크 | no | 80,000 | 30 | |
| 17 | cosmetic | 미백 | no | 300,000 | 60 | 가격 문의 소재 |
| 18 | perio | 잇몸치료 | yes | 35,000 | 30 | |
| 19 | consult | 상담 | no | 0 | 30 | 상담실장, 체어 불필요 |

**치아 행 규칙**: 치아 단위 시술은 같은 날 여러 치아면 **치아당 `emr_treatments` 1행, 청구 1줄**. 전악 시술(1,2,3,18)은 `tooth_no='00'`(센티널, NULL 아님) 1행, 같은 날 같은 전악 시술 2회 금지. 내원당 급여 줄 1~3개.

### 2.2 환자

**`emr_families`**: `id, surname`
**`emr_patients`**: `id, chart_no('C-00001'), name, birth_date, sex, phone('010-0000-xxxx'), family_id?, guardian_patient_id?(소아), registered_at, recall_cycle_months(기본 6), has_private_insurance, note?`

파생(쿼리): 마지막 내원일, 신환(첫 내원 30일 이내), 휴면(6개월↑ 미내원), 가족 리콜 지남(가족 구성원 `MAX(내원)+recall_cycle < 기준일`)

| 축 | 분포 (1,000명) |
|---|---|
| 연령 | 소아(0–12) 15% / 청소년(13–19) 8% / 성인 62% / 노인(65↑) 15% |
| 가족 | 35%가 2–4명 묶음. 소아는 100% 같은 가족 성인이 보호자 |
| 활동 | 활동(6개월 내 내원) 70% — 그중 최근 3개월 신환 15%p / 휴면 30% |
| 신환 | 월 내원의 12% |
| 실손 | 50% / 메모 10% / 이름 전부 한국 이름 |

연령별 시술 가중: 소아 → 불소·실란트·아말감 / 청소년 → 교정 / 노인 → 임플란트·발치·잇몸

### 2.3 예약·진료

**`emr_appointments`**
| 컬럼 | 설명 |
|---|---|
| id, patient_id, staff_id, chair_no | 진료=원장, 위생=위생사, 상담=상담실장 |
| scheduled_at, duration_min, type | `treatment/hygiene/consult` |
| planned_procedure_code FK | 예정 시술(준비표·전사가 읽음). 전 예약 필수 |
| status | `scheduled/completed/cancelled/noshow` |
| rescheduled_from_id? FK | 변경으로 생긴 새 예약이면 원래 예약. **변경 쌍** = 원래(cancelled) + 새(scheduled). 변경 쌍이 아닌 `cancelled`는 당일 취소(4%)뿐 — 취소 통화는 여기서 역산 |
| confirmed | 전날 리마인드 응답 |
| created_at, cancelled_at? | 통화 시각·as-of의 근거 |

**`emr_treatments`** — completed 예약에 1~N행: `id, appointment_id, patient_id, procedure_code, tooth_no('00' 센티널), performed_at, fee_total, insured_amount, patient_amount`

### 2.4 수납·보험

**`emr_payments`**
| 컬럼 | 설명 |
|---|---|
| id, patient_id, appointment_id? | |
| kind | `visit`(당일 수납) / `settlement`(이전 미수 정산, `amount_due=0`) |
| amount_due, amount_paid, paid_at, method | `card/cash/transfer` |

환자 잔액 = Σ(amount_due − amount_paid). **미수 규칙**: 내원의 15%에서 일부만 수납 → 그중 80%는 다음 내원 시 `settlement` 행으로 정산, 20%는 잔류. (발생률만 두면 잔액이 누적돼 준비표 미수 칩이 절반 점등 — 6차 검증)

**`emr_insurance_usage`** (PK `patient_id, benefit, year`): `benefit='scaling'`, `year`, `used`, `limit=1`. 19세↑ 연 1회. 준비표 "보험 잔여" 칩의 근거

**`emr_claims`** — 청구 장부
| 컬럼 | 설명 |
|---|---|
| id, treatment_id, patient_id, chart_no(비정규화), tooth_no NOT NULL | |
| claim_month('2026-07'), batch_type | `monthly`(기본)/`supplementary`(~10%, 정형 5단계에서 배정) |
| service_date, procedure_code, claim_amount(=insured_amount), submitted_at(익월 초) | |
| status | `pending`(11월, 미제출) / `submitted`(9~10월) / `paid·adjusted·denied`(확정 월 2~8월, 생성기 추첨 — 병원이 이미 아는 과거 결과) |
| paid_amount?, adjust_code? | 확정 월만 |

**UNIQUE `(chart_no, service_date, procedure_code, tooth_no)`** — EOB 대조 키. 최근 월(9~10월)의 심사 결과는 이 테이블에 없고 `truth_claims`에만(정형 5단계에서 생성).

### 2.5 문자

**`emr_messages`**: `id, patient_id, direction(out/in), sent_at, body, trigger(appt_confirm/reminder/noshow/balance/doc_ready/reply/other), related_appointment_id?`

| trigger | 규칙 | 건수 |
|---|---|---|
| appt_confirm | 예약 생성 시 | ~5,600 |
| reminder | 전날 | ~5,600 |
| noshow | 노쇼 시 | ~340 |
| balance | 미수 발생의 50% | ~375 |
| doc_ready | 서류 요청 **통화 이후** | ~40 |
| reply(in) | out의 20%, LLM 짧은 생성 | ~2,400 |

`out`은 템플릿(LLM 없음). 색인은 건별이 아니라 **환자별 월 스레드 1청크**(`[환자: 김○○(C-00001) | 2026-07] 07-03 예약 확인 … 07-10 답장: …`) ≈ 3,000청크.

## 3. `ax_*`

> 생성기는 `ax_calls`(전사 1,000 + 메타 5,000)와 `ax_documents`/`ax_chunks`만 적재. 나머지는 앱이 채움. 테스트 fixture(small)는 예외 — §5.4.

### 3.1 통화·분류·판정

**`ax_calls`**
| 컬럼 | 설명 |
|---|---|
| id | |
| call_uid UNIQUE | **불변 키 `c-0481`** — 시드 셔플 순번. 캐시 키·human import 폴백. (목적을 담은 `purpose:순번` 형식은 라벨 누출 — 6차 검증) |
| source | `generated/pasted/sample_audio/uploaded` |
| patient_id?, caller_phone, caller_name? | **분류기 입력에 쓰지 않음** |
| direction | `inbound/outbound` |
| started_at, duration_sec, has_transcript, audio_path?, transcript? | 전사는 화자 구분된 **순수 대화만**(prefix 없음, 차트번호 발화 없음) |
| in_eval_pool, eval_sort_order? | 생성기가 층화 추출해 마킹(300). **카테고리 정보 없음**. 순번 **dev = 1~150, test = 151~300**(large 기준. 코드는 고정 숫자 대신 풀 앞쪽 절반 = dev, 홀수면 dev가 1건 더. 각 split 안에서 고정 셔플, `truth_call_labels.split`과 일치) |
| judge_status | `none/pending/skipped/done` — 생성기는 풀 300만 `pending`, 나머지 `none`. 붙여넣기는 `pending` |
| in_blind_pool, blind_sort_order?, blind_status | 150. **판정 풀 300의 부분집합** |
| indexed_at? | |

골드 라벨·노이즈 유형은 여기 없음 → `truth_call_labels`. **분류기 입력 = `transcript` + `direction`만** (환자·번호·시각은 정답 힌트. 함수 시그니처 `classify(transcript, direction)`로 강제).

**`ax_call_classifications`**: `id, call_id, model_alias(A/B), model_actual, prompt_version, summary, todo json, labels json{purpose, counterpart, urgency, has_todo, wants_appt, appt_confirmed, price_inquiry, complaint, emergency}, status(ok/failed), error?, llm_call_id, created_at`
UNIQUE `(call_id, model_alias, prompt_version)`. **`prompt_version`은 (프롬프트 텍스트 × 모델 쌍)의 버전** — 모델만 바꿔도 올림(`v3-final`). 대시보드는 버전마다 `model_actual` 쌍 표기.

**`ax_call_disagreements`**: PK `(call_id, prompt_version)`, `fields json, is_core`(목적·급함·응급 불일치)

**`ax_judgments`**: `id, call_id, labels json, memo?, judged_by, judged_at(실제 시계), elapsed_sec, shown_model?`(판정 중 요약을 펼쳤으면 어느 모델)

**`ax_blind_votes`**: `id, call_id, classification_left_id, classification_right_id(FK → classifications), choice(left/right/tie/both_wrong), judged_by, voted_at`
좌우 배치·승률 집계 규칙은 [[치과 AX - 평가 설계]] §2.4.

### 3.2 LLM 로그

**`ax_llm_calls`**: `id, task, model_alias, model_actual, prompt_version, input_path?(메시지 덤프 파일), fallback_used, prompt_tokens, completion_tokens, latency_ms, cost_usd, success, error?, ref_type, ref_id, created_at`
`fallback_used` 건의 집계 제외는 [[치과 AX - 평가 설계]] §1. `ref_type/ref_id`는 다형 참조 — verify에서 타입별 조인으로 검사.

### 3.3 검색

**`ax_documents`**: `id, type(manual/faq/insurance_guide/notice/education/external), title, version, effective_date?, path, body`
(방해 여부·심은 QA는 `truth_doc_meta`에만)

**`ax_chunks`**: `id text PK('doc:12:3' / 'call:481:0' / 'thread:C-00001:2026-07'), source_type(document/call/message_thread), source_id, chunk_index, chart_no?, event_date?, doc_type?(실컬럼 — 필터용), text, meta json`
`text`는 **출처 prefix 의무**: 문서 `제목 > 섹션`, 통화 `[환자: 김○○(C-00001) | 2026-07-03 | 수신]`(환자 없는 통화는 `[상대: 보험사 | 2026-07-03 | 수신]`), 스레드 `[환자: … | 2026-07]`.
- FTS5 `ax_chunks_fts(text)` — `unicode61` + 질의 측 `원본어절* OR 조사제거어절*`
- Chroma 컬렉션 `chunks`(id = `ax_chunks.id`, metadata flat 복사). 단일 조건 필터는 Chroma `where`, id 선필터는 후보 수백 이하일 때만

**`ax_search_logs`**: `id, query, tools_used json[{tool, args(Pydantic 정규화 후), ok}], retrieved_ids json, answer, recommended_queries json, is_eval, llm_call_ids json, created_at`

**도구 5개 — 시그니처 고정** (평가 설계 §3.2 `expected_args` 채점의 기준)

| 도구 | 인자 | 반환 |
|---|---|---|
| `search_docs` | `query`, `source_types?[]`, `date_from?/date_to?`, `patient_chart_no?`, `top_k?=5` | 청크 목록 |
| `query_patient` | `chart_no?` / `name?` (하나 필수) | `{matches[], summary?}` — 동명이인이면 summary 없이 후보만. summary: 기본정보·마지막 내원·잔액·다음 예약·가족 리콜 |
| `query_schedule` | `date_from, date_to`(단일 날짜는 같은 값), `status?, type?, has_unpaid_balance?, is_new_patient?` | 예약 목록 + `{count, by_status, by_type}` |
| `query_claims` | `date_from?/date_to?, chart_no?, procedure_code?, status?` | 청구 목록 + `{count, total_amount, by_status}`. 확정 월은 결과·사유 포함 |
| `refuse` | `reason`(`not_found` / `restricted` — 비공개 행정정보) | 없음. 호출 시 턴 종료, 앱이 고정 거절 문구 표시 |

- 날짜 전부 `YYYY-MM-DD`. 도구 description에 `TODAY=2026-12-01` 예시 하드코딩
- 호출 상한 6회/질문. 경계: 건수·목록 → `query_*`, 절차·규정·과거 대화 → `search_docs`, 근거가 없거나 비공개 정보면 `refuse`
- **검색은 버킷 분리**: `document` 상위 25 + `call/message_thread` 상위 25 따로 → 리랭커 → `manual_results` / `case_results`. `event_date ≤ TODAY` 강제

### 3.4 준비표·리포트

**`ax_prep_sheets`**: `id, sheet_date, patient_id, appointment_id, facts json, facts_hash, suggestion json?{text, used_facts[{key,value}]}, suggestion_valid?, llm_call_id?, generated_at`. UNIQUE `(sheet_date, patient_id, appointment_id)`
**`ax_reports`**(슬롯 ⑥): `id, kind(daily/weekly/monthly), period_start, period_end, metrics json, body_by_model json, pdf_path?, generated_at`

### 3.5 EOB (슬롯 ②, 조건부)

**`ax_eob_documents`**: `id, kind(monthly/per_patient), form_variant(A/B/C), pdf_path, page_count, degraded_type?, status`
**`ax_eob_lines`**: `id, eob_doc_id, page, line_no, chart_no, service_date, procedure_code, tooth_no, patient_id?, claim_amount, approved_amount, paid_amount, adjust_code?, extracted_by, llm_call_id`
**`ax_eob_reconciliations`**: `id, claim_id?, eob_line_id?, result(confirmed/amount_mismatch/denied/unmatched_claim/unmatched_eob), diff_amount?, reviewed, review_decision?(ok/fix_ledger/appeal), reviewed_at?`

### 3.6 설정·작업·인덱스·고정 쿼리

**`ax_settings(key, value)`**: `active_prompt_version`, `active_models`(A·B의 model_actual), `today_override`, `daily_usd_limit`, `judge_upto`(판정 큐 개방 범위. 기본 = dev 크기(large 150), test 판정 시 풀 전체(large 300))
**`ax_jobs(id, kind, status, step, progress, result_json, error, created_at, updated_at)`**: 30초 넘는 작업(배치 분류·준비표 재생성)

| 테이블 | 인덱스 |
|---|---|
| `emr_appointments` | `(scheduled_at, status)`, `(patient_id, scheduled_at)`, `(chair_no, scheduled_at)`, `(staff_id, scheduled_at)` |
| `emr_treatments` | `(patient_id, procedure_code)`, `(appointment_id)` |
| `emr_payments` | `(patient_id)` |
| `emr_claims` | UNIQUE `(chart_no, service_date, procedure_code, tooth_no)`, `(claim_month)` |
| `emr_messages` | `(patient_id, sent_at)` |
| `ax_calls` | `(judge_status, eval_sort_order)`, `(blind_status, blind_sort_order)`, `(started_at)` |
| `ax_llm_calls` | `(task, created_at)`, `(ref_type, ref_id)` |
| `ax_chunks` | `(source_type, source_id)` |

**판정 큐 "다음 건"**
```sql
SELECT c.* FROM ax_calls c
LEFT JOIN ax_call_disagreements d ON d.call_id = c.id AND d.prompt_version = :active_version
WHERE c.judge_status IN ('pending', 'skipped')
  AND (c.in_eval_pool = 0 OR c.eval_sort_order <= :judge_upto)
  AND (SELECT COUNT(*) FROM ax_call_classifications k
       WHERE k.call_id = c.id AND k.prompt_version = :active_version AND k.status = 'ok') = 2
ORDER BY (c.judge_status = 'skipped') ASC, COALESCE(d.is_core, 0) DESC,
         (c.source != 'generated') DESC, c.eval_sort_order ASC
LIMIT 1;
```
**블라인드 큐**: 현재 `active_version`의 분류 ID 쌍에 대한 `ax_blind_votes`가 `NOT EXISTS`이고 `judge_status='done'`인 건(판정 전에 요약을 보는 앵커링 방지).

## 4. 비정형 생성

### 4.1 통화 전사 (1,000건)

**절차**(건당)
1. 상대 → 목적 → 급함·플래그를 분포에서 추첨 = 골드 라벨
2. 환자/보호자면 EMR에서 환자를 뽑고 fact 추출: 최근 내원·시술, 잔액, 다음 예약, 가족. **통화 시각 이후의 사실은 넣지 않음**(`created_at/performed_at/paid_at ≤ 통화 시각`, status 컬럼은 TODAY 스냅샷이라 읽지 않음)
3. 노이즈 추첨 25%
4. **라벨 → 상황 → 대화** (단일 JSON 호출 `{scenario, transcript}`). 라벨 어휘("예약 변경", "불만")를 대화에 직접 쓰지 않게 지시
5. 전사 규칙: `환자:`/`직원:`, 2~4분 분량, fact 날짜는 절대 날짜(`11월 24일`), 시술은 표준명 또는 `aliases`
6. **건별 검증 루프**: (a) fact 날짜·시술 별칭 substring 존재 (b) 라벨 어휘 직접 등장 없음 (c) 금지 토픽 미매칭 → 실패 시 그 건만 재생성(최대 3회, 그래도 실패면 fact 단순화)
7. 금지 토픽 정규식 정제
8. 저장: 전사 → `ax_calls`, 라벨·fact·노이즈 → `truth_call_labels`
9. 층화 추출 → `in_eval_pool`(300, 순번 dev 1~150 / test 151~300), 그중 150 → `in_blind_pool`. 규칙은 평가 설계 §2.1

**예약류 통화는 예약에서 역산**: 통화가 원인, 예약이 결과. `booked`(created_at) → 예약 잡기, 변경 쌍 → 예약 변경, cancelled → 취소, confirmed(전날) → 확인. 통화 시각 = 그 이벤트 시각. `reschedule_rate 5%`가 변경 통화 150건의 모수.

**목적 분포**
| 목적 | % | 건 | | 목적 | % | 건 |
|---|---|---|---|---|---|---|
| 예약 잡기 | 18 | 180 | | 가격·비용 문의 | 8 | 80 |
| 예약 변경 | 15 | 150 | | 수납·청구 문의 | 4 | 40 |
| 예약 확인 | 8 | 80 | | 서류·기록 요청 | 4 | 40 |
| 예약 취소 | 6 | 60 | | 응급·통증 | 5 | 50 |
| 리콜·안내 전화 | 8 | 80 | | 불만 | 3 | 30 |
| 치료 문의 | 10 | 100 | | 기타 | 3 | 30 |
| 보험 문의 | 6 | 60 | | 모르겠음·애매 | 2 | 20 |

**상대**: 환자 60 / 보호자 15 / 처음 연락 15 / 보험사 3 / 업체 4 / 기타 3

**enum 키**: 목적 `book / reschedule / confirm / cancel / recall / treatment_q / insurance_q / price_q / billing_q / document_req / emergency / complaint / other / unknown` · 상대 `patient / guardian / new_caller / insurer / vendor / other` · 급함 `urgent / normal / low`
**급함**: `emergency` → 전부 `urgent` / 노이즈 '급함 애매한 통증' → 50% `urgent` / 나머지 `normal` 85 · `low` 15
**플래그**: 주 목적이 함의하면 항상 true(`emergency`→emergency, `complaint`→complaint, `price_q`→price_inquiry, `book`→wants_appt) + 복수 주제 통화는 부 주제 플래그 1개. `appt_confirmed`는 `book`·`reschedule`의 70%. `has_todo`는 `confirm`·`recall` 30%, 나머지 80%
**노이즈**(25%): 주제 2개 섞임 10 / 말 끊김·되묻기 8 / 급함 애매한 통증 4 / 보호자가 정보 헷갈림 3
**방향**: outbound ~20%, **리콜에만 두지 않음** — 리콜·안내 8%p 전부 + 예약 확인 4%p + 수납 문의 3%p + 서류 요청 2%p + 치료 문의(경과 확인) 3%p. outbound의 절반은 복합 주제("리콜 안내 + 예약 희망")로 — 단일 구조면 흐름만으로 식별됨.

**복수 주제 통화의 생성 제약**: `primary_purpose` 1개 + 부 주제는 불리언 플래그(가격·불만·응급·예약 희망)로 표현되는 조합만 생성. 플래그로 표현 불가한 조합(예: 예약 변경 + 서류 요청)은 만들지 않음. `accepted_purposes[]`에 주·부 모두 기록 → 채점은 평가 설계 §2.2.

**메타 통화 5,000**: 시각·길이·방향·목적만(전사 없음). 통화 목록 볼륨·시간대 분포용. 분류·판정 대상 아님.

생성 분포는 현실대로(치우침 유지). 층화 추출 규칙·지표는 평가 설계 §2.

### 4.2 RAG 문서 (코어 50 + 방해 27)

**순서**: 문서 생성 → (평가 설계 §3.1) 질문 60개를 사람이 **생성 후** 작성 → 방해 문서에 정답 텍스트가 없는지 verify. 거절형 상식 2개는 **`make data` 전**에 `forbidden_topics.json`에 넣어 정제 필터가 코퍼스에서 제거.

**어휘 일치 방지**(생성 지시): 질문이 물을 핵심 어휘를 문단에 그대로 쓰지 않게 — "노쇼 3회"의 답 문단은 "예약 부도가 세 차례 누적되면"처럼 동의어·우회 표현. 공지는 날짜·버전·수치가 바뀌는 구조로(최신성 평가용), 옛 버전은 공지마다 1개.

**문서 구성**
| 종류 | 수 | 생성 |
|---|---|---|
| 직원 매뉴얼 | 12 | LLM, 정답 문단 심기 |
| FAQ | 10 | LLM |
| 보험 안내 | 6 | LLM + 마스터 값 주입 |
| 원내 공지 | 12 (2~3개 PDF) | 템플릿 + LLM. 날짜·버전 |
| 치과 상식 | 10 | LLM |
| 공지 옛 버전 | 12 (공지마다 1개) | 파생, 다른 수치·이전 날짜 |
| 유사 주제 무답 | 10 | LLM |
| 타 병원·일반 | 5 | LLM |

청킹 300~500자, 겹침 50, 마크다운 헤더 우선. 색인 전체 ≈ 문서 300 + 통화 3,500 + 스레드 3,000 = **~7,000청크**. 금지 토픽 정제는 문서·통화·문자 전부.

### 4.3 EOB PDF (슬롯 ②, 조건부)

- 최근 월(**9~10월**) outcome 추첨과 `truth_claims`(앱 DB 밖)는 정형 5단계에서 이미 생성 — 여기서는 `truth_claims` → 렌더링만. 11월은 `pending`이라 통보서 없음. 확정 월(2~8월)은 "이미 확인된 통보서"로 판독 벤치마크만
- 분포(청구 줄 기준): paid_full 85 / adjusted 8 / denied 4 / missing_in_eob 2 / extra_in_eob 1. 사유 코드 10개 지어냄
- 월별 통보서 **9개**(2~10월, ~300줄, **페이지당 고정 25줄**, 양식 2~6월 A / 7~10월 B) + 건별(`supplementary`를 `(patient, service_date)`로 묶은 수 ≈ 200, 1~4줄, 양식 C)
- Jinja → PDF(WeasyPrint, 실패 시 Playwright). 페이지 분할은 생성기가 슬라이싱 → `(page, line_no)` 정답 확정
- 열화 30%: 200dpi → 블러·회전 ±1.5°·노이즈·JPEG 40~60
- 현실 프레이밍 한 줄: 심사 결과를 EDI 자동 반영하지 않고 통보서(스캔·팩스)로 받아 수기 반영하는 병원 가정

## 5. 생성·정답·검증

### 5.1 파이프라인

```
make data [--scale small|medium|large] [--regen-llm]
 0. 시드 로드      holidays, forbidden_topics                               (정적)
 1. 마스터         clinic, staff, procedures(19)                            (코드)
 2. 환자           역방향 할당: 상태 그룹 → 내원 템플릿. 구환은 창 이전 마지막 위생 내원일 시드
 3. 용량 검사      요일별 Σ소요 vs 원장 32슬롯·위생사 3. 초과면 실패 (60분 시술 비중 조정)
 4. 예약 + 상태    한 루프: 배치(60분↑ 비피크 우선) → 노쇼 6%·당일취소 4%·변경 5%(쌍)·confirmed 85%
 5. 진료·수납·청구 completed → treatments(치아당) → payments(미수 15%, 정산 80%) → insurance_usage → claims(확정 월 status 추첨, 11월 pending)
                   → supplementary 10% 배정 → 9~10월 outcome 추첨 → truth_claims   (EOB 청구 측. 나중에 넣으면 정형 재생성 → LLM 캐시 전량 미스)
 6. 통화 메타      5,000 + call_uid(시드 셔플)                              (코드)
 7. 통화 전사      1,000 (§4.1, 예약류는 4단계 이벤트에서 역산)              (LLM, 캐시)
 8. 문자           out 템플릿(doc_ready는 통화 이후) → in 20% LLM → 정제
 9. RAG 문서       코어 50 → 방해 27 → 정제. (이후 사람이 질문 60 작성 — 평가 설계 §3.1)  (LLM, 캐시)
10. 적재·색인·검증 app.sqlite → make index(FTS5 + Chroma) → make verify
━━ 조건부 ━━
11. EOB PDF        truth_claims → PDF → 열화 → truth_eob_lines   (청구·outcome은 5단계)
```

- **단계별 독립 시드**: 각 단계는 `seed`와 단계 이름으로 파생한 자기 난수 생성기를 쓴다(예: `hash(seed, "patients")`). 단계를 추가·삭제해도 다른 단계의 결과는 바뀌지 않는다
- **LLM 캐시 키** = `(call_uid, seed, prompt_template_hash, used_fact_hash, attempt)` — attempt가 없으면 검증 실패 후 재시도에 같은 실패작이 돌아옴. 정형(1~6) 완성·verify 후 LLM(7~9) 1회
- 생성기 LLM 호출도 게이트웨이 경유 → `llm_calls_gen.jsonl`, 상한 `DAILY_GEN_USD_LIMIT`(런타임 상한과 분리, 금액은 [[치과 AX - 기능 정의]] §7). 전사는 프론티어 실시간(`Semaphore(20)` + 백오프), 1,200~1,500 호출. 문자 답장은 경량

### 5.2 정답 출력 `ground_truth/`

테이블 정의는 **평가 설계 §6**에만. 생성기가 채우는 것: `truth_call_labels`(라벨·fact·노이즈·split), `truth_doc_meta`(방해 여부·심은 QA·supersedes), `truth_claims`(5단계), 조건부 `truth_eob_lines`. 같은 내용 JSON(`call_labels.json`, `doc_meta.json`)도 출력. 정답 격리 원칙은 평가 설계 §1.

### 5.3 `make verify`

**필수**(실패 시 중단)
| 영역 | 검사 |
|---|---|
| 참조 | FK 전부 유효. 다형 참조(`ax_llm_calls.ref_*`, `ax_chunks.source_*`)는 타입별 조인 |
| 기준일 | TODAY 당일은 `scheduled/cancelled`만; 12/2~15는 예약만(treatments·payments·claims 없음); `started_at`·`sent_at`·`submitted_at`·`effective_date` ≤ TODAY(11월 claims `pending`) |
| 용량·충돌 | 요일별 원장 슬롯 ≤ 32(토 16); 같은 체어·같은 시간 중복 없음; 동시 treatment ≤ 2, hygiene + treatment ≤ 3; 같은 환자 같은 날 중복 없음; 영업시간·휴진일 밖 없음 |
| 예약 | 전 예약에 `planned_procedure_code`; 변경 쌍 ≥ 5% × 예약 수; 예약류 통화 시각 == 대응 이벤트 시각(확인 통화는 예약 전날 날짜만) |
| 시술·청구 | `performed_at` == 예약 일시; `service_date` == 진료일; 비급여 시술은 claims에 없음; 스케일링 급여 19세↑ 연 1회; `tooth_no` NULL 없음; 대조 키 UNIQUE; 9~10월 청구는 `submitted`·결과(`paid_amount`·`adjust_code`) 없음 |
| 수납 | 환자별 시점 누적 잔액 ≥ 0; `visit` 행은 completed 예약에만 |
| RAG | 금지 토픽 코퍼스 전체 미포함; 통화·스레드 청크에 prefix; 옛 공지의 `effective_date` < 최신. **`rag_qa.json`이 있으면 추가로**: 정답 청크가 `ax_chunks`에 존재, 방해 문서에 정답 텍스트 미포함, 데이터형 답이 쿼리로 재계산되고 ≥ 2건, QA 환자 이름 중복 0, dev/test 유형 비율 동일 |
| 누출 | `ax_calls`에 라벨·노이즈·카테고리 컬럼 없음; **`call_uid`에 목적 enum 문자열 없음**; 풀 마킹에서 카테고리 추론 불가; 분류기 입력 덤프에 전사·방향 외 메타 없음 |
| 결정성 | 같은 시드 2회 → 테이블 canonical 덤프 해시 동일(`indexed_at` 제외, 벡터 디렉토리 제외) |
| 가상 PII | 전화 `010-0000-xxxx`만 |

**경고**(리포트만)
| 영역 | 검사 |
|---|---|
| 분포 | 연령·목적·노이즈 ±2%p; 하루 내원 ±30%; 피크 패턴; 60분 시술의 피크 비중 < 40% |
| 치아 | FDI 유효 또는 `'00'`; 치아 단위는 `'00'` 금지, 전악은 `'00'`만; 유치는 소아만; 연령 제한 시술 |
| 문자 | out은 트리거 이후, in은 out 이후; 건수 ±10% |
| 통화 | 라벨 enum 유효; 화자 형식; 길이 2~4분; fact 날짜·시술 별칭 substring 존재; 라벨 어휘 직접 등장 < 20%; 복수 주제는 `accepted_purposes` ≥ 2 |
| RAG | (`rag_qa.json` 있을 때) 거절형 답이 어느 문서에도 없음(키워드 + LLM 1회); 질문 어휘가 정답 문단에 그대로 있는 비율 < 30% |

verify를 생성기보다 **먼저** 작성(TDD).

### 5.4 기술 선택

| 항목 | 선택 |
|---|---|
| 언어·라이브러리 | Python. Faker(ko_KR), numpy 시드. **lock 파일 커밋**(Faker 버전이 이름 결정성에 영향) |
| DB | SQLite 1파일 + **Chroma PersistentClient**. `VectorStore` 인터페이스 뒤, 두 번째 구현 안 함 |
| FTS | `unicode61` + 조사 제거 OR prefix. 복합어는 적재 시 공백 정규화 |
| 리랭커·임베딩 | **비교 후 확정**: `text-embedding-3-small` vs `bge-m3`, 로컬 `bge-reranker-v2-m3` vs Cohere. dev 질문 10개로 비교 |
| LLM | 게이트웨이 모듈 1개, 내부 LiteLLM 라이브러리. 프로바이더 조합 확정 + 구조화 출력·도구 호출 테스트 |
| 테스트 fixture | **`make fixture`**: small 데이터 + fake 게이트웨이로 분류 300×2 적재 + 해시 기반 결정적 fake 임베딩으로 Chroma까지 → `tests/fixtures/` 커밋. 테스트는 복사본 사용. CI 키 없이 판정 큐·검색이 동작 |
| 사람 데이터 보존 | `export-human/import-human`: 판정은 `hash(transcript)`로만 복원 — 미스 시 버리고 "복원 실패 N건" 리포트(전사가 바뀐 건에 `call_uid`로 붙이면 다른 대화에 라벨이 붙음). 투표는 `(hash, left_model, left_version, right_model, right_version, choice)` |
| PDF(조건부) | Jinja2 → WeasyPrint(Nanum 폰트), 열화 Pillow |
| 시간 | DB naive KST, API `+09:00`. `emr_*`는 TODAY, `ax_*` 활동은 실제 시계 |
| 실행 | `make data / index / verify / fixture / save-demo / reset-demo` |

## 6. 미결

- [ ] 리랭커·임베딩 모델
- [ ] 프로바이더 조합 A/B
- [ ] 거절형 금지 토픽 2개 선정 (`make data` 전)
- [ ] 홈 화면·네비 (와이어프레임에서)
