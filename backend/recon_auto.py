"""⑧-1 일계표 자동 마감 (기능 정의 §5 ⑧-1·§3 원칙 1, 설계 docs/superpowers/specs/2026-11-11-finance-recon-design.md §3
"자동 마감", 앱 설계 §3b). 규칙 원인이 "규칙으로 되는 원인" 묶음이고 조사 에이전트 1순위가 규칙과 같은 건을 auto_policy
(키 recon_autoclose, 기본 끔)가 켜졌을 때 apply_auto_action으로 닫는다 — 장부 쪽 원인은 모두 애매한 묶음이라 대상이 아니다.
키는 불일치 건 키. 에이전트가 고르는 경로(request_auto_action)는 두지 않는다(코드 경로, 설계 §3). 앱 상태는 ax_auto_actions
행 자체 — 확정 안 된 건 수(backend/recon.py open_summary)와 오늘 화면이 active()로 뺀다. 조건 함수 autoclose_reason은
채점의 자동 마감 오류율도 같이 쓴다."""

import json
import sqlite3
from datetime import date, datetime

from backend import db, recon_agent, recon_match
from backend.harness.auto import AutoKind, apply_auto_action

KIND = "recon_autoclose"
REASON = "규칙과 에이전트 원인이 같음 · 장부 수정 불필요"


def autoclose_reason(rule: str, agent: str | None, groups: dict) -> str | None:
    """규칙 원인이 rule 묶음이고 에이전트 1순위가 같으면 마감 이유, 아니면 None"""
    return REASON if groups.get(rule) == "rule" and agent == rule else None


def _blocked(conn, key: str, result: dict) -> str | None:
    """요청 값이 아니라 저장된 에이전트 결과와 맞춰 본다"""
    agent = recon_agent.result(conn, key)
    return None if agent and agent["cause"] == result.get("cause") else "에이전트 결과와 다름"


def _noop(conn, row) -> None:
    """앱 상태는 ax_auto_actions 행 자체 — 일계표·오늘 화면이 되돌리지 않은 행을 읽는다"""


KINDS = {KIND: AutoKind(condition=_blocked, apply=_noop, undo=_noop)}


def policy_on(conn) -> bool:
    return bool(db.get_setting(conn, "auto_policy", {}).get(KIND))


def active(conn: sqlite3.Connection, keys: set[str]) -> dict[str, dict]:
    """되돌리지 않은 자동 마감 중 keys(지금 탐지 건 키)에 있는 것 — 정책이 꺼져 있으면 없음(자동 종결과 같은 유효 조건)"""
    if not policy_on(conn):
        return {}
    rows = conn.execute(
        "SELECT id, target, result, reason, created_at FROM ax_auto_actions"
        " WHERE kind = ? AND reverted_at IS NULL ORDER BY id",
        (KIND,),
    ).fetchall()
    return {
        r["target"]: {
            "id": r["id"],
            "cause": json.loads(r["result"])["cause"],
            "reason": r["reason"],
            "at": r["created_at"],
        }
        for r in rows
        if r["target"] in keys
    }


def refresh(conn: sqlite3.Connection, today: date, clock=datetime.now) -> dict:
    """지금 탐지 건마다 다시 계산 — 사람 확정·사람이 되돌린 건은 그대로, 조건을 못 넘게 된 마감은 행을 지워 푼다.
    커밋은 부르는 쪽"""
    if not policy_on(conn):
        return {"off": True}
    from backend import recon  # recon이 active()를 읽으므로 순환을 피해 여기서

    books = recon_match.load(conn, today)
    groups = books["mv"]["recon_causes"]
    confirmed = set(recon._confirmations(conn))
    out = {"closed": 0, "opened": 0, "kept": 0}
    for item in recon_match.detect(books):
        k = item["key"]
        row = conn.execute(
            "SELECT id, reverted_at FROM ax_auto_actions WHERE kind = ? AND target = ? AND period = ''",
            (KIND, k),
        ).fetchone()
        if (row and row["reverted_at"]) or k in confirmed:
            out["kept"] += 1
            continue
        agent = recon_agent.result(conn, k)
        reason = autoclose_reason(recon.rule_cause(item, books), agent and agent["cause"], groups)
        if reason and not row:
            d = apply_auto_action(
                conn,
                KINDS,
                KIND,
                k,
                result={"cause": agent["cause"], "run_id": agent["run_id"]},
                reason=reason,
                run_id=agent["run_id"],
                clock=clock,
            )
            out["closed" if d.applied else "kept"] += 1
        elif not reason and row:
            conn.execute("DELETE FROM ax_auto_actions WHERE id = ?", (row["id"],))
            out["opened"] += 1
        else:
            out["kept"] += 1
    return out
