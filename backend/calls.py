"""통화 화면 조회·쓰기 (통화·② 판정 화면 설계 "통화 화면", 기능 정의 §4 ①).
생성 통화(source.ax_calls, 판정 풀 밖)와 새로 추가한 통화(ax_pasted_calls)를 한 목록으로 본다 — 판정 풀 통화는
통화 화면에 나오지 않는다(평가 설계 §1 원칙 2). 분류·판정기 값은 활성 버전(앱 설계 §6 ax_settings).
"""

import json
import sqlite3
from datetime import date, datetime, timedelta
from itertools import groupby

from backend import classify as cl
from backend import db, disagreements
from shared.call_labels import todo_items

TODAY_WINDOW_DAYS = 7  # "오늘"에 올라오는 생성 통화 — 기준일 전 이 날수 안(설계 "오늘")
CALLS_CTE = (
    "WITH calls AS ("
    " SELECT a.call_uid, a.direction, a.started_at AS at, a.transcript, a.duration_sec,"
    "  a.caller_phone AS phone, COALESCE(p.name, a.caller_name) AS name, p.chart_no,"
    "  'generated' AS source"
    " FROM source.ax_calls a LEFT JOIN source.emr_patients p ON p.id = a.patient_id"
    " WHERE a.source = 'generated' AND a.in_eval_pool = 0 AND a.transcript IS NOT NULL"
    " UNION ALL"
    " SELECT c.call_uid, c.direction, c.started_at, c.transcript, NULL, c.caller_phone, p.name,"
    "  p.chart_no, 'pasted'"
    " FROM ax_pasted_calls c LEFT JOIN source.emr_patients p ON p.id = c.patient_id)"
)
ROWS_SQL = (
    CALLS_CTE + " SELECT calls.*, k.id AS classification_id, k.status, k.labels, k.summary,"
    " k.todo, j.labels AS judge_labels, d.items AS disagree_items, d.is_core"
    " FROM calls"
    " LEFT JOIN ax_call_classifications k ON k.call_uid = calls.call_uid"
    "  AND k.model_alias = 'classify' AND k.prompt_version = :a"
    " LEFT JOIN ax_model_judgments j ON j.call_uid = calls.call_uid"
    "  AND j.model_alias = 'judge' AND j.prompt_version = :b AND j.status = 'ok'"
    " LEFT JOIN ax_call_disagreements d ON d.call_uid = calls.call_uid"
    "  AND d.a_version = :a AND d.b_version = :b"
)


def active_versions(conn: sqlite3.Connection) -> tuple[str, str]:
    """(분류, 판정기) 활성 버전 — 앱 설계 §6"""
    judge = db.get_setting(conn, "prompt_versions", {}).get("judge")
    return db.get_setting(conn, "active_prompt_version", "v1"), judge


def _rows(conn, where: str = "", params: dict | None = None) -> list[sqlite3.Row]:
    a, b = active_versions(conn)
    sql = (
        ROWS_SQL + (f" WHERE {where}" if where else "") + " ORDER BY calls.at DESC, calls.call_uid"
    )
    return conn.execute(sql, {"a": a, "b": b, **(params or {})}).fetchall()


def _checks(conn, classification_ids: list[int]) -> dict:
    if not classification_ids:
        return {}
    marks = ",".join("?" * len(classification_ids))
    rows = conn.execute(
        f"SELECT * FROM ax_todo_checks WHERE classification_id IN ({marks})", classification_ids
    )
    return {(r["classification_id"], r["item"]): r for r in rows}


def _labels(r) -> dict | None:
    return json.loads(r["labels"]) if r["status"] == "ok" and r["labels"] else None


def _todo(r, checks: dict) -> list[dict]:
    """항목 dict + 끝낸 상태. 분류가 ok가 아니면 빈 목록"""
    if r["status"] != "ok":
        return []
    out = []
    for i, t in enumerate(todo_items(json.loads(r["todo"] or "[]"))):
        c = checks.get((r["classification_id"], i))
        out.append(
            t
            | {
                "item": i,
                "done": c is not None,
                "done_by": c["done_by"] if c else None,
                "done_at": c["done_at"] if c else None,
            }
        )
    return out


def today_board(conn: sqlite3.Connection, today: date) -> dict:
    """ "오늘" 두 칸 — 기준일 전 TODAY_WINDOW_DAYS일 안의 생성 통화 + 새로 추가한 통화(날짜 무관)의 할 일.
    응급·급함 통화 먼저, 그다음 통화 시각 최근 순, 같은 통화 안은 항목 번호 순"""
    since = (today - timedelta(days=TODAY_WINDOW_DAYS)).isoformat()
    rows = _rows(
        conn,
        "k.status = 'ok' AND (calls.source = 'pasted' OR calls.at >= :since)",
        {"since": since},
    )
    checks = _checks(conn, [r["classification_id"] for r in rows])
    items = []
    for r in rows:
        labels = _labels(r)
        urgent, emergency = labels["urgency"] == "urgent", bool(labels["emergency"])
        for t in _todo(r, checks):
            items.append(
                t
                | {
                    "call_uid": r["call_uid"],
                    "classification_id": r["classification_id"],
                    "at": r["at"],
                    "name": r["name"],
                    "chart_no": r["chart_no"],
                    "urgent": urgent,
                    "emergency": emergency,
                }
            )
    items.sort(key=lambda i: (i["at"], -i["item"]), reverse=True)
    items.sort(key=lambda i: not (i["urgent"] or i["emergency"]))  # 안정 정렬 — 시각 순서 유지
    open_ = [i for i in items if not i["done"]]
    return {
        "internal": [i for i in open_ if i["kind"] == "internal"],
        "contact": [i for i in open_ if i["kind"] == "contact"],
        "done": [i for i in items if i["done"]],
    }


FILTERS = {
    "inbound": lambda c: c["direction"] == "inbound",
    "outbound": lambda c: c["direction"] == "outbound",
    "emergency": lambda c: c["emergency"],
    "todo_left": lambda c: c["todo_left"] > 0,
    "disagree": lambda c: c["disagree"],
}


def _card(r, checks: dict) -> dict:
    labels, todo = _labels(r), _todo(r, checks)
    return {
        "call_uid": r["call_uid"],
        "at": r["at"],
        "direction": r["direction"],
        "name": r["name"],
        "chart_no": r["chart_no"],
        "phone": r["phone"],
        "source": r["source"],
        "status": r["status"],
        "purpose": labels["primary_purpose"] if labels else None,
        "urgency": labels["urgency"] if labels else None,
        "emergency": bool(labels and labels["emergency"]),
        "disagree": r["disagree_items"] is not None,
        "todo_total": len(todo),
        "todo_left": sum(not t["done"] for t in todo),
        "summary": r["summary"],
    }


def list_calls(
    conn: sqlite3.Connection,
    *,
    flt: str | None,
    q: str | None,
    before: str | None,
    limit: int = 50,
) -> dict:
    """통화 기록 — 날짜별 묶음. flt는 FILTERS 키, q는 이름·번호·차트번호 부분 일치, before는 그 시각보다 이전.
    next = 다음 쪽의 before(더 없으면 None)"""
    if flt is not None and flt not in FILTERS:
        raise ValueError(f"filter: {flt}")
    where, params = [], {}
    if q and q.strip():
        where.append("(calls.name LIKE :q OR calls.phone LIKE :q OR calls.chart_no LIKE :q)")
        params["q"] = f"%{q.strip()}%"
    if before:
        where.append("calls.at < :before")
        params["before"] = before
    rows = _rows(conn, " AND ".join(where), params)
    checks = _checks(conn, [r["classification_id"] for r in rows if r["classification_id"]])
    cards = [_card(r, checks) for r in rows]
    if flt:
        cards = [c for c in cards if FILTERS[flt](c)]
    page, more = cards[:limit], len(cards) > limit
    days = [
        {"date": d, "calls": list(group)} for d, group in groupby(page, key=lambda c: c["at"][:10])
    ]
    counts = {}
    for c in cards:
        counts[c["at"][:10]] = counts.get(c["at"][:10], 0) + 1
    for d in days:
        d["count"] = counts[d["date"]]
    return {"days": days, "next": page[-1]["at"] if more else None}


def call_detail(conn: sqlite3.Connection, call_uid: str) -> dict | None:
    """상세 — 통화·분류(할 일 체크 상태 포함)·판정기 라벨·엇갈림. 판정 풀 통화·없는 통화는 None"""
    rows = _rows(conn, "calls.call_uid = :uid", {"uid": call_uid})
    if not rows:
        return None
    r = rows[0]
    checks = _checks(conn, [r["classification_id"]] if r["classification_id"] else [])
    return {
        "call": {
            k: r[k]
            for k in (
                "call_uid",
                "at",
                "direction",
                "name",
                "chart_no",
                "phone",
                "duration_sec",
                "transcript",
                "source",
            )
        },
        "classification": {
            "id": r["classification_id"],
            "status": r["status"],
            "labels": _labels(r),
            "summary": r["summary"],
            "todo": _todo(r, checks),
        },
        "judge": json.loads(r["judge_labels"]) if r["judge_labels"] else None,
        "disagree": (
            {"items": json.loads(r["disagree_items"]), "is_core": bool(r["is_core"])}
            if r["disagree_items"] is not None
            else None
        ),
    }


def set_todo_done(
    conn: sqlite3.Connection, classification_id: int, item: int, *, part: str, at: str, done: bool
) -> None:
    """할 일 하나를 끝냄/되돌림 — 이미 같은 상태면 그대로. 없는 분류·항목은 KeyError"""
    row = conn.execute(
        "SELECT todo FROM ax_call_classifications WHERE id = ? AND status = 'ok'",
        (classification_id,),
    ).fetchone()
    if row is None or not 0 <= item < len(json.loads(row["todo"] or "[]")):
        raise KeyError(f"{classification_id}/{item}")
    if done:
        conn.execute(
            "INSERT OR IGNORE INTO ax_todo_checks VALUES (?, ?, ?, ?)",
            (classification_id, item, part, at),
        )
    else:
        conn.execute(
            "DELETE FROM ax_todo_checks WHERE classification_id = ? AND item = ?",
            (classification_id, item),
        )
    conn.commit()


DIRECTIONS = ("inbound", "outbound")


def add_call(
    conn: sqlite3.Connection,
    gw,
    *,
    direction: str,
    transcript: str,
    phone: str | None,
    part: str,
    now: datetime,
    today: date,
) -> str:
    """새 통화 추가 — 시각은 기준일 날짜 + now의 시각, 번호가 EMR 환자와 맞으면 연결(화면 표시용). 저장 뒤 분류·판정.
    빈 녹취록·잘못된 방향은 ValueError(통화를 만들지 않는다)"""
    if direction not in DIRECTIONS:
        raise ValueError(f"direction: {direction}")
    if not transcript.strip():
        raise ValueError("transcript: 통화 내용이 비어 있습니다")
    phone = (phone or "").strip() or None
    patient = (
        conn.execute("SELECT id FROM source.emr_patients WHERE phone = ?", (phone,)).fetchone()
        if phone
        else None
    )
    n = conn.execute("SELECT COUNT(*) FROM ax_pasted_calls").fetchone()[0] + 1
    uid = f"p-{n:04d}"
    conn.execute(
        "INSERT INTO ax_pasted_calls (call_uid, direction, started_at, transcript, caller_phone,"
        " patient_id, added_by, added_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            uid,
            direction,
            f"{today.isoformat()} {now.strftime('%H:%M:%S')}",
            transcript.strip(),
            phone,
            patient[0] if patient else None,
            part,
            now.strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    conn.commit()
    classify_one(conn, gw, uid)
    return uid


def classify_one(conn: sqlite3.Connection, gw, call_uid: str) -> None:
    """한 통화를 활성 버전으로 분류·판정하고 엇갈림·판정 큐를 맞춘다(새 통화, 다시 분류)"""
    from backend import judge_queue  # judge_queue가 이 모듈을 import한다

    row = conn.execute(
        CALLS_CTE + " SELECT * FROM calls WHERE call_uid = ?", (call_uid,)
    ).fetchone()
    if row is None:
        raise KeyError(call_uid)
    a, b = active_versions(conn)
    cl.save_classification(
        conn, call_uid, a, cl.classify(gw, row["transcript"], row["direction"], a)
    )
    if b:
        cl.save_judgment(conn, call_uid, b, cl.judge(gw, row["transcript"], row["direction"], b))
        disagreements.compute_call(conn, call_uid, a, b)
    judge_queue.refresh_call(conn, call_uid)
