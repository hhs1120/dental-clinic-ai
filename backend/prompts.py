"""앱 프롬프트 불러오기 (앱 설계 §7.2). 파일명이 곧 버전 — prompts/<task>/<version>.md. 분류기는 호출이 둘이라
버전 폴더 prompts/classify/<version>/의 labels.md·summary.md(분류기 설계 "분류기"). 머리말(--- 사이 YAML)은 떼고 본문만
"""

import re
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"
CLASSIFY_PARTS = ("labels", "summary")


def _body(path: Path) -> str:
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    return re.sub(r"\A---\n.*?\n---(?:\n|\Z)", "", text, count=1, flags=re.S).strip()


def load(task: str, version: str, root: Path = PROMPTS_DIR) -> dict[str, str]:
    """classify면 {"labels", "summary"}, 그 밖은 {"system"}. 없는 버전은 FileNotFoundError"""
    if task == "classify":
        return {part: _body(root / task / version / f"{part}.md") for part in CLASSIFY_PARTS}
    return {"system": _body(root / task / f"{version}.md")}
