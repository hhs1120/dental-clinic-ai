"""⑤-3 서류 사유 차트 문장: 서류 사유(내역설명·증빙·부위·산정 내역 기재 누락)로 조정·불능된 청구 줄마다 그 사유의 청구 기준
절이 요구하는 내용에 해당하는 차트 문장을 AI가 0~3개 고르고, 인용이 차트에 실제로 있는지 대조해 저장한다(설계
docs/superpowers/specs/2026-11-10-claim-review-design.md §3, 저장·실행은 backend/claim_store.py). 청구 금액·조정 결과·판단
기록은 입력에 넣지 않는다. 화면은 조정 건 펼친 곳 ② "차트에서 옮겨 적을 문장(AI)"."""

import argparse
import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from backend import claim_store, db
from backend.claim_check import CUT, found, sections
from backend.prep import master_values
from shared.llm import load_config

ROOT = Path(__file__).resolve().parents[1]
TASK, ALIAS, PROMPT_VERSION = "claim_evidence", "chat", "v1"
MAX_QUOTES = 3
SCHEMA = {
    "type": "object",
    "properties": {
        "quotes": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_QUOTES}
    },
    "required": ["quotes"],
    "additionalProperties": False,
}


def evidence_facts(row: dict) -> dict:
    """LLM 입력 — 시술·치아·차트 서술 + 그 사유의 기준 절(targets가 붙인 heading·body)"""
    return {
        "procedure": row["procedure_name"],
        "tooth": None if row["tooth_no"] == "00" else row["tooth_no"],
        "note": row["note"] or "",
        "standard": {"heading": row["heading"], "body": row["body"]},
    }


def check_result(facts: dict, r: dict) -> bool:
    """후검증 — 모든 인용이 차트 서술에 글자 그대로 있고 MAX_QUOTES개 이하"""
    return len(r["quotes"]) <= MAX_QUOTES and all(found(q, facts["note"]) for q in r["quotes"])


KIND = claim_store.Kind(
    table="ax_claim_evidence",
    task=TASK,
    alias=ALIAS,
    version=PROMPT_VERSION,
    facts=evidence_facts,
    schema=lambda conn: SCHEMA,
    check=check_result,
    empty={"quotes": []},
)


def targets(conn: sqlite3.Connection, today: date, *, scope: str) -> list[dict]:
    """서류 사유로 조정·불능된 청구 줄 — scope "app"은 통보일이 today까지이고 재심 신청 기한이 남은 건, "all"은 전부.
    행의 status·adjust_note·reviewed_at은 대상 고르기와 절 찾기에만 쓰고 evidence_facts는 읽지 않는다"""
    mv = master_values(conn)
    secs = sections(conn)
    docs = [n for n, t in mv["adjust_note_types"].items() if t == "documentation"]
    out = []
    for r in conn.execute(
        "SELECT c.id AS claim_id, c.procedure_code, pr.name AS procedure_name, c.tooth_no,"
        " c.status, c.adjust_note, c.reviewed_at, n.note"
        " FROM source.emr_claims c"
        " JOIN source.emr_procedures pr ON pr.code = c.procedure_code"
        " LEFT JOIN source.emr_chart_notes n ON n.treatment_id = c.treatment_id"
        f" WHERE c.status IN ({', '.join('?' * len(CUT))})"
        f" AND c.adjust_note IN ({', '.join('?' * len(docs))}) ORDER BY c.id",
        (*CUT, *docs),
    ):
        row = dict(r)
        if scope == "app" and not (
            row["reviewed_at"] <= today.isoformat()
            and date.fromisoformat(row["reviewed_at"][:10]) + timedelta(days=mv["appeal_days"])
            >= today
        ):
            continue
        row["heading"] = mv["adjust_note_headings"][row["adjust_note"]]
        row["body"] = secs.get(row["heading"], "")
        out.append(row)
    return out


def todo_ids(conn: sqlite3.Connection, today: date, model: str | None = None) -> set[int]:
    """앱 범위에서 다시 불러야 할 줄 — 화면 갱신 버튼이 2a 삭감 대응 할 일과 합쳐 센다"""
    model = model or load_config()["aliases"][ALIAS][0]
    return claim_store.todo_ids(conn, KIND, targets(conn, today, scope="app"), model)


def build(conn, gw, rows: list[dict], *, use_batch: bool, poll_seconds: int = 30) -> dict:
    return claim_store.build(conn, gw, KIND, rows, use_batch=use_batch, poll_seconds=poll_seconds)


def results(conn: sqlite3.Connection, claim_ids: list[int] | None = None) -> dict[int, dict]:
    """저장된 차트 문장 — {claim_id: {quotes: list | None, valid, at, prompt_version}}, 후검증 실패는 quotes None"""
    out = {}
    for cid, r in claim_store.saved(conn, KIND, claim_ids).items():
        got = json.loads(r["result"]) if r["result"] else None
        out[cid] = {
            "quotes": got["quotes"] if got else None,
            "valid": got is not None,
            "at": r["created_at"],
            "prompt_version": r["prompt_version"],
        }
    return out


def main(argv=None) -> None:
    from backend.harness.llm import make_gateway
    from shared.clinic import load_clinic

    p = argparse.ArgumentParser(prog="python -m backend.claim_evidence")
    p.add_argument("command", choices=("run",))
    p.add_argument(
        "--scope", choices=("app", "all"), default="app", help="app: 기한 남은 건, all: 전부"
    )
    p.add_argument("--no-batch", action="store_true", help="Batch 없이 일반 호출만")
    args = p.parse_args(argv)
    conn = db.connect(ROOT / "data" / "app.sqlite", ROOT / "data" / "source.sqlite")
    db.init_schema(conn)
    rows = targets(conn, load_clinic().today, scope=args.scope)
    print(build(conn, make_gateway(conn), rows, use_batch=not args.no_batch))


if __name__ == "__main__":
    main()
