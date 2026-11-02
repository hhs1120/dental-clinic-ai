"""자동 처리 (기능 정의 §3 원칙 1, 앱 설계 §3b). LLM 경로(`request_auto_action`)와 코드 경로가 모두
apply_auto_action()을 지난다 — auto_policy(기본 끔)와 작업별 조건을 코드가 검사하고, 허용한 건만 이유와 함께 남긴다.
거절은 규칙 id `auto.<이유>`로 센다. 중복 방지 키의 기간은 코드가 정한다 — 모델이 고르면 되돌린 건이 다시 살아난다.
커밋하지 않고 부르는 쪽 트랜잭션에 든다."""

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, Field

from backend import db
from backend.harness.store import count_block
from backend.harness.tools import Tool

UNKNOWN_KIND = "unknown_kind"
POLICY_OFF = "policy_off"
REVERTED = "reverted"
DUPLICATE = "duplicate"
CONDITION = "condition"
WRONG_TARGET = "wrong_target"


def no_period(conn, target, result) -> str:
    return ""


@dataclass(frozen=True)
class AutoKind:
    # (conn, target, result) → 막는 이유 또는 None. result는 모델 값일 수 있으므로 저장된 사실과 대조해 판단한다
    condition: Callable
    apply: Callable  # (conn, 행 dict) — 앱 상태 반영
    undo: Callable  # (conn, 행 dict) — 앱 상태 복원
    period: Callable = no_period  # (conn, target, result) → 기간. 반복되는 일만 쓴다


@dataclass(frozen=True)
class Decision:
    applied: bool
    why: str | None = None
    action_id: int | None = None
    detail: str | None = None  # 조건이 막은 이유


def _refuse(conn, why, action_id=None, detail=None) -> Decision:
    count_block(conn, f"auto.{why}")
    return Decision(False, why, action_id, detail)


def apply_auto_action(
    conn: sqlite3.Connection,
    kinds: dict,
    kind: str,
    target: str,
    *,
    result: dict,
    reason: str,
    run_id: int | None = None,
    clock=datetime.now,
) -> Decision:
    spec = kinds.get(kind)
    if spec is None:
        return _refuse(conn, UNKNOWN_KIND)
    if not db.get_setting(conn, "auto_policy", {}).get(kind):
        return _refuse(conn, POLICY_OFF)
    period = spec.period(conn, target, result)
    old = conn.execute(
        "SELECT id, reverted_at FROM ax_auto_actions WHERE kind = ? AND target = ? AND period = ?",
        (kind, target, period),
    ).fetchone()
    if old:
        return _refuse(conn, REVERTED if old["reverted_at"] else DUPLICATE, old["id"])
    if why := spec.condition(conn, target, result):
        return _refuse(conn, CONDITION, detail=why)
    cur = conn.execute(
        "INSERT INTO ax_auto_actions (kind, target, period, result, reason, run_id, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
        (
            kind,
            target,
            period,
            json.dumps(result, ensure_ascii=False),
            reason,
            run_id,
            clock().isoformat(sep=" ", timespec="seconds"),
        ),
    )
    if cur.rowcount != 1:  # 확인과 쓰기 사이에 다른 실행이 먼저 씀
        return _refuse(conn, DUPLICATE)
    spec.apply(conn, _row(conn, cur.lastrowid))
    return Decision(True, action_id=cur.lastrowid)


def _row(conn: sqlite3.Connection, action_id: int) -> dict:
    row = dict(conn.execute("SELECT * FROM ax_auto_actions WHERE id = ?", (action_id,)).fetchone())
    row["result"] = json.loads(row["result"])
    return row


def revert_auto_action(
    conn: sqlite3.Connection, kinds: dict, action_id: int, by: str, clock=datetime.now
) -> bool:
    """앱 상태만 복원한다. 이미 되돌린 건이면 False"""
    row = _row(conn, action_id)
    if row["reverted_at"]:
        return False
    kinds[row["kind"]].undo(conn, row)
    conn.execute(
        "UPDATE ax_auto_actions SET reverted_by = ?, reverted_at = ? WHERE id = ?",
        (by, clock().isoformat(sep=" ", timespec="seconds"), action_id),
    )
    return True


class AutoRequest(BaseModel):
    kind: str
    target: str
    result: dict
    reason: str = Field(min_length=1)


def auto_action_tool(kinds: dict, allowed: tuple) -> Tool:
    """LLM이 자동 처리를 요청하는 도구. 실행의 대상에만 쓸 수 있고, 허용 여부는 코드가 정한다"""

    def request(ctx, args):
        conn = ctx.store.conn
        if args["target"] != ctx.target:
            decision = _refuse(conn, WRONG_TARGET)
        else:
            decision = apply_auto_action(
                conn,
                {k: v for k, v in kinds.items() if k in allowed},
                args["kind"],
                args["target"],
                result=args["result"],
                reason=args["reason"],
                run_id=ctx.run_id,
                clock=ctx.store.clock,
            )
        return {"applied": decision.applied, "why": decision.why}

    return Tool(
        "request_auto_action",
        "자동 처리를 요청한다. 허용 여부는 정책과 조건으로 코드가 정한다.",
        AutoRequest,
        request,
        writes=True,
    )
