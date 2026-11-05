"""블라인드 비교 (평가 설계 §2.4, 통화·② 판정 화면 설계 "블라인드 비교"·"LLM 쌍 비교"). 비교하는 두 분류 버전은
ax_settings.blind_versions, 대상은 dev 블라인드 통화(backend/pools.py) — test 블라인드는 최종 비교 때만.
좌우 배치는 hash(transcript, seed) % 2로 고정, 버전 이름은 고른 뒤에만 돌려준다. LLM 쌍 비교는 입력이 녹취록과
두 요약뿐이고, 좌우를 바꿔 두 번 물어 두 답이 같으면 그 값·다르면 tie.
"""

import hashlib
import json
import sqlite3
from datetime import datetime

from backend import classify as cl
from backend import db, pools, prompts
from shared.call_labels import PAIR_SCHEMA, todo_items
from shared.clinic import Clinic

BLIND_SEED = "blind-v1"
SWAP = {"left": "right", "right": "left", "tie": "tie", "both_wrong": "both_wrong"}


def first_on_left(transcript: str) -> bool:
    """blind_versions 첫 버전이 왼쪽인지 — 새로고침·재실행에도 같다(평가 설계 §2.4 좌우 배치)"""
    return int(hashlib.sha256((BLIND_SEED + transcript).encode()).hexdigest(), 16) % 2 == 0


def summary_text(summary: str, todo: list) -> str:
    texts = [t["text"] for t in todo_items(todo)]
    return f"{summary} 할 일: {' · '.join(texts) if texts else '없음'}"


def _pairs(conn, clinic: Clinic, a: str, b: str) -> list[sqlite3.Row]:
    """dev 블라인드 통화 중 두 버전 분류가 모두 ok인 것"""
    return conn.execute(
        "SELECT x.call_uid, x.direction, x.started_at AS at, x.transcript,"
        " ka.id AS a_id, ka.summary AS a_summary, ka.todo AS a_todo,"
        " kb.id AS b_id, kb.summary AS b_summary, kb.todo AS b_todo"
        " FROM source.ax_calls x"
        " JOIN ax_call_classifications ka ON ka.call_uid = x.call_uid"
        "  AND ka.model_alias = 'classify' AND ka.prompt_version = ? AND ka.status = 'ok'"
        " JOIN ax_call_classifications kb ON kb.call_uid = x.call_uid"
        "  AND kb.model_alias = 'classify' AND kb.prompt_version = ? AND kb.status = 'ok'"
        f" WHERE {pools.dev_blind_sql(clinic, 'x')} ORDER BY x.blind_sort_order, x.call_uid",
        (a, b),
    ).fetchall()


def _sides(r) -> tuple[dict, dict]:
    """(왼쪽, 오른쪽) — 각 {classification_id, text}"""
    first = {
        "classification_id": r["a_id"],
        "text": summary_text(r["a_summary"], json.loads(r["a_todo"] or "[]")),
    }
    second = {
        "classification_id": r["b_id"],
        "text": summary_text(r["b_summary"], json.loads(r["b_todo"] or "[]")),
    }
    return (first, second) if first_on_left(r["transcript"]) else (second, first)


def next_pair(conn: sqlite3.Connection, clinic: Clinic) -> dict | None:
    """사람 투표 다음 쌍 — 투표하지 않은 첫 쌍. 버전 이름 없음. 설정이 없거나 남은 쌍이 없으면 None"""
    versions = db.get_setting(conn, "blind_versions")
    if not versions:
        return None
    voted = {
        (r[0], r[1])
        for r in conn.execute(
            "SELECT left_classification_id, right_classification_id FROM ax_blind_votes"
        )
    }
    rows = _pairs(conn, clinic, *versions)
    left_rows = []
    for r in rows:
        left, right = _sides(r)
        if (left["classification_id"], right["classification_id"]) not in voted:
            left_rows.append((r, left, right))
    if not left_rows:
        return None
    r, left, right = left_rows[0]
    return {
        "call_uid": r["call_uid"],
        "direction": r["direction"],
        "at": r["at"],
        "transcript": r["transcript"],
        "left": left,
        "right": right,
        "left_count": len(left_rows),
    }


def _version(conn, classification_id: int) -> str:
    return conn.execute(
        "SELECT prompt_version FROM ax_call_classifications WHERE id = ?", (classification_id,)
    ).fetchone()[0]


def save_vote(
    conn: sqlite3.Connection,
    clinic: Clinic,
    left_id: int,
    right_id: int,
    choice: str,
    *,
    part: str,
    at: str,
) -> dict:
    """사람 투표 저장 → 좌우 버전 공개. next_pair가 줄 수 있는 쌍(dev 블라인드·blind_versions·좌우 배치)이
    아니면 ValueError. 이미 투표한 쌍은 sqlite3.IntegrityError"""
    versions = db.get_setting(conn, "blind_versions")
    pairs = _pairs(conn, clinic, *versions) if versions else []
    offered = {tuple(s["classification_id"] for s in _sides(r)) for r in pairs}
    if (left_id, right_id) not in offered:
        raise ValueError(f"blind pair: {left_id}/{right_id}")
    conn.execute(
        "INSERT INTO ax_blind_votes (left_classification_id, right_classification_id, choice,"
        " voted_by, voted_at) VALUES (?, ?, ?, ?, ?)",
        (left_id, right_id, choice, part, at),
    )
    conn.commit()
    return {"left": _version(conn, left_id), "right": _version(conn, right_id)}


def pair_request(transcript: str, left_text: str, right_text: str, version: str) -> dict:
    content = f"녹취록:\n{transcript}\n\n요약 1: {left_text}\n\n요약 2: {right_text}"
    return {
        "system": prompts.load("blind_judge", version)["system"],
        "messages": [{"role": "user", "content": content}],
        "schema": PAIR_SCHEMA,
    }


def unswap(choice: str) -> str:
    """좌우를 바꿔 물은 답을 첫 배치 기준으로"""
    return SWAP[choice]


def combine(first: str, second: str) -> str:
    """first·second 모두 첫 배치 기준 — 같으면 그 값, 다르면 tie(위치 편향 보정)"""
    return first if first == second else "tie"


def run_pairs(conn: sqlite3.Connection, gw, clinic: Clinic, a: str, b: str, version: str) -> dict:
    """dev 블라인드 통화마다 LLM 쌍 비교 두 번 → ax_model_blind_votes. 이미 있는 쌍은 건너뛰고, 한 번이라도
    실패하면 저장하지 않는다(다음 실행이 다시 한다)"""
    have = {
        (r[0], r[1])
        for r in conn.execute(
            "SELECT left_classification_id, right_classification_id FROM ax_model_blind_votes"
            " WHERE model_alias = 'blind_judge' AND prompt_version = ?",
            (version,),
        )
    }
    out = {"done": 0, "failed": 0}
    for r in _pairs(conn, clinic, a, b):
        left, right = _sides(r)
        if (left["classification_id"], right["classification_id"]) in have:
            continue
        calls = [
            gw.complete(
                "blind_judge",
                task="blind_judge",
                prompt_version=version,
                max_tokens=cl.MAX_TOKENS,
                **pair_request(r["transcript"], x["text"], y["text"], version),
            )
            for x, y in ((left, right), (right, left))
        ]
        if not all(c.ok for c in calls):
            out["failed"] += 1
            continue
        first, second = calls[0].data["choice"], unswap(calls[1].data["choice"])
        conn.execute(
            "INSERT INTO ax_model_blind_votes (left_classification_id, right_classification_id,"
            " model_alias, model_actual, prompt_version, first_choice, second_choice, choice,"
            " first_llm_call_id, second_llm_call_id, created_at)"
            " VALUES (?, ?, 'blind_judge', ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                left["classification_id"],
                right["classification_id"],
                calls[0].model,
                version,
                first,
                second,
                combine(first, second),
                calls[0].log_id,
                calls[1].log_id,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        conn.commit()
        out["done"] += 1
    return out
