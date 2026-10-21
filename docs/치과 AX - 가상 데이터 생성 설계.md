---
title: 치과 AX - 가상 데이터 생성 설계
related:
  - "[[치과 AX - 기능 정의]]"
  - "[[치과 AX - 평가 설계]]"
  - "[[치과 AX - 앱 설계]]"
created: 2026-10-16
updated: 2026-10-21
tags:
  - 치과AX
  - 데이터
---

> **어떤 데이터를 어떻게 만드나.** 규칙·절차의 원본. 값은 코드가 원본이고 이름으로 가리킨다(CLAUDE.md). 아직 코드가 없는 값(§2.4 심사 결과 분포, §4.1 분포 등)은 구현 때 코드로 옮긴다. config 키는 `config` `unpaid_rate`처럼 접두어를 붙인다(`tests/test_doc_sync.py`가 검사).

## 0. 전제

| 항목 | 결정 |
|---|---|
| 규모 | large = `config/generator.yaml` 기본값(기간·환자·하루 상한·통화·문서 수) |
| 축소 규모 | `SCALE=small`은 끝(`config` `range.end`) 고정·시작일만 늦춘다(`config` `scale_presets`) → TODAY 기준 구간(확정월·제출월·전월·미래)이 모두 남음. **하루 밀도와 구환 수는 모든 규모에서 같다**(신환만 기간에 비례) — 환자 1인당 수요가 같아야 같은 파라미터로 두 규모가 모두 성립한다. 통화 전사·판정/블라인드 풀만 기간 비율로 축소, 문서 수(`config` `documents`)는 고정 |
| 평가 규모 | 사람 판정·RAG 질문 작성·`eval.py` 보고·`make gate`는 **large 하나에서만**. small은 개발·테스트용 |
| 기준일 | `TODAY`(config) 고정. 앱에서는 바꾸지 않는다(원본 [[치과 AX - 앱 설계]] §6) |
| 보험 | 건강보험 단일. 월 1회 일괄 청구 + 소량 보완 청구. 실손은 환자 플래그 + 통화 소재만. 수가·심사는 흉내만(급여 청구액 = fee × 0.7, 소아 급여 특례 없음) |
| 테이블 접두어 | 현실의 소속 시스템 기준: `emr_*` 병원 원본(앱은 읽기 전용) / `ax_*` AX 시스템(앱이 읽고 씀) / `truth_*` 정답 별도 DB. `ax_calls`·`ax_documents`는 생성기가 채우지만 현실에선 AX 도입 후 쌓이므로 `ax_`. 슬롯 데이터도 같은 기준(통장·카드 거래 → `emr_`). **기존 `emr_*`는 수정하지 않고 새 테이블로** — 바뀌면 LLM 캐시·전사·사람 판정까지 연쇄 무효(LLM 캐시는 §5.1, 쓰기 보호는 [[치과 AX - 기능 정의]] §9) |

## 1. 병원 프로필

- 직원 구성은 `master.STAFF`(원장·위생사·데스크·상담실장). 진료=원장, 위생=위생사(순환), 상담=상담실장
- 영업시간·휴진(목·일·공휴일)·30분 슬롯은 `master.HOURS`·`SLOT_MIN`, 공휴일은 `config/holidays_2026.json`
- **생성 시 강제하는 운영 제약은 셋뿐**: ① 영업시간 안 ② 하루 비취소 예약 ≤ 상한(평일 `config` `visits_per_day`, 토 `master.SATURDAY_VISIT_RATIO` 내림) ③ 같은 시작 시각의 비취소 예약 ≤ `MAX_APPOINTMENTS_PER_SLOT`. 체어·원장/위생사 동시성·원장 슬롯은 모델링하지 않는다
- 비율 목표(노쇼·변경·신환·활동 등)는 시뮬레이션 파라미터로 맞추고 경고로만 확인 — 목록은 §5.3

## 2. `emr_*`

컬럼·enum은 `shared/schema.sql`. 여기서는 **규칙이 붙은 것만** 적는다.

### 2.1 시술

값(코드·분류·급여·비용·소요·적용 연령·치아 단위)은 `master.PROCEDURES`. 코드에 없는 의미:
- 구강검진은 진찰·파노라마를 대체하는 가상 코드
- 임플란트 2차는 마스터에만 두고 **생성하지 않는다**(1차 선행 조건을 추적하지 않으려고)
- 미백은 가격 문의 통화 소재, 상담은 상담실장 담당
- 시술의 `age_group`은 적용 대상(`age19plus` = 19세 이상, 노인 포함)이라 §2.2 연령 구간과 이름이 다르다
- `aliases`는 구어체 별칭 — 전사 fact 검증·검색 동의어(7단계에서 채움)

**치아 행**: 내원당 시술 1행(청구 1줄). 치아 단위 시술은 `master.TOOTH_CHOICES`에서 1개를 뽑기만, 아니면 `'00'`(센티널, NULL 아님).

### 2.2 환자

- **내원** = `completed` 예약. 노쇼·취소·아직 오지 않은 `scheduled`(TODAY 당일 포함)는 내원이 아니다. 활동·휴면·마지막 내원일은 모두 이 기준
- **신환** = 창 안에서 처음 등록한 환자: `registered_at` = 첫 예약 `created_at`의 날짜(전화 예약 때 차트 생성) ≥ `range_start`. 화면·지표의 신환은 이 정의 하나뿐
- `chart_no` 형식 `C-00001`, 전화 `010-0000-xxxx`(가상 번호)
- **구환** = `range_start` 전 등록(`master.REGISTERED_FROM` 이후 출생 다음 날부터, 그 환자의 창 이전 마지막 위생일보다 앞). 차트 번호는 등록순
- 파생(쿼리): 마지막 내원일, 휴면(6개월↑ 미내원), 가족 리콜 지남(가족 구성원 `MAX(내원) + recall_cycle < 기준일`)
- 연령 구간·비중은 `master.AGE_BANDS`. 비중은 추첨 없이 비중 × 인원을 반올림한 인원으로 맞춘다. 환자의 `master.FAMILY_SHARE`가 `master.FAMILY_SIZE`명 가족으로 묶이고, 소아는 100% 같은 가족 성인이 보호자(가족의 소아·청소년보다 `master.GUARDIAN_MIN_AGE_GAP`세 이상 많음) — 신환 소아의 보호자는 `master.FAMILY_SIZE` 상한 미만 가족의 구환 성인(4단계). 보호자·소아·청소년은 가족 성, 다른 성인·노인(배우자·부모)은 자기 성. 소아 전화는 보호자 번호를 같이 쓴다(나머지는 환자마다 고유). 실손 `master.PRIVATE_INSURANCE_SHARE`, 이름은 전부 한국 이름, `recall_cycle_months`는 전원 `master.RECALL_CYCLE_MONTHS`. 노인 나이 상한 `master.MAX_AGE`
- **메모**(`note`): 환자 `master.NOTE_SHARE`에 `master.NOTES` 문구 하나(예: "치과 공포 있음", "고혈압 약 복용", "오전 진료 선호"). ⑧ 준비표 fact와 `query_patient` 요약에 들어가 제안 문장의 근거가 된다(예: 고혈압 → 발치 전 혈압 확인)

### 2.3 예약·진료

| 컬럼 | 의미 |
|---|---|
| `planned_procedure_code` | 예정 시술 — 준비표·전사가 읽음 |
| `rescheduled_from_id` | 변경으로 생긴 새 예약이면 원래 예약. **변경 쌍** = 원래(`cancelled`) + 새 예약(이후 상태는 일반 예약과 같음). 변경 쌍이 아닌 `cancelled`는 당일 취소뿐 — 취소 통화는 여기서 역산 |
| `booked_via` | 예약 경로 — `phone`(전화. 변경으로 생긴 새 예약 포함) / `desk`(내원 종료 때 데스크에서 잡은 코스 다음 회차). 예약 잡기 통화는 `phone`에서 역산 |
| `confirmed` | 직전 진료일 리마인드 응답 |
| `created_at`, `cancelled_at` | 예약을 잡은·취소한 시각 — 통화 시각·as-of의 근거 |

진료 기록(`emr_treatments`)은 completed 예약에 1행, 예정 시술 그대로. 불변식은 §5.3.

### 2.4 수납·청구

- **잔액** = Σ(`amount_due − amount_paid`). 창 안 completed 내원마다 `visit` 수납 행 1개(0원이어도). 미수는 `amount_due > 0`인 내원 중 `config` `unpaid_rate`에서 일부만 수납 → 그중 `config` `settle_next_visit`은 다음 내원 때 `settlement` 행(`amount_due = 0`)으로 정산, 나머지는 잔류. 발생률만 두면 잔액이 누적돼 준비표 미수 칩이 절반 넘게 켜진다
- **보험 잔여**(준비표 칩): 별도 테이블 없이 `emr_claims`의 그해 스케일링 급여 청구 유무로 계산 — 같은 사실을 두 곳에 두지 않는다
- **청구**: 급여 시술 completed마다 1줄. `claim_month` = 진료월(보완 청구도 같음). `batch_type` = `monthly` / `supplementary`(청구 줄의 `config` `claims.supplementary_ratio`를 무작위로 — 월 일괄 청구에서 빠져 나중에 낸 건)
  - `submitted_at`: monthly = 익월 첫 진료일 10:00, supplementary = 그 + 14일(휴진일이면 다음 진료일), 전월분(`pending`)은 NULL
  - `status`: 전월 = `pending`(미제출) / 그 전 `SUBMITTED_MONTHS`개월 = `submitted`(결과는 장부에 없음) / 그 이전 = 확정(`paid·adjusted·denied`, 병원이 이미 아는 과거 결과)
- **대조 키** `(chart_no, service_date, procedure_code, tooth_no)` UNIQUE — 심사결과 대조용. 제출 월의 심사 결과는 `truth_claims`에만(심사결과 슬롯 착수 때 생성 — 정답 DB라 나중에 만들어도 `app.sqlite`·LLM 캐시가 바뀌지 않음)

**심사 결과 추첨**(확정 월 `emr_claims`는 정형 5단계, 제출 월 `truth_claims`는 심사결과 슬롯 — 같은 규칙)
| 대상 | 분포(청구 줄 기준) |
|---|---|
| 확정 월 → `emr_claims.status` | paid 88 / adjusted 8 / denied 4 (`missing`·`extra`는 통보서 대조 결과라 장부에 없음) |
| 제출 월 → `truth_claims.outcome` | paid_full 85 / adjusted 8 / denied 4 / missing_in_claim_review 2 / extra_in_claim_review 1 |

- 금액: paid → `paid_amount = claim_amount` / adjusted → `claim_amount × U(0.5, 0.9)`, 10원 단위 내림 + `adjust_code` / denied → `paid_amount = 0` + `adjust_code` / missing_in_claim_review → 둘 다 NULL
- 사유 코드 10개는 `master.ADJUST_CODES`(5단계 구현 때 추가)
- `extra_in_claim_review` 줄(장부에 없음, `claim_id` NULL): 제출 월 실제 내원(환자·진료일)에 그 내원에 없는 급여 시술 또는 다른 치아를 붙여 만든다 → 대조 키가 `emr_claims`와 겹치지 않음

### 2.5 문자

| trigger | 규칙 |
|---|---|
| appt_confirm | 예약 생성 시(변경 쌍·당일 취소 행 포함) |
| reminder | 직전 진료일의 리마인드 시각(`master.reminder_time`), 그 시점에 있고 취소되지 않은 예약 |
| noshow | 노쇼 시 |
| balance | 미수 발생 중 `config` `messages.balance` 비율 |
| doc_ready | 서류 요청 **통화 이후** |
| reply(in) | out 중 `config` `messages.reply_rate` 비율, 고정 문구에서 추첨("네", "확인했습니다", "시간 변경 가능할까요?" 등) |

문자는 전부 템플릿(LLM 없음). 색인 방식은 [[치과 AX - 앱 설계]] §3.

### 2.6 정형 생성 규칙 (2~5단계) — 정방향 시뮬레이션

> 하루씩 진행하며 예약을 잡고 그날의 예약을 처리한다. 사건이 시간 순서대로만 일어나므로 `created_at`·`registered_at`·코스 다음 회차·변경·노쇼 뒤 재예약이 저절로 정합된다. 강제하는 것은 §1의 운영 제약 셋과 §5.3 필수 검사뿐이고, 분포는 파라미터로 맞춘 뒤 경고로만 확인한다. 환자는 전부 **단발 내원** 모델(회차 추적 없음).

**① 환자와 수요** (2·3단계)
- **구환**: 창 이전 마지막 위생 내원일을 **시뮬레이션 시작 전 리콜 한 주기에 고르게** 시드(2단계) → 첫 리콜 기한 = 그날 + 리콜 주기(3단계, 첫날에 기한이 몰리지 않게 — 지터로 시작 전이 된 기한은 첫 `master.MAX_LEAD_DAYS`일에 나눠 받음). `config` `lapse_rate`만큼은 첫 리콜에 응하지 않는다(구환 첫 리콜만 — 신환과 이후 리콜은 응답. 치료 수요가 생기면 내원할 수 있어 휴면 비율과 같지 않음 — 튜닝 때 `master.ACTIVE_SHARE`와 같이 본다)
- **리콜 주기** = `master.RECALL_CYCLE_MONTHS`개월 ± `master.RECALL_JITTER_DAYS`일
- **구환 수** = `config` `returning_patients`(규모와 무관하게 고정). 신환은 시뮬레이션이 목표 비율대로 만든 만큼이고, 환자 총수는 결과값이다
- **신환 후보 풀**: 이름·연령 등만 미리 만들어 두고 필요할 때 꺼내 등록. 모자라면 더 만들고, 쓰지 않은 후보와 출생일이 등록일 이후인 후보는 버림
- **치료 수요**: 연령 구간별 월 발생률(`config` `treatment_demand_monthly`, 그날 나이 기준 포아송)로 '치료 필요'가 생기면 에피소드 하나(②)를 시작. **환자당 열린 수요는 동시에 1개**(진행 중인 에피소드·잡힌 예약이 있으면 새 수요는 그 뒤로) — 신환은 첫 내원 전에 다른 수요가 없다. 구환의 수요 발생일·유형은 3단계가 창 전체를 자기 시드로 미리 뽑는다(`generator/demand.py`) — 예약 파라미터를 바꿔도 수요는 그대로
- **리콜 시드**: completed 내원이 생기면 다음 리콜 기한 = 마지막 위생 내원(없으면 그 내원) + 리콜 주기

**② 에피소드**
| 유형 | 내원 | 다음 회차 간격 |
|---|---|---|
| H 정기 위생 | hygiene 1 | 리콜 주기 뒤 다시 리콜 |
| T1 단발 | treatment 또는 consult 1 | — |
| Tn 치료 코스 | treatment `master.COURSE_VISITS` | `master.COURSE_INTERVAL_DAYS` |

- 치료 수요의 유형 비중: `master.EPISODE_MIX`(연령 구간별)
- 코스 안의 시술은 내원마다 ④에서 독립 추첨(교정 월 체크도 일반 시술 — 교정 과정을 따로 추적하지 않음)

**③ 하루 진행** (4단계) — 스냅샷 시점은 **TODAY 개원 전**: `range_start`부터 TODAY 전날까지 돌고(워밍업 없음), TODAY에는 아무 일도 없다. **4단계는 예약·상태·시각만 정한다** — 진료 기록·수납·청구는 5단계가 자기 시드로 completed에서 파생하므로(스케줄에 영향 없음), `config` `unpaid_rate`를 바꿔도 예약은 그대로다
```
for d in 진료일(range_start … TODAY − 1):       # 휴진일은 예약 생성·처리 없음
  1. 그날 예약 처리 (영업시간 순)
     - 상태 추첨: completed / noshow / 당일 취소(cancelled_at = 그날 영업시간 안, 예약 시각 전 — 그래서 첫 슬롯 예약은 당일 취소 없음)
     - completed → (진료·수납·청구는 5단계) 코스 다음 회차가 있으면 그 자리에서 데스크 예약(booked_via = desk, created_at = 내원 종료 시각, 희망 간격 ② 뒤 첫 가능일). 가능일이 없으면 다음 날 전화(희망 리드라 간격보다 이를 수 있음 — 용량이 끝까지 찬 경우뿐)
     - noshow·당일 취소 → 그 수요는 남고, 재예약 지연(`config` `rebook_delay_days`) 뒤 다시 전화 예약 — 같은 시술(연령 구간·연도가 바뀌면 다시 뽑음)
  2. 전화 예약(booked_via = phone): 그날 기한이 된 수요(리콜·치료 시작·재예약)와 신환
     - created_at = 그날 영업시간 안 임의 시각(그날 내원했으면 그 종료 뒤), 희망 리드 1~`master.MAX_LEAD_DAYS`일 뒤 첫 가능한 진료일, 시각은 그날 가능한 슬롯 중 균등 추첨
     - 가능 = §1 운영 제약 셋 + 같은 환자 같은 날 예약 없음 + 날짜 ≤ range_end. 그때까지 가능일이 없으면 다음 날 다시
     - 신환: 그날 잡힌 기존 환자 예약(전화·데스크, 변경 제외) B건에 대해 B × s/(1−s)명(s = `master.NEW_PATIENT_SHARE`, 확률적 반올림)을 후보 풀에서 꺼냄 — 상태 추첨이 같아 내원 비중도 s 근처. 예약이 잡혀야 등록(registered_at = 그날), 빈자리가 없으면 그날 신환은 그만. 첫 유형은 내원 구성 목표(VISIT_MIX)대로, 진료면 T1·Tn은 `master.EPISODE_MIX` 비율
     - 새 예약마다(전화·데스크 모두) reschedule_rate 확률로 "변경 예정" + 변경일(created_at 다음 날 ~ 예약 전날 중 진료일) 추첨.
       그 사이에 진료일이 없으면(리드 1일, 수→금처럼 휴진일만 낀 경우) 변경 없음
  3. 변경: 오늘이 변경일인 예약 → 원래 예약 cancelled(cancelled_at = 오늘 영업시간 안), 새 예약은 원래 날짜 뒤 1~`master.RESCHEDULE_MAX_DAYS`일의
     첫 가능일(같은 유형·시술, booked_via = phone, created_at = 같은 시각, 다시 변경하지 않음). 가능일이 없으면 변경하지 않음
  4. 리마인드: 그날 리마인드 시각(master.reminder_time — 토요일은 마감 전), 그 시점에 있고 취소되지 않은 다음 진료일 예약에
     발송 → confirm_rate 확률로 confirmed

5단계(진료·수납·청구): 창 안 completed마다 진료 기록 1행, visit 수납(paid_at = 내원 종료 시각), 미수 정산은 다음 completed
내원 종료 시각에 settlement 행, 급여 시술은 청구 1줄(§2.4)
```
- **사건 시각**: 생성기는 예약·취소·통화 시각을 진료일 영업시간 안에서 고른다(검사하지 않음)
- **시작 직후**: 워밍업 없이 `range_start`부터 돌아 모든 사건이 창 안에 있다. 처음 최대 리드(`master.MAX_LEAD_DAYS`)는 미리 잡힌 예약이 없어 덜 찬다 — large에선 2월 초(확정 청구 월)라 기능 영향 없고, 하루 채움 리포트에서 뺀다. 리콜 기한이 이미 지난 구환 수요는 첫날 몰리지 않게 첫 리드 기간에 나눠 받는다
- TODAY 이후에는 TODAY 전날까지 잡힌 예약만 남는다 — 미래 일정이 덜 찬 상태가 정상
- 하루 상한이 차면 수요는 뒤로 밀린다(희망 리드보다 늦어질 수 있음). 과거 진료일이 상한에 크게 못 미치면 수요 파라미터를 올림
- **위생 비중·활동 비율·리콜 주기는 서로 걸려 있어** 튜닝 때 같이 본다. 활동 비율이 안 맞으면 파라미터를 비틀지 말고 목표(`master.ACTIVE_SHARE`, 평가에 안 쓰는 값)를 첫 large 결과에 맞춰 낮춘다. 0.5 아래가 필요하면 목표 대신 구환 수부터 조정(활동 환자 1인당 내원이 너무 잦아짐)
- 연령 기준일은 예약일(= 시술일)

**④ 유형별 예정 시술 가중** (`planned_procedure_code`): `master.PLANNED_WEIGHTS`(유형 × 연령 구간, 예약일 기준). 성인·노인 hygiene은 가중이 아니라 규칙(`master.HYGIENE_SCALING_BANDS`) — 그해 급여 스케일링(`master.SCALING_MIN_AGE` 이상)을 이미 했으면(completed) 구강검진, 아니면 스케일링. 첫 후보일보다 늦은 날 잡혀 연령 구간·연도가 바뀌면 그날 기준으로 다시 뽑는다

- **스케일링**: `master.SCALING_MIN_AGE` 미만은 비급여(청구 없음). 성인은 그해 이미 썼으면 hygiene이 구강검진으로 바뀌므로 2회째가 생기지 않음
- **임플란트 1차(`conditional`)**: 시술일 기준 `master.CONDITIONAL_INSURED_MIN_AGE` 이상 급여(마스터의 본인부담률), 미만 비급여(본인부담 1.0, 청구 없음)

## 3. 생성기가 적재하는 `ax_*`

생성기는 `ax_calls`와 `ax_documents`만 적재한다(생성기와 앱 사이의 계약). 나머지 `ax_*`와 `make index`·`make fixture`는 [[치과 AX - 앱 설계]].

**`ax_calls`**
| 컬럼 | 의미 |
|---|---|
| `call_uid` | 불변 키 `c-0481` — 시드 셔플 순번. 캐시 키·human import 폴백. 목적을 담은 형식(`purpose:순번`)은 라벨 누출이라 금지 |
| `patient_id`, `caller_phone`, `caller_name` | **분류기 입력에 쓰지 않음**(분류기 입력 규칙은 앱 설계 §1) |
| `transcript` | 화자 구분된 **순수 대화만**(prefix 없음, 차트번호 발화 없음) |
| `in_eval_pool`·`eval_sort_order`, `in_blind_pool`·`blind_sort_order` | 생성기가 층화 추출해 마킹 — **카테고리 정보 없음**. 크기·순번·split 규칙은 [[치과 AX - 평가 설계]] §2.1 |
| `judge_status` | 생성기는 판정 풀만 `pending`, 나머지 `none`. 붙여넣기는 `pending` |

골드 라벨·노이즈는 `truth_call_labels`에만. **`ax_documents`**의 방해 여부·심은 QA는 `truth_doc_meta`에만.

## 4. 비정형 생성

### 4.1 통화 전사

**절차**(건당)
1. 상대 → 목적 → 급함·플래그를 분포에서 추첨 = 골드 라벨
2. 환자/보호자면 EMR에서 환자를 뽑고 fact 추출: 최근 내원·시술, 잔액, 다음 예약, 가족, 메모. **통화 시각 이후의 사실은 넣지 않음**(`created_at/performed_at/paid_at ≤ 통화 시각`, status 컬럼은 TODAY 스냅샷이라 읽지 않음)
3. 노이즈 추첨 25%
4. **라벨 → 상황 → 대화** (단일 JSON 호출 `{scenario, transcript}`). 라벨 어휘("예약 변경", "불만")를 대화에 직접 쓰지 않게 지시
5. 전사 규칙: `환자:`/`직원:`, 2~4분 분량, fact 날짜는 절대 날짜(`11월 24일`), 시술은 표준명 또는 `aliases`
6. **건별 검증 루프**: (a) fact 날짜·시술 별칭 substring 존재 (b) 라벨 어휘 직접 등장 없음 (c) 금지 토픽 미매칭 → 실패 시 그 건만 재생성(최대 3회, 그래도 실패면 fact 단순화)
7. 금지 토픽 정규식 정제
8. 저장: 전사 → `ax_calls`, 라벨·fact·노이즈 → `truth_call_labels`
9. 층화 추출 → 판정 풀·블라인드 풀 마킹(평가 설계 §2.1)

**예약류 통화는 예약에서 역산**: 통화가 원인, 예약이 결과. `booked_via = phone` 예약(변경으로 생긴 새 예약은 변경 통화로 쓰이므로 제외) → 예약 잡기, 변경 쌍 → 예약 변경, 당일 취소 → 취소, confirmed(직전 진료일) → 확인. **확인 통화**: confirmed 예약에서 역산, 시각은 리마인드 날(직전 진료일) 영업시간 안. 방향은 아래 방향 분포대로. 통화 시각 = 그 이벤트 시각. 연결은 `truth_call_labels.used_fact`에 `appointment_id`를 담는다.

**목적 분포**(%)
| 목적 | % | | 목적 | % |
|---|---|---|---|---|
| 예약 잡기 | 18 | | 가격·비용 문의 | 8 |
| 예약 변경 | 15 | | 수납·청구 문의 | 4 |
| 예약 확인 | 8 | | 서류·기록 요청 | 4 |
| 예약 취소 | 6 | | 응급·통증 | 5 |
| 리콜·안내 전화 | 8 | | 불만 | 3 |
| 치료 문의 | 10 | | 기타 | 3 |
| 보험 문의 | 6 | | 모르겠음·애매 | 2 |

**상대**: 환자 60 / 보호자 15 / 처음 연락 15 / 보험사 3 / 업체 4 / 기타 3

**enum 키**: 목적 `book / reschedule / confirm / cancel / recall / treatment_q / insurance_q / price_q / billing_q / document_req / emergency / complaint / other / unknown` · 상대 `patient / guardian / new_caller / insurer / vendor / other` · 급함 `urgent / normal / low`
**급함**: `emergency` → 전부 `urgent` / 노이즈 '급함 애매한 통증' → 50% `urgent` / 나머지 `normal` 85 · `low` 15
**플래그**: 주 목적이 함의하면 항상 true(`emergency`→emergency, `complaint`→complaint, `price_q`→price_inquiry, `book`→wants_appt) + 복수 주제 통화는 부 주제 플래그 1개. `appt_confirmed`는 `book`·`reschedule`의 70%. `has_todo`는 `confirm`·`recall` 30%, 나머지 80%
**노이즈**(25%): 주제 2개 섞임 10 / 말 끊김·되묻기 8 / 급함 애매한 통증 4 / 보호자가 정보 헷갈림 3
**방향**: outbound ~20%, **리콜에만 두지 않음** — 리콜·안내 8%p 전부 + 예약 확인 4%p + 수납 문의 3%p + 서류 요청 2%p + 치료 문의(경과 확인) 3%p. outbound의 절반은 복합 주제("리콜 안내 + 예약 희망")로 — 단일 구조면 흐름만으로 식별됨

**복수 주제 통화의 생성 제약**: `primary_purpose` 1개 + 부 주제는 불리언 플래그(가격·불만·응급·예약 희망)로 표현되는 조합만 생성. 플래그로 표현 불가한 조합(예: 예약 변경 + 서류 요청)은 만들지 않음. `accepted_purposes[]`에 주·부 모두 기록 → 채점은 평가 설계 §2.2

**`call_uid`는 6단계에서 전사 슬롯에 부여** — 7단계 LLM 캐시 키에 들어가므로 전사 전에 확정. 전사 슬롯은 6단계에서 `has_transcript=0` 빈 행으로 만들고(시각은 §2.6 사건 시각) 7단계가 채움

생성 분포는 현실대로(치우침 유지).

### 4.2 RAG 문서

**순서**: 문서 생성 → 질문 60개를 사람이 **생성 후** 작성(평가 설계 §3.1) → 방해 문서에 정답 텍스트가 없는지 verify. 금지 토픽 정제는 문서·통화·문자 전부.

**어휘 일치 방지**(생성 지시): 질문이 물을 핵심 어휘를 문단에 그대로 쓰지 않게 — "노쇼 3회"의 답 문단은 "예약 부도가 세 차례 누적되면"처럼 동의어·우회 표현.

| 종류 | 수 | 생성 |
|---|---|---|
| 직원 매뉴얼 | 12 | LLM, 정답 문단 심기 |
| FAQ | 10 | LLM |
| 보험 안내 | 6 | LLM + 마스터 값 주입 |
| 원내 공지 | 12 | 템플릿 + LLM. 날짜·버전·수치가 바뀌는 구조(최신성 평가용) |
| 공지 옛 버전 | 12 | 공지마다 1개, 다른 수치·이전 날짜 |
| 치과 상식 | 10 | LLM |
| 유사 주제 무답 | 10 | LLM |
| 타 병원·일반 | 5 | LLM |

전부 마크다운. 청킹·색인은 [[치과 AX - 앱 설계]] §3.

## 5. 생성·정답·검증

### 5.1 파이프라인

```
make data [SCALE=small|large] [REGEN_LLM=1]
 0. 시드 로드      holidays, forbidden_topics                    (정적)
 1. 마스터         clinic, staff, procedures                     (코드)
 2. 환자           §2.2, §2.6 ①                                  (코드)
 3. 첫 수요        §2.6 ①                                        (코드)
 4. 예약 시뮬레이션 §2.6 ③                                        (코드)
 5. 진료·수납·청구 §2.4, §2.6 ④                                   (코드)
 6. 통화 슬롯      §4.1 절차 1~3·예약 역산·call_uid               (코드)
 7. 통화 전사      §4.1                                          (LLM, 캐시)
 8. 문자           §2.5                                          (코드)
 9. RAG 문서       §4.2                                          (LLM, 캐시)
10. 적재·색인·검증 app.sqlite → make index(앱 설계 §3, 앱 색인 코드 전엔 건너뜀) → make verify
━━ 조건부 ━━
11. 심사결과 PDF   슬롯 메모 참고                                 (정답 DB만 — app.sqlite 불변)
```

- **단계별 독립 시드**: 각 단계는 `config` `seed`와 단계 이름으로 자기 난수 생성기를 만든다 — 단계를 추가·삭제해도 다른 단계의 결과가 바뀌지 않게(구현 방법은 `generator/seeding.py`)
- **정형 먼저, LLM 한 번**: 정형(1~6) 완성·verify(small·large 둘 다) 뒤에 LLM(7·9)을 한 번 돌린다. 정형 로직을 고치면 LLM 캐시가 전량 미스. **LLM은 large에서만** — small의 7·9단계는 fake 게이트웨이(입력 해시 기반 결정적 가짜 전사·문서)를 써서 과금 없이 구조만 채운다(테스트 픽스처용)
- **LLM 캐시**: 생성물은 파일 캐시로 커밋, `make data REGEN_LLM=1`로만 재생성. 키 = `(call_uid, seed, prompt_template_hash, used_fact_hash, attempt)` — attempt가 없으면 검증 실패 후 재시도에 같은 실패작이 돌아옴

### 5.2 정답 출력 `ground_truth/`

DDL은 `ground_truth/schema.sql`(테이블은 그 단계를 구현할 때 추가 — `shared/`는 앱이 읽는 위치라 두지 않음). 테이블 정의와 채우는 쪽은 [[치과 AX - 평가 설계]] §6. `truth_gen_meta.scale`은 프리셋 이름, 또는 verify 테스트가 손으로 만든 DB(`tests/verify_db.py`)의 `fixture` — 앱의 `make fixture`(small 데이터로 만듦)와는 다르다.

### 5.3 `make verify`

불변식("항상 성립해야 하는 것")의 원본은 이 표다. §2의 절차는 이것을 지키도록 짠다. 구현 현황은 `generator/verify.py` docstring.

**필수**(실패 시 중단)
| 영역 | 검사 |
|---|---|
| 참조 | FK 전부 유효. 다형 참조(`ax_llm_calls.ref_*`, `ax_chunks.source_*`)는 타입별 조인 |
| TODAY 구간 | TODAY 당일은 `scheduled/cancelled`만; TODAY 이후는 예약만(treatments·payments·claims 없음); `started_at`·`sent_at`·`submitted_at`·`effective_date`·`created_at`·`cancelled_at`·`registered_at`·`birth_date` ≤ TODAY; `confirmed`는 직전 진료일 ≤ TODAY인 예약만; 청구 상태는 §2.4(전월 `pending`, 제출 월은 `submitted`·결과 없음). 생성 기간은 `truth_gen_meta`에서 읽음(없으면 실패) |
| 운영 제약(§1) | 영업시간·휴진일 밖 없음; 예약 시작 :00·:30, 소요 30분 배수; 하루 비취소 예약 ≤ 상한; 같은 시작 시각 비취소 예약 ≤ `master.MAX_APPOINTMENTS_PER_SLOT`; 같은 환자 같은 날 중복 없음 |
| 예약 | `planned_procedure_code` 필수; `created_at` < `scheduled_at`; 취소는 `created_at` ≤ `cancelled_at` < `scheduled_at`; `cancelled_at`은 cancelled에만(cancelled는 필수); 취소는 변경 쌍의 원래 예약 또는 TODAY 이전 당일 취소뿐; 변경 쌍 = 원래 예약은 cancelled·새 `created_at` = 원래 `cancelled_at`, 변경은 예약당 1회(새 예약은 다시 원래 예약이 되지 않음); 예약류 통화 시각 == 대응 이벤트 시각(확인 통화는 리마인드 날 영업시간 안); 예약 이벤트 수 ≥ 그 목적 통화 수(6단계, LLM 전) |
| 시술·청구 | 진료 기록은 completed 예약에만, completed 예약은 정확히 1행; `performed_at` == 예약 일시; `service_date` == 진료일; 비급여(`master.CONDITIONAL_INSURED_MIN_AGE` 미만 conditional 포함)는 claims에 없음; 스케일링 급여 `master.SCALING_MIN_AGE`↑ 연 1회; `tooth_no` NULL 없음; 대조 키 UNIQUE; (`truth_claims`가 있을 때만) `extra_in_claim_review` 대조 키가 `emr_claims`와 겹치지 않음, 제출 월 `emr_claims` ↔ `truth_claims` 1:1(`claim_id`·대조 키·금액 일치), `extra_in_claim_review`도 제출 월 진료일 |
| 수납 | 환자별 시점 누적 잔액 ≥ 0; `visit` 행은 completed 예약에만 |
| RAG | 금지 토픽 코퍼스 전체 미포함; 통화·스레드 청크에 prefix; 옛 공지의 `effective_date` < 최신. **`rag_qa.json`이 있으면 추가로**: 정답 청크가 `ax_chunks`에 존재, 방해 문서에 정답 텍스트 미포함, 데이터형 답이 쿼리로 재계산되고 ≥ 2건, QA 환자 이름 중복 0, dev/test 유형 비율 동일 |
| 누출 | `ax_calls`에 라벨·노이즈·카테고리 컬럼 없음; **`call_uid`에 목적 enum 문자열 없음**; 풀 마킹에서 카테고리 추론 불가; 분류기 입력 덤프에 전사·방향 외 메타 없음 |
| 결정성 | 같은 시드 2회 → `app.sqlite`·`truth.sqlite` 각각 canonical 덤프 해시 동일(`indexed_at` 제외, 벡터 디렉토리 제외). `make verify` 밖 — `tests/test_generator.py` |
| 가상 PII | 환자 전화·통화 상대 번호 `010-0000-xxxx`만 |

**경고**(위반만 보여 주고 실패시키지 않는다. 기능에 직접 영향을 주는 지표만 둔다. 분모는 **추첨이 실제로 실현된 모집단** — TODAY 이전·창 안·진료일·리마인드 시각 등 추첨이 일어날 수 있었던 것만 센다. 설계 목표라 숫자를 여기 둔다 — 값의 원본은 '값 위치'의 코드이고, 둘이 어긋나면 `tests/test_doc_sync.py`가 실패한다)

| 지표 | 목표 | 허용 | 값 위치 | 분모 | 건너뛰는 경우 |
|---|---|---|---|---|---|
| 연령 구간 비중 | 소아 15% · 청소년 8% · 성인 62% · 노인 15% | ±2%p | `master.AGE_BANDS` | 전체 환자(TODAY 기준 나이) | — |
| 활동(TODAY 전 `master.ACTIVE_MONTHS`개월 안 내원) | 60% | ±5%p | `master.ACTIVE_SHARE` | 예정 신환(창 안 등록 + 비취소 예약이 전부 TODAY 이후)을 뺀 환자 | 생성 기간 < `master.ACTIVE_MONTHS`개월(small) |
| 신환 = 월 내원의 비율 | 12% | ±3%p | `master.NEW_PATIENT_SHARE` | 그달 completed | 그달 completed < `verify.NEW_PATIENT_MIN_VISITS` |
| 노쇼·당일 취소 | 노쇼 6% · 당일 취소 4% | ±2%p | `config` `noshow_rate`·`sameday_cancel_rate` | TODAY 이전 예약 − 변경 원래 예약(당일 취소는 포함) | — |
| 내원 구성 | 진료 70% · 위생 25% · 상담 5% | ±3%p | `master.VISIT_MIX` | completed | — |
| 통화 목적·노이즈 | §4.1 | ±2%p | §4.1 | 전사 | — |

경고 — 분포 밖 항목:
| 영역 | 검사 |
|---|---|
| 치아 | 치아 단위 시술은 `'00'` 금지, 치아 단위가 아닌 시술은 `'00'`만; 소아 시술(`age_group='child'`)은 소아만 |
| 문자 | out은 트리거 이후, in은 out 이후 |
| 통화 | 라벨 enum 유효; 화자 형식; 길이 2~4분; fact 날짜·시술 별칭 substring 존재; 라벨 어휘 직접 등장 < 20%; 복수 주제는 `accepted_purposes` ≥ 2 |
| RAG | (`rag_qa.json` 있을 때) 거절형 답이 어느 문서에도 없음(키워드 + LLM 1회); 질문 어휘가 정답 문단에 그대로 있는 비율 < 30% |

**리포트**(허용치 없이 수치만 — 튜닝 때 눈으로 본다. 분모 정의는 `verify.report_*` docstring): 리마인드 확인률(목표 `config` `confirm_rate`), 미수 발생률(`config` `unpaid_rate`), 변경률(`config` `reschedule_rate`), 하루 채움(평균·최저, 시작 후 `master.MAX_LEAD_DAYS`일 제외), 잔액 있음 비율(준비표 범위 = 최근 `verify.PREP_SHEET_DAYS`일 + 오늘의 예약, 잔액 있음 정의는 [[치과 AX - 앱 설계]] §3 `has_unpaid_balance`)

verify를 생성기보다 **먼저** 작성(TDD).

### 5.4 기술 선택

Python · Faker(ko_KR) · numpy 시드(lock 파일 커밋 — Faker 버전이 이름 결정성에 영향). DB 시각은 naive KST, `emr_*`는 TODAY 기준. LLM은 `shared/` 게이트웨이(앱 설계 §7).

## 6. 미결

- [ ] 거절형 금지 토픽 2개 선정(`make data` 전 — 평가 설계 §3.1)

## 슬롯 메모 — 심사결과통보서(②, 조건부)

착수 전까지 정형 단계에 영향 없음. 이미 정한 것:
- 제출 월 outcome 추첨 → `truth_claims`(§2.4 규칙) → 렌더링. 전월은 `pending`이라 통보서 없음. 확정 월은 "이미 확인된 통보서"로 판독 벤치마크만
- 월별 통보서(창 안 확정·제출 월, 페이지당 고정 25줄, 양식 상반기 A / 하반기 B) + 건별(`supplementary`를 `(patient, service_date)`로 묶음, 1~4줄, 양식 C)
- Jinja2 → PDF(WeasyPrint + Nanum 폰트, 실패 시 Playwright). 페이지 분할은 생성기가 슬라이싱 → `(page, line_no)` 정답 확정 → `truth_claim_review_lines`
- 열화 30%(Pillow): 200dpi → 블러·회전 ±1.5°·노이즈·JPEG 40~60
- 현실 프레이밍: 심사 결과를 EDI 자동 반영하지 않고 통보서(스캔·팩스)로 받아 수기 반영하는 병원 가정
