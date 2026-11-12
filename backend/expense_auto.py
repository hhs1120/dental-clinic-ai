"""⑧-2 규칙 비교·자동 확정 (기능 정의 §5 ⑧-2·§3 원칙 1, 설계 docs/superpowers/specs/2026-11-11-finance-recon-design.md §4
"자동 확정", 앱 설계 §3b·§7.3). 규칙과 같음(agree) = 규칙 증빙 집합 = AI 증빙 집합이고 AI 과목 = 거래처 표 기본 과목.
자동 확정 = 규칙과 같음 + 금액 차이 0 + 품목으로 정하는 거래처가 아님 + 같은 증빙을 고른 다른 후보·다른 확정이 없음 —
auto_policy(키 expense_autoconfirm, 기본 끔)가 켜졌을 때 apply_auto_action으로. 키는 출금 키(expense.outflow_key).
앱 상태는 ax_auto_actions 행 자체(사람 확정 테이블에 쓰지 않는다) — 화면·오늘 화면이 active()로 읽고, 사람이 확정하면
expense.confirm이 그 행을 되돌린다(HUMAN_REVERT). 조건 함수는 채점의 자동 확정 오류율도 같이 쓴다."""

import json
import sqlite3
from datetime import date, datetime

from backend import db, expense, expense_agent
from backend.harness.auto import AutoKind, apply_auto_action

KIND = expense.AUTO_KIND
REASON = "규칙과 AI 증빙·과목이 같음"
HUMAN_REVERT = expense.HUMAN_REVERT


def agree(rule_ids: list[int], rule_account: str | None, agent: dict | None) -> bool:
    """규칙과 같음 — 증빙은 정렬한 집합, 과목은 거래처 표 기본 과목(사람 확정 기억 아님). 결과 없음이면 False"""
    if not agent or agent.get("account") is None or agent.get("document_ids") is None:
        return False
    return sorted(set(rule_ids)) == sorted(set(agent["document_ids"])) and (
        agent["account"] == rule_account
    )


def autoconfirm_reason(agreed: bool, diff: int | None, by_item: bool, shared: bool) -> str | None:
    """자동 확정 조건을 모두 넘으면 이유, 아니면 None(증빙 없음 = diff None은 대상 아님)"""
    return REASON if agreed and diff == 0 and not by_item and not shared else None


def _blocked(conn, key: str, result: dict) -> str | None:
    """요청 값이 아니라 저장된 AI 결과와 맞춰 본다"""
    a = expense_agent.result(conn, key)
    same = a and a["document_ids"] == sorted(result.get("document_ids") or [])
    return None if same and a["account"] == result.get("account") else "AI 결과와 다름"


def _noop(conn, row) -> None:
    """앱 상태는 ax_auto_actions 행 자체 — 화면·오늘 화면이 되돌리지 않은 행을 읽는다"""


KINDS = {KIND: AutoKind(condition=_blocked, apply=_noop, undo=_noop)}


def policy_on(conn) -> bool:
    return bool(db.get_setting(conn, "auto_policy", {}).get(KIND))


def active(conn: sqlite3.Connection, keys: set[str]) -> dict[str, dict]:
    """되돌리지 않은 자동 확정 중 keys(지금 화면의 출금 키)에 있는 것 — 정책이 꺼져 있으면 없음"""
    if not policy_on(conn):
        return {}
    rows = conn.execute(
        "SELECT id, target, result, created_at FROM ax_auto_actions"
        " WHERE kind = ? AND reverted_at IS NULL ORDER BY id",
        (KIND,),
    ).fetchall()
    out = {}
    for r in rows:
        if r["target"] in keys:
            res = json.loads(r["result"])
            out[r["target"]] = {
                "id": r["id"],
                "document_ids": res["document_ids"],
                "account": res["account"],
                "at": r["created_at"],
            }
    return out


def candidates(conn: sqlite3.Connection, data: dict) -> dict[str, dict]:
    """출금 키 → 자동 확정 판단 재료 {o, rule_ids, rule_account, agent, agreed, diff, by_item, shared, reason} —
    사람 확정이 있는 출금은 뺀다. shared = 그 증빙을 다른 후보가 고르거나 다른 출금의 확정이 가짐"""
    by_id = {d["id"]: d for d in data["docs"]}
    keys = {expense.outflow_key(o): o for o in data["outflows"]}
    agents = expense_agent.results(conn, set(keys))
    owners = expense.live_doc_owners(conn, data)
    out = {}
    for k, o in keys.items():
        if (o["table"], o["id"]) in data["conf"]:
            continue
        v = data["vendors"].get(o["label"])
        rule_ids = expense.rule_match(o, data["docs"], data["vendors"])
        rule_acc = expense.rule_account(o, data["vendors"])
        docs = [by_id[i] for i in rule_ids if i in by_id]
        st = [d for d in docs if d["kind"] == "statement"]
        a = agents.get(k)
        out[k] = {
            "o": o,
            "rule_ids": rule_ids,
            "rule_account": rule_acc,
            "agent": a,
            "agreed": agree(rule_ids, rule_acc, a),
            "diff": o["amount"] - sum(d["amount"] for d in st or docs) if docs else None,
            "by_item": bool(v and v["by_item"])
            or any(n in o["label"] for n in data["by_item_names"]),
            "owned_elsewhere": any(
                owners.get(d, (o["table"], o["id"])) != (o["table"], o["id"]) for d in rule_ids
            ),
        }
    picks: dict[int, int] = {}
    for c in out.values():
        if c["agreed"]:
            for d in c["rule_ids"]:
                picks[d] = picks.get(d, 0) + 1
    for c in out.values():
        c["shared"] = c["owned_elsewhere"] or any(picks.get(d, 0) > 1 for d in c["rule_ids"])
        c["reason"] = autoconfirm_reason(c["agreed"], c["diff"], c["by_item"], c["shared"])
    return out


def refresh(conn: sqlite3.Connection, today: date, clock=datetime.now) -> dict:
    """기준일 전 출금마다 다시 계산 — 사람 확정·사람이 되돌린 건은 그대로, 조건을 못 넘게 된 자동 확정은 행을 지워
    푼다. 커밋은 부르는 쪽"""
    if not policy_on(conn):
        return {"off": True}
    data = expense._load(conn, today)
    out = {"closed": 0, "opened": 0, "kept": 0}
    for k, c in candidates(conn, data).items():
        row = conn.execute(
            "SELECT id, reverted_at FROM ax_auto_actions WHERE kind = ? AND target = ? AND period = ''",
            (KIND, k),
        ).fetchone()
        if row and row["reverted_at"]:
            out["kept"] += 1
        elif c["reason"] and not row:
            a = c["agent"]
            d = apply_auto_action(
                conn,
                KINDS,
                KIND,
                k,
                result={
                    "document_ids": a["document_ids"],
                    "account": a["account"],
                    "run_id": a["run_id"],
                },
                reason=c["reason"],
                run_id=a["run_id"],
                clock=clock,
            )
            out["closed" if d.applied else "kept"] += 1
        elif not c["reason"] and row:
            conn.execute("DELETE FROM ax_auto_actions WHERE id = ?", (row["id"],))
            out["opened"] += 1
        else:
            out["kept"] += 1
    return out
