"""⑤-2b 추가 처치 누락: 청구 줄마다 차트 서술에 그날 시행했다고 적혔는데 그 청구에 없는 처치를 AI에 짧게 묻고(처치 이름·
치아·인용), 인용이 차트에 실제로 있는지 대조해 저장한다(설계 docs/superpowers/specs/2026-11-10-claim-review-design.md §2b,
틀은 2a backend/claim_check.py). 급여 여부는 AI에 묻지 않고 읽을 때 코드가 시술 표(emr_procedures)로 맞춰 비급여·치아 단위
아님·그날 나이에 급여 아님·청구된 시술·치아와 같은 것을 거른다. 청구 상태·조정 결과는 입력에 넣지 않는다."""

import json
import re

from backend.claim_check import found

TASK, ALIAS, PROMPT_VERSION = "claim_omission", "chat", "v1"
_TOOTH = re.compile(r"[1-4][1-8]")  # 영구치 치아 번호(FDI)


def schema(names: list[str]) -> dict:
    """출력 스키마 — 처치 이름은 시술 표의 이름 중에서(별칭·일상어를 코드가 맞추지 않아도 되게)"""
    return {
        "type": "object",
        "properties": {
            "omissions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "procedure": {"type": "string", "enum": names},
                        "tooth": {"type": ["string", "null"]},
                        "quote": {"type": "string"},
                    },
                    "required": ["procedure", "tooth", "quote"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["omissions"],
        "additionalProperties": False,
    }


def omission_facts(row: dict) -> dict:
    """LLM 입력 — 청구된 시술·치아·나이·차트 서술. 청구 상태·조정 사유는 row에 있어도 넣지 않는다"""
    return {
        "procedure": row["procedure_name"],
        "tooth": None if row["tooth_no"] == "00" else row["tooth_no"],
        "age": row["age"],
        "note": row["note"] or "",
    }


def check_result(facts: dict, r: dict) -> bool:
    """후검증 — 모든 인용이 차트 서술에 글자 그대로 있다"""
    return all(found(o["quote"], facts["note"]) for o in r["omissions"])


def procedure_index(rows: list[dict]) -> dict[str, dict]:
    """처치 이름·별칭 → 시술 행. 별칭이 시술 둘 이상에 걸리면('임플란트' — 1차·2차) 그 별칭은 버린다(급여를 추측하지
    않음), 표준 이름이 별칭보다 앞선다"""
    by_alias: dict[str, list[dict]] = {}
    for p in rows:
        for a in json.loads(p["aliases"] or "[]"):
            by_alias.setdefault(a, []).append(p)
    out = {a: ps[0] for a, ps in by_alias.items() if len(ps) == 1}
    out.update({p["name"]: p for p in rows})
    return out


def _insured(p: dict, age: int, age_range: dict) -> bool:
    """그 나이에 급여인지 — is_insured가 no가 아니고 적용 연령 안(generator.master.insured_on과 같은 규칙)"""
    if p["is_insured"] == "no":
        return False
    lo, hi = age_range.get(p["age_group"], (0, None))
    return lo <= age and (hi is None or age <= hi)


def insured_items(
    items: list[dict], claimed_code: str, facts: dict, index: dict, age_range: dict
) -> list[dict]:
    """AI 항목 중 청구에서 빠진 급여 처치만 — [{procedure_code, procedure, tooth, quote}].
    시술 표에 없는 이름, 비급여, 치아 단위가 아닌 시술, 치아 번호가 아닌 값, 그날 나이에 급여가 아닌 경우,
    청구된 (시술, 치아)와 같은 항목, 이미 나온 (시술, 치아)는 버린다"""
    out, seen = [], set()
    for o in items:
        p = index.get(o["procedure"])
        tooth = o["tooth"]
        if p is None or not p["is_tooth_level"] or not tooth or not _TOOTH.fullmatch(tooth):
            continue
        key = (p["code"], tooth)
        if not _insured(p, facts["age"], age_range) or key in seen:
            continue
        if key == (claimed_code, facts["tooth"]):
            continue
        seen.add(key)
        out.append(
            {
                "procedure_code": p["code"],
                "procedure": p["name"],
                "tooth": tooth,
                "quote": o["quote"],
            }
        )
    return out
