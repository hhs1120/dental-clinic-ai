"""대본대로 답하는 게이트웨이 — 하네스 테스트용."""

import copy

from shared.llm import LLMResult


class Script:
    """Gateway.complete()와 같은 인터페이스 — 대본의 응답을 순서대로 낸다. 예외를 넣으면 던진다"""

    def __init__(self, *turns):
        self.turns, self.calls = list(turns), []

    def complete(self, alias, **kwargs):
        self.calls.append(copy.deepcopy({"alias": alias, **kwargs}))  # 호출 시점의 내용
        turn = self.turns.pop(0)
        if isinstance(turn, BaseException):
            raise turn
        return turn


def answer(text="끝", cost=0.01):
    return LLMResult(
        True,
        text,
        None,
        "m",
        False,
        1,
        1,
        cost,
        1,
        stop_reason="end_turn",
        content=[{"type": "text", "text": text}],
    )


def call(*tools, cost=0.01):
    """tools: (이름, 인자) 또는 (id, 이름, 인자)"""
    calls = []
    for i, t in enumerate(tools):
        tid, name, args = t if len(t) == 3 else (f"t{i}", *t)
        calls.append({"id": tid, "name": name, "input": args})
    return LLMResult(
        True,
        "",
        None,
        "m",
        False,
        1,
        1,
        cost,
        1,
        stop_reason="tool_use",
        tool_calls=tuple(calls),
        content=[{"type": "tool_use", **c} for c in calls],
    )
