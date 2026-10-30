"""평가 지표 순수 함수 — 정의는 docs/치과 AX - 평가 설계.md §2.2(분류)·§3.2·§3.3(RAG)."""

import re
from collections import defaultdict
from math import comb

TOP_K = 5  # Hit@5·MRR@5 (평가 설계 §3.3)
MAX_TOOL_CALLS = 6  # 질문당 도구 호출 상한 (평가 설계 §3.2)
DATE_FROM, DATE_TO = "date_from", "date_to"


def fraction(k: int, n: int) -> str:
    """분수 병기 (평가 설계 §1 원칙 4) — '14/20 (70%)'"""
    return f"{k}/{n} ({int(100 * k / n + 0.5)}%)" if n else f"{k}/{n} (—)"


def mcnemar_exact(b: int, c: int) -> float:
    """짝 비교 유의성 — 엇갈린 문항(b: A만 맞음, c: B만 맞음)의 양측 이항 검정 p값"""
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(comb(n, i) for i in range(min(b, c) + 1)) / 2**n
    return min(1.0, 2 * tail)


def inverse_weights(population: dict, sample: dict) -> dict:
    """역확률 가중 w_c = N_c / n_c (평가 설계 §2.2) — 층화 추출로 치우친 표본을 모집단 비중으로"""
    return {c: population[c] / n for c, n in sample.items() if n}


def weighted_accuracy(rows: list[tuple], weights: dict) -> float | None:
    """rows = [(카테고리, 맞음)]"""
    total = sum(weights[c] for c, _ in rows)
    return sum(weights[c] for c, ok in rows if ok) / total if total else None


def macro_accuracy(rows: list[tuple]) -> float | None:
    """카테고리별 정확도의 단순 평균 — 카테고리 동등"""
    by = defaultdict(list)
    for c, ok in rows:
        by[c].append(ok)
    return sum(sum(v) / len(v) for v in by.values()) / len(by) if by else None


def source_id(chunk_id: str) -> str:
    """청크 id → 출처 id (앱 설계 §3 — 'doc:12:3' → 'doc:12', 'call:c-0481:0' → 'call:c-0481'. 스레드·차트는 청크 = 출처)"""
    parts = chunk_id.split(":")
    return ":".join(parts[:2]) if parts[0] in ("doc", "call") and len(parts) == 3 else chunk_id


def first_hit_rank(ranked: list[str], answers: set, k: int = TOP_K) -> int | None:
    """상위 k 안에서 출처가 정답인 첫 청크의 순위(1부터), 없으면 None"""
    return next((i for i, c in enumerate(ranked[:k], 1) if source_id(c) in answers), None)


_DATE = re.compile(r"(?<![\d.])(?:\d{4}-\d{2}-\d{2}|\d{1,2}월\s*\d{1,2}일|\d{1,2}월|\d{1,2}일)")
# 한글 단위 — 숫자에 바로 붙은 천만·백만·만·천·백 덩어리들 + 띄어 쓴 나머지는 '원' 앞일 때만 ('1만 6,300원' → 16300,
# '1만 9천 2회' → 19000·2). 한계: 원이 아닌 단위의 띄어 쓴 나머지('1만 2천 3명')는 따로 읽는다
_NUM = r"\d[\d,]*(?:\.\d+)?"
_UNITS = re.compile(rf"(?<![\d.,])(?:{_NUM}\s*(?:천만|백만|만|천|백)\s*)+(?:{_NUM}(?=\s*원))?")
_UNIT = {"천만": 10_000_000, "백만": 1_000_000, "만": 10_000, "천": 1000, "백": 100, "": 1}


def _units(m) -> str:
    """덩어리 → 수. 단위가 작아지지 않으면 거기서 다른 수('1만 2만' → 10000 20000)"""
    values, last = [], None
    for n, u in re.findall(rf"({_NUM})\s*(천만|백만|만|천|백)?", m[0]):
        v, mult = float(n.replace(",", "")) * _UNIT[u], _UNIT[u]
        if last is not None and mult < last:
            values[-1] += v
        else:
            values.append(v)
        last = mult
    return " ".join(str(int(v)) if v.is_integer() else str(v) for v in values) + " "


def numbers_in(text: str) -> list[float]:
    """답변의 수 — 날짜(YYYY-MM-DD·○월 ○일)는 빼고, 천만·백만·만·천·백 단위를 풀고(_UNITS), 쉼표를 뗀다. 소수는 소수 그대로"""
    text = _DATE.sub(" ", text or "")
    text = _UNITS.sub(_units, text)
    return [float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", text)]


def number_match(text: str, value: int) -> bool:
    """정답 숫자가 답변에 있음 — 쉼표·단위(건·명·원)를 떼고, 다른 수의 일부(126의 26)·날짜의 숫자는 아님"""
    return value in numbers_in(text)


def args_match(expected: dict, actual: dict) -> bool:
    """기대 인자 부분집합 일치 — 날짜 범위는 정답 범위를 포함하면 일치(평가 설계 §3.2), 나머지 키는 같은 값"""
    for key, want in expected.items():
        got = actual.get(key)
        if got is None:
            return False
        if key == DATE_FROM and not got <= want or key == DATE_TO and not got >= want:
            return False
        if key not in (DATE_FROM, DATE_TO) and got != want:
            return False
    return True


def tool_path(calls: list[dict], tools: list[str], args: dict) -> str | None:
    """도구 경로 판정 (평가 설계 §3.2) — 통과면 None, 실패면 이유. 목록 밖 도구 실패, 기대 도구는 순서대로 나오면 됨(사이 호출
    허용), 기대 인자는 그 도구 호출 중 하나와 일치, 호출 MAX_TOOL_CALLS회 초과 실패"""
    names = [c["name"] for c in calls]
    if len(calls) > MAX_TOOL_CALLS:
        return f"호출 {MAX_TOOL_CALLS}회 초과"
    if set(names) - set(tools):
        return "목록 밖 도구"
    it = iter(names)
    if not all(t in it for t in tools):
        return "순서"
    if not all(
        any(c["name"] == tool and args_match(want, c["args"]) for c in calls)
        for tool, want in args.items()
    ):
        return "인자"
    return None
