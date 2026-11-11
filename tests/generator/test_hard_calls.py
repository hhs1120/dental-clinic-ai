"""① 어려운 통화 평가셋(어려운 통화 설계 docs/superpowers/specs/2026-11-11-hard-calls-design.md) — hc- 통화가 기존 c- 통화
검사·평가 경로에 섞이지 않는지, hard 분할·truth_hard_calls 표"""

import pytest

from tests.generator.test_verify_calls import CALL_CHECKS
from tests.generator.verify_db import CTX, make_db

HC_FACT = '{"call_date": "2026-10-20", "topic_procedure": "미백"}'


def _add_hard_call(conn, started_at="2026-10-20 10:00:00"):
    """풀 밖 hc- 통화 한 건 — 행은 기존 뒤에 붙고(id 5) 시각은 기존보다 앞설 수 있다(설계 §3.1)"""
    conn.execute(
        "INSERT INTO ax_calls (id, call_uid, source, patient_id, caller_phone, caller_name, direction,"
        " started_at, duration_sec, has_transcript, transcript) VALUES"
        " (5, 'hc-0001', 'generated', NULL, '010-0000-8888', '이영희', 'inbound', ?, 140, 1,"
        " '환자: 미백 비용이 궁금해서요.\n직원: 상담 뒤 안내드려요.')",
        (started_at,),
    )
    conn.execute(
        "INSERT INTO truth.truth_call_labels VALUES ('hc-0001', 'price_q', '[\"price_q\"]',"
        " 'new_caller', 'normal', 0, 0, 1, 0, 0, 0, NULL, ?, 'hard')",
        (HC_FACT,),
    )
    conn.execute(
        "INSERT INTO truth.truth_hard_calls VALUES ('hc-0001', 'indirect', NULL, 'generated')"
    )


def test_hard_call_does_not_break_call_checks(tmp_path):
    """c- 규칙(통화 수·행 순서·판정 풀 silver 등)은 hc-를 세지 않는다 — hc-는 자기 검사가 본다"""
    conn = make_db(tmp_path)
    _add_hard_call(conn)
    assert {c.__name__: c(conn, CTX) for c in CALL_CHECKS if c(conn, CTX)} == {}


def test_hard_split_and_table_constraints(tmp_path):
    conn = make_db(tmp_path)
    _add_hard_call(conn)
    with pytest.raises(Exception, match="CHECK"):
        conn.execute(
            "INSERT INTO truth.truth_hard_calls VALUES ('hc-0002', 'indirect', NULL, 'guess')"
        )
    with pytest.raises(Exception, match="CHECK"):
        conn.execute(
            "UPDATE truth.truth_call_labels SET split = 'gold' WHERE call_uid = 'hc-0001'"
        )
