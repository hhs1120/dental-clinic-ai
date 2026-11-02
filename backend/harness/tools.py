"""도구 등록 (앱 설계 §3b 도구 실행). 인자는 Pydantic으로 검사·정규화한다(평가 설계 §3.2)."""

from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args: type[BaseModel]
    # fn(ctx, args dict) → JSON으로 바꿀 수 있는 값. 하네스가 연 트랜잭션 안에서 돈다 — 커밋하지 않고, 같은 연결로
    # LLM을 부르지 않는다(호출 기록이 커밋해 쓰기와 단계 기록이 갈라진다). LLM이 필요한 일은 도구 밖에서 한다
    fn: Callable
    writes: bool = False  # ax_ 테이블에 쓰는 도구. False인데 쓰면 롤백하고 막는다

    def schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.args.model_json_schema(),
        }


class ToolRegistry:
    def __init__(self):
        self.tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self.tools[tool.name] = tool

    def schemas(self, names) -> list[dict]:
        return [self.tools[n].schema() for n in names]
