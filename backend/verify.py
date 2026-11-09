"""숫자 후검증 (기능 정의 §4 ③ 챗·④ 제안 후검증과 같은 방식). 답·근거에 나온 숫자가 모두 도구 결과·입력 글에
있어야 한다 — 지어낸 날짜·금액·건수를 막는다. 쉼표를 뗀 정수로 비교하고, 합계·날짜 계산은 허용하지 않는다."""

import json
import re


def numbers(text: str) -> set[int]:
    return {int(n) for n in re.findall(r"\d+", text.replace(",", ""))}


def _list_lengths(value) -> set[int]:
    if isinstance(value, list):
        return {len(value)}.union(*(_list_lengths(v) for v in value))
    if isinstance(value, dict):
        return set().union(*(_list_lengths(v) for v in value.values()))
    return set()


def allowed_numbers(sources: list[str], results=(), count_rows: bool = False) -> set[int]:
    """입력 글과 도구 결과의 숫자. count_rows면 결과 안 목록의 행 수도(모델이 목록을 세어 낸 건수 — 챗만)"""
    have = set().union(*(numbers(s) for s in sources)) if sources else set()
    for r in results:
        have |= numbers(json.dumps(r, ensure_ascii=False, default=str))
        if count_rows:
            have |= _list_lengths(r)
    return have


def numbers_ok(text: str, allowed: set[int]) -> bool:
    return numbers(text) <= allowed


def verify_facts(summary: str, facts: list[str], sources: list[str]) -> bool:
    """처리안의 숫자가 모두 도구 결과나 녹취록에 있으면 참(① 후속 에이전트)"""
    return numbers_ok(" ".join([summary, *facts]), allowed_numbers(sources))
