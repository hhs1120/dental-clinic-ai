"""후속 에이전트 (기능 정의 §4 ①, 설계 "PR ② 후속 에이전트") — 스크립트 게이트웨이로 도구 호출 순서를 정해 둔다."""

import json
from dataclasses import replace
from datetime import date

import pytest

from backend import followup
from backend.harness.store import SqliteLlmLog
from shared.clinic import Clinic
from shared.llm import FakeGateway
from tests.backend.call_db import LABELS, classify_row, make_call_db

CLINIC = Clinic(today=date(2026, 12, 1), range_start=date(2026, 1, 1), eval_pool_size=4)
SUBMIT = {
    "action": "doctor_reply",
    "summary": "원장님 확인 뒤 회신",
    "facts": ["마지막 내원 기록 없음"],
}


class Script(FakeGateway):
    """실행마다 정해 둔 단계를 차례로 — ("tool", 이름, 인자) 또는 ("text", 글). runs = 실행별 단계 목록"""

    def __init__(self, conn, *runs):
        super().__init__(SqliteLlmLog(conn), scope="runtime")
        self.runs, self.pos = [list(r) for r in runs], {}

    def complete(self, alias, **kw):
        r = super().complete(alias, **kw)
        if not kw.get("ref"):  # 분류·판정 호출 — 가짜 응답 그대로
            return r
        run = kw["ref"][1]
        order = sorted(self.pos | {run: 0})
        steps = self.runs[min(order.index(run), len(self.runs) - 1)]
        i = self.pos.get(run, 0)
        self.pos[run] = i + 1
        kind, *rest = steps[min(i, len(steps) - 1)]
        if kind == "text":
            return replace(
                r, text=rest[0], tool_calls=(), content=[{"type": "text", "text": rest[0]}]
            )
        name, args = rest
        call = {"id": f"tu{run}-{i}", "name": name, "input": args}
        return replace(r, text="", tool_calls=(call,), content=[{"type": "tool_use", **call}])


@pytest.fixture
def conn(tmp_path):
    return make_call_db(tmp_path)


def _row(conn, uid):
    return conn.execute("SELECT * FROM ax_call_followups WHERE call_uid = ?", (uid,)).fetchone()


def test_plan_saved(conn):
    gw = Script(
        conn,
        [("tool", "query_patient", {"chart_no": "C-00001"}), ("tool", "submit_followup", SUBMIT)],
    )
    out = followup.run_for_call(conn, gw, "c-0001", CLINIC)
    row = _row(conn, "c-0001")
    assert out["action"] == "doctor_reply" and row["status"] == "done"
    assert json.loads(row["facts"]) == SUBMIT["facts"] and row["run_id"]


def test_no_chart_still_plans(conn):
    assert "차트 없음" in followup.input_text(
        {"chart_no": None, "direction": "inbound", "transcript": "환자: 네"},
        {"labels": LABELS, "summary": "요약", "todo": []},
    )
    gw = Script(conn, [("tool", "submit_followup", SUBMIT | {"action": "other"})])
    assert followup.run_for_call(conn, gw, "c-0004", CLINIC)["action"] == "other"


def test_no_submit_is_failure(conn):
    gw = Script(conn, [("text", "처리안은 회신입니다")])
    followup.run_for_call(conn, gw, "c-0001", CLINIC)
    row = _row(conn, "c-0001")
    assert row["status"] == "failed" and row["action"] is None


def test_unverified_retries_once(conn):
    bad = SUBMIT | {"summary": "12월 25일까지 회신"}  # 녹취록·도구 결과에 없는 날짜
    gw = Script(conn, [("tool", "submit_followup", bad)], [("tool", "submit_followup", bad)])
    followup.run_for_call(conn, gw, "c-0001", CLINIC)
    row = _row(conn, "c-0001")
    assert row["status"] == "failed" and "후검증" in row["reason"]
    assert conn.execute("SELECT COUNT(*) FROM ax_agent_runs").fetchone()[0] == 2


def test_retry_succeeds(conn):
    bad = SUBMIT | {"summary": "12월 25일까지 회신"}
    gw = Script(conn, [("tool", "submit_followup", bad)], [("tool", "submit_followup", SUBMIT)])
    assert followup.run_for_call(conn, gw, "c-0001", CLINIC)["status"] == "done"


def test_skips_without_todo_or_existing(conn):
    classify_row(conn, "c-0003", version="v3", labels=LABELS | {"has_todo": 0})
    conn.execute("INSERT OR REPLACE INTO ax_settings VALUES ('active_prompt_version', '\"v3\"')")
    assert followup.run_for_call(conn, Script(conn, []), "c-0003", CLINIC) is None
    conn.execute("INSERT OR REPLACE INTO ax_settings VALUES ('active_prompt_version', '\"v2\"')")
    gw = Script(conn, [("tool", "submit_followup", SUBMIT)])
    followup.run_for_call(conn, gw, "c-0001", CLINIC)
    gw2 = Script(conn, [("tool", "submit_followup", SUBMIT)])
    assert followup.run_for_call(conn, gw2, "c-0001", CLINIC) is None and gw2.pos == {}


def test_verify_facts():
    assert (
        followup.verify_facts("2월 7일 스케일링", ["잔액 0원"], ["{'date': '2026-02-07'} 잔액 0"])
        is True
    )
    assert followup.verify_facts("12월 25일", [], ["2026-02-07"]) is False


def test_targets(conn):
    assert set(followup.targets(conn, CLINIC.today)) == {
        "c-0001",
        "c-0004",
    }  # 7일 안·판정 풀 밖·할 일


def test_max_tool_calls_matches_eval():
    from evaluation.metrics import MAX_TOOL_CALLS

    assert followup.FOLLOWUP_MAX_TOOL_CALLS == MAX_TOOL_CALLS


# ── 리뷰 반영 ────────────────────────────────────────────────────────────


def test_input_has_call_time_and_today():
    t = followup.input_text(
        {
            "chart_no": "C-1",
            "direction": "inbound",
            "transcript": "환자: 네",
            "at": "2026-11-28 10:00:00",
        },
        {"labels": LABELS, "summary": "요약", "todo": []},
        date(2026, 12, 1),
    )
    assert "통화 일시: 2026-11-28 10:00" in t and "오늘: 2026-12-01" in t


def test_tool_args_and_input_count_as_evidence(conn):
    """조회 인자의 날짜·입력의 오늘 날짜를 쓴 근거는 지어낸 것이 아니다"""
    sub = SUBMIT | {"summary": "2026-12-20 예약 없음 — 오늘 2026-12-01 기준 회신"}
    gw = Script(
        conn,
        [
            ("tool", "query_schedule", {"date_from": "2026-12-20", "date_to": "2026-12-20"}),
            ("tool", "submit_followup", sub),
        ],
    )
    assert followup.run_for_call(conn, gw, "c-0001", CLINIC)["status"] == "done"


def test_reclassify_drops_old_plan(conn):
    """같은 버전으로 다시 분류하면 그 분류 행의 옛 처리안을 지우고 새로 낸다"""
    from backend import calls

    followup.run_for_call(
        conn, Script(conn, [("tool", "submit_followup", SUBMIT)]), "c-0001", CLINIC
    )
    gw = Script(conn, [("tool", "submit_followup", SUBMIT | {"action": "billing"})])
    calls.classify_one(conn, gw, "c-0001")
    rows = conn.execute(
        "SELECT action FROM ax_call_followups WHERE call_uid = 'c-0001'"
    ).fetchall()
    assert [r["action"] for r in rows] != ["doctor_reply"]  # 옛 처리안이 그대로 남지 않는다


def test_stuck_run_is_closed_and_retried(conn):
    """끊긴 실행(잠금 만료)이 남아 있어도 막히지 않는다 — 실패로 닫고 새로 돈다"""
    conn.execute(
        "INSERT INTO ax_agent_runs (agent, target, status, prompt_version, locked_by, heartbeat_at, started_at)"
        " VALUES ('call_followup', 'c-0001', 'running', 'v1', 'old', '2000-01-01 00:00:00', '2000-01-01 00:00:00')"
    )
    conn.commit()
    out = followup.run_for_call(
        conn, Script(conn, [("tool", "submit_followup", SUBMIT)]), "c-0001", CLINIC
    )
    assert out["status"] == "done"
    assert (
        conn.execute(
            "SELECT status FROM ax_agent_runs WHERE locked_by IS NULL AND started_at = '2000-01-01 00:00:00'"
        ).fetchone()["status"]
        == "failed"
    )


def test_prompts_carry_action_definitions():
    from pathlib import Path

    from evaluation.call_actions import PROMPT_VERSION as LABEL_VERSION
    from shared.followup import ACTION_DESC

    root = Path(__file__).resolve().parents[2] / "prompts"
    for f in (
        root / "followup" / f"{followup.PROMPT_VERSION}.md",
        root / "call_actions" / f"{LABEL_VERSION}.md",
    ):
        text = f.read_text(encoding="utf-8")
        assert all(f"- {a} — {d}" in text for a, d in ACTION_DESC.items()), f


def test_drop_stale_redoes_only_old_prompt_plans(conn):
    """REDO — 오늘 할 일 통화 중 옛 프롬프트 버전으로 낸 처리안만 지워 다시 낼 대상으로 만든다"""
    for uid in ("c-0001", "c-0004"):
        followup.run_for_call(
            conn, Script(conn, [("tool", "submit_followup", SUBMIT)]), uid, CLINIC
        )
    conn.execute(
        "UPDATE ax_agent_runs SET prompt_version = 'v0' WHERE target = 'c-0001'"
    )  # 옛 버전 처리안
    assert followup.targets(conn, CLINIC.today) == []
    assert followup.drop_stale(conn, CLINIC.today) == 1
    assert followup.targets(conn, CLINIC.today) == ["c-0001"]
    assert followup.drop_stale(conn, CLINIC.today) == 0
