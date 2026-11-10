-- source.sqlite 스키마 — 생성기 출력(emr_* / 생성기가 적재하는 ax_calls·ax_documents). 컬럼·enum·인덱스의 원본.
-- 생성기만 쓰고 앱은 읽기 전용으로 ATTACH한다(가상 데이터 생성 설계 §0). 의미·생성 규칙은 그 문서 §2·§3.
-- 앱이 쓰는 테이블은 shared/app_schema.sql
-- truth_* 는 여기 두지 않는다 (평가 설계 §6, ground_truth/)
-- 시각은 naive KST ISO 문자열('2026-07-03 10:30:00'), 날짜는 'YYYY-MM-DD'

-- ── emr_* (§2) ──────────────────────────────────────────────

CREATE TABLE emr_clinic (
    name       TEXT NOT NULL,
    hours_json TEXT NOT NULL
);

CREATE TABLE emr_staff (
    id     INTEGER PRIMARY KEY,
    name   TEXT NOT NULL,
    role   TEXT NOT NULL CHECK (role IN ('doctor', 'hygienist', 'desk', 'consultant')),
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE emr_procedures (
    code               TEXT PRIMARY KEY,
    name               TEXT NOT NULL,
    category           TEXT NOT NULL CHECK (category IN ('preventive', 'restorative', 'endo', 'prosth',
                           'surgery', 'implant', 'ortho', 'cosmetic', 'perio', 'consult')),
    is_insured         TEXT NOT NULL CHECK (is_insured IN ('yes', 'no', 'conditional')),
    fee_total          INTEGER NOT NULL,
    patient_share_rate REAL NOT NULL,
    duration_min       INTEGER NOT NULL,
    insured_limit      INTEGER,
    limit_scope        TEXT,
    age_group          TEXT CHECK (age_group IN ('child', 'age19plus', 'senior')),
    is_tooth_level     INTEGER NOT NULL,
    aliases            TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE emr_families (
    id      INTEGER PRIMARY KEY,
    surname TEXT NOT NULL
);

CREATE TABLE emr_patients (
    id                     INTEGER PRIMARY KEY,
    chart_no               TEXT NOT NULL UNIQUE,
    name                   TEXT NOT NULL,
    birth_date             TEXT NOT NULL,
    sex                    TEXT NOT NULL CHECK (sex IN ('M', 'F')),
    phone                  TEXT NOT NULL,
    family_id              INTEGER REFERENCES emr_families(id),
    guardian_patient_id    INTEGER REFERENCES emr_patients(id),
    registered_at          TEXT NOT NULL,
    recall_cycle_months    INTEGER NOT NULL DEFAULT 12,
    has_private_insurance  INTEGER NOT NULL DEFAULT 0
);

-- 주의사항 정의 (§2.2). 관련 시술·행동·대상·비율은 master.ALERT_TYPES에만
CREATE TABLE emr_alert_types (
    code     TEXT PRIMARY KEY,
    label    TEXT NOT NULL,
    category TEXT NOT NULL CHECK (category IN ('systemic', 'allergy', 'procedure'))
);

-- 환자 주의사항 (§2.2, 4b단계). intake = 문진(appointment_id 없음), visit = 진료 중 발견(발견한 내원)
CREATE TABLE emr_patient_alerts (
    id             INTEGER PRIMARY KEY,
    patient_id     INTEGER NOT NULL REFERENCES emr_patients(id),
    code           TEXT NOT NULL REFERENCES emr_alert_types(code),
    source         TEXT NOT NULL CHECK (source IN ('intake', 'visit')),
    recorded_at    TEXT NOT NULL,  -- 기록일 'YYYY-MM-DD'. 이 날부터 보인다
    appointment_id INTEGER REFERENCES emr_appointments(id),
    ends_at        TEXT,           -- 끝나는 날(이 날부터 안 보인다). 기간이 있는 항목(임신·소아)만, 나머지는 NULL
    UNIQUE (patient_id, code)
);

CREATE TABLE emr_appointments (
    id                     INTEGER PRIMARY KEY,
    patient_id             INTEGER NOT NULL REFERENCES emr_patients(id),
    staff_id               INTEGER NOT NULL REFERENCES emr_staff(id),
    scheduled_at           TEXT NOT NULL,
    duration_min           INTEGER NOT NULL,
    type                   TEXT NOT NULL CHECK (type IN ('treatment', 'hygiene', 'consult')),
    planned_procedure_code TEXT NOT NULL REFERENCES emr_procedures(code),
    status                 TEXT NOT NULL CHECK (status IN ('scheduled', 'completed', 'cancelled', 'noshow')),
    rescheduled_from_id    INTEGER REFERENCES emr_appointments(id),
    booked_via             TEXT NOT NULL CHECK (booked_via IN ('phone', 'desk')),  -- 예약 경로. desk = 내원 종료 때 데스크에서 잡은 다음 회차
    confirmed              INTEGER NOT NULL DEFAULT 0,
    created_at             TEXT NOT NULL,
    cancelled_at           TEXT,
    chair_no               INTEGER,  -- 체어 번호(§1). 상담은 NULL
    plan_item_id           INTEGER REFERENCES emr_treatment_plan_items(id)  -- 이 예약이 이행하는 치료계획 항목. 위생·리콜은 NULL
);

-- 치료계획(§2.3). 만든 날·만든 예약 + 항목. 완료 시각은 항목의 completed 예약에서 파생
CREATE TABLE emr_treatment_plans (
    id                     INTEGER PRIMARY KEY,
    patient_id             INTEGER NOT NULL REFERENCES emr_patients(id),
    created_at             TEXT NOT NULL,
    created_appointment_id INTEGER NOT NULL REFERENCES emr_appointments(id)
);

CREATE TABLE emr_treatment_plan_items (
    id             INTEGER PRIMARY KEY,
    plan_id        INTEGER NOT NULL REFERENCES emr_treatment_plans(id),
    seq            INTEGER NOT NULL,
    procedure_code TEXT NOT NULL REFERENCES emr_procedures(code),
    tooth_no       TEXT NOT NULL,  -- 치아 단위가 아닌 시술은 '00'
    est_fee        INTEGER NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('open', 'done', 'replaced')),
    UNIQUE (plan_id, seq)
);

CREATE TABLE emr_treatments (
    id             INTEGER PRIMARY KEY,
    appointment_id INTEGER NOT NULL REFERENCES emr_appointments(id),
    patient_id     INTEGER NOT NULL REFERENCES emr_patients(id),
    procedure_code TEXT NOT NULL REFERENCES emr_procedures(code),
    tooth_no       TEXT NOT NULL,  -- 치아 단위가 아닌 시술은 '00' 센티널
    performed_at   TEXT NOT NULL,
    fee_total      INTEGER NOT NULL,
    insured_amount INTEGER NOT NULL,
    patient_amount INTEGER NOT NULL
);

CREATE TABLE emr_payments (
    id             INTEGER PRIMARY KEY,
    patient_id     INTEGER NOT NULL REFERENCES emr_patients(id),
    appointment_id INTEGER REFERENCES emr_appointments(id),
    kind           TEXT NOT NULL CHECK (kind IN ('visit', 'settlement')),
    amount_due     INTEGER NOT NULL,
    amount_paid    INTEGER NOT NULL,
    paid_at        TEXT NOT NULL,
    method         TEXT NOT NULL CHECK (method IN ('card', 'cash', 'transfer')),
    approval_no    TEXT  -- 연동 단말기 카드 승인번호(§2.4). 손으로 입력한 수납은 NULL
);

CREATE TABLE emr_claims (
    id             INTEGER PRIMARY KEY,
    treatment_id   INTEGER NOT NULL REFERENCES emr_treatments(id),
    patient_id     INTEGER NOT NULL REFERENCES emr_patients(id),
    chart_no       TEXT NOT NULL,
    tooth_no       TEXT NOT NULL,
    claim_month    TEXT NOT NULL,
    service_date   TEXT NOT NULL,
    procedure_code TEXT NOT NULL REFERENCES emr_procedures(code),
    claim_amount   INTEGER NOT NULL,
    submitted_at   TEXT,
    status         TEXT NOT NULL CHECK (status IN ('pending', 'submitted', 'paid', 'adjusted', 'denied')),
    paid_amount    INTEGER,
    adjust_code    TEXT,  -- 심평원 조정 사유 범주 문자(B·C·AK·M 등 — master.ADJUST_REASONS)
    adjust_note    TEXT,  -- 일반사유 서술(상세 사유 템플릿)
    reviewed_at    TEXT   -- 심사결과 통보일(확정 월만, §2.4). 재심 기한 = + master.APPEAL_DAYS
);

-- 차트 상병 정의 (§4.3). 가상 코드 — 시술별 허용 상병은 master.PROCEDURE_DIAGNOSES에만
CREATE TABLE emr_diagnoses (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL
);

-- 차트 기록 (§4.3, 5b단계 — 서술 줄 본문은 9b단계가 LLM 서술로 덮어씀). 진료 1건당 1행.
-- 특이·디코이·요건 충족 여부는 두지 않는다(정답 누출 — truth_chart_notes에만)
CREATE TABLE emr_chart_notes (
    id           INTEGER PRIMARY KEY,
    treatment_id INTEGER NOT NULL UNIQUE REFERENCES emr_treatments(id),
    dx_code      TEXT NOT NULL REFERENCES emr_diagnoses(code),
    note         TEXT NOT NULL
);

CREATE TABLE emr_messages (
    id                     INTEGER PRIMARY KEY,
    patient_id             INTEGER NOT NULL REFERENCES emr_patients(id),
    direction              TEXT NOT NULL CHECK (direction IN ('out', 'in')),
    sent_at                TEXT NOT NULL,
    body                   TEXT NOT NULL,
    "trigger"              TEXT NOT NULL CHECK ("trigger" IN ('appt_confirm', 'reminder', 'noshow',
                               'balance', 'doc_ready', 'recall', 'reply', 'other')),
    related_appointment_id INTEGER REFERENCES emr_appointments(id)
);

CREATE INDEX ix_appt_sched_status  ON emr_appointments(scheduled_at, status);
CREATE INDEX ix_appt_patient_sched ON emr_appointments(patient_id, scheduled_at);
CREATE INDEX ix_appt_staff_sched   ON emr_appointments(staff_id, scheduled_at);
CREATE INDEX ix_appt_chair_sched   ON emr_appointments(chair_no, scheduled_at);
CREATE INDEX ix_plan_items_plan    ON emr_treatment_plan_items(plan_id);
CREATE INDEX ix_treat_patient_proc ON emr_treatments(patient_id, procedure_code);
CREATE INDEX ix_treat_appt         ON emr_treatments(appointment_id);
CREATE INDEX ix_pay_patient        ON emr_payments(patient_id);
CREATE UNIQUE INDEX ux_claims_match_key ON emr_claims(chart_no, service_date, procedure_code, tooth_no);
CREATE INDEX ix_claims_month       ON emr_claims(claim_month);
CREATE INDEX ix_msg_patient_sent   ON emr_messages(patient_id, sent_at);

-- ── fin_* (§2.7) — 재무 원본: 금융기관 내역·회계 증빙 ───────────────────────

-- 가맹점 카드 승인·취소. 취소 행은 cancel_of = 원승인번호, 금액은 음수 아님(취소 여부로 구분)
CREATE TABLE fin_card_sales (
    id          INTEGER PRIMARY KEY,
    approved_at TEXT NOT NULL,
    issuer      TEXT NOT NULL,  -- master.CARD_ISSUERS
    amount      INTEGER NOT NULL,
    approval_no TEXT NOT NULL,
    cancel_of   TEXT  -- 취소면 원승인번호
);

-- 카드사 정산 입금. (카드사, 매출일)마다 1행
CREATE TABLE fin_card_deposits (
    id           INTEGER PRIMARY KEY,
    deposited_on TEXT NOT NULL,
    issuer       TEXT NOT NULL,
    sales_date   TEXT NOT NULL,
    sales_total  INTEGER NOT NULL,
    fee          INTEGER NOT NULL,
    amount       INTEGER NOT NULL,
    UNIQUE (issuer, sales_date)
);

-- 병원 통장 입출금. 적요(memo)는 입금자명·거래처·종류
CREATE TABLE fin_bank_transactions (
    id          INTEGER PRIMARY KEY,
    occurred_at TEXT NOT NULL,
    direction   TEXT NOT NULL CHECK (direction IN ('in', 'out')),
    amount      INTEGER NOT NULL,
    memo        TEXT NOT NULL
);

-- 병원 명의 카드 사용(⑧-2). merchant = 카드 내역에 찍힌 가맹점명
CREATE TABLE fin_card_expenses (
    id       INTEGER PRIMARY KEY,
    used_at  TEXT NOT NULL,
    merchant TEXT NOT NULL,
    amount   INTEGER NOT NULL
);

-- 회계 증빙(⑧-2) — 코드 템플릿 본문
CREATE TABLE fin_expense_documents (
    id        INTEGER PRIMARY KEY,
    kind      TEXT NOT NULL CHECK (kind IN ('statement', 'tax_invoice', 'order_email', 'receipt', 'bill')),
    issuer    TEXT NOT NULL,
    issued_on TEXT NOT NULL,
    amount    INTEGER NOT NULL,
    body      TEXT NOT NULL
);

CREATE INDEX ix_card_sales_at   ON fin_card_sales(approved_at);
CREATE INDEX ix_bank_tx_at      ON fin_bank_transactions(occurred_at);

-- ── ax_* (§3) ───────────────────────────────────────────────
-- 생성기는 ax_calls(6~7단계)·ax_documents·ax_master_values만 적재. ax_chunks·ax_chunks_fts·ax_chunk_vectors는 make index(앱 색인 코드)가 채움.
-- 분류·판정·투표·LLM 로그는 앱이 쓰고 evaluation/eval.py가 읽는다 — 지금은 그 칸만(앱 설계 §1·§2), 나머지 칸·테이블은 해당 기능
-- 구현 때 추가. 검색 로그(ax_search_logs)는 eval.py가 읽지 않는다 — 챗 채점은 앱 chat()의 반환값(evaluation/rag.py)

-- 라벨·노이즈·카테고리 컬럼을 두지 않는다 (정답 누출 — truth_call_labels에만)
CREATE TABLE ax_calls (
    id               INTEGER PRIMARY KEY,
    call_uid         TEXT NOT NULL UNIQUE,  -- 'c-0481' 시드 셔플 순번
    source           TEXT NOT NULL CHECK (source IN ('generated')),  -- 붙여넣은 통화는 앱 DB(앱 설계 §1)
    patient_id       INTEGER REFERENCES emr_patients(id),
    caller_phone     TEXT,
    caller_name      TEXT,
    direction        TEXT NOT NULL CHECK (direction IN ('inbound', 'outbound')),
    started_at       TEXT NOT NULL,
    duration_sec     INTEGER NOT NULL,
    has_transcript   INTEGER NOT NULL DEFAULT 0,
    transcript       TEXT,
    in_eval_pool     INTEGER NOT NULL DEFAULT 0,
    eval_sort_order  INTEGER,
    in_blind_pool    INTEGER NOT NULL DEFAULT 0,
    blind_sort_order INTEGER
);

CREATE INDEX ix_calls_blind   ON ax_calls(in_blind_pool, blind_sort_order);
CREATE INDEX ix_calls_started ON ax_calls(started_at);

-- backend가 읽는 master 값(마지막 단계) — 원본은 generator/master.py, 이름 → JSON(④ 주의 칩 기준표 등)
CREATE TABLE ax_master_values (
    name  TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- RAG 문서 (9단계). 방해 여부·심은 사실은 truth_doc_meta에만. 방해 문서의 type은 짝과 같은 값
CREATE TABLE ax_documents (
    id             INTEGER PRIMARY KEY,
    type           TEXT NOT NULL CHECK (type IN ('manual', 'faq', 'insurance_guide', 'claim_standard', 'notice',
                                                 'education', 'external')),
    title          TEXT NOT NULL,
    version        TEXT NOT NULL,
    effective_date TEXT,
    path           TEXT NOT NULL,
    body           TEXT NOT NULL
);
