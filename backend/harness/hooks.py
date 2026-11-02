"""훅 (앱 설계 §3b). 하네스의 before_tool·after_tool 두 지점에 규칙을 등록한다. 규칙마다 id가 있어 차단 횟수를 센다.
before(ctx, 도구 이름, 인자)·after(ctx, 도구 이름, 인자, 결과)가 문자열을 돌려주면 그 이유로 막는다."""

from collections.abc import Callable
from dataclasses import dataclass

NOT_ALLOWED = "tool.not_allowed"
BAD_ARGS = "tool.bad_args"
READ_ONLY = "tool.read_only"


@dataclass(frozen=True)
class Rule:
    id: str
    before: Callable | None = None
    after: Callable | None = None


def first_block(rules, point: str, *args) -> tuple[str, str] | None:
    """처음 막은 규칙의 (id, 이유)"""
    for rule in rules:
        check = getattr(rule, point)
        why = check(*args) if check else None
        if why:
            return rule.id, why
    return None
