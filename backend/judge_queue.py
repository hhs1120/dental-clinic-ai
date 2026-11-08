"""판정 큐 (앱 설계 §6 "판정 큐 다음 건", 기능 정의 §4 ②). 대상 = 판정 풀 밖 생성 통화·새로 추가한 통화 중
엇갈림 또는 무작위 표본이고 활성 분류가 ok. 순서 = 건너뛴 건 뒤로 → 새 통화 → 핵심 엇갈림 → 나머지 엇갈림 → 표본.
모델 라벨은 저장한 뒤에만 돌려준다(앵커링 방지).
"""

import hashlib
import json
import sqlite3

from backend.calls import CALLS_CTE, active_versions
from backend.classify import ENUMS
from shared.call_labels import FLAGS, ITEMS

SAMPLE_SHARE = 0.05  # 엇갈림 없는 대상 중 표본 비율 — call_uid 해시로 고른다


def is_sample(call_uid: str) -> bool:
    return (
        int(hashlib.sha256(call_uid.encode()).hexdigest()[:8], 16) % 10000 < SAMPLE_SHARE * 10000
    )


TARGETS_SQL = (
    CALLS_CTE + " SELECT calls.call_uid, d.call_uid IS NOT NULL AS disagree FROM calls"
    " JOIN ax_call_classifications k ON k.call_uid = calls.call_uid"
    "  AND k.model_alias = 'classify' AND k.prompt_version = ? AND k.status = 'ok'"
    " LEFT JOIN ax_call_disagreements d ON d.call_uid = calls.call_uid"
    "  AND d.a_version = ? AND d.b_version = ?"
)


def refresh(conn: sqlite3.Connection, a: str, b: str) -> int:
    """대상 중 상태가 없는 통화를 pending으로 — 새로 넣은 수"""
    have = {r[0] for r in conn.execute("SELECT call_uid FROM ax_judge_state")}
    new = [
        r["call_uid"]
        for r in conn.execute(TARGETS_SQL, (a, a, b))
        if r["call_uid"] not in have and (r["disagree"] or is_sample(r["call_uid"]))
    ]
    conn.executemany(
        "INSERT INTO ax_judge_state VALUES (?, 'pending', datetime('now', 'localtime'))",
        [(u,) for u in new],
    )
    conn.commit()
    return len(new)


def refresh_if_active(conn: sqlite3.Connection, a: str, b: str) -> int:
    """지금 쓰는 버전 쌍일 때만 refresh — 실험으로 다른 쌍의 엇갈림을 계산해도 사람 큐에 남지 않게"""
    return refresh(conn, a, b) if (a, b) == active_versions(conn) else 0


def refresh_call(conn: sqlite3.Connection, call_uid: str) -> None:
    """한 통화만(새 통화 추가·다시 분류 뒤) — 활성 버전 기준"""
    a, b = active_versions(conn)
    row = conn.execute(TARGETS_SQL + " WHERE calls.call_uid = ?", (a, a, b, call_uid)).fetchone()
    if row and (row["disagree"] or is_sample(call_uid)):
        conn.execute(
            "INSERT OR IGNORE INTO ax_judge_state VALUES (?, 'pending', datetime('now', 'localtime'))",
            (call_uid,),
        )
        conn.commit()


def next_call(conn: sqlite3.Connection, day: str) -> dict | None:
    """다음 건 — 녹취록·방향·시각과 진행 현황(남은 수·day에 판정한 수·누적). 모델 라벨 없음. 비면 None"""
    a, b = active_versions(conn)
    rows = conn.execute(
        CALLS_CTE + " SELECT calls.call_uid, calls.direction, calls.at, calls.transcript, s.state,"
        " calls.source, d.is_core, d.call_uid IS NOT NULL AS disagree"
        " FROM ax_judge_state s JOIN calls ON calls.call_uid = s.call_uid"
        " JOIN ax_call_classifications k ON k.call_uid = calls.call_uid"
        "  AND k.model_alias = 'classify' AND k.prompt_version = ? AND k.status = 'ok'"
        " LEFT JOIN ax_call_disagreements d ON d.call_uid = calls.call_uid"
        "  AND d.a_version = ? AND d.b_version = ?"
        " WHERE s.state IN ('pending', 'skipped')"
        " ORDER BY s.state = 'skipped', calls.source != 'pasted', NOT COALESCE(d.is_core, 0),"
        "  d.call_uid IS NULL, CASE WHEN s.state = 'skipped' THEN s.updated_at END, calls.call_uid",
        (a, a, b),
    ).fetchall()
    if not rows:
        return None
    r = rows[0]
    done_total, done_today = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(substr(judged_at, 1, 10) = ?), 0) FROM ax_judgments", (day,)
    ).fetchone()
    return {
        "call_uid": r["call_uid"],
        "direction": r["direction"],
        "at": r["at"],
        "transcript": r["transcript"],
        "left": len(rows),
        "done_today": done_today,
        "done_total": done_total,
    }


def _model_labels(conn, table: str, alias: str, call_uid: str, version: str) -> dict | None:
    row = conn.execute(
        f"SELECT labels FROM {table} WHERE call_uid = ? AND model_alias = ? AND prompt_version = ?"
        " AND status = 'ok'",
        (call_uid, alias, version),
    ).fetchone()
    return json.loads(row[0]) if row else None


def save(
    conn: sqlite3.Connection, call_uid: str, labels: dict, note: str | None, *, part: str, at: str
) -> dict:
    """사람 판정 저장(통화당 1행, 재판정은 덮어씀) → 그 뒤에 두 모델 라벨을 돌려준다.
    큐에 있는 대상 통화가 아니면 KeyError, 9항목 밖 키·목록 밖 값은 ValueError(모르겠음은 키를 뺀다)"""
    in_queue = conn.execute(
        CALLS_CTE + " SELECT 1 FROM ax_judge_state s JOIN calls ON calls.call_uid = s.call_uid"
        " WHERE s.call_uid = ?",
        (call_uid,),
    ).fetchone()
    if not in_queue:
        raise KeyError(call_uid)
    ok_enum = all(labels[k] in v for k, v in ENUMS.items() if k in labels)
    ok_flag = all(labels[f] in (True, False) for f in FLAGS if f in labels)
    if not set(labels) <= set(ITEMS) or not ok_enum or not ok_flag:
        raise ValueError("labels: 9항목·목록 안 값만")
    conn.execute(
        "INSERT INTO ax_judgments (call_uid, labels, note, judged_by, judged_at)"
        " VALUES (?, ?, ?, ?, ?) ON CONFLICT (call_uid) DO UPDATE SET labels = excluded.labels,"
        " note = excluded.note, judged_by = excluded.judged_by, judged_at = excluded.judged_at",
        (call_uid, json.dumps(labels, ensure_ascii=False), note, part, at),
    )
    conn.execute(
        "INSERT INTO ax_judge_state VALUES (?, 'done', ?)"
        " ON CONFLICT (call_uid) DO UPDATE SET state = 'done', updated_at = excluded.updated_at",
        (call_uid, at),
    )
    conn.commit()
    if any(labels.get(k) for k in ("has_todo", "emergency", "complaint")) or (
        labels.get("urgency") == "urgent"
    ):  # 자동 종결된 통화면 푼다 — 사람 되돌림이라 다시 닫히지 않는다(기능 정의 §4 ①)
        from backend import autoclose  # autoclose가 calls를 거쳐 이 모듈과 얽힌다

        if autoclose.unclose_by_human(conn, call_uid, part):
            conn.commit()
    a, b = active_versions(conn)
    return {
        "classifier": _model_labels(conn, "ax_call_classifications", "classify", call_uid, a),
        "judge": _model_labels(conn, "ax_model_judgments", "judge", call_uid, b),
    }


def skip(conn: sqlite3.Connection, call_uid: str, at: str) -> None:
    conn.execute(
        "UPDATE ax_judge_state SET state = 'skipped', updated_at = ? WHERE call_uid = ?",
        (at, call_uid),
    )
    conn.commit()
