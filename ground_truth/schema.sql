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

-- 생성 설정 기록 (seed·scale·range_start·range_end). verify가 규모별 기간을 여기서 읽는다.
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
