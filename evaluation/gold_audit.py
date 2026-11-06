"""골드 점검 — 판정기와 골드가 다른 통화를 중재 판정기(LLM)가 가리고 사람이 표본으로 확인한다.
원본은 평가 설계 §2.3 "골드 점검", 이 모듈이 정한 세부는 docs/superpowers/specs/2026-11-06-gold-audit-design.md.
골드를 보므로 evaluation/에서만 돌고, 기록·호출 로그도 evaluation/에 쓴다(app.sqlite에 쓰지 않는다 — CLAUDE.md 절대 규칙).
"""

import hashlib
from pathlib import Path

from evaluation.classification import _calls, _judgments, _usable
from shared.call_labels import COUNTERPART_KO, FLAG_KO, PURPOSE_KO, differing_items

ROOT = Path(__file__).resolve().parents[1]
PROMPT_DIR = ROOT / "prompts" / "gold_audit"

ITEM_KO = {
    "primary_purpose": "용건",
    "counterpart": "통화 상대",
    "urgency": "급한 정도",
    **FLAG_KO,
}
THREE_WAY = (
    "primary_purpose",
    "counterpart",
)  # 둘 다 틀릴 수 있는 항목 — 나머지는 둘 중 하나가 반드시 맞다
SIDES = {"가", "나"}
NEITHER = "둘 다 아님"


def targets(conn, judge_version: str) -> list[dict]:
    """test 판정 풀이 아닌 생성 통화 중 판정기 라벨이 골드와 다른 통화(급함은 urgent 여부만 — differing_items)"""
    calls = _calls(conn)
    judged = _usable(_judgments(conn), judge_version, calls)
    direction = dict(conn.execute("SELECT call_uid, direction FROM source.ax_calls"))
    out = []
    for uid in sorted(judged):
        c = calls[uid]
        if c["split"] == "test":
            continue
        labels = judged[uid]["labels"]
        diff = differing_items(labels, c["gold"])
        if not diff:
            continue
        out.append(
            {
                "call_uid": uid,
                "direction": direction[uid],
                "transcript": c["transcript"],
                "transcript_sha": transcript_sha(c["transcript"]),
                "items": {i: {"gold": c["gold"][i], "judge": labels[i]} for i in diff},
            }
        )
    return out


def transcript_sha(transcript: str) -> str:
    return hashlib.sha256(transcript.encode()).hexdigest()


def gold_side(call_uid: str, item: str) -> str:
    """골드를 가·나 중 어디에 둘지 — 통화·항목마다 해시로 섞는다(평가 설계 §2.3)"""
    return "가" if hashlib.sha256(f"{call_uid}:{item}".encode()).digest()[0] % 2 == 0 else "나"


def choices(item: str) -> tuple[str, ...]:
    return ("가", "나", NEITHER) if item in THREE_WAY else ("가", "나")


def show(item: str, value, codes: bool = False) -> str:
    """라벨 값을 우리말로 — 급함은 대상이 urgent 여부로 정해져 두 값으로만 보인다.
    codes면 프롬프트 정의와 맞게 코드 값을 앞에(중재 입력용)"""
    if item == "urgency":
        return "급함" if value == "urgent" else "급하지 않음"
    if item in THREE_WAY:
        ko = (PURPOSE_KO if item == "primary_purpose" else COUNTERPART_KO)[value]
        return f"{value} ({ko})" if codes else ko
    ko = "예" if value else "아니오"
    return f"{'true' if value else 'false'} ({ko})" if codes else ko


def pair(target: dict, item: str, codes: bool = False) -> dict[str, str]:
    """{가: 값, 나: 값}"""
    v = target["items"][item]
    side = gold_side(target["call_uid"], item)
    other = "나" if side == "가" else "가"
    return {side: show(item, v["gold"], codes), other: show(item, v["judge"], codes)}


def arbiter_request(target: dict) -> tuple[str, dict]:
    """중재 판정기 입력(user 메시지)과 구조화 출력 스키마 — 어느 쪽이 골드인지 드러내지 않는다"""
    lines = [
        f"방향: {target['direction']}",
        "",
        "녹취록:",
        target["transcript"],
        "",
        "고를 항목:",
    ]
    for item in target["items"]:
        p = pair(target, item, codes=True)
        lines.append(f"- {item} ({ITEM_KO[item]}): 가 = {p['가']} / 나 = {p['나']}")
    schema = {
        "type": "object",
        "properties": {i: {"type": "string", "enum": list(choices(i))} for i in target["items"]},
        "required": list(target["items"]),
        "additionalProperties": False,
    }
    return "\n".join(lines), schema


def decode(target: dict, answer: dict) -> dict[str, str]:
    """가·나·둘 다 아님 → gold·judge·neither. 항목이 빠졌거나 선택지 밖이면 ValueError"""
    out = {}
    for item in target["items"]:
        a = answer.get(item)
        if a not in choices(item):
            raise ValueError(f"{target['call_uid']} {item}: {a!r}")
        out[item] = (
            "neither"
            if a == NEITHER
            else ("gold" if a == gold_side(target["call_uid"], item) else "judge")
        )
    return out
