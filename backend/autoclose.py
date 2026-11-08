"""① 자동 종결 (기능 정의 §4 ①·§3 원칙 1, 앱 설계 §3b·§7.3). 확인할 것이 없는 통화 — 두 모델 모두 할 일 없음이고
응급·불만·급함·엇갈림·폴백이 없는 통화 — 를 `auto_policy`가 켜졌을 때 `apply_auto_action`으로 닫는다.
조건 함수 `autoclose_reason`은 eval.py의 자동 종결 오류율도 같이 쓴다(평가 설계 §5)."""

from shared.call_labels import differing_items

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
