-- truth.sqlite 스키마 (정답 저장소). 앱은 이 파일·DB를 모른다.
-- 정의: docs/치과 AX - 평가 설계.md §6. 생성 규칙: 가상 데이터 생성 설계 §2.4
-- 테이블은 그 단계를 구현할 때 추가한다

-- 9~10월 청구 심사 결과 + 장부에 없는 심사결과 줄(extra_in_claim_review). 심사결과 슬롯 착수 때 채움 — 그 전에는 비어 있음
CREATE TABLE truth_claims (
    id             INTEGER PRIMARY KEY,
    claim_id       INTEGER UNIQUE,  -- app.sqlite emr_claims.id (다른 DB라 FK 없음). extra_in_claim_review는 NULL
    chart_no       TEXT NOT NULL,
    service_date   TEXT NOT NULL,
    procedure_code TEXT NOT NULL,
    tooth_no       TEXT NOT NULL,
    claim_amount   INTEGER NOT NULL,
    outcome        TEXT NOT NULL CHECK (outcome IN ('paid_full', 'adjusted', 'denied',
                       'missing_in_claim_review', 'extra_in_claim_review')),
    paid_amount    INTEGER,
    adjust_code    TEXT,
    adjust_note    TEXT,
    CHECK ((outcome = 'extra_in_claim_review') = (claim_id IS NULL))
);

CREATE UNIQUE INDEX ux_truth_claims_match_key ON truth_claims(chart_no, service_date, procedure_code, tooth_no);

-- 생성 설정 기록 (seed·range_start·range_end·call_slots·eval_pool·blind_pool). verify가 검사하는 DB의 기간·건수를 여기서 읽는다
-- (verify 테스트 DB는 config와 값이 다름).
-- 생성 시각은 넣지 않는다 (같은 시드 2회 해시 비교)
CREATE TABLE truth_gen_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 차트 정답 (5b단계, 평가 설계 §6). kind = template/narrative, 특이 = 청구 오류 줄 ∪ 임상 요건형 시술 줄,
-- requirement = 요건 사유 키(master.ADJUST_REASONS, 요건 줄만), evidence_present = 차트에 요건 근거가 있음(요건 줄만),
-- facts = 5b가 정한 사실(JSON) — 9b 서술의 입력이자 건별 검증 기준
CREATE TABLE truth_chart_notes (
    treatment_id     INTEGER PRIMARY KEY,  -- app.sqlite emr_treatments.id
    kind             TEXT NOT NULL CHECK (kind IN ('template', 'narrative')),
    is_special       INTEGER NOT NULL,
    requirement      TEXT,
    evidence_present INTEGER,
    is_decoy         INTEGER NOT NULL,
    facts            TEXT NOT NULL
);

-- 청구 오류 정답 (5b단계, 생성 설계 §2.4·§4.3). 진료 1건당 최대 1개. requirement는 임상 요건형 오류의 요건 사유 키
CREATE TABLE truth_claim_errors (
    treatment_id INTEGER PRIMARY KEY,  -- app.sqlite emr_treatments.id
    kind         TEXT NOT NULL CHECK (kind IN ('diagnosis', 'requirement')),
    requirement  TEXT,
    CHECK ((kind = 'requirement') = (requirement IS NOT NULL))
);

-- 통화 골드 라벨 (6단계, 평가 설계 §6). 목적·상대·급함·노이즈 enum은 master.CALL_*.
-- accepted_purposes = [주 목적, 부 주제 목적](JSON), noise_type NULL = 노이즈 없음,
-- used_fact = 통화 시점 사실(JSON — 예약류는 appointment_id), split = 판정 풀 dev/test, 나머지 silver
CREATE TABLE truth_call_labels (
    call_uid          TEXT PRIMARY KEY,  -- app.sqlite ax_calls.call_uid
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
-- 대체한 옛 버전. planted_facts = 심은 사실 JSON [{id, text, words[], reason}] — words는 본문에 그대로 있는 말,
-- 청구 기준 절은 reason(master.ADJUST_REASONS 키)과 소제목('## …')
CREATE TABLE truth_doc_meta (
    doc_id            INTEGER PRIMARY KEY,  -- app.sqlite ax_documents.id
    doc_key           TEXT NOT NULL UNIQUE,  -- generator/doc_catalog.py
    kind              TEXT NOT NULL CHECK (kind IN ('core', 'old_version', 'similar', 'external')),
    is_distractor     INTEGER NOT NULL,
    pair_doc_id       INTEGER,
    supersedes_doc_id INTEGER,
    planted_facts     TEXT NOT NULL,
    CHECK (is_distractor = (kind != 'core'))
);
