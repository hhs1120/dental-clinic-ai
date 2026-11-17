"""④ 진료 준비표 채점 (평가 설계 §4, 설계 docs/superpowers/specs/2026-11-10-prep-sheet-design.md §6).
저장된 ax_prep_sheets 행으로 후검증 통과율·주의사항 반영률·과잉 언급률·과잉 제안률을 낸다. 정답이 없는 코드 채점이라
분할이 없고, 주의사항이 그날 시술과 관련 있는지는 앱이 저장한 값이 아니라 master로 다시 센다(앱의 실수가 드러나게)."""

import json
from collections import Counter
from datetime import date

from backend.prep import check_suggestion, llm_facts
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
    """예약(항상)·내원 확인 전(제안 사실이 아님) 밖 칩이 하나라도 켜짐 — 사실로 다시 센다"""
    return bool(
        facts["scaling_unused"]
        or facts["balance"] > 0
        or facts["family_recall"]
        or facts["open_plan"]
        or rel
    )


def _blank() -> dict:
    return {m: {"k": 0, "n": 0} for m in ("valid", "reflect", "over_mention", "over_suggest")}


def _template(facts: dict) -> dict:
    """템플릿 기준선 — 실제 LLM 입력(backend.prep.llm_facts)에서 켜진 칩의 키·값을 모두 근거로, 본문은 그 값을 이어
    붙인 글(LLM 없음). 주의 칩은 앱이 저장한 related로 고른다. 칩이 다 꺼지면 none"""
    f = llm_facts(facts)
    related = {f"alert.{a['code']}" for a in facts["alerts"] if a["related"]}
    keys = [
        k
        for k in f
        if k in related or k in ("balance", "scaling_unused") or k.startswith(("plan.", "family."))
    ]
    if not keys:
        return {"action": "none", "text": "", "used_facts": []}
    used = [{"key": k, "value": str(f[k])} for k in keys]
    return {"action": "suggest", "text": " · ".join(u["value"] for u in used), "used_facts": used}


def _tally(out: dict, facts: dict, s: dict | None, valid: bool) -> None:
    """한 행을 네 지표에 더한다"""
    keys = {u["key"] for u in s["used_facts"]} if s else set()
    rel, unrel = _related(facts)
    out["valid"]["n"] += 1
    out["valid"]["k"] += valid
    if rel:  # 관련 주의사항이 있는 행 — 그 키가 모두 근거에 있어야(제안이 없으면 빠뜨린 것)
        out["reflect"]["n"] += 1
        out["reflect"]["k"] += all(f"alert.{c}" in keys for c in rel)
    out["over_mention"]["n"] += 1
    out["over_mention"]["k"] += any(f"alert.{c}" in keys for c in unrel)
    if s and not _chips_on(facts, rel):  # 제안이 없는 행(후검증 실패)은 과잉 제안을 판단할 수 없다
        out["over_suggest"]["n"] += 1
        out["over_suggest"]["k"] += s["action"] == "suggest"


def score(conn, days: list[date]) -> dict:
    """{valid, reflect, over_mention, over_suggest: {k, n}, template: 같은 네 지표, not_generated, by_prompt_version}"""
    ds = [d.isoformat() for d in days]
    marks = ", ".join("?" * len(ds))
    rows = conn.execute(
        # 승인된 AX 예약의 행(appointment_id "ax-<id>", 글)은 빼고 원본 예약만 — 평가 경로는 원본만(AI 비서 설계 §3.3)
        f"SELECT facts, suggestion, suggestion_valid, prompt_version FROM ax_prep_sheets"
        f" WHERE sheet_date IN ({marks}) AND typeof(appointment_id) = 'integer'",
        ds,
    ).fetchall()
    out = _blank()
    template = _blank()  # 템플릿 기준선 — 같은 행 사실만으로(LLM 없이) 만든 제안을 같은 지표로
    for facts_json, sug_json, valid, _ in rows:
        facts = json.loads(facts_json)
        s = json.loads(sug_json) if sug_json else None
        _tally(out, facts, s, bool(valid))
        t = _template(facts)
        _tally(template, facts, t, check_suggestion(llm_facts(facts), t))  # 후검증도 실제로 잰다
    missing = conn.execute(  # 저장 행이 없는 예약 — 취소된 예약의 남은 행은 세지 않는다
        f"SELECT COUNT(*) FROM source.emr_appointments a WHERE a.status != 'cancelled'"
        f" AND date(a.scheduled_at) IN ({marks}) AND NOT EXISTS (SELECT 1 FROM ax_prep_sheets s"
        " WHERE s.appointment_id = a.id AND s.sheet_date = date(a.scheduled_at))",
        ds,
    ).fetchone()[0]
    return out | {
        "template": template,
        "not_generated": missing,
        "by_prompt_version": dict(Counter(r[3] for r in rows)),
    }
