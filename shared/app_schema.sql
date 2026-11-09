-- app.sqlite 스키마 — 앱이 쓰는 ax_*(앱 설계 §1·§2·§3b·§6). 생성 데이터는 source.sqlite(shared/source_schema.sql)를 읽기 전용으로 붙여 읽는다.
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
    created_at    TEXT NOT NULL,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd      REAL NOT NULL DEFAULT 0,  -- 일일 상한이 읽는다
    latency_ms    INTEGER NOT NULL DEFAULT 0,
    error         TEXT,
    ref_type      TEXT,
    ref_id        TEXT
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
    llm_call_id    INTEGER NOT NULL REFERENCES ax_llm_calls(id),  -- 라벨 호출. 폴백 여부를 여기서 읽는다
    summary_model_actual TEXT,  -- 요약·할 일 호출(분류기 설계 "분류기")
    summary_llm_call_id  INTEGER REFERENCES ax_llm_calls(id),
    UNIQUE (call_uid, model_alias, prompt_version)
);

-- 판정기(모델 B) 판정 (앱 설계 §1). 사람 판정과 섞지 않으려고 테이블을 나눈다. 칸은 분류와 같다
CREATE TABLE ax_model_judgments (
    id             INTEGER PRIMARY KEY,
    call_uid       TEXT NOT NULL,  -- source.sqlite ax_calls.call_uid
    model_alias    TEXT NOT NULL,
    model_actual   TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('ok', 'failed')),
    labels         TEXT,
    llm_call_id    INTEGER NOT NULL REFERENCES ax_llm_calls(id),  -- 폴백 여부를 여기서 읽는다
    UNIQUE (call_uid, model_alias, prompt_version)
);

-- 분류기 vs 판정기 엇갈림 (앱 설계 §1). 9항목 중 하나라도 다름, 어느 쪽이든 폴백, 분류기 할 일 목록이 있는데
-- has_todo = 0 중 하나면 1행(backend/disagreements.py)
CREATE TABLE ax_call_disagreements (
    call_uid      TEXT NOT NULL,  -- source.sqlite ax_calls.call_uid
    a_version     TEXT NOT NULL,  -- 분류기 prompt_version
    b_version     TEXT NOT NULL,  -- 판정기 prompt_version
    is_core       INTEGER NOT NULL,  -- 목적·급함·응급 중 하나라도 다름
    fallback      INTEGER NOT NULL,  -- 라벨·판정 호출 중 하나라도 폴백 — 자동 종결하지 않는다
    todo_mismatch INTEGER NOT NULL,  -- 할 일 목록이 있는데 분류기 has_todo = 0
    items         TEXT NOT NULL,  -- 다른 항목 이름 JSON 목록
    PRIMARY KEY (call_uid, a_version, b_version)
);

-- 새로 추가한 통화(통화 화면 "새 통화 추가", 앱 설계 §1) — 앱이 만든 통화라 앱 DB. call_uid는 p-0001부터.
-- 시각은 기준일 날짜 + 추가한 시각, 번호가 EMR 환자와 맞으면 patient_id(화면 표시에만 — 분류기 입력은 녹취록·방향뿐)
CREATE TABLE ax_pasted_calls (
    id           INTEGER PRIMARY KEY,
    call_uid     TEXT NOT NULL UNIQUE,
    direction    TEXT NOT NULL CHECK (direction IN ('inbound', 'outbound')),
    started_at   TEXT NOT NULL,
    transcript   TEXT NOT NULL,
    caller_phone TEXT,
    patient_id   INTEGER,  -- source.sqlite emr_patients.id
    added_by     TEXT NOT NULL,  -- 담당 파트
    added_at     TEXT NOT NULL  -- 실제 시계
);

-- 판정 큐 상태(앱 설계 §6 "판정 큐 다음 건") — 엇갈림·표본이 정해질 때 pending으로 채운다
CREATE TABLE ax_judge_state (
    call_uid   TEXT PRIMARY KEY,
    state      TEXT NOT NULL CHECK (state IN ('pending', 'skipped', 'done')),
    updated_at TEXT NOT NULL
);

-- 할 일 체크(통화 화면 "오늘") — 분류 행의 todo 항목 번호(0부터) 단위. 끝낸 파트·실제 시계
CREATE TABLE ax_todo_checks (
    classification_id INTEGER NOT NULL REFERENCES ax_call_classifications(id),
    item              INTEGER NOT NULL,
    done_by           TEXT NOT NULL,
    done_at           TEXT NOT NULL,
    PRIMARY KEY (classification_id, item)
);

-- 사람 판정(운영 구간 엇갈림·표본, 평가 설계 §2.3) — 운영 감사라 eval.py는 읽지 않는다. 통화당 1행(재판정은 덮어씀). judged_at은 실제 시계
CREATE TABLE ax_judgments (
    id        INTEGER PRIMARY KEY,
    call_uid  TEXT NOT NULL UNIQUE,  -- source.sqlite ax_calls.call_uid
    labels    TEXT NOT NULL,
    note      TEXT,  -- 메모(선택, 평가 설계 §2.3)
    judged_by TEXT NOT NULL,
    judged_at TEXT NOT NULL
);

-- LLM 쌍 비교 (평가 설계 §2.4). 좌·우 = 첫 판정의 배치. second_choice는 좌우를 바꿔 물은 답을 첫 배치로 되돌린 값,
-- choice = 둘이 같으면 그 값, 다르면 tie
CREATE TABLE ax_model_blind_votes (
    id                       INTEGER PRIMARY KEY,
    left_classification_id   INTEGER NOT NULL REFERENCES ax_call_classifications(id),
    right_classification_id  INTEGER NOT NULL REFERENCES ax_call_classifications(id),
    model_alias              TEXT NOT NULL,
    model_actual             TEXT NOT NULL,
    prompt_version           TEXT NOT NULL,
    first_choice             TEXT NOT NULL CHECK (first_choice IN ('left', 'right', 'tie', 'both_wrong')),
    second_choice            TEXT NOT NULL CHECK (second_choice IN ('left', 'right', 'tie', 'both_wrong')),
    choice                   TEXT NOT NULL CHECK (choice IN ('left', 'right', 'tie', 'both_wrong')),
    first_llm_call_id        INTEGER NOT NULL REFERENCES ax_llm_calls(id),  -- 둘 중 하나라도 폴백이면 집계에서 뺀다
    second_llm_call_id       INTEGER NOT NULL REFERENCES ax_llm_calls(id),
    created_at               TEXT NOT NULL,
    UNIQUE (left_classification_id, right_classification_id, model_alias, prompt_version)
);

-- 사람 블라인드 투표 — dev 블라인드는 blind_versions 쌍, test 블라인드는 최종 비교 때만 (평가 설계 §2.4). 좌·우 = 화면 배치
CREATE TABLE ax_blind_votes (
    id                       INTEGER PRIMARY KEY,
    left_classification_id   INTEGER NOT NULL REFERENCES ax_call_classifications(id),
    right_classification_id  INTEGER NOT NULL REFERENCES ax_call_classifications(id),
    choice                   TEXT NOT NULL CHECK (choice IN ('left', 'right', 'tie', 'both_wrong')),
    voted_by                 TEXT NOT NULL,
    voted_at                 TEXT NOT NULL,
    UNIQUE (left_classification_id, right_classification_id)
);

-- ── 에이전트 하네스 (앱 설계 §3b) ─────────────────────────────────────────

CREATE TABLE ax_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL  -- JSON
);

-- 후속 에이전트 처리안(기능 정의 §4 ①) — 분류 행 단위(분류 버전이 바뀌면 새 처리안). 실패면 action 없이 이유
CREATE TABLE ax_call_followups (
    id                INTEGER PRIMARY KEY,
    call_uid          TEXT NOT NULL,
    classification_id INTEGER NOT NULL UNIQUE REFERENCES ax_call_classifications(id),
    action            TEXT,  -- shared/followup.py ACTIONS
    summary           TEXT,
    facts             TEXT NOT NULL DEFAULT '[]',  -- 근거 문장 JSON
    run_id            INTEGER,  -- ax_agent_runs.id(실행 기록은 다시 만들 수 있어 외래 키 없음)
    status            TEXT NOT NULL CHECK (status IN ('done', 'failed')),
    reason            TEXT,
    created_at        TEXT NOT NULL
);

CREATE TABLE ax_agent_runs (
    id             INTEGER PRIMARY KEY,
    agent          TEXT NOT NULL,
    target         TEXT NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('running', 'done', 'handed_off', 'failed')),
    reason         TEXT,  -- 사람에게 넘긴 이유·실패 이유
    result         TEXT,  -- 모델의 마지막 답
    prompt_version TEXT NOT NULL,  -- 재개해도 처음 값
    parent_run_id  INTEGER REFERENCES ax_agent_runs(id),  -- 운영 에이전트가 부른 경우
    model_calls    INTEGER NOT NULL DEFAULT 0,  -- 재개해도 누적
    tool_calls     INTEGER NOT NULL DEFAULT 0,
    cost_usd       REAL NOT NULL DEFAULT 0,
    locked_by      TEXT,
    heartbeat_at   TEXT,
    started_at     TEXT NOT NULL,
    finished_at    TEXT
);
-- 같은 대상의 실행은 겹치지 않는다
CREATE UNIQUE INDEX ax_agent_runs_one_running ON ax_agent_runs (agent, target) WHERE status = 'running';

CREATE TABLE ax_agent_steps (
    id          INTEGER PRIMARY KEY,
    run_id      INTEGER NOT NULL REFERENCES ax_agent_runs(id),
    seq         INTEGER NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('input', 'model', 'tool')),
    tool_use_id TEXT,
    tool        TEXT,
    args        TEXT,  -- 정규화된 인자 JSON
    response    TEXT,  -- 모델 응답 JSON
    result      TEXT,  -- 모델이 받은 도구 결과 전체 JSON(재개용)
    raw_result  TEXT,  -- 실행 뒤 막힌 건의 원래 결과 JSON(재생용)
    hook_status TEXT CHECK (hook_status IN ('pass', 'blocked')),
    rule_id     TEXT,  -- 막은 규칙
    is_error    INTEGER NOT NULL DEFAULT 0,
    truncated   INTEGER NOT NULL DEFAULT 0,  -- 모델에 줄 때 잘렸는지
    created_at  TEXT NOT NULL,
    UNIQUE (run_id, seq)
);

CREATE TABLE ax_auto_actions (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,
    target      TEXT NOT NULL,
    period      TEXT NOT NULL DEFAULT '',  -- 반복되는 일은 기간이 키
    result      TEXT NOT NULL,  -- 결과값 JSON
    reason      TEXT NOT NULL,
    run_id      INTEGER,  -- 코드 경로면 NULL. 실행 기록은 다시 만들므로 외래 키 없음(앱 설계 §7.3)
    reverted_by TEXT,
    reverted_at TEXT,
    created_at  TEXT NOT NULL,
    UNIQUE (kind, target, period)
);

-- 검색·챗 1회 = 1행 (앱 설계 §3). running은 실행 중 — 실행 target이 이 행 id라 먼저 넣는다
CREATE TABLE ax_search_logs (
    id            INTEGER PRIMARY KEY,
    kind          TEXT NOT NULL CHECK (kind IN ('chat', 'search_summary')),
    question      TEXT NOT NULL,
    tool_calls    TEXT NOT NULL DEFAULT '[]',  -- [{name, args}] 정규화 인자, answer 제외
    retrieved_ids TEXT NOT NULL DEFAULT '[]',
    answer_text   TEXT,
    cited_ids     TEXT NOT NULL DEFAULT '[]',
    status        TEXT NOT NULL CHECK (status IN ('running', 'answered', 'refused', 'cite_failed',
                      'number_failed', 'call_limit', 'cost_limit', 'budget', 'failed')),
    refuse_reason TEXT,
    run_id        INTEGER,  -- ax_agent_runs.id
    fallback_used INTEGER NOT NULL DEFAULT 0,
    is_eval       INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);

-- 규칙 id별 차단 누계 — 실행 기록을 다시 만들어도 남는다(앱 설계 §7.3)
CREATE TABLE ax_rule_counts (
    rule_id TEXT PRIMARY KEY,
    blocked INTEGER NOT NULL DEFAULT 0
);

-- ── 검색 색인 (앱 설계 §3) ───────────────────────────────────────────────

CREATE TABLE ax_chunks (
    id          TEXT PRIMARY KEY,  -- 출처에서 결정적으로 'doc:12:3' / 'call:c-0481:0' / 'thread:C-00001:2026-07' / 'chart:123'
    source_type TEXT NOT NULL CHECK (source_type IN ('document', 'call', 'message_thread', 'chart_note')),
    text        TEXT NOT NULL,  -- 출처 prefix 포함
    chart_no    TEXT,
    event_date  TEXT,
    doc_type    TEXT
);
CREATE INDEX ix_chunks_filter ON ax_chunks (source_type, event_date, chart_no);

CREATE VIRTUAL TABLE ax_chunks_fts USING fts5(chunk_id UNINDEXED, text, tokenize = 'unicode61');

-- 임베딩 모델마다 한 벌 — 모델을 비교할 때 다시 계산하지 않게. embedding은 float32 BLOB
CREATE TABLE ax_chunk_vectors (
    chunk_id  TEXT NOT NULL REFERENCES ax_chunks(id),
    model     TEXT NOT NULL,
    embedding BLOB NOT NULL,
    PRIMARY KEY (chunk_id, model)
) WITHOUT ROWID;
