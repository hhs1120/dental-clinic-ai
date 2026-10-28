-- app.sqlite 스키마 — 앱이 쓰는 ax_*(앱 설계 §1·§2). 생성 데이터는 source.sqlite(shared/source_schema.sql)를 읽기 전용으로 붙여 읽는다.
-- 생성 통화는 call_uid로 가리킨다 — 붙인 DB로는 외래 키를 걸 수 없다(앱 설계 §1)
-- truth_* 는 여기 두지 않는다 (평가 설계 §6, ground_truth/)
-- 시각은 naive KST ISO 문자열('2026-07-03 10:30:00'), 날짜는 'YYYY-MM-DD'

-- ── eval.py가 읽는 칸 — 나머지 칸·테이블은 앱 착수 때 추가 ─────────────────
-- labels JSON = 9항목 {primary_purpose, counterpart, urgency, emergency, complaint, price_inquiry, wants_appt,
-- appt_confirmed, has_todo}(enum은 generator/master.py CALL_*), 플래그는 0/1

-- LLM 게이트웨이 호출 1건 = 1행. fallback_used면 평가 집계에서 제외 (평가 설계 §1 원칙 5)
CREATE TABLE ax_llm_calls (
    id            INTEGER PRIMARY KEY,
    task          TEXT NOT NULL,
    model_alias   TEXT NOT NULL,
    model_actual  TEXT NOT NULL,
    prompt_version TEXT,
    fallback_used INTEGER NOT NULL DEFAULT 0,
    ok            INTEGER NOT NULL,
    is_eval       INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);

CREATE TABLE ax_call_classifications (
    id             INTEGER PRIMARY KEY,
    call_uid       TEXT NOT NULL,  -- source.sqlite ax_calls.call_uid
    model_alias    TEXT NOT NULL,
    model_actual   TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('ok', 'failed')),
    labels         TEXT,
    summary        TEXT,
    todo           TEXT,
    llm_call_id    INTEGER NOT NULL REFERENCES ax_llm_calls(id),  -- 폴백 여부를 여기서 읽는다
    UNIQUE (call_uid, model_alias, prompt_version)
);

-- 사람 판정. 통화당 1행(재판정은 덮어씀). judged_at은 실제 시계
CREATE TABLE ax_judgments (
    id        INTEGER PRIMARY KEY,
    call_uid  TEXT NOT NULL UNIQUE,  -- source.sqlite ax_calls.call_uid
    labels    TEXT NOT NULL,
    note      TEXT,  -- 메모(선택, 평가 설계 §2.3)
    judged_by TEXT NOT NULL,
    judged_at TEXT NOT NULL
);

-- 블라인드 투표 (평가 설계 §2.4). 좌·우 = 화면 배치
CREATE TABLE ax_blind_votes (
    id                       INTEGER PRIMARY KEY,
    left_classification_id   INTEGER NOT NULL REFERENCES ax_call_classifications(id),
    right_classification_id  INTEGER NOT NULL REFERENCES ax_call_classifications(id),
    choice                   TEXT NOT NULL CHECK (choice IN ('left', 'right', 'tie', 'both_wrong')),
    voted_by                 TEXT NOT NULL,
    voted_at                 TEXT NOT NULL,
    UNIQUE (left_classification_id, right_classification_id)
);
