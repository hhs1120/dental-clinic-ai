"""④ 진료 준비표 채점 (평가 설계 §4, 설계 docs/superpowers/specs/2026-11-10-prep-sheet-design.md §6).
저장된 ax_prep_sheets 행으로 후검증 통과율·주의사항 반영률·과잉 언급률·과잉 제안률을 낸다. 정답이 없는 코드 채점이라
분할이 없고, 주의사항이 그날 시술과 관련 있는지는 앱이 저장한 값이 아니라 master로 다시 센다(앱의 실수가 드러나게)."""

import json
from collections import Counter
from datetime import date

from generator.master import ALERT_TYPES


def _related(facts: dict) -> tuple[set[str], set[str]]:
    """(관련 주의사항 코드, 무관 주의사항 코드) — master 관련 시술 기준"""
    rel, unrel = set(), set()
    for a in facts["alerts"]:
        (rel if facts["procedure_code"] in ALERT_TYPES[a["code"]]["procedures"] else unrel).add(
            a["code"]
        )
    return rel, unrel


def _chips_on(facts: dict, rel: set[str]) -> bool:
    """예약(항상)·예약 미확인(제안 사실이 아님) 밖 칩이 하나라도 켜짐 — 사실로 다시 센다"""
    return bool(
        facts["scaling_unused"]
        or facts["balance"] > 0
        or facts["family_recall"]
        or facts["open_plan"]
        or rel
    )


def score(conn, days: list[date]) -> dict:
    """{valid, reflect, over_mention, over_suggest: {k, n}, not_generated, by_prompt_version}"""
    ds = [d.isoformat() for d in days]
    marks = ", ".join("?" * len(ds))
    rows = conn.execute(
        f"SELECT facts, suggestion, suggestion_valid, prompt_version FROM ax_prep_sheets"
        f" WHERE sheet_date IN ({marks})",
        ds,
    ).fetchall()
    out = {m: {"k": 0, "n": 0} for m in ("valid", "reflect", "over_mention", "over_suggest")}
    for facts_json, sug_json, valid, _ in rows:
        facts = json.loads(facts_json)
        s = json.loads(sug_json) if sug_json else None
        keys = {u["key"] for u in s["used_facts"]} if s else set()
        rel, unrel = _related(facts)
        out["valid"]["n"] += 1
        out["valid"]["k"] += bool(valid)
        if rel:  # 관련 주의사항이 있는 행 — 그 키가 모두 근거에 있어야(제안이 없으면 빠뜨린 것)
            out["reflect"]["n"] += 1
            out["reflect"]["k"] += all(f"alert.{c}" in keys for c in rel)
        out["over_mention"]["n"] += 1
        out["over_mention"]["k"] += any(f"alert.{c}" in keys for c in unrel)
        if s and not _chips_on(
            facts, rel
        ):  # 제안이 없는 행(후검증 실패)은 과잉 제안을 판단할 수 없다
            out["over_suggest"]["n"] += 1
            out["over_suggest"]["k"] += bool(s and s["action"] == "suggest")
    missing = conn.execute(  # 저장 행이 없는 예약 — 취소된 예약의 남은 행은 세지 않는다
        f"SELECT COUNT(*) FROM source.emr_appointments a WHERE a.status != 'cancelled'"
        f" AND date(a.scheduled_at) IN ({marks}) AND NOT EXISTS (SELECT 1 FROM ax_prep_sheets s"
        " WHERE s.appointment_id = a.id AND s.sheet_date = date(a.scheduled_at))",
        ds,
    ).fetchone()[0]
    return out | {
        "not_generated": missing,
        "by_prompt_version": dict(Counter(r[3] for r in rows)),
    }
