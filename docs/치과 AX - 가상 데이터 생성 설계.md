---
title: 치과 AX - 가상 데이터 생성 설계
related:
  - "[[치과 AX - 기능 정의]]"
  - "[[치과 AX - 평가 설계]]"
  - "[[치과 AX - 앱 설계]]"
created: 2026-10-16
updated: 2026-10-20
status: v2.4 (2026-10-20. '데이터 설계'에서 이름 변경, 앱 쪽 `ax_*`·기술 선택은 [[치과 AX - 앱 설계]]로 분리. §2.6 정형 생성 규칙 초안 — 예산 맞춤 순서·TODAY 스냅샷(⑧)·O 교정 고정·예정 신환, 내원 = completed 정의, 환자 2,000, 규모 프리셋(시작일), 청구 심사 결과·보완 청구·제출일 규칙. 이전 판은 프로젝트 밖에 별도 보관)
tags:
  - 치과AX
  - 데이터
---

> **어떤 데이터를 어떻게 만드나.** 생성기 코드가 읽는 문서. 앱이 그 데이터를 어떻게 쓰나(테이블·도구·쿼리)는 [[치과 AX - 앱 설계]], 뭘 측정하나(지표·채점·정답 저장소 정의)는 [[치과 AX - 평가 설계]], 구현 디테일(WAL·워커·헤더·에러 처리)은 `CLAUDE.md`.
> **범위 밖(프로젝트 후 확장)**: 연쇄 치료계획, 리콜 큐, 임플란트 평생 한도, 다회차 시술의 회차 추적. 환자는 전부 **단발 내원** 모델.

## 0. 전제

| 항목 | 결정 |
|---|---|
| 규모 | 환자 2,000 / 2026-02-01~12-15 / 평일 26명·토 13명 / 통화 전사 1,000 + 메타 5,000 / 문서 50 + 방해 27 / 문자 ~1.5만 / (조건부) EOB 월별 9 + 건별 파생 (= large) |
| 축소 규모 | `--scale small\|medium`은 **하루 밀도(평일 26명) 유지**, 끝(12-15) 고정·시작일만 늦춤(small 08-01 / medium 05-01) → TODAY 기준 구간(확정월·제출월 9~10·전월 11·미래 12/2~15)이 모두 남음. 환자 수·통화 전사·메타·판정/블라인드 풀은 기간 비율(달력 일수)로 축소·반올림. **문서는 모든 규모에서 77개(core 50 + distractor 27) 고정**. 판정 풀 층화는 고정 숫자가 아니라 비율 규칙(희귀 60% 상한 + 나머지 비례, 평가 설계 §2.1)으로 계산 — 문서의 숫자는 large 기준 결과값 |
| 평가 규모 | **사람 판정·RAG 질문 작성·`eval.py` 보고·`make gate`는 모두 large 하나에서만**. small·medium은 개발·테스트용 |
| 기준일 | `TODAY = 2026-12-01 (화)` 고정. 앱은 설정값을 읽음(환경변수로 변경 = 타임머신) |
| 보험 | 건강보험 단일. 월 1회 일괄 청구 + 소량 보완 청구. 실손은 환자 플래그 + 통화 소재만 |
| 스키마(테이블 접두어) | 접두어는 **현실에서 어느 시스템의 데이터인가**로 붙인다. `emr_*` = 미니 EMR, 병원 원본 장부(생성기가 만들고 앱은 읽기 전용 — AI는 원본에 쓰지 않음) / `ax_*` = AX 시스템이 쌓는 데이터(앱이 읽고 씀) / `truth_*` = 정답, 별도 DB `ground_truth/truth.sqlite`(앱은 모름). 헷갈리는 점: `ax_calls`·`ax_documents`는 생성기가 채우지만 현실에선 AX 도입 후 쌓이는 데이터라 `ax_`. 슬롯 추가 데이터도 같은 기준(통장·카드 거래 = 병원 원본 → `emr_`) |
| 재현성 | 시드 고정, `make data` 한 번에 전체. LLM 생성물은 파일 캐시·커밋, `--regen-llm`으로만 재생성 |
| 단순화 | 수가·심사는 흉내만. 급여 청구액 = fee × 0.7. 소아 급여 특례 없음. '구강검진'은 진찰·파노라마 대체 가상 코드 |
| 생성 순서 | 사람 판정 시작 후 데이터셋 교체 금지(판정이 call_id FK). 정형 로직을 고치면 LLM 캐시 전량 미스 → 정형 완성·verify **뒤에** LLM 1회 |

```yaml
seed: 20261201
today: 2026-12-01
range: { start: 2026-02-01, end: 2026-12-15 }
patients: 2000
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
| 13 | implant | 임플란트 1차 | conditional | 1,300,000 | 90 | senior, 치아. 65세↑ 급여·미만 비급여 (한도 추적은 범위 밖 → 생성 시 환자당 ≤ 2개로만 제한) |
| 14 | implant | 임플란트 2차 | **no** | 0 | 60 | 청구 대상 아님 |
| 15 | ortho | 교정 상담 | no | 0 | 30 | |
| 16 | ortho | 교정 월 체크 | no | 80,000 | 30 | |
| 17 | cosmetic | 미백 | no | 300,000 | 60 | 가격 문의 소재 |
| 18 | perio | 잇몸치료 | yes | 35,000 | 30 | |
| 19 | consult | 상담 | no | 0 | 30 | 상담실장, 체어 불필요 |

**치아 행 규칙**: 치아 단위 시술은 같은 날 여러 치아면 **치아당 `emr_treatments` 1행, 청구 1줄**. 치아 단위가 아닌 시술(`is_tooth_level=0` — 전악 1·2·3·18과 10·14·15·16·17·19)은 전부 `tooth_no='00'`(센티널, NULL 아님) 1행, 같은 날 같은 시술 2회 금지. 내원당 급여 줄 1~3개.

### 2.2 환자

**`emr_families`**: `id, surname`
**`emr_patients`**: `id, chart_no('C-00001'), name, birth_date, sex, phone('010-0000-xxxx'), family_id?, guardian_patient_id?(소아), registered_at, recall_cycle_months(기본 6), has_private_insurance, note?`

**내원** = `completed` 예약. 노쇼·취소·아직 오지 않은 `scheduled`(TODAY 당일 포함)는 내원이 아니다. 아래 파생값과 활동·휴면은 모두 이 기준.

파생(쿼리): 마지막 내원일, 신환(첫 내원 30일 이내), 휴면(6개월↑ 미내원), 가족 리콜 지남(가족 구성원 `MAX(내원)+recall_cycle < 기준일`)

| 축 | 분포 (large 2,000명) |
|---|---|
| 연령 | 소아(0–12) 15% / 청소년(13–18) 8% / 성인(19–64) 62% / 노인(65↑) 15% |
| 가족 | 35%가 2–4명 묶음. 소아는 100% 같은 가족 성인이 보호자 |
| 활동 | 활동(TODAY 전 6개월 = 06-01~11-30에 내원 ≥ 1회) 70% / 휴면 30% — 예정 신환(§2.6 ②) 제외 기준. 신환은 월 내원의 12%(§1) — 최근 3개월 신환 수는 그 결과값(large ≈ 170명) |
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
| claim_month('2026-07'), batch_type | `claim_month` = 진료월(보완 청구도 동일). `monthly`(기본)/`supplementary`(청구 줄 10% 무작위, 정형 5단계에서 배정 — 월 일괄 청구에서 빠져 나중에 보완 청구한 건) |
| service_date, procedure_code, claim_amount(=insured_amount), submitted_at | monthly = 익월 **첫 진료일**(목·일·공휴일 제외) 10:00 / supplementary = monthly 제출일 + 7~20일(휴진일이면 다음 진료일) / 11월(`pending`)은 NULL |
| status | `pending`(11월, 미제출) / `submitted`(9~10월) / `paid·adjusted·denied`(확정 월 2~8월, 생성기 추첨 — 병원이 이미 아는 과거 결과) |
| paid_amount?, adjust_code? | 확정 월만 |

**UNIQUE `(chart_no, service_date, procedure_code, tooth_no)`** — EOB 대조 키. 최근 월(9~10월)의 심사 결과는 이 테이블에 없고 `truth_claims`에만(정형 5단계에서 생성).

**심사 결과 추첨**(정형 5단계. 확정 월 `emr_claims`와 9~10월 `truth_claims`가 같은 규칙)
| 대상 | 분포(청구 줄 기준) |
|---|---|
| 확정 월 2~8월 → `emr_claims.status` | paid 88 / adjusted 8 / denied 4 (`missing`·`extra`는 통보서 대조 결과라 장부에 없음) |
| 9~10월 → `truth_claims.outcome` | paid_full 85 / adjusted 8 / denied 4 / missing_in_eob 2 / extra_in_eob 1 |

- 금액: paid → `paid_amount = claim_amount` / adjusted → `claim_amount × U(0.5, 0.9)`, 10원 단위 내림 + `adjust_code` / denied → `paid_amount = 0` + `adjust_code`. missing_in_eob은 `paid_amount`·`adjust_code` NULL
- 사유 코드 10개는 `generator/master.py`의 `ADJUST_CODES`
- `extra_in_eob` 줄(장부에 없음, `claim_id` NULL): 9~10월 실제 내원(환자·진료일)에 그 내원에 없는 급여 시술 또는 다른 치아를 붙여 만든다 → 대조 키가 `emr_claims`와 겹치지 않음. `truth_claims` 컬럼은 평가 설계 §6

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

`out`은 템플릿(LLM 없음). 색인 방식은 [[치과 AX - 앱 설계]] §3.

### 2.6 정형 생성 규칙 (2~5단계) — 초안

> 역방향 할당: **슬롯 예산 → 환자 그룹 → 에피소드 템플릿 → 날짜·시각 배치**. 분포를 사후 검사가 아니라 정의에서 보장한다. 숫자는 large 기준(진료일 211 = 평일 170 + 토 41).

**① 슬롯 예산** — `visits_per_day` = 그날 **취소되지 않은 예약** 수(completed·noshow·scheduled). 취소(당일 취소·변경 원래 예약)는 그 위에 얹히는 행.
- 진료일마다 평일 26 / 토 13 → large 4,953건(small 2,145)
- 날짜별 유형 배분: treatment 70 / hygiene 25 / consult 5 (최대 나머지법) → large ≈ 3,467 / 1,238 / 248

**② 환자 그룹** (TODAY 기준 상태. large 2,000명)
| 그룹 | 수 | 등록 | 창 안 내원 |
|---|---|---|---|
| 창 내 신환 | 월별 내원의 12% (≈ 594) | `registered_at` = 첫 예약 `created_at`의 날짜(전화 예약 때 차트 생성. 2월 초 신환은 1월일 수 있음) | 첫 내원부터 |
| └ 예정 신환 | 그중 첫 내원 ≥ TODAY (≈ 31 = TODAY 이후 내원 260 × 12%) | 같음 | 미래 예약 1건만 |
| 구환 | 나머지 (≈ 1,406) | `range.start` 이전(2015~2026-01) | 창 이전 마지막 위생 내원일을 시드 → 첫 리콜 시점 |
| 활동·휴면 | 예정 신환을 뺀 ≈ 1,969명 기준 | | |
| └ 휴면 | 30% (≈ 591) | (a) 신환·구환 / (b) 구환만 | (a) 06-01 이전에만 내원 / (b) 창 안 내원 0 |
| └ 활동 | 70% (≈ 1,378) | | 06-01~11-30에 내원 ≥ 1회 |

- 신환 수는 **월 내원의 12%가 기준**(§1). 최근 3개월(9~11월) 신환 ≈ 170명은 그 결과값
- 휴면 (a) 비율 = 50% × (창 안 06-01 이전 일수 / 120). large 50%(≈ 295) / medium 5월만 ≈ 13% / small 0% → 나머지는 (b). 06-01 이전 구간이 짧은 규모에서 5월 슬롯이 휴면 환자로 넘치지 않게 함
- 활동 환자당 평균 ≈ 3.2회 / 10.5개월(large). small은 ≈ 3.6회 / 4.5개월로 더 촘촘하지만 개발용이라 허용

**③ 에피소드 템플릿**
| 템플릿 | 내원 | 간격 | 주 대상 |
|---|---|---|---|
| H 정기 위생 | hygiene 1 | `recall_cycle_months` ±1개월마다 반복 | 전 연령 |
| T1 단발 치료 | treatment 1 | — | 전 연령, 응급·발치 |
| Tn 치료 코스 | treatment 2~4 | 7~14일 | 성인·노인 |
| C 상담 코스 | consult 1 → treatment 2~5 | 7~21일 | 보철·임플란트·미백 |
| O 교정 | 교정 월 체크 매월 (`planned` = 교정 월 체크 **고정**, ④ 추첨 안 함) | 28~35일 | 교정 환자: **전체 환자 기준** 청소년 25% + 성인 2% (≈ 40 + 25 = 65명), 전원 활동 그룹에 배정 |

- O 교정 환자의 60%는 창 시작 전부터 진행 중(창 시작부터 매월), 40%는 창 안에서 시작 — 교정 상담(consult) 1회 → 7~21일 뒤부터 매월. 교정 기간(18~30개월)이 창보다 길어 창 끝까지 계속 → large ≈ 455건(65명 × 평균 7회)
- 단발 내원 모델이라 코스 안의 시술은 회차 추적 없이 내원마다 ④에서 독립 추첨. 예외: **임플란트 2차는 같은 환자의 1차가 8주 이상 앞에 있을 때만**(없으면 재추첨), 1차는 환자당 ≤ 2개

**예산 맞춤** (유형별 합계 == ①의 예산. 0단계에서 그룹을 정한 뒤 1~6을 순서대로 채우고, 앞 단계가 쓴 만큼 뒤 단계 예산이 줄어듦. 각 단계의 대상 집합을 벗어나 배정하지 않는다)

| 단계 | 대상 | 내용 |
|---|---|---|
| 0. 그룹 배정 | 전체 | 환자마다 ②의 그룹(신환·예정 신환·구환 / 활동·휴면 (a)·(b))과 교정 여부를 먼저 정한다 |
| 1. 신환 첫 내원 | 신환(예정 신환 포함) | 월별 내원의 12%만큼 그 달 진료일에 첫 내원을 배정. 유형은 그날의 유형 비율대로, 휴면 (a) 신환은 05-31 이전, 예정 신환은 TODAY 이후. 이 내원이 그 신환의 첫 에피소드 시작 |
| 2. 활동 커버리지 | 활동 | 06-01~11-30에 1단계 내원이 없는 활동 환자마다 그 구간 내원 1건(H 회차가 그 구간에 있으면 그것, 없으면 T1). **이 내원은 노쇼·당일 취소·변경 추첨에서 제외**(completed 보장) |
| 3. O 교정 | 교정 환자(활동) | 고정분. treatment 예산에서, 교정 시작 상담은 consult 예산에서 차감 |
| 4. H 정기 위생 | 활동 + 휴면 (a) | 체인: 구환은 시드된 창 이전 마지막 위생일 + 주기, 신환은 첫 내원 + 주기부터. **휴면 (a)는 05-31 이전 회차만**. 합계가 hygiene 예산보다 많으면 2단계 고정분은 남기고 06-01 이후 내원이 이미 있는 환자의 회차부터 무작위로 지움. 모자라면 활동 환자에게 단발 H(시술은 ④ 규칙대로) |
| 5. 치료 에피소드 | 활동(가중 1) + 휴면 (a)(가중 0.5, 05-31 이전만) | 남은 treatment 예산을 다 쓸 때까지 환자 추첨 → 에피소드 추첨(성인·노인 T1 50 / Tn 35 / C 15, 소아·청소년 T1 70 / Tn 30). 마지막 에피소드는 남은 예산만큼 자름 |
| 6. consult | 활동 | 5단계 C의 상담으로 쓰고, 남으면 단발 상담으로 채움. C가 consult 예산을 다 쓰면 이후 C는 Tn으로 바꿔 추첨 |

휴면 (b)와 예정 신환(1단계의 미래 예약 1건 외)은 어느 단계에도 들지 않는다.

**④ 유형별 예정 시술 가중** (`planned_procedure_code`. %)
| 유형 | 소아 0–12 | 청소년 13–18 | 성인 19–64 | 노인 65↑ |
|---|---|---|---|---|
| hygiene | 구강검진 40 · 불소도포 60 | 구강검진 60 · 스케일링(비급여) 40 | 그해 미사용이면 스케일링, 사용했으면 구강검진 | 성인과 같음 |
| treatment (O 교정 제외) | 아말감/GI 35 · 실란트 30 · 레진 20 · 단순발치 15 | 레진 60 · 아말감/GI 20 · 사랑니 발치 20 | 레진 28 · 잇몸치료 17 · 아말감/GI 11 · 신경치료 9 · 단순발치 8 · 크라운 7 · 사랑니 발치 6 · 인레이 5 · 임플란트 1차 3 · 임플란트 2차 3 · 미백 2 · 브릿지 1 | 잇몸치료 22 · 단순발치 14 · 임플란트 1차 14 · 레진 12 · 아말감/GI 10 · 임플란트 2차 8 · 신경치료 8 · 크라운 8 · 브릿지 4 |
| consult | 상담 100 (교정 시작 상담만 교정 상담 — ③ O) | 같음 | 같음 | 같음 |

- treatment 평균 소요 ≈ 39분: O 교정 ≈ 455건 × 30분 + 나머지 ≈ 3,012건 × 40.25분(소아 30 · 청소년 36 · 성인 41.7 · 노인 46.8을 연령 비중으로 가중) → 평일 원장 ≈ 23.6슬롯 / 32 (§1의 40분 가정 이내). 3단계 용량 검사는 안전장치로 유지
- **임플란트 1차(`conditional`)**: 시술일 기준 65세↑ 급여(본인부담 0.3, 청구 대상), 65세 미만 비급여(청구 없음)
- 스케일링: 19세 미만은 비급여(청구 없음, `insurance_usage` 미반영). 성인은 그해 이미 썼으면 hygiene이 구강검진으로 바뀌므로 2회째가 생기지 않음

**⑤ 날짜·시각 배치** (4단계)
- 날짜: 에피소드 간격으로 원하는 날짜 → 그 유형의 남은 예산이 있는 가장 가까운 진료일(±3일, 같은 환자 같은 날 금지). 옮길 때 대상 구간(휴면 (a)는 05-31 이전, 2단계 커버리지는 06-01~11-30)을 벗어나지 않음
- 시각: 60분↑ 시술을 비피크(09:30–10, 11–13, 14–15, 17–18:30)에 먼저 → 나머지를 피크 가중(10–11·15–17 ×2)으로. 체어·원장 2·체어 작업 3 한도를 넘으면 다음 슬롯 → 다음 날
- 담당: treatment = 원장, hygiene = 위생사 3명 순환, consult = 상담실장(`chair_no` NULL)

**⑥ `created_at` 리드타임**
| 경우 | `created_at` |
|---|---|
| 코스·교정의 다음 내원 | 직전 내원 종료 시각(데스크에서 예약 — 통화 없음) |
| 그 외 hygiene | 7~30일 전 |
| 그 외 treatment·consult | 1~14일 전 |
| 신환 첫 내원 | 0~7일 전 |

시각은 영업시간 안. 이 중 "데스크에서 예약"이 아닌 것만 예약 잡기 통화의 역산 후보(§4.1). TODAY 이후 예약의 예외는 ⑧.

**⑦ 내원당 시술 행** (5단계, completed만)
- 치아 단위 시술: 치아 1개 70 / 2개 22 / 3개 8 → 치아당 1행. 전악 시술은 `'00'` 1행
- 성인 스케일링 내원의 30%에 구강검진 추가(둘 다 `'00'`)
- 급여 줄은 내원당 ≤ 3. 예정 시술 = 실제 시술(변경 없음)
- 치아 번호(FDI): 영구치 11–18·21–28·31–38·41–48, 유치 51–55·61–65·71–75·81–85(소아만). 사랑니 발치는 x8만, 실란트는 6·7번 영구 대구치

**⑧ TODAY 스냅샷 규칙** — 데이터는 TODAY(12-01) 시점의 장부다. TODAY 이후(12-02~15)에는 예약만 있고, 아직 일어나지 않은 일의 흔적은 없다
- `created_at`·`cancelled_at`·`registered_at` ≤ TODAY. TODAY 이후 예약에서 리드타임(⑥)으로 뽑은 `created_at`이 TODAY를 넘으면 리드 범위를 **[예약일 − TODAY, 예약일 − TODAY + (원래 범위 폭)]일 전**으로 옮겨 다시 뽑음(예: 12-15 신환 예약 0~7일 → 14~21일 전). 시각은 영업시간 안
- 코스·교정의 다음 내원은 **직전 내원이 TODAY 이전일 때만** 존재(직전 내원이 TODAY 이후면 아직 예약 전 → 만들지 않고, 그 예산은 다른 환자의 T1으로)
- TODAY 이상 날짜의 예약은 `scheduled` 또는 변경 쌍의 원래 예약(`cancelled`, 변경 통화 시각 ≤ TODAY). **당일 취소는 TODAY 이전 날짜에만**
- `confirmed`(전날 리마인드 응답)는 예약일 ≤ TODAY+1만. 그 이후 예약은 0
- 예정 신환(②)은 미래 예약 1건만(코스 다음 내원은 위 규칙상 생기지 않음)

## 3. 생성기가 적재하는 `ax_*`

> 생성기는 `ax_calls`(전사 1,000 + 메타 5,000)와 `ax_documents`만 적재. 생성기와 앱 사이의 계약이라 여기 정의한다. 나머지 `ax_*`(분류·판정·청크·검색·준비표 등)와 `make index`·`make fixture`는 [[치과 AX - 앱 설계]].

### 3.1 통화

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

골드 라벨·노이즈 유형은 여기 없음 → `truth_call_labels`. 분류기 입력 규칙은 [[치과 AX - 앱 설계]] §1.

### 3.2 문서

**`ax_documents`**: `id, type(manual/faq/insurance_guide/notice/education/external), title, version, effective_date?, path, body`
(방해 여부·심은 QA는 `truth_doc_meta`에만)

### 3.3 인덱스 (`emr_*`·`ax_calls`)

| 테이블 | 인덱스 |
|---|---|
| `emr_appointments` | `(scheduled_at, status)`, `(patient_id, scheduled_at)`, `(chair_no, scheduled_at)`, `(staff_id, scheduled_at)` |
| `emr_treatments` | `(patient_id, procedure_code)`, `(appointment_id)` |
| `emr_payments` | `(patient_id)` |
| `emr_claims` | UNIQUE `(chart_no, service_date, procedure_code, tooth_no)`, `(claim_month)` |
| `emr_messages` | `(patient_id, sent_at)` |
| `ax_calls` | `(judge_status, eval_sort_order)`, `(blind_status, blind_sort_order)`, `(started_at)` |

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

**메타 통화 5,000**: 시각·길이·방향만(전사 없음). 통화 목록 볼륨·시간대 분포용. 분류·판정 대상 아님. 목적은 생성 시 추첨해 길이·방향·시간대를 정하는 데만 쓰고 **저장하지 않음**(`ax_calls`에 두면 누출, `truth_call_labels`는 전사 1,000건 전용).

**`call_uid`는 정형 6단계에서 6,000건 전체**(메타 5,000 + 전사 슬롯 1,000)에 시드 셔플로 부여 — 7단계 LLM 캐시 키에 들어가므로 전사 전에 확정. 전사 슬롯은 6단계에서 `has_transcript=0` 빈 행으로 만들고 7단계가 채움.

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

청킹·색인은 [[치과 AX - 앱 설계]] §3. 금지 토픽 정제는 문서·통화·문자 전부.

### 4.3 EOB PDF (슬롯 ②, 조건부)

- 최근 월(**9~10월**) outcome 추첨과 `truth_claims`(앱 DB 밖)는 정형 5단계에서 이미 생성 — 여기서는 `truth_claims` → 렌더링만. 11월은 `pending`이라 통보서 없음. 확정 월(2~8월)은 "이미 확인된 통보서"로 판독 벤치마크만
- outcome 분포·금액·사유 코드·`extra_in_eob` 생성 규칙은 §2.4 "심사 결과 추첨"(정형 5단계)
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
 2. 환자           역방향 할당(§2.6): 슬롯 예산 → 상태 그룹 → 에피소드 템플릿. 구환은 창 이전 마지막 위생 내원일 시드
 3. 용량 검사      요일별 Σ소요 vs 원장 32슬롯·위생사 3. 초과면 실패 (60분 시술 비중 조정)
 4. 예약 + 상태    한 루프: 배치(§2.6 ⑤⑥, 60분↑ 비피크 우선) → 노쇼 6%·당일취소 4%·변경 5%(쌍)·confirmed 85%
 5. 진료·수납·청구 completed → treatments(치아당) → payments(미수 15%, 정산 80%) → insurance_usage → claims(확정 월 status 추첨, 11월 pending)
                   → supplementary 10% 배정 → 9~10월 outcome 추첨 → truth_claims   (EOB 청구 측. 나중에 넣으면 정형 재생성 → LLM 캐시 전량 미스)
 6. 통화 메타      5,000 + 전사 슬롯 1,000 빈 행, call_uid 6,000(시드 셔플)  (코드)
 7. 통화 전사      1,000 (§4.1, 예약류는 4단계 이벤트에서 역산)              (LLM, 캐시)
 8. 문자           out 템플릿(doc_ready는 통화 이후) → in 20% LLM → 정제
 9. RAG 문서       코어 50 → 방해 27 → 정제. (이후 사람이 질문 60 작성 — 평가 설계 §3.1)  (LLM, 캐시)
10. 적재·색인·검증 app.sqlite → make index(FTS5 + Chroma, 앱 설계 §3) → make verify
━━ 조건부 ━━
11. EOB PDF        truth_claims → PDF → 열화 → truth_eob_lines   (청구·outcome은 5단계)
```

- **단계별 독립 시드**: 각 단계는 `seed`와 단계 이름으로 파생한 자기 난수 생성기를 쓴다(예: `hash(seed, "patients")`). 단계를 추가·삭제해도 다른 단계의 결과는 바뀌지 않는다
- **LLM 캐시 키** = `(call_uid, seed, prompt_template_hash, used_fact_hash, attempt)` — attempt가 없으면 검증 실패 후 재시도에 같은 실패작이 돌아옴. 정형(1~6) 완성·verify 후 LLM(7~9) 1회
- 생성기 LLM 호출도 게이트웨이 경유 → `llm_calls_gen.jsonl`, 상한 `DAILY_GEN_USD_LIMIT`(런타임 상한과 분리, 금액은 [[치과 AX - 기능 정의]] §7). 전사는 프론티어 실시간(`Semaphore(20)` + 백오프), 1,200~1,500 호출. 문자 답장은 경량

### 5.2 정답 출력 `ground_truth/`

테이블 정의는 **평가 설계 §6**에만. DDL 파일은 `ground_truth/schema.sql`(커밋, 테이블은 그 단계를 구현할 때 추가 — `shared/`는 앱이 읽는 위치라 두지 않음). 생성기가 채우는 것: `truth_gen_meta`(seed·scale·생성 기간 — verify가 읽음), `truth_call_labels`(라벨·fact·노이즈·split), `truth_doc_meta`(방해 여부·심은 QA·supersedes), `truth_claims`(5단계), 조건부 `truth_eob_lines`. 같은 내용 JSON(`call_labels.json`, `doc_meta.json`)도 출력. 정답 격리 원칙은 평가 설계 §1.

### 5.3 `make verify`

**필수**(실패 시 중단)
| 영역 | 검사 |
|---|---|
| 참조 | FK 전부 유효. 다형 참조(`ax_llm_calls.ref_*`, `ax_chunks.source_*`)는 타입별 조인 |
| 기준일 | TODAY 당일은 `scheduled/cancelled`만; 12/2~15는 예약만(treatments·payments·claims 없음); `started_at`·`sent_at`·`submitted_at`·`effective_date`·`created_at`·`cancelled_at`·`registered_at`·`birth_date` ≤ TODAY(11월 claims `pending`); `confirmed`는 예약일 ≤ TODAY+1만(§2.6 ⑧). 생성 기간은 `truth_gen_meta`에서 읽음(없으면 실패) |
| 용량·충돌 | 예약 시작 :00·:30, 소요 30분 배수; 요일별 원장 슬롯 ≤ 32(토 16); 같은 체어·같은 시간 중복 없음; 동시 treatment ≤ 2, hygiene + treatment ≤ 3; 같은 환자 같은 날 중복 없음; 영업시간·휴진일 밖 없음 |
| 예약 | 전 예약에 `planned_procedure_code`; 변경 쌍 ≥ 5% × 예약 수; `cancelled_at`은 cancelled에만(cancelled는 필수); 취소는 변경 쌍의 원래 예약 또는 TODAY 이전 당일 취소뿐; 예정 신환은 비취소 예약 1건; 예약류 통화 시각 == 대응 이벤트 시각(확인 통화는 예약 전날 날짜만) |
| 시술·청구 | 진료 기록은 completed 예약에만, completed 예약은 ≥ 1행; `performed_at` == 예약 일시; `service_date` == 진료일; 비급여 시술은 claims에 없음; 스케일링 급여 19세↑ 연 1회; `tooth_no` NULL 없음; 대조 키 UNIQUE; 9~10월 청구는 `submitted`·결과(`paid_amount`·`adjust_code`) 없음; `truth_claims`의 `extra_in_eob` 대조 키가 `emr_claims`와 겹치지 않음; 9~10월 `emr_claims` ↔ `truth_claims` 1:1(`claim_id`·대조 키·금액 일치), `extra_in_eob`도 9~10월 진료일 |
| 수납 | 환자별 시점 누적 잔액 ≥ 0; `visit` 행은 completed 예약에만 |
| RAG | 금지 토픽 코퍼스 전체 미포함; 통화·스레드 청크에 prefix; 옛 공지의 `effective_date` < 최신. **`rag_qa.json`이 있으면 추가로**: 정답 청크가 `ax_chunks`에 존재, 방해 문서에 정답 텍스트 미포함, 데이터형 답이 쿼리로 재계산되고 ≥ 2건, QA 환자 이름 중복 0, dev/test 유형 비율 동일 |
| 누출 | `ax_calls`에 라벨·노이즈·카테고리 컬럼 없음; **`call_uid`에 목적 enum 문자열 없음**; 풀 마킹에서 카테고리 추론 불가; 분류기 입력 덤프에 전사·방향 외 메타 없음 |
| 결정성 | 같은 시드 2회 → 테이블 canonical 덤프 해시 동일(`indexed_at` 제외, 벡터 디렉토리 제외) |
| 가상 PII | 전화 `010-0000-xxxx`만 |

**경고**(리포트만)
| 영역 | 검사 |
|---|---|
| 분포 | 연령·목적·노이즈 ±2%p; 활동 70% ±2%p(예정 신환 제외); 휴면 (a) = 30% × 50% × min(1, 창 안 06-01 이전 일수/120) ±2%p; 하루 내원 ±30%(생성 기간 안 모든 진료일); 피크 패턴; 60분 시술의 피크 비중 < 40% |
| 치아 | FDI 유효 또는 `'00'`; 치아 단위는 `'00'` 금지, 전악은 `'00'`만; 유치는 소아만; 연령 제한 시술 |
| 문자 | out은 트리거 이후, in은 out 이후; 건수 ±10% |
| 통화 | 라벨 enum 유효; 화자 형식; 길이 2~4분; fact 날짜·시술 별칭 substring 존재; 라벨 어휘 직접 등장 < 20%; 복수 주제는 `accepted_purposes` ≥ 2 |
| RAG | (`rag_qa.json` 있을 때) 거절형 답이 어느 문서에도 없음(키워드 + LLM 1회); 질문 어휘가 정답 문단에 그대로 있는 비율 < 30% |

verify를 생성기보다 **먼저** 작성(TDD).

### 5.4 기술 선택

| 항목 | 선택 |
|---|---|
| 언어·라이브러리 | Python. Faker(ko_KR), numpy 시드. **lock 파일 커밋**(Faker 버전이 이름 결정성에 영향) |
| PDF(조건부) | Jinja2 → WeasyPrint(Nanum 폰트), 열화 Pillow |
| LLM | `shared/` 게이트웨이 경유(정의는 [[치과 AX - 앱 설계]] §7) |
| 시간 | DB naive KST. `emr_*`는 TODAY 기준 |
| 실행 | `make data / verify` |

## 6. 미결

- [ ] 거절형 금지 토픽 2개 선정 (`make data` 전)
- [ ] 프로바이더 조합 A/B — 7단계 전사 생성 전에 필요 ([[치과 AX - 앱 설계]] §8)
