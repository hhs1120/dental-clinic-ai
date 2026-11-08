"""① 자동 종결 오류율 (평가 설계 §5). 앱의 조건 함수(backend/autoclose.py)를 그대로 써서, 조건을 넘은 통화 중 골드가
할 일·응급·불만·급함(urgent)인 비율을 잰다. dev는 silver 검증 구간, test는 test 판정 풀 — 판정 풀은 앱이 자동
처리하지 않으므로 여기서 조건 함수를 직접 돌린다."""

import json

from backend.autoclose import autoclose_reason
from evaluation.classification import _calls, _classifications, _judgments
from evaluation.metrics import wilson
from evaluation.splits import silver_split

ERROR_ITEMS = ("has_todo", "emergency", "complaint", "urgent")


def _population(calls: dict, split: str) -> set:
    if split == "test":
        return {u for u, c in calls.items() if c["split"] == "test" and c["pool"]}
    silver = [
        {
            "call_uid": u,
            "primary_purpose": c["gold"]["primary_purpose"],
            "emergency": c["gold"]["emergency"],
            "complaint": c["gold"]["complaint"],
        }
        for u, c in calls.items()
        if c["split"] == "silver"
    ]
    return {u for u, part in silver_split(silver).items() if part == "validation"}


def _arg(row: dict | None, todo=None, summary_fallback=False) -> dict | None:
    """앱 backend/autoclose.py pair()와 같은 모양 — 분류는 라벨·요약 호출 중 하나라도 폴백이면 폴백"""
    if row is None:
        return None
    ok = row["status"] == "ok" and row["labels"] is not None
    return {
        "labels": row["labels"],
        "ok": ok,
        "fallback": bool(row["fallback"]) or summary_fallback,
        "todo": todo or [],
    }


def evaluate(conn, split: str, a: str, b: str) -> dict:
    """분류 버전 a × 판정기 버전 b 한 쌍"""
    calls = _calls(conn)
    pop = _population(calls, split)
    cls = {r["call_uid"]: r for r in _classifications(conn) if r["prompt_version"] == a}
    jud = {r["call_uid"]: r for r in _judgments(conn) if r["prompt_version"] == b}
    todo, summary_fb = {}, {}
    for u, t, fb in conn.execute(
        "SELECT k.call_uid, k.todo, COALESCE(s.fallback_used, 0) FROM ax_call_classifications k"
        " LEFT JOIN ax_llm_calls s ON s.id = k.summary_llm_call_id"
        " WHERE k.model_alias = 'classify' AND k.prompt_version = ?",
        (a,),
    ):
        todo[u], summary_fb[u] = (json.loads(t) if t else []), bool(fb)
    closed = [
        u
        for u in sorted(pop)
        if autoclose_reason(
            _arg(cls.get(u), todo.get(u), summary_fb.get(u, False)), _arg(jud.get(u))
        )
    ]
    by_item = {i: 0 for i in ERROR_ITEMS}
    wrong = 0
    for u in closed:
        g = calls[u]["gold"]
        hit = {
            "has_todo": bool(g["has_todo"]),
            "emergency": bool(g["emergency"]),
            "complaint": bool(g["complaint"]),
            "urgent": g["urgency"] == "urgent",
        }
        for i, v in hit.items():
            by_item[i] += v
        wrong += any(hit.values())
    return {
        "pair": f"{a}×{b}",
        "n": len(pop),
        "closed": len(closed),
        "rate": len(closed) / len(pop) if pop else None,
        "errors": {"k": wrong, "n": len(closed), "ci": wilson(wrong, len(closed))},
        "by_item": by_item,
    }
