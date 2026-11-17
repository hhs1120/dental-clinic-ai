"""오늘 화면 한 장 (기능 정의 §4 앱 구성 "오늘 화면", 앱 설계 §4 "오늘 화면"). 요약 카드 → 사람이 볼 일 → AI가 한 일.
숫자는 전부 쿼리(기능 정의 §3 원칙 2)이고, 숫자마다 그 숫자가 가리키는 화면과 같은 함수로 센다 — 예약·신환·주의·
내원 확인 전은 준비표(prep.prep_facts), 할 일·급한 통화는 통화 화면 오늘 할 일(calls.today_board), 확인할 통화는
calls.review_calls, 판정 대기는 판정 큐(judge_queue.pending_count), 자동 처리는 통화 화면과 같은 유효 조건,
재심 신청 기한 임박·청구 전 상병 경고는 청구 점검 화면(claims.adjusted `soon` 칩·claims.pre_claim 경고 줄),
일계표 불일치는 재무 대조 화면의 확정 안 된 건(recon.open_summary — 링크는 그런 건이 있는 가장 이른 달),
지출 증빙 확인은 지출 증빙 탭의 지난달 확인할 출금(expense.open_summary). 일계표 자동 마감·지출 증빙 자동 확정은
그 화면과 같은 유효 조건(recon_auto.active·expense_auto.active — 정책 켜짐·지금 화면의 건).
미예약 환자 줄은 숫자 없는 고정 링크라 여기서 세지 않는다. ⑦ 아침 브리핑은 코드 템플릿(backend/briefing.py),
야간 실행 끝난 시각·실패는 마지막 ax_jobs nightly 행(backend/nightly.py last_run). LLM을 부르지 않는다."""

import sqlite3
from datetime import date, datetime, timedelta

from backend import (
    autoclose,
    briefing,
    calls,
    claims,
    expense,
    expense_auto,
    judge_queue,
    nightly,
    prep,
    recon,
    recon_auto,
    recon_match,
)

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
    """열린 할 일 수(줄·통화·급한 줄 — 카드의 두 숫자를 같은 단위로)와 급한 통화(응급·급함, 통화마다 한 줄 — 첫 열린
    할 일). 순서는 오늘 할 일과 같다(통화 시각 최근 순)"""
    open_ = board["internal"] + board["contact"]
    urgent: dict[str, dict] = {}
    for i in sorted(open_, key=lambda i: (i["at"], -i["item"]), reverse=True):  # 시각 최근·번호 순
        if i["urgent"] or i["emergency"]:
            urgent.setdefault(
                i["call_uid"],
                {k: i[k] for k in ("call_uid", "name", "text", "at", "emergency")},
            )
    counts = {"items": len(open_), "calls": len({i["call_uid"] for i in open_})}
    counts["urgent_items"] = sum(bool(i["urgent"] or i["emergency"]) for i in open_)
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


def _recon_auto(conn: sqlite3.Connection, today: date) -> dict:
    """되돌리지 않은 일계표 자동 마감 — 최근 마감 순"""
    if not recon_auto.policy_on(conn):
        return {"on": False, "total": 0, "recent": []}
    dates = {i["key"]: i["date"] for i in recon_match.detect(recon_match.load(conn, today))}
    act = recon_auto.active(conn, set(dates))
    rows = sorted(act.items(), key=lambda kv: (kv[1]["at"], kv[1]["id"]), reverse=True)
    recent = [
        {"id": v["id"], "key": k, "date": dates[k], "cause": v["cause"], "at": v["at"]}
        for k, v in rows[:AUTO_RECENT]
    ]
    return {"on": True, "total": len(act), "recent": recent}


def _expense_auto(conn: sqlite3.Connection, today: date) -> dict:
    """되돌리지 않은 지출 증빙 자동 확정(backend/expense_auto.py) — 최근 확정 순, 기준일 전 출금만"""
    if not expense_auto.policy_on(conn):
        return {"on": False, "total": 0, "recent": []}
    outflows = {expense.outflow_key(o): o for o in expense._load(conn, today)["outflows"]}
    act = expense_auto.active(conn, set(outflows))
    rows = sorted(act.items(), key=lambda kv: (kv[1]["at"], kv[1]["id"]), reverse=True)
    recent = [
        {
            "id": v["id"],
            "key": k,
            "at": outflows[k]["at"],
            "label": outflows[k]["label"],
            "amount": outflows[k]["amount"],
            "account": v["account"],
        }
        for k, v in rows[:AUTO_RECENT]
    ]
    return {"on": True, "total": len(act), "recent": recent}


def _failures(conn) -> dict:
    """활성 분류 행 기준 — 분류 실패(ok가 아님, 또는 분류 행 없이 남은 새 통화 — 분류 중 예외로 통화만 남는
    경우, calls.add_call)·처리안 실패 통화 수. 분류 행 없는 생성 통화(버전 전환 직후 일괄 분류 전)는 세지 않는다.
    판정 풀 통화는 CALLS_CTE가 뺀다"""
    a, _ = calls.active_versions(conn)
    base = (
        calls.CALLS_CTE + " SELECT COUNT(*) FROM calls"
        " LEFT JOIN ax_call_classifications k ON k.call_uid = calls.call_uid"
        "  AND k.model_alias = 'classify' AND k.prompt_version = ?"
    )
    classify = conn.execute(
        base + " WHERE k.status != 'ok' OR (k.id IS NULL AND calls.source = 'pasted')", (a,)
    ).fetchone()[0]
    followup = conn.execute(
        base + " JOIN ax_call_followups fu ON fu.classification_id = k.id"
        " WHERE fu.status = 'failed'",
        (a,),
    ).fetchone()[0]
    return {"classify_failed": classify, "followup_failed": followup}


def _nightly(conn, now: datetime) -> dict | None:
    """마지막 야간 실행 — "AI가 한 일"의 상태 꼬리표. recent = 끝난 지 briefing.NIGHT_HOURS(실제 시계) 안 — 아니면
    화면이 날짜를 붙여 어젯밤 실행으로 읽히지 않게. failed = 단계 실패 건수 합"""
    job = nightly.last_run(conn)
    if job is None:
        return None
    end = job["finished_at"]
    return {
        "finished_at": end,
        "state": nightly.state(job, now),
        "recent": bool(
            end and now - datetime.fromisoformat(end) <= timedelta(hours=briefing.NIGHT_HOURS)
        ),
        "failed": sum(d["failed"] for d in job["detail"]),
    }


def _prep_saved(conn, today: date) -> dict:
    """오늘 준비표 저장 행 — 제안(suggest) 수, 후검증 실패(suggestion_valid 0) 수"""
    r = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(json_extract(suggestion, '$.action') = 'suggest'), 0),"
        " COALESCE(SUM(suggestion_valid = 0), 0) FROM ax_prep_sheets WHERE sheet_date = ?",
        (today.isoformat(),),
    ).fetchone()
    return {"rows": r[0], "suggest": r[1], "failed": r[2]}


def _refused(conn, now: datetime) -> list[dict]:
    """근거 없는 질문 — 물어보기에서 문서 근거를 못 찾아 거절된 질문(챗·검색 요약, 평가 실행 제외), 실제 시계 최근
    REFUSED_DAYS일. 비공개 정보라 거절한 챗(refuse_reason restricted)은 문서가 없어서가 아니라 뺀다"""
    since = (now - timedelta(days=REFUSED_DAYS)).isoformat(sep=" ", timespec="seconds")
    return [
        dict(r)
        for r in conn.execute(
            "SELECT id, kind, question, created_at AS at FROM ax_search_logs"
            " WHERE status = 'refused' AND COALESCE(refuse_reason, '') != 'restricted'"
            " AND is_eval = 0 AND replace(created_at, 'T', ' ') >= ?"
            " ORDER BY created_at DESC, id DESC",
            (since,),
        )
    ]


def _recon(conn: sqlite3.Connection, today: date) -> dict:
    """일계표 불일치 — 전체 기간 미확정 수(/finance 칩 open_all)와 링크할 달(미확정이 있는 가장 이른 달)"""
    s = recon.open_summary(conn, today)
    return {"recon_open": s["count"], "recon_month": s["month"]}


def _expense(conn: sqlite3.Connection, today: date) -> dict:
    """지출 증빙 확인 — 지난달 출금 중 사람이 볼 줄(증빙 없음·과목 확인 필요·규칙과 다름) 수와 링크할 달"""
    s = expense.open_summary(conn, today)
    return {"expense_open": s["count"], "expense_month": s["month"]}


def sheet(conn: sqlite3.Connection, today: date, now: datetime) -> dict:
    """GET /today — today = 앱 기준일(shared/clinic.py), now = 실제 시계(근거 없는 질문 7일)"""
    rows = prep.prep_facts(conn, today, today, include_ax=True)
    cards = _cards(rows)
    todo, urgent = _todo(calls.today_board(conn, today))
    pre = claims.pre_claim(conn, today)["counts"]
    return {
        "date": today.isoformat(),
        "cards": cards | {"todo": todo},
        "people": {
            "urgent_calls": urgent,
            "review_calls": calls.review_calls(conn, today),
            "unconfirmed": sum("unconfirmed" in r["chips"] for r in rows),
            "alert_patients": cards["alert_patients"],
            "todo_left": {k: todo[k] for k in ("items", "calls")},
            "judge_pending": judge_queue.pending_count(conn),
            "refused": _refused(conn, now),
            "appeal_soon": claims.adjusted(conn, today)["chips"]["soon"]["count"],
            "preclaim_warning": pre["warning"],
            "preclaim_ai": pre[
                "ai_missing"
            ],  # 화면 문구가 '요건 근거 못 찾음' — 누락만 있는 줄은 빼고
            **_recon(conn, today),
            **_expense(conn, today),
        },
        "briefing": briefing.lines(conn, today, now),
        "ai": _auto(conn)
        | {
            "nightly": _nightly(conn, now),
            "recon_auto": _recon_auto(conn, today),
            "expense_auto": _expense_auto(conn, today),
            "prep": _prep_saved(conn, today),
        }
        | _failures(conn),
    }
