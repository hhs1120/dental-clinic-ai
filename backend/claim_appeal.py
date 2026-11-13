"""⑤-4 재심 사유서 초안: 사람이 "재심 신청"을 기록한 조정 건에 AI가 사유서 문장을 쓴다. 모든 문장은 조정 내역·기준 절·차트
인용이거나 추측 목록에 묶이고, 인용 구절은 원문에 있고 그 문장 안에도 글자 그대로 있어야 한다(설계
docs/superpowers/specs/2026-11-10-claim-review-design.md §4). 청구 금액·판단 기록은 입력에 넣지 않는다. 사람이 검토
확인한 뒤에만 제출 결정(기록만 — AI는 제출하지 않는다)."""

import json
import re
import sqlite3
from datetime import date, timedelta

from backend import claim_store
from backend.claim_check import CUT, found, section_cards
from backend.prep import master_values

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


KIND = claim_store.Kind(
    table="ax_claim_appeals",
    task=TASK,
    alias=ALIAS,
    version=PROMPT_VERSION,
    facts=appeal_facts,
    schema=lambda conn: SCHEMA,
    check=check_result,
    empty={"sentences": [], "assumptions": []},  # 조정 사유는 늘 있어 쓰지 않는다
    text_key="adjust_note",
)


# ── 대상·저장·초안·검토·제출 ──────────────────────────────────────


def row(conn: sqlite3.Connection, claim_id: int) -> dict | None:
    """조정·불능 청구 한 줄 + 그 사유의 기준 절(heading·body) — 없거나 조정·불능이 아니면 None. 판단·기한은 보지 않는다
    (평가가 정답 다툴 만함 건에 같은 입력을 쓴다)"""
    r = conn.execute(
        "SELECT c.id AS claim_id, c.status, c.adjust_note, c.reviewed_at, c.tooth_no,"
        " pr.name AS procedure_name, n.note, j.judgment"
        " FROM source.emr_claims c"
        " JOIN source.emr_procedures pr ON pr.code = c.procedure_code"
        " LEFT JOIN source.emr_chart_notes n ON n.treatment_id = c.treatment_id"
        " LEFT JOIN ax_claim_judgments j ON j.claim_id = c.id WHERE c.id = ?",
        (claim_id,),
    ).fetchone()
    if r is None or r["status"] not in CUT:
        return None
    out = dict(r)
    out["heading"] = master_values(conn)["adjust_note_headings"][out["adjust_note"]]
    card = section_cards(conn).get(out["heading"])
    out["body"] = card["body"] if card else ""
    return out


def target(conn: sqlite3.Connection, claim_id: int, today: date) -> dict:
    """초안을 쓸 행 — 사람이 재심 신청(contestable)을 기록했고 재심 신청 기한이 남은 조정·불능 건, 아니면 ValueError"""
    r = row(conn, claim_id)
    if r is None or r["judgment"] != "contestable":
        raise ValueError("재심 신청을 기록한 조정 건만 사유서 초안을 쓸 수 있습니다")
    deadline = date.fromisoformat(r["reviewed_at"][:10]) + timedelta(
        days=master_values(conn)["appeal_days"]
    )
    if r["reviewed_at"] > today.isoformat() or deadline < today:
        raise ValueError("재심 신청 기한이 지났습니다")
    return r


def _row(conn, claim_id: int):
    return conn.execute(
        "SELECT * FROM ax_claim_appeals WHERE claim_id = ?", (claim_id,)
    ).fetchone()


def _view(r) -> dict:
    got = json.loads(r["result"]) if r["result"] else None
    return {
        "sentences": got["sentences"] if got else None,
        "assumptions": got["assumptions"] if got else None,
        "valid": got is not None,
        "reviewed": {"by": r["reviewed_by"], "at": r["reviewed_at"]} if r["reviewed_by"] else None,
        "submitted": {"by": r["submitted_by"], "at": r["submitted_at"]}
        if r["submitted_by"]
        else None,
        "at": r["created_at"],
    }


def appeal(conn: sqlite3.Connection, claim_id: int) -> dict | None:
    """저장된 초안 — {sentences, assumptions, valid, reviewed, submitted, at}, 후검증 실패는 sentences None, 없으면 None"""
    r = _row(conn, claim_id)
    return None if r is None else _view(r)


def appeals(conn: sqlite3.Connection) -> dict[int, dict]:
    """저장된 초안 전부 {claim_id: appeal} — 조정 목록이 표마다 한 번 읽는다"""
    return {r["claim_id"]: _view(r) for r in conn.execute("SELECT * FROM ax_claim_appeals")}


def submitted(conn: sqlite3.Connection, claim_id: int) -> bool:
    r = _row(conn, claim_id)
    return bool(r and r["submitted_by"])


def draft(conn: sqlite3.Connection, gw, claim_id: int, today: date) -> dict:
    """초안 쓰기·다시 쓰기 — 일반 호출(사람이 기다림), 후검증 실패면 한 번 더(claim_store). 있던 초안과 검토 확인은
    지운다. 제출 결정한 건은 ValueError"""
    r = target(conn, claim_id, today)
    if submitted(conn, claim_id):
        raise ValueError("재심 사유서를 제출 결정한 건은 다시 쓸 수 없습니다")
    with conn:
        conn.execute("DELETE FROM ax_claim_appeals WHERE claim_id = ?", (claim_id,))
    claim_store.build(conn, gw, KIND, [r], use_batch=False)
    return appeal(conn, claim_id)


def review(conn: sqlite3.Connection, claim_id: int, by: str, at: str, checked: bool) -> None:
    """검토 확인(체크·해제) — 통과한 초안만, 제출 결정 뒤에는 바꿀 수 없다"""
    a = appeal(conn, claim_id)
    if a is None or not a["valid"]:
        raise ValueError("검토할 초안이 없습니다")
    if a["submitted"]:
        raise ValueError("제출 결정한 사유서의 검토 확인은 바꿀 수 없습니다")
    with conn:
        conn.execute(
            "UPDATE ax_claim_appeals SET reviewed_by = ?, reviewed_at = ? WHERE claim_id = ?",
            (by if checked else None, at if checked else None, claim_id),
        )


def submit(conn: sqlite3.Connection, claim_id: int, by: str, at: str, today: date) -> None:
    """제출 결정 기록(AI는 제출하지 않는다) — 재심 신청 판단·통과한 초안·검토 확인이 있어야"""
    target(conn, claim_id, today)
    a = appeal(conn, claim_id)
    if a is None or not a["valid"]:
        raise ValueError("제출할 초안이 없습니다")
    if not a["reviewed"]:
        raise ValueError("검토 확인 전에는 제출 결정을 할 수 없습니다")
    if a["submitted"]:
        raise ValueError("이미 제출 결정한 사유서입니다")
    with conn:
        conn.execute(
            "UPDATE ax_claim_appeals SET submitted_by = ?, submitted_at = ? WHERE claim_id = ?",
            (by, at, claim_id),
        )
