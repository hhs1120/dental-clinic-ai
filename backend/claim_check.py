"""⑤-2 AI 점검: 요건이 있는 시술의 급여 청구 줄을 차트 서술과 그 시술의 청구 기준 절만으로 읽어 임상 요건 근거 유무를
판단하고, 인용이 원문에 실제로 있는지 대조해 저장한다(설계 docs/superpowers/specs/2026-11-10-claim-review-design.md §2,
틀은 ④ backend/prep.py). 청구 상태·조정 결과·사유는 입력에 넣지 않는다 — 깎인 줄을 보이면 차트를 안 읽고도 맞힌다.
차트는 emr_chart_notes의 dx_code·note만(가상 데이터 생성 설계 §0 허용 컬럼)."""

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from backend import db, prompts
from backend.prep import _age, _batchable, master_values
from backend.rag_index import _sections
from shared.llm import load_config

ROOT = Path(__file__).resolve().parents[1]
CUT = (
    "adjusted",
    "denied",
)  # 깎인 청구 — backend/claims.py CUT과 같다(그 모듈이 이 모듈을 읽어 import하지 않음)

TASK, ALIAS, PROMPT_VERSION = "claim_check", "chat", "v2"
SCHEMA = {
    "type": "object",
    "properties": {
        "requirement": {"type": "string", "enum": ["met", "missing", "na"]},
        "omissions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"procedure": {"type": "string"}, "quote": {"type": "string"}},
                "required": ["procedure", "quote"],
                "additionalProperties": False,
            },
        },
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source": {"type": "string", "enum": ["chart", "standard"]},
                    "quote": {"type": "string"},
                },
                "required": ["source", "quote"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["requirement", "omissions", "evidence"],
    "additionalProperties": False,
}
MIN_QUOTE = (
    6  # 인용 최소 글자 수(공백 뺌) — "있음"·"시행함" 같은 조각은 어느 서술에나 있어 근거가 못 된다
)
_EDGE = "\"'“”‘’「」『』.,·…:;!? "  # 인용 앞뒤에 붙기 쉬운 따옴표·문장부호


def section_cards(conn: sqlite3.Connection) -> dict[str, dict]:
    """청구 기준 문서(ax_documents claim_standard)의 절 제목 → {doc_title, heading, body} — 머리말은 절이 아니라 뺀다.
    ⑤-3 조항 카드(backend/claims.py)도 쓴다(설계 §3)"""
    out = {}
    for d in conn.execute(
        "SELECT title, body FROM source.ax_documents WHERE type = 'claim_standard' ORDER BY id"
    ):
        for heading, body in _sections(d["title"], d["body"]):
            if heading != d["title"]:
                out[heading] = {"doc_title": d["title"], "heading": heading, "body": body.strip()}
    return out


def sections(conn: sqlite3.Connection) -> dict[str, str]:
    """청구 기준 절 제목 → 본문"""
    return {h: c["body"] for h, c in section_cards(conn).items()}


def claim_facts(row: dict, mv: dict, secs: dict[str, str]) -> dict:
    """LLM 입력 — 시술·치아·상병·나이(R06 절이 나이를 요건으로 든다)·차트 서술 + 그 시술의 요건 절 본문.
    청구 상태·조정 사유는 row에 있어도 넣지 않는다"""
    headings = [
        mv["adjust_note_headings"][n] for n in mv["requirement_notes"][row["procedure_code"]]
    ]
    return {
        "procedure": row["procedure_name"],
        "tooth": None if row["tooth_no"] == "00" else row["tooth_no"],
        "diagnosis": row["dx_name"],
        "age": row["age"],
        "note": row["note"] or "",
        "standards": [
            {"heading": h, "body": secs.get(h, "")} for h in headings
        ],  # 문서 없음은 빈 절
    }


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def found(quote: str, text: str) -> bool:
    """인용이 원문에 글자 그대로 있나 — 공백은 접고 앞뒤 따옴표·문장부호는 떼고 비교, MIN_QUOTE자보다 짧으면 거짓"""
    q = _norm(quote).strip(_EDGE)
    return len(q.replace(" ", "")) >= MIN_QUOTE and q in _norm(text)


def check_result(facts: dict, r: dict) -> bool:
    """후검증 — 모든 인용이 출처 본문에 있고, met은 차트 인용·missing은 기준 인용이 하나 이상"""
    std = [s["body"] for s in facts["standards"]]
    for e in r["evidence"]:
        texts = [facts["note"]] if e["source"] == "chart" else std
        if not any(found(e["quote"], t) for t in texts):
            return False
    if not all(found(o["quote"], facts["note"]) for o in r["omissions"]):
        return False
    need = {"met": "chart", "missing": "standard"}.get(r["requirement"])
    return need is None or any(e["source"] == need for e in r["evidence"])


# ── 대상·저장·실행 ────────────────────────────────────────────────


def targets(
    conn: sqlite3.Connection, today: date, *, scope: str, claim_ids: list[int] | None = None
) -> list[dict]:
    """점검할 청구 줄 — 시술이 요건 대상(requirement_notes)인 줄. scope "app"은 대기 줄 + 통보일이 today까지이고
    재심 신청 기한이 남은 요건 사유 조정·불능 건(앱 화면 범위), "all"은 전 기간(평가). claim_ids가 있으면 그 줄만.
    행의 status·adjust_note는 대상 고르기에만 쓰고 claim_facts는 읽지 않는다"""
    mv = master_values(conn)
    procs = list(mv["requirement_notes"])
    req_notes = {n for n, t in mv["adjust_note_types"].items() if t == "requirement"}
    keep = None if claim_ids is None else set(claim_ids)
    out = []
    for r in conn.execute(
        "SELECT c.id AS claim_id, c.procedure_code, pr.name AS procedure_name, c.tooth_no,"
        " c.service_date, c.status, c.adjust_note, c.reviewed_at, d.name AS dx_name, n.note,"
        " p.birth_date"
        " FROM source.emr_claims c"
        " JOIN source.emr_procedures pr ON pr.code = c.procedure_code"
        " JOIN source.emr_patients p ON p.id = c.patient_id"
        " LEFT JOIN source.emr_chart_notes n ON n.treatment_id = c.treatment_id"
        " LEFT JOIN source.emr_diagnoses d ON d.code = n.dx_code"
        f" WHERE c.procedure_code IN ({', '.join('?' * len(procs))}) ORDER BY c.id",
        procs,
    ):
        row = dict(r)
        if keep is not None and row["claim_id"] not in keep:
            continue
        if scope == "app" and not (
            row["status"] == "pending"
            or (
                row["status"] in CUT
                and row["adjust_note"] in req_notes
                and row["reviewed_at"] <= today.isoformat()
                and date.fromisoformat(row["reviewed_at"][:10]) + timedelta(days=mv["appeal_days"])
                >= today
            )
        ):
            continue
        row["age"] = _age(row.pop("birth_date"), date.fromisoformat(row["service_date"]))
        out.append(row)
    return out


def _hash(facts: dict) -> str:
    return hashlib.sha256(
        json.dumps(facts, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def _request(facts: dict) -> dict:
    return {
        "system": prompts.load(TASK, PROMPT_VERSION)["system"],
        "messages": [
            {"role": "user", "content": "청구 줄:\n" + json.dumps(facts, ensure_ascii=False)}
        ],
        "schema": SCHEMA,
    }


def _valid(result, facts: dict):
    """통과한 결과 dict, 아니면 None"""
    if result is None or not result.ok or not isinstance(result.data, dict):
        return None
    try:
        return result.data if check_result(facts, result.data) else None
    except (KeyError, TypeError, AttributeError):  # 칸이 빠진 답 — 그 줄만 실패
        return None


def _reusable(old, h: str, model: str) -> bool:
    """저장된 결과를 그대로 쓰는 줄 — 해시·버전·모델이 같고 통과(backend/prep.py _reusable과 같은 규칙)"""
    return bool(
        old
        and old["valid"]
        and (old["facts_hash"], old["prompt_version"], old["model"]) == (h, PROMPT_VERSION, model)
    )


def _save(conn, claim_id: int, facts: dict, h: str, result, model: str) -> None:
    with conn:  # 줄마다 커밋 — 다시 부르는 동안 쓰기 잠금을 쥐지 않는다
        conn.execute(
            "INSERT INTO ax_claim_checks (claim_id, facts, facts_hash, result, valid,"
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
    """(부를 줄, 건너뛸 수, 서술 없는 줄) — 줄은 (claim_id, facts, hash)"""
    mv, secs = master_values(conn), sections(conn)
    saved = {  # 한 번에 읽는다 — 청구 전 점검 화면이 열릴 때마다 센다
        r["claim_id"]: r
        for r in conn.execute(
            "SELECT claim_id, facts_hash, prompt_version, model, valid FROM ax_claim_checks"
        )
    }
    todo, skipped, empty = [], 0, []
    for row in rows:
        facts = claim_facts(row, mv, secs)
        h = _hash(facts)
        old = saved.get(row["claim_id"])
        if not facts["note"].strip():
            empty.append((row["claim_id"], facts, h))
        elif _reusable(old, h, model):
            skipped += 1
        else:
            todo.append((row["claim_id"], facts, h))
    return todo, skipped, empty


def todo_ids(conn: sqlite3.Connection, today: date, model: str | None = None) -> set[int]:
    """앱 범위에서 다시 불러야 할 줄(서술 없는 줄 제외) — 화면 갱신 버튼 줄 수는 backend/claims.py _ai_pending이 2b와 합쳐 센다"""
    model = model or load_config()["aliases"][ALIAS][0]
    return {cid for cid, _, _ in _plan(conn, targets(conn, today, scope="app"), model)[0]}


def build(conn, gw, rows: list[dict], *, use_batch: bool, poll_seconds: int = 30) -> dict:
    """줄마다 점검해 저장한다(④ backend/prep.py build와 같은 흐름). 서술이 없는 줄은 부르지 않고 na로, 해시·버전·모델이
    같고 통과한 줄은 건너뛴다. 첫 답은 use_batch면 Batch, 아니면 일반 호출. 후검증 실패·Batch에서 빠진 줄만 일반 호출로
    한 번 더, 또 실패면 결과 NULL. 일반 호출은 차례로 — 게이트웨이 기록이 앱 연결 하나에 쓴다"""
    model = gw.config["aliases"][ALIAS][0]
    todo, skipped, empty = _plan(conn, rows, model)
    for cid, facts, h in empty:
        _save(conn, cid, facts, h, {"requirement": "na", "omissions": [], "evidence": []}, model)
    out = {"rows": len(rows), "called": 0, "skipped": skipped, "failed": 0, "empty": len(empty)}
    first = {}
    if todo and use_batch and _batchable(gw):
        bid = gw.submit_batch(
            ALIAS,
            [{"custom_id": str(cid), **_request(facts)} for cid, facts, _ in todo],
            task=TASK,
            prompt_version=PROMPT_VERSION,
        )
        # 기다리다 끊기면 이 id로 결과를 받아 온다 — 다시 제출하면 비용이 두 번
        print(f"batch {bid} ({len(todo)}줄)", file=sys.stderr, flush=True)
        while gw.batch_status(bid) != "ended":
            time.sleep(poll_seconds)
        first = gw.batch_results(
            bid, ALIAS, task=TASK, prompt_version=PROMPT_VERSION, schema=SCHEMA
        )

    def plain(cid, facts):
        return gw.complete(
            ALIAS, task=TASK, prompt_version=PROMPT_VERSION, ref=("claim", cid), **_request(facts)
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
    """저장된 점검 — {claim_id: {requirement, evidence, omissions, valid, at, prompt_version}}, 후검증 실패는 valid False·나머지 None.
    claim_ids가 없으면 저장된 줄 전부(화면이 표마다 한 번 읽는다)"""
    sql = "SELECT claim_id, result, created_at, prompt_version FROM ax_claim_checks"
    if claim_ids is not None:
        sql += f" WHERE claim_id IN ({', '.join('?' * len(claim_ids))})"
    out = {}
    for r in conn.execute(sql, claim_ids or []):
        got = json.loads(r["result"]) if r["result"] else None
        out[r["claim_id"]] = {
            "requirement": got["requirement"] if got else None,
            "evidence": got["evidence"] if got else None,
            "omissions": got["omissions"] if got else None,
            "valid": got is not None,
            "at": r["created_at"],
            "prompt_version": r["prompt_version"],
        }
    return out


def main(argv=None) -> None:
    from backend.harness.llm import make_gateway
    from shared.clinic import load_clinic

    p = argparse.ArgumentParser(prog="python -m backend.claim_check")
    p.add_argument("command", choices=("run",))
    p.add_argument(
        "--scope", choices=("app", "all"), default="app", help="app: 화면 범위, all: 전 기간"
    )
    p.add_argument("--no-batch", action="store_true", help="Batch 없이 일반 호출만")
    args = p.parse_args(argv)
    conn = db.connect(ROOT / "data" / "app.sqlite", ROOT / "data" / "source.sqlite")
    db.init_schema(conn)
    rows = targets(conn, load_clinic().today, scope=args.scope)
    print(build(conn, make_gateway(conn), rows, use_batch=not args.no_batch))


if __name__ == "__main__":
    main()
