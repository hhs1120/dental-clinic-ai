"""앱 프롬프트 불러오기 (앱 설계 §7.2). 파일명이 곧 버전 — prompts/<task>/<version>.md. 분류기는 호출이 둘이라
버전 폴더 prompts/classify/<version>/의 labels.md·summary.md(분류기 설계 "분류기"). 머리말(--- 사이 YAML)은 떼고 본문만
"""

import re
from pathlib import Path

import yaml

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"
CLASSIFY_PARTS = ("labels", "summary")


EFFORTS = ("low", "medium", "high", "xhigh", "max")  # 두 프로바이더가 받는 추론 단계(앱 설계 §7.2)
FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---(?:\n|\Z)", re.S)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def _body(path: Path) -> str:
    return FRONT_MATTER.sub("", _text(path), count=1).strip()


def load(task: str, version: str, root: Path = PROMPTS_DIR) -> dict[str, str]:
    """classify면 {"labels", "summary"}, 그 밖은 {"system"}. 없는 버전은 FileNotFoundError"""
    if task == "classify":
        return {part: _body(root / task / version / f"{part}.md") for part in CLASSIFY_PARTS}
    return {"system": _body(root / task / f"{version}.md")}


def summary_kinds(version: str, root: Path = PROMPTS_DIR) -> bool:
    """분류기 요약 프롬프트 머리말 todo_kinds — 참이면 할 일을 종류로 나눈 스키마(통화·② 판정 화면 설계 "분류기 v2")"""
    m = FRONT_MATTER.match(_text(root / "classify" / version / "summary.md"))
    return bool(m and (yaml.safe_load(m[1]) or {}).get("todo_kinds"))


def effort(task: str, version: str, root: Path = PROMPTS_DIR) -> str | None:
    """버전 파일 머리말의 effort(추론 단계, 앱 설계 §7) — 없으면 None. 분류기 버전 폴더는 아직 쓰지 않는다"""
    m = FRONT_MATTER.match(_text(root / task / f"{version}.md"))
    value = (yaml.safe_load(m[1]) or {}).get("effort") if m else None
    if value is not None and value not in EFFORTS:  # YAML은 off·no를 False로 읽는다
        raise ValueError(f"{task}/{version}: effort {value!r}는 {EFFORTS} 중 하나여야 함")
    return value
