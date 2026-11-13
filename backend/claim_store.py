"""⑤ AI 점검 공용 저장·실행 — 청구 줄 하나에 한 행인 표(ax_claim_omissions·ax_claim_evidence)에 입력(facts)·해시·결과·
통과 여부·프롬프트 버전·모델을 저장하고, 해시·버전·모델이 같고 통과한 줄은 다시 부르지 않는다. 첫 답은 Batch 또는 일반
호출, 후검증 실패·Batch에서 빠진 줄만 일반 호출로 한 번 더, 또 실패면 결과 NULL(틀은 2a backend/claim_check.py, 설계
docs/superpowers/specs/2026-11-10-claim-review-design.md §2b·§3)."""

import json
import sqlite3
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from backend import prompts
from backend.claim_check import _hash
from backend.prep import _batchable

facts_hash = _hash  # 2a와 같은 해시 — 저장된 행이 그대로 다시 쓰인다


@dataclass(frozen=True)
class Kind:
    """점검 한 종류 — table은 ax_claim_omissions와 같은 칸, facts(row)는 LLM 입력, schema(conn)는 출력 스키마,
    check(facts, 답)는 후검증, empty는 서술(text_key)이 빈 줄에 부르지 않고 저장하는 결과"""

    table: str
    task: str
    alias: str
    version: str
    facts: Callable[[dict], dict]
    schema: Callable[[sqlite3.Connection], dict]
    check: Callable[[dict, dict], bool]
    empty: dict
    text_key: str = "note"


def request(kind: Kind, facts: dict, schema: dict) -> dict:
    return {
        "system": prompts.load(kind.task, kind.version)["system"],
        "messages": [
            {"role": "user", "content": "청구 줄:\n" + json.dumps(facts, ensure_ascii=False)}
        ],
        "schema": schema,
    }


def valid(kind: Kind, result, facts: dict):
    """통과한 결과 dict, 아니면 None"""
    if result is None or not result.ok or not isinstance(result.data, dict):
        return None
    try:
        return result.data if kind.check(facts, result.data) else None
    except (KeyError, TypeError, AttributeError):  # 칸이 빠진 답 — 그 줄만 실패
        return None


def save(conn, kind: Kind, claim_id: int, facts: dict, h: str, result, model: str) -> None:
    with conn:  # 줄마다 커밋 — 다시 부르는 동안 쓰기 잠금을 쥐지 않는다
        conn.execute(
            f"INSERT INTO {kind.table} (claim_id, facts, facts_hash, result, valid,"
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
                kind.version,
                model,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )


def plan(conn, kind: Kind, rows: list[dict], model: str, force: bool = False):
    """(부를 줄, 건너뛸 수, 서술 없는 줄) — 줄은 (claim_id, facts, hash). force면 저장된 결과를 재사용하지 않는다"""
    old_rows = {
        r["claim_id"]: r
        for r in conn.execute(
            f"SELECT claim_id, facts_hash, prompt_version, model, valid FROM {kind.table}"
        )
    }
    todo, skipped, empty = [], 0, []
    for row in rows:
        facts = kind.facts(row)
        h = facts_hash(facts)
        old = old_rows.get(row["claim_id"])
        if not facts[kind.text_key].strip():
            empty.append((row["claim_id"], facts, h))
        elif (
            not force
            and old
            and old["valid"]
            and (old["facts_hash"], old["prompt_version"], old["model"])
            == (h, kind.version, model)
        ):
            skipped += 1
        else:
            todo.append((row["claim_id"], facts, h))
    return todo, skipped, empty


def todo_ids(conn, kind: Kind, rows: list[dict], model: str) -> set[int]:
    """다시 불러야 할 줄(서술 없는 줄 제외)"""
    return {cid for cid, _, _ in plan(conn, kind, rows, model)[0]}


def build(
    conn,
    gw,
    kind: Kind,
    rows: list[dict],
    *,
    use_batch: bool,
    poll_seconds: int = 30,
    force: bool = False,
) -> dict:
    """줄마다 묻고 저장한다. 일반 호출은 차례로 — 게이트웨이 기록이 앱 연결 하나에 쓴다"""
    model = gw.config["aliases"][kind.alias][0]
    schema = kind.schema(conn)
    todo, skipped, empty = plan(conn, kind, rows, model, force)
    for cid, facts, h in empty:
        save(conn, kind, cid, facts, h, kind.empty, model)
    out = {"rows": len(rows), "called": 0, "skipped": skipped, "failed": 0, "empty": len(empty)}
    first = {}
    if todo and use_batch and _batchable(gw):
        bid = gw.submit_batch(
            kind.alias,
            [{"custom_id": str(cid), **request(kind, facts, schema)} for cid, facts, _ in todo],
            task=kind.task,
            prompt_version=kind.version,
        )
        # 기다리다 끊기면 이 id로 결과를 받아 온다 — 다시 제출하면 비용이 두 번
        print(f"batch {bid} ({len(todo)}줄)", file=sys.stderr, flush=True)
        while gw.batch_status(bid) != "ended":
            time.sleep(poll_seconds)
        first = gw.batch_results(
            bid, kind.alias, task=kind.task, prompt_version=kind.version, schema=schema
        )

    def plain(cid, facts):
        return gw.complete(
            kind.alias,
            task=kind.task,
            prompt_version=kind.version,
            ref=("claim", cid),
            **request(kind, facts, schema),
        )

    for cid, facts, h in todo:
        out["called"] += 1
        used = first.get(str(cid)) or plain(cid, facts)
        got = valid(kind, used, facts)
        if got is None:
            used = plain(cid, facts)
            got = valid(kind, used, facts)
        out["failed"] += got is None
        save(conn, kind, cid, facts, h, got, used.model if used else model)  # 실제로 답한 모델
    return out


def saved(conn, kind: Kind, claim_ids: list[int] | None = None) -> dict[int, sqlite3.Row]:
    """저장된 행 — {claim_id: 행(claim_id, facts, result, created_at, prompt_version)}"""
    sql = f"SELECT claim_id, facts, result, created_at, prompt_version FROM {kind.table}"
    if claim_ids is not None:
        sql += f" WHERE claim_id IN ({', '.join('?' * len(claim_ids))})"
    return {r["claim_id"]: r for r in conn.execute(sql, claim_ids or [])}
