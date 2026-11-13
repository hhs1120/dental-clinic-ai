"""⑤-2b 추가 처치 누락: 청구 줄마다 차트 서술에 그날 시행했다고 적혔는데 그 청구에 없는 처치를 AI에 짧게 묻고(처치 이름·
치아·인용), 인용이 차트에 실제로 있는지 대조해 저장한다(설계 docs/superpowers/specs/2026-11-10-claim-review-design.md §2b,
틀은 2a backend/claim_check.py). 급여 여부는 AI에 묻지 않고 읽을 때 코드가 시술 표(emr_procedures)로 맞춰 비급여·치아 단위
아님·그날 나이에 급여 아님·청구된 시술·치아와 같은 것을 거른다. 청구 상태·조정 결과는 입력에 넣지 않는다."""

import argparse
import json
import re
import sqlite3
from datetime import date
from pathlib import Path

from backend import claim_store, db
from backend.claim_check import found
from backend.prep import _age, master_values
from shared.llm import load_config

ROOT = Path(__file__).resolve().parents[1]
TASK, ALIAS, PROMPT_VERSION = "claim_omission", "chat", "v2"
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
                        "tooth": {"anyOf": [{"type": "string"}, {"type": "null"}]},
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
    """그 나이에 급여인지 — is_insured가 no가 아니고 적용 연령 안(가상 데이터 생성 설계 §2.1 급여 조건과 같은 규칙)"""
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


# ── 대상·저장·실행 ────────────────────────────────────────────────

SCOPES = {
    "app": ("pending",),
    "all": ("submitted", "pending"),
}  # 앱 화면은 대기 줄, 평가는 제출·대기(설계 §2b)


def targets(
    conn: sqlite3.Connection, today: date, *, scope: str, claim_ids: list[int] | None = None
) -> list[dict]:
    """점검할 청구 줄 — scope "app"은 대기 줄, "all"은 제출·대기 줄(요건 대상 여부와 무관). claim_ids가 있으면 그 줄만.
    today는 2a targets와 같은 꼴로 받는다(대상이 상태로만 정해져 쓰지 않음)"""
    statuses = SCOPES[scope]
    keep = None if claim_ids is None else set(claim_ids)
    out = []
    for r in conn.execute(
        "SELECT c.id AS claim_id, c.procedure_code, pr.name AS procedure_name, c.tooth_no,"
        " c.service_date, c.status, n.note, p.birth_date"
        " FROM source.emr_claims c"
        " JOIN source.emr_procedures pr ON pr.code = c.procedure_code"
        " JOIN source.emr_patients p ON p.id = c.patient_id"
        " LEFT JOIN source.emr_chart_notes n ON n.treatment_id = c.treatment_id"
        f" WHERE c.status IN ({', '.join('?' * len(statuses))}) ORDER BY c.id",
        statuses,
    ):
        row = dict(r)
        if keep is not None and row["claim_id"] not in keep:
            continue
        row["age"] = _age(row.pop("birth_date"), date.fromisoformat(row["service_date"]))
        out.append(row)
    return out


def _names(conn) -> list[str]:
    return [r[0] for r in conn.execute("SELECT name FROM source.emr_procedures ORDER BY code")]


KIND = claim_store.Kind(
    table="ax_claim_omissions",
    task=TASK,
    alias=ALIAS,
    version=PROMPT_VERSION,
    facts=omission_facts,
    schema=lambda conn: schema(_names(conn)),
    check=check_result,
    empty={"omissions": []},
)


def _request(facts: dict, names: list[str]) -> dict:
    """평가 합성 사례가 앱과 같은 요청을 쓴다(evaluation/claim_omission.py)"""
    return claim_store.request(KIND, facts, schema(names))


def _valid(result, facts: dict):
    """통과한 결과 dict, 아니면 None"""
    return claim_store.valid(KIND, result, facts)


def todo_ids(conn: sqlite3.Connection, today: date, model: str | None = None) -> set[int]:
    """앱 범위에서 다시 불러야 할 줄(서술 없는 줄 제외) — 화면 갱신 버튼이 2a 할 일과 합쳐 센다"""
    model = model or load_config()["aliases"][ALIAS][0]
    return claim_store.todo_ids(conn, KIND, targets(conn, today, scope="app"), model)


def build(conn, gw, rows: list[dict], *, use_batch: bool, poll_seconds: int = 30) -> dict:
    """줄마다 묻고 저장한다(backend/claim_store.py build). 서술 없는 줄은 부르지 않고 빈 목록으로"""
    return claim_store.build(conn, gw, KIND, rows, use_batch=use_batch, poll_seconds=poll_seconds)


def results(conn: sqlite3.Connection, claim_ids: list[int] | None = None) -> dict[int, dict]:
    """저장된 점검 — {claim_id: {omissions: 거른 항목 | None, valid, at, prompt_version}}, 후검증 실패는 valid False.
    claim_ids가 없으면 저장된 줄 전부"""
    index = procedure_index([dict(r) for r in conn.execute("SELECT * FROM source.emr_procedures")])
    age_range = master_values(conn)["age_group_range"]
    out = {}
    claims = {r[0]: r[1] for r in conn.execute("SELECT id, procedure_code FROM source.emr_claims")}
    for cid, r in claim_store.saved(conn, KIND, claim_ids).items():
        got = json.loads(r["result"]) if r["result"] else None
        out[cid] = {
            "omissions": None
            if got is None
            else insured_items(
                got["omissions"], claims[cid], json.loads(r["facts"]), index, age_range
            ),
            "valid": got is not None,
            "at": r["created_at"],
            "prompt_version": r["prompt_version"],
        }
    return out


def main(argv=None) -> None:
    from backend.harness.llm import make_gateway
    from shared.clinic import load_clinic

    p = argparse.ArgumentParser(prog="python -m backend.claim_omission")
    p.add_argument("command", choices=("run",))
    p.add_argument(
        "--scope", choices=("app", "all"), default="app", help="app: 대기 줄, all: 제출·대기 줄"
    )
    p.add_argument("--no-batch", action="store_true", help="Batch 없이 일반 호출만")
    args = p.parse_args(argv)
    conn = db.connect(ROOT / "data" / "app.sqlite", ROOT / "data" / "source.sqlite")
    db.init_schema(conn)
    rows = targets(conn, load_clinic().today, scope=args.scope)
    print(build(conn, make_gateway(conn), rows, use_batch=not args.no_batch))


if __name__ == "__main__":
    main()
