-- app.sqlite 스키마 (emr_* / ax_*). 생성기와 앱의 단일 원본.
-- 정의: emr_*·생성기 적재 ax_*(ax_calls·ax_documents)는 docs/치과 AX - 가상 데이터 생성 설계.md §2·§3,
--       나머지 ax_*는 docs/치과 AX - 앱 설계.md
-- truth_* 는 여기 두지 않는다 (평가 설계 §6, ground_truth/)
-- 시각은 naive KST ISO 문자열('2026-07-03 10:30:00'), 날짜는 'YYYY-MM-DD'

-- ── emr_* (§2) ──────────────────────────────────────────────

CREATE TABLE emr_clinic (
    name       TEXT NOT NULL,
    chairs     INTEGER NOT NULL,
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
    typical_sessions   INTEGER NOT NULL DEFAULT 1,
    insured_limit      INTEGER,
    limit_scope        TEXT,
    age_group          TEXT CHECK (age_group IN ('child', 'adult', 'senior')),
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
    recall_cycle_months    INTEGER NOT NULL DEFAULT 6,
    has_private_insurance  INTEGER NOT NULL DEFAULT 0,
    note                   TEXT
);

CREATE TABLE emr_appointments (
    id                     INTEGER PRIMARY KEY,
    patient_id             INTEGER NOT NULL REFERENCES emr_patients(id),
    staff_id               INTEGER NOT NULL REFERENCES emr_staff(id),
    chair_no               INTEGER,  -- 상담은 NULL (체어 불필요)
    scheduled_at           TEXT NOT NULL,
    duration_min           INTEGER NOT NULL,
    type                   TEXT NOT NULL CHECK (type IN ('treatment', 'hygiene', 'consult')),
    planned_procedure_code TEXT REFERENCES emr_procedures(code),
    status                 TEXT NOT NULL CHECK (status IN ('scheduled', 'completed', 'cancelled', 'noshow')),
    rescheduled_from_id    INTEGER REFERENCES emr_appointments(id),
    confirmed              INTEGER NOT NULL DEFAULT 0,
    created_at             TEXT NOT NULL,
    cancelled_at           TEXT
);

CREATE TABLE emr_treatments (
    id             INTEGER PRIMARY KEY,
    appointment_id INTEGER NOT NULL REFERENCES emr_appointments(id),
    patient_id     INTEGER NOT NULL REFERENCES emr_patients(id),
    procedure_code TEXT NOT NULL REFERENCES emr_procedures(code),
    tooth_no       TEXT NOT NULL,  -- 전악은 '00' 센티널
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
    method         TEXT NOT NULL CHECK (method IN ('card', 'cash', 'transfer'))
);

CREATE TABLE emr_insurance_usage (
    patient_id INTEGER NOT NULL REFERENCES emr_patients(id),
    benefit    TEXT NOT NULL,
    year       INTEGER NOT NULL,
    used       INTEGER NOT NULL,
    "limit"    INTEGER NOT NULL,
    PRIMARY KEY (patient_id, benefit, year)
);

CREATE TABLE emr_claims (
    id             INTEGER PRIMARY KEY,
    treatment_id   INTEGER NOT NULL REFERENCES emr_treatments(id),
    patient_id     INTEGER NOT NULL REFERENCES emr_patients(id),
    chart_no       TEXT NOT NULL,
    tooth_no       TEXT NOT NULL,
    claim_month    TEXT NOT NULL,
    batch_type     TEXT NOT NULL CHECK (batch_type IN ('monthly', 'supplementary')),
    service_date   TEXT NOT NULL,
    procedure_code TEXT NOT NULL REFERENCES emr_procedures(code),
    claim_amount   INTEGER NOT NULL,
    submitted_at   TEXT,
    status         TEXT NOT NULL CHECK (status IN ('pending', 'submitted', 'paid', 'adjusted', 'denied')),
    paid_amount    INTEGER,
    adjust_code    TEXT
);

CREATE TABLE emr_messages (
    id                     INTEGER PRIMARY KEY,
    patient_id             INTEGER NOT NULL REFERENCES emr_patients(id),
    direction              TEXT NOT NULL CHECK (direction IN ('out', 'in')),
    sent_at                TEXT NOT NULL,
    body                   TEXT NOT NULL,
    "trigger"              TEXT NOT NULL CHECK ("trigger" IN ('appt_confirm', 'reminder', 'noshow',
                               'balance', 'doc_ready', 'reply', 'other')),
    related_appointment_id INTEGER REFERENCES emr_appointments(id)
);

CREATE INDEX ix_appt_sched_status  ON emr_appointments(scheduled_at, status);
CREATE INDEX ix_appt_patient_sched ON emr_appointments(patient_id, scheduled_at);
CREATE INDEX ix_appt_staff_sched   ON emr_appointments(staff_id, scheduled_at);
CREATE INDEX ix_treat_patient_proc ON emr_treatments(patient_id, procedure_code);
CREATE INDEX ix_treat_appt         ON emr_treatments(appointment_id);
CREATE INDEX ix_pay_patient        ON emr_payments(patient_id);
CREATE UNIQUE INDEX ux_claims_match_key ON emr_claims(chart_no, service_date, procedure_code, tooth_no);
CREATE INDEX ix_claims_month       ON emr_claims(claim_month);
CREATE INDEX ix_msg_patient_sent   ON emr_messages(patient_id, sent_at);

-- ── ax_* (§3) ───────────────────────────────────────────────
-- 생성기는 ax_calls(6~7단계)·ax_documents만 적재. ax_chunks는 make index(앱 색인 코드)가 채움.
-- 나머지는 해당 기능 구현 때 추가

-- 라벨·노이즈·카테고리 컬럼을 두지 않는다 (정답 누출 — truth_call_labels에만)
CREATE TABLE ax_calls (
    id               INTEGER PRIMARY KEY,
    call_uid         TEXT NOT NULL UNIQUE,  -- 'c-0481' 시드 셔플 순번
    source           TEXT NOT NULL CHECK (source IN ('generated', 'pasted', 'sample_audio', 'uploaded')),
    patient_id       INTEGER REFERENCES emr_patients(id),
    caller_phone     TEXT,
    caller_name      TEXT,
    direction        TEXT NOT NULL CHECK (direction IN ('inbound', 'outbound')),
    started_at       TEXT NOT NULL,
    duration_sec     INTEGER NOT NULL,
    has_transcript   INTEGER NOT NULL DEFAULT 0,
    audio_path       TEXT,
    transcript       TEXT,
    in_eval_pool     INTEGER NOT NULL DEFAULT 0,
    eval_sort_order  INTEGER,
    judge_status     TEXT NOT NULL DEFAULT 'none' CHECK (judge_status IN ('none', 'pending', 'skipped', 'done')),
    in_blind_pool    INTEGER NOT NULL DEFAULT 0,
    blind_sort_order INTEGER,
    blind_status     TEXT NOT NULL DEFAULT 'none' CHECK (blind_status IN ('none', 'pending', 'done')),
    indexed_at       TEXT
);

CREATE INDEX ix_calls_judge   ON ax_calls(judge_status, eval_sort_order);
CREATE INDEX ix_calls_blind   ON ax_calls(blind_status, blind_sort_order);
CREATE INDEX ix_calls_started ON ax_calls(started_at);
