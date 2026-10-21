-- truth.sqlite 스키마 (정답 저장소). 앱은 이 파일·DB를 모른다.
-- 정의: docs/치과 AX - 평가 설계.md §6. 생성 규칙: 가상 데이터 생성 설계 §2.4
-- 테이블은 그 단계를 구현할 때 추가한다

-- 9~10월 청구 심사 결과 + 장부에 없는 통보서 줄(extra_in_eob). EOB 슬롯 착수 때 채움 — 그 전에는 비어 있음
CREATE TABLE truth_claims (
    id             INTEGER PRIMARY KEY,
    claim_id       INTEGER UNIQUE,  -- app.sqlite emr_claims.id (다른 DB라 FK 없음). extra_in_eob는 NULL
    chart_no       TEXT NOT NULL,
    service_date   TEXT NOT NULL,
    procedure_code TEXT NOT NULL,
    tooth_no       TEXT NOT NULL,
    claim_amount   INTEGER NOT NULL,
    outcome        TEXT NOT NULL CHECK (outcome IN ('paid_full', 'adjusted', 'denied',
                       'missing_in_eob', 'extra_in_eob')),
    paid_amount    INTEGER,
    adjust_code    TEXT,
    CHECK ((outcome = 'extra_in_eob') = (claim_id IS NULL))
);

CREATE UNIQUE INDEX ux_truth_claims_match_key ON truth_claims(chart_no, service_date, procedure_code, tooth_no);

-- 생성 설정 기록 (seed·scale·range_start·range_end). verify가 규모별 기간을 여기서 읽는다.
-- 생성 시각은 넣지 않는다 (같은 시드 2회 해시 비교)
CREATE TABLE truth_gen_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
