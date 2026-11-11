"""① 어려운 통화 평가셋(어려운 통화 설계 docs/superpowers/specs/2026-11-11-hard-calls-design.md) — hc- 통화가 기존 c- 통화
검사·평가 경로에 섞이지 않는지, hard 분할·truth_hard_calls 표"""

import sqlite3
from collections import Counter

import pytest

from generator import build, hard_calls, verify
from generator.master import (
    EVAL_RARE_PURPOSES,
    HARD_CALL_TYPES,
    HARD_CALLS_PER_TYPE,
    HARD_RARE_MIN,
    HARD_URGENCY_HALVES,
)
from generator.paths import ROOT
from tests.generator.test_verify_calls import CALL_CHECKS
from tests.generator.verify_db import CTX, make_db

CONFIG = ROOT / "config" / "generator.yaml"
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


# ── 슬롯 단계 hard_calls (설계 §3) — 가짜 LLM 생성 결과에서 검사가 통과하고, 한 곳씩 깨면 잡는다 ──

HARD_CHECKS = [
    verify.check_hard_counts,
    verify.check_hard_uids,
    verify.check_hard_window,
    verify.check_hard_purposes,
    verify.check_hard_booking_events,
    verify.check_hard_emergency_appt,
    verify.check_hard_excluded,
    verify.check_hard_not_pooled,
    verify.check_hard_urgency_labels,
]


def _open(out):
    conn = sqlite3.connect(out / "source.sqlite")
    conn.row_factory = sqlite3.Row
    conn.execute("ATTACH DATABASE ? AS truth", (str(out / "truth.sqlite"),))
    return conn, verify.load_ctx(CONFIG, conn)


def test_hard_checks_pass_on_generated(generated):
    conn, ctx = _open(generated("hard_pass")[0])
    assert {c.__name__: c(conn, ctx)[:3] for c in HARD_CHECKS if c(conn, ctx)} == {}
    assert all(c in verify.REQUIRED for c in HARD_CHECKS)


def _one(conn, sql):
    return conn.execute(sql).fetchone()[0]


BOOK_HC = (
    "SELECT l.call_uid FROM truth.truth_call_labels l WHERE l.call_uid LIKE 'hc-%'"
    " AND l.primary_purpose = 'cancel' ORDER BY l.call_uid LIMIT 1"
)
C_EVENT = (
    "SELECT json_extract(used_fact, '$.appointment_id') FROM truth.truth_call_labels"
    " WHERE call_uid LIKE 'c-%' AND primary_purpose = 'cancel' LIMIT 1"
)
BROKEN = {
    "check_hard_counts": [
        "DELETE FROM truth.truth_hard_calls WHERE call_uid = 'hc-0001'",
    ],
    "check_hard_uids": [
        "UPDATE ax_calls SET call_uid = 'hc-9999' WHERE call_uid = 'hc-0002'",
        "UPDATE truth.truth_call_labels SET call_uid = 'hc-9999' WHERE call_uid = 'hc-0002'",
        "UPDATE truth.truth_hard_calls SET call_uid = 'hc-9999' WHERE call_uid = 'hc-0002'",
    ],
    "check_hard_window": [
        "UPDATE ax_calls SET started_at = '2026-01-05 10:00:00' WHERE call_uid = 'hc-0003'",
    ],
    "check_hard_purposes": [
        "UPDATE truth.truth_call_labels SET primary_purpose = 'complaint' WHERE call_uid IN"
        " (SELECT call_uid FROM truth.truth_hard_calls WHERE hard_type = 'outbound_real' LIMIT 1)",
    ],
    "check_hard_booking_events": [
        "UPDATE truth.truth_call_labels SET used_fact = json_set(used_fact, '$.appointment_id',"
        f" ({C_EVENT})) WHERE call_uid = ({BOOK_HC})",
    ],
    "check_hard_emergency_appt": [
        "UPDATE truth.truth_call_labels SET used_fact = json_remove(used_fact, '$.existing_visit')"
        " WHERE call_uid IN (SELECT call_uid FROM truth.truth_hard_calls WHERE half = 'calm_urgent'"
        " LIMIT 1)",
    ],
    "check_hard_excluded": [
        "UPDATE ax_calls SET patient_id = (SELECT p.patient_id FROM truth.truth_ledger_errors e"
        " JOIN emr_payments p ON p.id = e.payment_id LIMIT 1) WHERE call_uid = 'hc-0004'"
        " AND patient_id IS NOT NULL",
        "UPDATE ax_calls SET patient_id = (SELECT p.patient_id FROM truth.truth_ledger_errors e"
        " JOIN emr_payments p ON p.id = e.payment_id LIMIT 1) WHERE call_uid = (SELECT min(call_uid)"
        " FROM ax_calls WHERE call_uid LIKE 'hc-%' AND patient_id IS NOT NULL)",
    ],
    "check_hard_not_pooled": [
        "UPDATE ax_calls SET in_eval_pool = 1 WHERE call_uid = 'hc-0005'",
    ],
    "check_hard_urgency_labels": [
        "UPDATE truth.truth_call_labels SET urgency = 'normal' WHERE call_uid IN"
        " (SELECT call_uid FROM truth.truth_hard_calls WHERE half = 'calm_urgent' LIMIT 1)",
    ],
}


@pytest.mark.parametrize("name", sorted(BROKEN))
def test_hard_checks_catch_breakage(generated, name):
    conn, ctx = _open(generated(f"hard_{name}")[0])
    for sql in BROKEN[name]:
        conn.execute(sql)
    assert getattr(verify, name)(conn, ctx)


def test_allocation_counts():
    a = hard_calls.allocation()
    assert len(a) == len(HARD_CALL_TYPES) * HARD_CALLS_PER_TYPE
    by_type = Counter(t for t, *_ in a)
    assert set(by_type.values()) == {HARD_CALLS_PER_TYPE}
    halves = Counter(h for t, h, *_ in a if t == "urgency_inverted")
    assert halves == {h: HARD_CALLS_PER_TYPE // 2 for h in HARD_URGENCY_HALVES}
    rare = Counter((t, p) for t, _, p, _ in a if p in EVAL_RARE_PURPOSES)
    assert all(n >= HARD_RARE_MIN for (t, p), n in rare.items() if t != "urgency_inverted")


def _dump(out) -> dict:
    """hc- 행을 뺀 source·truth 전 테이블"""
    conn, _ = _open(out)
    tables = {}
    for schema in ("main", "truth"):
        for (name,) in conn.execute(
            f"SELECT name FROM {schema}.sqlite_master WHERE type = 'table'"
        ):
            cols = [r[1] for r in conn.execute(f"PRAGMA {schema}.table_info({name})")]
            where = " WHERE call_uid NOT LIKE 'hc-%'" if "call_uid" in cols else ""
            tables[f"{schema}.{name}"] = conn.execute(
                f"SELECT * FROM {schema}.{name}{where} ORDER BY 1"
            ).fetchall()
    return {k: [tuple(r) for r in v] for k, v in tables.items()}


def test_existing_rows_unchanged(generated, tmp_path, monkeypatch):
    """hard_calls 단계를 빼고 만든 DB와 hc- 밖의 모든 행이 같다 — 앞 단계 결과·캐시 그대로(설계 §3.1)"""
    with_hard = _dump(generated("hard_same")[0])
    monkeypatch.setattr(build, "STAGES", [s for s in build.STAGES if s[0] != "hard_calls"])
    build.generate(tmp_path / "source.sqlite", tmp_path / "truth.sqlite", CONFIG, fake_llm=True)
    without = _dump(tmp_path)
    assert with_hard.keys() == without.keys()
    assert [k for k in with_hard if with_hard[k] != without[k]] == []
