"""⑤-4 재심 사유서 초안: 사람이 "재심 신청"을 기록한 조정 건에 AI가 사유서 문장을 쓴다. 모든 문장은 조정 내역·기준 절·차트
인용이거나 추측 목록에 묶이고, 인용 구절은 원문에 있고 그 문장 안에도 글자 그대로 있어야 한다(설계
docs/superpowers/specs/2026-11-10-claim-review-design.md §4). 청구 금액·판단 기록은 입력에 넣지 않는다. 사람이 검토
확인한 뒤에만 제출 결정(기록만 — AI는 제출하지 않는다)."""

import re

from backend.claim_check import found

TASK, ALIAS, PROMPT_VERSION = "claim_appeal", "chat", "v1"
BASES = ("adjustment", "standard", "chart", "assumption")
_NULL_STR = {"anyOf": [{"type": "string"}, {"type": "null"}]}
SCHEMA = {
    "type": "object",
    "properties": {
        "sentences": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "basis": {"type": "string", "enum": list(BASES)},
                    "quote": _NULL_STR,
                    "assumption": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                },
                "required": ["text", "basis", "quote", "assumption"],
                "additionalProperties": False,
            },
        },
        "assumptions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["sentences", "assumptions"],
    "additionalProperties": False,
}
_SPLIT = re.compile(r"(?<=[.?!])\s+|\n+")  # 마침표·물음표·느낌표 뒤 공백, 줄바꿈(설계 §4 채점)


def appeal_facts(row: dict) -> dict:
    """LLM 입력 — 조정 사유 문구·시술·치아·그 사유의 기준 절(행에 붙은 heading·body)·차트 서술.
    청구 금액·상태·판단은 row에 있어도 넣지 않는다"""
    return {
        "adjust_note": row["adjust_note"],
        "procedure": row["procedure_name"],
        "tooth": None if row["tooth_no"] == "00" else row["tooth_no"],
        "standard": {"heading": row["heading"], "body": row["body"]},
        "note": row["note"] or "",
    }


def split_sentences(text: str) -> list[str]:
    return [p.strip() for p in _SPLIT.split(text) if p.strip()]


def _source(facts: dict, basis: str) -> str:
    return {
        "adjustment": facts["adjust_note"],
        "standard": facts["standard"]["body"],
        "chart": facts["note"],
    }[basis]


def _backed(facts: dict, s: dict, piece: str, n_assumptions: int) -> bool:
    """그 문장(piece)이 칸의 근거를 만족하나 — 인용은 원문과 문장 안에 있고, 추측은 번호가 목록 안"""
    if s["basis"] == "assumption":
        idx = s["assumption"]
        return isinstance(idx, int) and 1 <= idx <= n_assumptions
    q = s["quote"] or ""
    return found(q, _source(facts, s["basis"])) and found(q, piece)


def check_result(facts: dict, r: dict) -> bool:
    """후검증 — 문장이 하나 이상, 추측 항목은 빈 글이 아님, 칸마다 한 문장이고 그 근거를 만족"""
    n = len(r["assumptions"])
    if not r["sentences"] or not all(a.strip() for a in r["assumptions"]):
        return False
    for s in r["sentences"]:
        pieces = split_sentences(s["text"])
        if len(pieces) != 1 or s["basis"] not in BASES or not _backed(facts, s, pieces[0], n):
            return False
    return True


def unbacked(facts: dict, r: dict) -> tuple[int, int]:
    """(나눈 문장 수, 근거 없는 문장 수) — 칸을 나눈 문장마다 그 칸의 근거를 따로 본다(평가: 첫 답 원출력)"""
    n = len(r.get("assumptions") or [])
    total = bad = 0
    for s in r.get("sentences") or []:
        for piece in split_sentences(s.get("text") or ""):
            total += 1
            bad += not (s.get("basis") in BASES and _backed(facts, s, piece, n))
    return total, bad
