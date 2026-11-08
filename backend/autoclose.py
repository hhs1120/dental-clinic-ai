"""① 자동 종결 (기능 정의 §4 ①·§3 원칙 1, 앱 설계 §3b·§7.3). 확인할 것이 없는 통화 — 두 모델 모두 할 일 없음이고
응급·불만·급함·엇갈림·폴백이 없는 통화 — 를 `auto_policy`가 켜졌을 때 `apply_auto_action`으로 닫는다.
조건 함수 `autoclose_reason`은 eval.py의 자동 종결 오류율도 같이 쓴다(평가 설계 §5)."""

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from backend import calls, db
from backend.harness.auto import AutoKind, apply_auto_action, revert_auto_action
from shared.call_labels import differing_items

ROOT = Path(__file__).resolve().parents[1]

KIND = "call_autoclose"
REASON = "두 모델 모두 할 일 없음 · 응급·불만·급함·엇갈림 없음"


def autoclose_reason(cls: dict | None, judge: dict | None) -> str | None:
    """cls·judge = {"labels", "ok", "fallback"} 또는 None. 조건을 다 넘으면 종결 이유, 하나라도 못 넘으면 None"""
    if cls is None or judge is None:
        return None
    if not (cls["ok"] and judge["ok"]) or cls["fallback"] or judge["fallback"]:
        return None
    a, b = cls["labels"], judge["labels"]
    for lab in (a, b):
        if lab["has_todo"] or lab["emergency"] or lab["complaint"] or lab["urgency"] == "urgent":
            return None
    if differing_items(a, b):
        return None
    return REASON


# ── 실행·풀기 (make autoclose, 새 통화, 사람 판정 — 앱 설계 §3b·§7.3) ─────────────


def pair(conn: sqlite3.Connection, call_uid: str) -> tuple[dict | None, dict | None]:
    """활성 분류·판정기 결과를 autoclose_reason 인자 모양으로 — 폴백은 그 결과의 LLM 호출 기록에서"""
    a, b = calls.active_versions(conn)

    def one(table: str, alias: str, version: str | None) -> dict | None:
        if version is None:
            return None
        r = conn.execute(
            f"SELECT t.status, t.labels, COALESCE(l.fallback_used, 0) AS fallback FROM {table} t"
            " LEFT JOIN ax_llm_calls l ON l.id = t.llm_call_id"
            " WHERE t.call_uid = ? AND t.model_alias = ? AND t.prompt_version = ?",
            (call_uid, alias, version),
        ).fetchone()
        if r is None:
            return None
        ok = r["status"] == "ok" and r["labels"] is not None
        return {
            "labels": json.loads(r["labels"]) if ok else None,
            "ok": ok,
            "fallback": bool(r["fallback"]),
        }

    return one("ax_call_classifications", "classify", a), one("ax_model_judgments", "judge", b)


def _blocked(conn, call_uid: str, result: dict) -> str | None:
    return None if autoclose_reason(*pair(conn, call_uid)) else "조건 안 맞음"


def _noop(conn, row) -> None:
    """앱 상태는 ax_auto_actions 행 자체 — 보드·목록·상세가 되돌리지 않은 종결 행을 읽는다"""


KINDS = {KIND: AutoKind(condition=_blocked, apply=_noop, undo=_noop)}


def _row(conn, call_uid: str):
    return conn.execute(
        "SELECT id, reverted_at FROM ax_auto_actions WHERE kind = ? AND target = ? AND period = ''",
        (KIND, call_uid),
    ).fetchone()


def policy_on(conn) -> bool:
    return bool(db.get_setting(conn, "auto_policy", {}).get(KIND))


def refresh_call(conn: sqlite3.Connection, call_uid: str, clock=datetime.now) -> str:
    """closed | opened | kept | off. 사람이 되돌린 통화는 그대로 두고(다시 닫지 않음), 조건을 못 넘게 된 종결은
    행을 지워 푼다(다음에 다시 넘으면 다시 닫힌다). 커밋은 부르는 쪽"""
    if not policy_on(conn):
        return "off"
    row = _row(conn, call_uid)
    if row and row["reverted_at"]:
        return "kept"
    reason = autoclose_reason(*pair(conn, call_uid))
    if reason and not row:
        a, b = calls.active_versions(conn)
        d = apply_auto_action(
            conn, KINDS, KIND, call_uid, result={"versions": [a, b]}, reason=reason, clock=clock
        )
        return "closed" if d.applied else "kept"
    if not reason and row:
        conn.execute("DELETE FROM ax_auto_actions WHERE id = ?", (row["id"],))
        return "opened"
    return "kept"


def run(conn: sqlite3.Connection, clock=datetime.now) -> dict:
    """앱 통화 전부를 활성 버전 쌍으로 다시 계산 — 정책이 꺼져 있으면 아무것도 하지 않는다"""
    if not policy_on(conn):
        return {"off": True}
    out = {"closed": 0, "opened": 0, "kept": 0}
    for (uid,) in conn.execute(calls.CALLS_CTE + " SELECT call_uid FROM calls").fetchall():
        out[refresh_call(conn, uid, clock)] += 1
    conn.commit()
    return out


def unclose_by_human(conn: sqlite3.Connection, call_uid: str, by: str) -> bool:
    """사람 판정이 할 일·응급·불만·급함을 켰을 때 — 사람 되돌림으로 남겨 다시 닫히지 않게"""
    row = _row(conn, call_uid)
    if not row or row["reverted_at"]:
        return False
    return revert_auto_action(conn, KINDS, row["id"], by)


def set_policy(conn: sqlite3.Connection, kind: str, on: bool) -> None:
    policy = db.get_setting(conn, "auto_policy", {})
    db.set_setting(conn, "auto_policy", policy | {kind: on})
    conn.commit()


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="python -m backend.autoclose")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="활성 버전 쌍으로 자동 종결을 다시 계산")
    pol = sub.add_parser("policy", help="auto_policy 켜고 끄기")
    pol.add_argument("kind")
    pol.add_argument("state", choices=("on", "off"))
    args = p.parse_args(argv)
    conn = db.connect(ROOT / "data" / "app.sqlite", ROOT / "data" / "source.sqlite")
    if args.cmd == "policy":
        set_policy(conn, args.kind, args.state == "on")
        print(f"auto_policy {args.kind} = {args.state}")
        return
    r = run(conn)
    print("auto_policy가 꺼져 있어 아무것도 하지 않음" if r.get("off") else f"자동 종결 {r}")


if __name__ == "__main__":
    main()
