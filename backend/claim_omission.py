"""⑤-2b 추가 처치 누락: 청구 줄마다 차트 서술에 그날 시행했다고 적혔는데 그 청구에 없는 처치를 AI에 짧게 묻고(처치 이름·
치아·인용), 인용이 차트에 실제로 있는지 대조해 저장한다(설계 docs/superpowers/specs/2026-11-10-claim-review-design.md §2b,
틀은 2a backend/claim_check.py). 급여 여부는 AI에 묻지 않고 읽을 때 코드가 시술 표(emr_procedures)로 맞춰 비급여·치아 단위
아님·그날 나이에 급여 아님·청구된 시술·치아와 같은 것을 거른다. 청구 상태·조정 결과는 입력에 넣지 않는다."""

import argparse
import json
import re
import sqlite3
import sys
import time
from datetime import date, datetime
from pathlib import Path

from backend import db, prompts
from backend.claim_check import _hash, found
from backend.prep import _age, _batchable, master_values
from shared.llm import load_config

ROOT = Path(__file__).resolve().parents[1]
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


def _request(facts: dict, names: list[str]) -> dict:
    return {
        "system": prompts.load(TASK, PROMPT_VERSION)["system"],
        "messages": [
            {"role": "user", "content": "청구 줄:\n" + json.dumps(facts, ensure_ascii=False)}
        ],
        "schema": schema(names),
    }


def _valid(result, facts: dict):
    """통과한 결과 dict, 아니면 None"""
    if result is None or not result.ok or not isinstance(result.data, dict):
        return None
    try:
        return result.data if check_result(facts, result.data) else None
    except (KeyError, TypeError, AttributeError):  # 칸이 빠진 답 — 그 줄만 실패
        return None


def _save(conn, claim_id: int, facts: dict, h: str, result, model: str) -> None:
    with conn:  # 줄마다 커밋 — 다시 부르는 동안 쓰기 잠금을 쥐지 않는다
        conn.execute(
            "INSERT INTO ax_claim_omissions (claim_id, facts, facts_hash, result, valid,"
            " prompt_version, model, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (claim_id) DO UPDATE SET facts = excluded.facts,"
            " facts_hash = excluded.facts_hash, result = excluded.result, valid = excluded.valid,"
            " prompt_version = excluded.prompt_version, model = excluded.model,"
            " created_at = excluded.created_at",
            (
                claim_id,
                json.dumps(facts, ensure_ascii=False),
                h,
                None if result is None else json.dumps(result, ensure_ascii=False),
                int(result is not None),
                PROMPT_VERSION,
                model,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )


def _plan(conn, rows: list[dict], model: str):
    """(부를 줄, 건너뛸 수, 서술 없는 줄) — 줄은 (claim_id, facts, hash). 다시 쓰는 규칙은 2a _reusable과 같다"""
    saved = {
        r["claim_id"]: r
        for r in conn.execute(
            "SELECT claim_id, facts_hash, prompt_version, model, valid FROM ax_claim_omissions"
        )
    }
    todo, skipped, empty = [], 0, []
    for row in rows:
        facts = omission_facts(row)
        h = _hash(facts)
        old = saved.get(row["claim_id"])
        if not facts["note"].strip():
            empty.append((row["claim_id"], facts, h))
        elif (
            old
            and old["valid"]
            and (old["facts_hash"], old["prompt_version"], old["model"])
            == (h, PROMPT_VERSION, model)
        ):
            skipped += 1
        else:
            todo.append((row["claim_id"], facts, h))
    return todo, skipped, empty


def todo_ids(conn: sqlite3.Connection, today: date, model: str | None = None) -> set[int]:
    """앱 범위에서 다시 불러야 할 줄(서술 없는 줄 제외) — 화면 갱신 버튼이 2a 할 일과 합쳐 센다"""
    model = model or load_config()["aliases"][ALIAS][0]
    return {cid for cid, _, _ in _plan(conn, targets(conn, today, scope="app"), model)[0]}


def build(conn, gw, rows: list[dict], *, use_batch: bool, poll_seconds: int = 30) -> dict:
    """줄마다 묻고 저장한다(2a backend/claim_check.py build와 같은 흐름). 서술 없는 줄은 부르지 않고 빈 목록으로, 해시·
    버전·모델이 같고 통과한 줄은 건너뛴다. 후검증 실패·Batch에서 빠진 줄만 일반 호출로 한 번 더, 또 실패면 결과 NULL"""
    model = gw.config["aliases"][ALIAS][0]
    names = _names(conn)
    todo, skipped, empty = _plan(conn, rows, model)
    for cid, facts, h in empty:
        _save(conn, cid, facts, h, {"omissions": []}, model)
    out = {"rows": len(rows), "called": 0, "skipped": skipped, "failed": 0, "empty": len(empty)}
    first = {}
    if todo and use_batch and _batchable(gw):
        bid = gw.submit_batch(
            ALIAS,
            [{"custom_id": str(cid), **_request(facts, names)} for cid, facts, _ in todo],
            task=TASK,
            prompt_version=PROMPT_VERSION,
        )
        # 기다리다 끊기면 이 id로 결과를 받아 온다 — 다시 제출하면 비용이 두 번
        print(f"batch {bid} ({len(todo)}줄)", file=sys.stderr, flush=True)
        while gw.batch_status(bid) != "ended":
            time.sleep(poll_seconds)
        first = gw.batch_results(
            bid, ALIAS, task=TASK, prompt_version=PROMPT_VERSION, schema=schema(names)
        )

    def plain(cid, facts):
        return gw.complete(
            ALIAS,
            task=TASK,
            prompt_version=PROMPT_VERSION,
            ref=("claim", cid),
            **_request(facts, names),
        )

    for cid, facts, h in todo:
        out["called"] += 1
        used = first.get(str(cid)) or plain(cid, facts)
        got = _valid(used, facts)
        if got is None:
            used = plain(cid, facts)
            got = _valid(used, facts)
        out["failed"] += got is None
        _save(conn, cid, facts, h, got, used.model if used else model)  # 실제로 답한 모델
    return out


def results(conn: sqlite3.Connection, claim_ids: list[int] | None = None) -> dict[int, dict]:
    """저장된 점검 — {claim_id: {omissions: 거른 항목 | None, valid, at, prompt_version}}, 후검증 실패는 valid False.
    claim_ids가 없으면 저장된 줄 전부"""
    index = procedure_index([dict(r) for r in conn.execute("SELECT * FROM source.emr_procedures")])
    age_range = master_values(conn)["age_group_range"]
    sql = (
        "SELECT o.claim_id, o.facts, o.result, o.created_at, o.prompt_version, c.procedure_code"
        " FROM ax_claim_omissions o JOIN source.emr_claims c ON c.id = o.claim_id"
    )
    if claim_ids is not None:
        sql += f" WHERE o.claim_id IN ({', '.join('?' * len(claim_ids))})"
    out = {}
    for r in conn.execute(sql, claim_ids or []):
        got = json.loads(r["result"]) if r["result"] else None
        out[r["claim_id"]] = {
            "omissions": None
            if got is None
            else insured_items(
                got["omissions"], r["procedure_code"], json.loads(r["facts"]), index, age_range
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
