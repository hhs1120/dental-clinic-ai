"""오늘 화면 한 장 (기능 정의 §4 앱 구성 "오늘 화면", 앱 설계 §4 "오늘 화면"). 요약 카드 → 사람이 볼 일 → AI가 한 일.
숫자는 전부 쿼리(기능 정의 §3 원칙 2)이고, 숫자마다 그 숫자가 가리키는 화면과 같은 함수로 센다 — 예약·신환·주의·
예약 미확인은 준비표(prep.prep_facts), 할 일·급한 통화는 통화 화면 오늘 할 일(calls.today_board), 확인할 통화는
calls.review_calls, 판정 대기는 판정 큐(judge_queue.pending_count), 자동 처리는 통화 화면과 같은 유효 조건.
LLM을 부르지 않는다."""

import sqlite3
from datetime import date, datetime, timedelta

from backend import autoclose, calls, judge_queue, prep

AUTO_RECENT = 20  # "AI가 한 일" 펼친 자동 처리 목록 — 최근에 닫힌 순, 나머지는 통화 기록에서
REFUSED_DAYS = 7  # 근거 없는 질문 — 실제 시계 기준 최근 이 날수(확인함 없이 지나면 빠진다)
APPT_TYPES = ("treatment", "hygiene", "consult")


def _cards(rows: list[dict]) -> dict:
    appts = {"total": len(rows)} | {t: sum(r["type"] == t for r in rows) for t in APPT_TYPES}
    return {
        "appointments": appts,
        "new_patients": sum(r["patient"]["new_patient"] for r in rows),
        "alert_patients": sum("alert" in r["chips"] for r in rows),
    }


def _todo(board: dict) -> tuple[dict, list[dict]]:
    """열린 할 일 수와 급한 통화(응급·급함, 통화마다 한 줄 — 첫 열린 할 일). 순서는 오늘 할 일과 같다(통화 시각 최근 순)"""
    open_ = board["internal"] + board["contact"]
    urgent: dict[str, dict] = {}
    for i in sorted(open_, key=lambda i: (i["at"], -i["item"]), reverse=True):  # 시각 최근·번호 순
        if i["urgent"] or i["emergency"]:
            urgent.setdefault(
                i["call_uid"],
                {k: i[k] for k in ("call_uid", "name", "text", "at", "emergency")},
            )
    counts = {"items": len(open_), "calls": len({i["call_uid"] for i in open_})}
    return counts, list(urgent.values())


def _auto(conn) -> dict:
    """되돌리지 않은 자동 종결 — 통화 화면(calls.ROWS_SQL auto 조인)과 같은 유효 조건: 정책 켜짐·활성 버전 쌍·앱 통화"""
    if not autoclose.policy_on(conn):
        return {"autoclose_on": False, "auto_total": 0, "auto_recent": []}
    a, b = calls.active_versions(conn)
    base = (
        calls.CALLS_CTE + " {cols} FROM ax_auto_actions auto"
        " JOIN calls ON calls.call_uid = auto.target"
        " WHERE auto.kind = :kind AND auto.reverted_at IS NULL"
        " AND json_extract(auto.result, '$.versions') = json_array(:a, :b)"
    )
    p = {"kind": autoclose.KIND, "a": a, "b": b, "n": AUTO_RECENT}
    total = conn.execute(base.format(cols="SELECT COUNT(*)"), p).fetchone()[0]
    recent = conn.execute(
        base.format(
            cols="SELECT auto.id, auto.target AS call_uid, calls.name, auto.reason,"
            " auto.created_at AS at"
        )
        + " ORDER BY auto.created_at DESC, auto.id DESC LIMIT :n",
        p,
    ).fetchall()
    return {"autoclose_on": True, "auto_total": total, "auto_recent": [dict(r) for r in recent]}


def _failures(conn) -> dict:
    """활성 분류 행 기준 — 분류 실패(ok가 아님)·처리안 실패 통화 수. 판정 풀 통화는 CALLS_CTE가 뺀다"""
    a, _ = calls.active_versions(conn)
    base = (
        calls.CALLS_CTE + " SELECT COUNT(*) FROM calls"
        " JOIN ax_call_classifications k ON k.call_uid = calls.call_uid"
        "  AND k.model_alias = 'classify' AND k.prompt_version = ?"
    )
    classify = conn.execute(base + " WHERE k.status != 'ok'", (a,)).fetchone()[0]
    followup = conn.execute(
        base + " JOIN ax_call_followups fu ON fu.classification_id = k.id"
        " WHERE fu.status = 'failed'",
        (a,),
    ).fetchone()[0]
    return {"classify_failed": classify, "followup_failed": followup}


def _prep_saved(conn, today: date) -> dict:
    """오늘 준비표 저장 행 — 제안(suggest) 수, 후검증 실패(suggestion_valid 0) 수"""
    r = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(json_extract(suggestion, '$.action') = 'suggest'), 0),"
        " COALESCE(SUM(suggestion_valid = 0), 0) FROM ax_prep_sheets WHERE sheet_date = ?",
        (today.isoformat(),),
    ).fetchone()
    return {"rows": r[0], "suggest": r[1], "failed": r[2]}


def _refused(conn, now: datetime) -> list[dict]:
    """근거 없는 질문 — 물어보기에서 거절된 질문(챗·검색 요약, 평가 실행 제외), 실제 시계 최근 REFUSED_DAYS일"""
    since = (now - timedelta(days=REFUSED_DAYS)).isoformat(sep=" ", timespec="seconds")
    return [
        dict(r)
        for r in conn.execute(
            "SELECT id, kind, question, created_at AS at FROM ax_search_logs"
            " WHERE status = 'refused' AND is_eval = 0 AND replace(created_at, 'T', ' ') >= ?"
            " ORDER BY created_at DESC, id DESC",
            (since,),
        )
    ]


def sheet(conn: sqlite3.Connection, today: date, now: datetime) -> dict:
    """GET /today — today = 앱 기준일(shared/clinic.py), now = 실제 시계(근거 없는 질문 7일)"""
    rows = prep.prep_facts(conn, today, today)
    cards = _cards(rows)
    todo, urgent = _todo(calls.today_board(conn, today))
    return {
        "date": today.isoformat(),
        "cards": cards | {"todo": todo | {"urgent_calls": len(urgent)}},
        "people": {
            "urgent_calls": urgent,
            "review_calls": calls.review_calls(conn, today),
            "unconfirmed": sum("unconfirmed" in r["chips"] for r in rows),
            "alert_patients": cards["alert_patients"],
            "todo_left": todo,
            "judge_pending": judge_queue.pending_count(conn),
            "refused": _refused(conn, now),
        },
        "ai": _auto(conn) | {"prep": _prep_saved(conn, today)} | _failures(conn),
    }
