"""자동 종결 (기능 정의 §4 ①·§3 원칙 1, 앱 설계 §3b) — 조건 함수·실행·풀기."""

from backend.autoclose import autoclose_reason

CLEAN = {
    "primary_purpose": "confirm",
    "counterpart": "patient",
    "urgency": "normal",
    "emergency": 0,
    "complaint": 0,
    "price_inquiry": 0,
    "wants_appt": 0,
    "appt_confirmed": 0,
    "has_todo": 0,
}


def _r(ok=True, fallback=False, **over):
    return {"labels": CLEAN | over, "ok": ok, "fallback": fallback}


def test_clean_pair_closes():
    assert autoclose_reason(_r(), _r())


def test_todo_blocks():
    assert autoclose_reason(_r(has_todo=1), _r()) is None
    assert autoclose_reason(_r(), _r(has_todo=1)) is None


def test_emergency_complaint_urgent_block():
    for over in ({"emergency": 1}, {"complaint": 1}, {"urgency": "urgent"}):
        assert autoclose_reason(_r(**over), _r(**over)) is None
        assert autoclose_reason(_r(), _r(**over)) is None


def test_disagreement_blocks():
    assert autoclose_reason(_r(primary_purpose="book"), _r()) is None
    assert autoclose_reason(
        _r(urgency="low"), _r()
    )  # 급함은 urgent 여부만 — normal↔low는 엇갈림 아님


def test_missing_failed_fallback_block():
    assert autoclose_reason(_r(), None) is None
    assert autoclose_reason(None, _r()) is None
    assert autoclose_reason(_r(ok=False), _r()) is None


def test_fallback_blocks():
    assert autoclose_reason(_r(), _r(fallback=True)) is None
    assert autoclose_reason(_r(fallback=True), _r()) is None


# ── 실행·풀기 (make autoclose, 새 통화, 사람 판정) ─────────────────────────────

import json  # noqa: E402

import pytest  # noqa: E402

from backend import autoclose, calls, db, judge_queue  # noqa: E402
from backend.harness.auto import revert_auto_action  # noqa: E402
from backend.harness.store import SqliteLlmLog  # noqa: E402
from shared.llm import FakeGateway  # noqa: E402
from tests.backend.call_db import classify_row, judge_row, make_call_db  # noqa: E402


@pytest.fixture
def conn(tmp_path):
    """c-0001·c-0003·c-0004(앱 통화)에 깨끗한 분류·판정 — c-0004만 분류가 급함이라 조건을 못 넘는다.
    c-0002는 판정 풀이라 앱 대상이 아니다"""
    c = make_call_db(tmp_path)
    c.execute("DELETE FROM ax_call_classifications")
    for uid in ("c-0001", "c-0002", "c-0003"):
        classify_row(c, uid, labels=CLEAN, todo=[])
        judge_row(c, uid, CLEAN)
    classify_row(c, "c-0004", labels=CLEAN | {"urgency": "urgent"}, todo=[])
    judge_row(c, "c-0004", CLEAN)
    return c


def _closed(conn) -> set:
    return {
        r[0]
        for r in conn.execute(
            "SELECT target FROM ax_auto_actions WHERE kind = ? AND reverted_at IS NULL",
            (autoclose.KIND,),
        )
    }


def _blocks(conn) -> int:
    return conn.execute("SELECT COALESCE(SUM(blocked), 0) FROM ax_rule_counts").fetchone()[0]


def test_run_policy_off_does_nothing(conn):
    assert autoclose.run(conn) == {"off": True}
    assert _closed(conn) == set() and _blocks(conn) == 0


def test_run_closes_clean_calls(conn):
    autoclose.set_policy(conn, autoclose.KIND, True)
    r = autoclose.run(conn)
    assert _closed(conn) == {"c-0001", "c-0003"}  # 판정 풀 c-0002·급함 c-0004 제외
    assert r["closed"] == 2
    row = conn.execute("SELECT reason FROM ax_auto_actions WHERE target = 'c-0001'").fetchone()
    assert row["reason"] == autoclose.REASON


def test_rerun_is_idempotent(conn):
    autoclose.set_policy(conn, autoclose.KIND, True)
    autoclose.run(conn)
    r = autoclose.run(conn)
    assert r["closed"] == 0 and _closed(conn) == {"c-0001", "c-0003"}


def test_system_unclose_when_condition_breaks(conn):
    autoclose.set_policy(conn, autoclose.KIND, True)
    autoclose.run(conn)
    conn.execute(
        "UPDATE ax_call_classifications SET labels = ? WHERE call_uid = 'c-0001'",
        (json.dumps(CLEAN | {"has_todo": 1}),),
    )
    assert autoclose.run(conn)["opened"] == 1
    assert "c-0001" not in _closed(conn)
    assert (
        conn.execute("SELECT COUNT(*) FROM ax_auto_actions WHERE target = 'c-0001'").fetchone()[0]
        == 0
    )
    conn.execute(
        "UPDATE ax_call_classifications SET labels = ? WHERE call_uid = 'c-0001'",
        (json.dumps(CLEAN),),
    )
    assert autoclose.run(conn)["closed"] == 1  # 시스템이 푼 통화는 조건을 다시 넘으면 다시 닫힌다


def test_human_revert_sticks(conn):
    autoclose.set_policy(conn, autoclose.KIND, True)
    autoclose.run(conn)
    aid = conn.execute("SELECT id FROM ax_auto_actions WHERE target = 'c-0001'").fetchone()[0]
    assert revert_auto_action(conn, autoclose.KINDS, aid, "데스크")
    autoclose.run(conn)
    row = conn.execute(
        "SELECT reverted_at FROM ax_auto_actions WHERE target = 'c-0001'"
    ).fetchone()
    assert row["reverted_at"] and "c-0001" not in _closed(conn)


def test_fallback_pair_not_closed(conn):
    conn.execute(
        "UPDATE ax_llm_calls SET fallback_used = 1 WHERE id = (SELECT llm_call_id FROM"
        " ax_model_judgments WHERE call_uid = 'c-0001')"
    )
    autoclose.set_policy(conn, autoclose.KIND, True)
    autoclose.run(conn)
    assert "c-0001" not in _closed(conn)


def test_classify_one_closes_after_both(conn):
    """판정기 버전이 없으면(판정 전) 닫지 않고, 판정이 들어오면 그때 계산한다"""
    autoclose.set_policy(conn, autoclose.KIND, True)
    gw = FakeGateway(SqliteLlmLog(conn), scope="runtime")
    db.set_setting(conn, "prompt_versions", {})
    calls.classify_one(conn, gw, "c-0003")
    assert "c-0003" not in _closed(conn)
    assert autoclose.refresh_call(conn, "c-0003") == "kept"


def test_refresh_call_closes_and_opens(conn):
    autoclose.set_policy(conn, autoclose.KIND, True)
    assert autoclose.refresh_call(conn, "c-0001") == "closed"
    assert autoclose.refresh_call(conn, "c-0001") == "kept"
    autoclose.set_policy(conn, autoclose.KIND, False)
    assert autoclose.refresh_call(conn, "c-0003") == "off"


def test_human_judgment_unclose(conn):
    autoclose.set_policy(conn, autoclose.KIND, True)
    autoclose.run(conn)
    conn.execute("INSERT INTO ax_judge_state VALUES ('c-0001', 'pending', '2026-11-05 09:00:00')")
    conn.commit()
    judge_queue.save(
        conn, "c-0001", {"has_todo": True}, None, part="데스크", at="2026-11-05 10:00:00"
    )
    row = conn.execute(
        "SELECT reverted_by FROM ax_auto_actions WHERE target = 'c-0001'"
    ).fetchone()
    assert row["reverted_by"] == "데스크"
