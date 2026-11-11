-- truth.sqlite 스키마 (정답 저장소). 앱은 이 파일·DB를 모른다.
-- 정의: docs/치과 AX - 평가 설계.md §6. 생성 규칙: 가상 데이터 생성 설계 §2.4
-- 테이블은 그 단계를 구현할 때 추가한다

-- 생성 설정 기록 (키 목록은 generator/build.py — 기간·통화·문서 건수·연동 실패일 수·장부 실수·불일치 건수). verify가 검사하는 DB의 기간·건수를 여기서 읽는다
-- (verify 테스트 DB는 config와 값이 다름).
-- 생성 시각은 넣지 않는다 (같은 시드 2회 해시 비교)
CREATE TABLE truth_gen_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 차트 정답 (5b단계, 평가 설계 §6). kind = template/narrative, 특이 = 청구 오류 줄 ∪ 임상 요건형 시술 줄,
-- requirement = 요건 사유 키(master.ADJUST_REASONS, 요건 줄만), evidence_present = 차트에 요건 근거가 있음(요건 줄만),
-- facts = 5b가 정한 사실(JSON) — 9b 서술의 입력이자 건별 검증 기준
-- 예약 → 코스(4단계 수요 에피소드). 앱 DB에는 두지 않는다(회차 추적 없음) — 5b R01(다른 코스의 재시행)과 verify가 읽는다.
-- 창 이전 내원처럼 에피소드가 없는 예약은 행이 없다
CREATE TABLE truth_appointment_courses (
    appointment_id INTEGER PRIMARY KEY,
    course_id      INTEGER NOT NULL
);

CREATE TABLE truth_chart_notes (
    treatment_id     INTEGER PRIMARY KEY,  -- source.sqlite emr_treatments.id
    kind             TEXT NOT NULL CHECK (kind IN ('template', 'narrative')),
    is_special       INTEGER NOT NULL,
    requirement      TEXT,
    evidence_present INTEGER,
    is_decoy         INTEGER NOT NULL,
    facts            TEXT NOT NULL
);

-- 청구 오류 정답 (5b단계, 생성 설계 §2.4·§4.3). 진료 1건당 최대 1개. requirement는 임상 요건형 오류의 요건 사유 키
CREATE TABLE truth_claim_errors (
    treatment_id         INTEGER PRIMARY KEY,  -- source.sqlite emr_treatments.id
    kind                 TEXT NOT NULL CHECK (kind IN ('diagnosis', 'requirement', 'omitted_extra')),
    requirement          TEXT,
    extra_procedure_code TEXT,  -- 추가 처치 누락(omitted_extra): 차트에만 있는 처치
    extra_tooth_no       TEXT,
    CHECK ((kind = 'requirement') = (requirement IS NOT NULL)),
    CHECK ((kind = 'omitted_extra') = (extra_procedure_code IS NOT NULL AND extra_tooth_no IS NOT NULL))
);

-- 다른 치과 이용(⑨ 정답, 가상 데이터 생성 설계 §2.2). churn = 이탈(이탈일부터 응하지 않음), parallel = 병행
CREATE TABLE truth_patient_churn (
    patient_id INTEGER PRIMARY KEY,  -- source.sqlite emr_patients.id
    kind       TEXT NOT NULL CHECK (kind IN ('churn', 'parallel')),
    churned_on TEXT,
    CHECK ((kind = 'churn') = (churned_on IS NOT NULL))
);

-- 장부 실수(⑧-1 장부 쪽 원인, §2.4). payment_id = 실수가 들어간 수납 행, 실제 값은 true_*
CREATE TABLE truth_ledger_errors (
    id             INTEGER PRIMARY KEY,
    kind           TEXT NOT NULL CHECK (kind IN ('entry_missing', 'amount_typo', 'date_shift', 'method_wrong')),
    payment_id     INTEGER,  -- source.sqlite emr_payments.id — NULL = 장부에 행이 아예 없는 누락(미수 정산 수납, 8b)
    true_amount    INTEGER NOT NULL,
    true_paid_at   TEXT NOT NULL,
    true_method    TEXT NOT NULL CHECK (true_method IN ('card', 'cash', 'transfer')),
    patient_id     INTEGER  -- 장부에 행이 없는 누락만(나머지는 payment_id로 안다)
);

-- ⑧-1 불일치 정답(§2.7). fin_ids·payment_ids = JSON 배열
CREATE TABLE truth_recon_mismatches (
    id              INTEGER PRIMARY KEY,
    occurred_on     TEXT NOT NULL,
    cause           TEXT NOT NULL CHECK (cause IN ('split_payment', 'cancel_reapprove', 'fee_mismatch', 'deposit_delay',
                        'non_clinical_sale', 'entry_missing', 'amount_typo', 'date_shift', 'method_wrong')),
    fin_ids         TEXT NOT NULL,  -- [{"table": "fin_card_sales", "id": 12}, ...]
    payment_ids     TEXT NOT NULL,
    split           TEXT NOT NULL CHECK (split IN ('dev', 'test')),
    ledger_error_id INTEGER REFERENCES truth_ledger_errors(id)
);

-- ⑧-2 출금 → 정답 증빙·계정 과목(§2.7). document_ids = JSON 배열(비면 증빙 없음)
CREATE TABLE truth_expense_matches (
    outflow_table TEXT NOT NULL CHECK (outflow_table IN ('fin_bank_transactions', 'fin_card_expenses')),
    outflow_id    INTEGER NOT NULL,
    document_ids  TEXT NOT NULL,
    account       TEXT NOT NULL,  -- master.ACCOUNTS
    split         TEXT CHECK (split IN ('dev', 'test')),  -- 평가 달(master.recon_months) 밖은 NULL
    PRIMARY KEY (outflow_table, outflow_id)
);

-- 통화 골드 라벨 (6단계, 평가 설계 §6). 목적·상대·급함·노이즈 enum은 master.CALL_*.
-- accepted_purposes = [주 목적, 부 주제 목적](JSON), noise_type NULL = 노이즈 없음,
-- used_fact = 통화 시점 사실(JSON — 예약류는 appointment_id), split = 판정 풀 dev/test, 나머지 silver
CREATE TABLE truth_call_labels (
    call_uid          TEXT PRIMARY KEY,  -- source.sqlite ax_calls.call_uid
    primary_purpose   TEXT NOT NULL,
    accepted_purposes TEXT NOT NULL,
    counterpart       TEXT NOT NULL,
    urgency           TEXT NOT NULL,
    emergency         INTEGER NOT NULL,
    complaint         INTEGER NOT NULL,
    price_inquiry     INTEGER NOT NULL,
    wants_appt        INTEGER NOT NULL,
    appt_confirmed    INTEGER NOT NULL,
    has_todo          INTEGER NOT NULL,
    noise_type        TEXT,
    used_fact         TEXT NOT NULL,
    split             TEXT NOT NULL CHECK (split IN ('dev', 'test', 'silver'))
);

-- RAG 문서 정답 (9단계 문서 계획, 평가 설계 §6). kind = core / old_version(공지 옛 버전) / similar(비슷한 주제, 짝의 사실 없음) /
-- external(타 병원·일반). pair_doc_id = 방해 문서가 흉내 내는 문서(옛 버전은 대체한 최신 공지), supersedes_doc_id = 최신 공지가
-- 대체한 옛 버전, avoid_doc_ids = 비슷한 주제 문서가 짝 말고도 사실 말을 쓰면 안 되는 핵심 문서 JSON [doc_id].
-- planted_facts = 심은 사실 JSON [{id, text, words[], reason, same}] — words는 본문에 그대로 있는 말,
-- 청구 기준 절은 reason(master.ADJUST_REASONS 키)과 소제목('## …'), same = 같은 사실 묶음 키(문서끼리 같은 사실)
CREATE TABLE truth_doc_meta (
    doc_id            INTEGER PRIMARY KEY,  -- source.sqlite ax_documents.id
    doc_key           TEXT NOT NULL UNIQUE,  -- generator/doc_catalog.py
    kind              TEXT NOT NULL CHECK (kind IN ('core', 'old_version', 'similar', 'external')),
    is_distractor     INTEGER NOT NULL,
    pair_doc_id       INTEGER,
    supersedes_doc_id INTEGER,
    avoid_doc_ids     TEXT NOT NULL,
    planted_facts     TEXT NOT NULL,
    CHECK (is_distractor = (kind != 'core'))
);

-- RAG 질문 70개 (평가 설계 §3.1, 사람이 생성 후 작성). 원본은 ground_truth/rag_qa.json — make data가 적재한다.
-- type = manual/notice/case/data/chart/refusal. answer_doc_ids JSON [출처 id] — 'doc:<ax_documents.id>' / 'call:<ax_calls.call_uid>' /
-- 'thread:<chart_no>:<YYYY-MM>' / 'chart:<emr_treatments.id>' (청크 id에서 끝 순번을 뗀 것 — 앱 설계 §3).
-- answer_chunk_hint = 정답 문단을 가리키는 심은 사실 id(truth_doc_meta.planted_facts.id) 또는 본문 구절, 거절형은 답이 있다면 나올 말.
-- answer_sql = 정답 재계산 쿼리(source.sqlite + truth ATTACH) — 데이터형은 답 숫자 한 칸, 차트형은 treatment_id 목록
CREATE TABLE truth_rag_qa (
    id                TEXT PRIMARY KEY,
    type              TEXT NOT NULL CHECK (type IN ('manual', 'notice', 'case', 'data', 'chart', 'refusal')),
    question          TEXT NOT NULL,
    answer_text       TEXT NOT NULL,
    answer_doc_ids    TEXT NOT NULL,
    answer_chunk_hint TEXT,
    expected_tools    TEXT NOT NULL,
    expected_args     TEXT NOT NULL,
    expect_refusal    INTEGER NOT NULL,
    split             TEXT NOT NULL CHECK (split IN ('dev', 'test')),
    answer_sql        TEXT
);
