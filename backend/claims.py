"""⑤ 청구 점검 1단계 (기능 정의 §5 ⑤, 설계 docs/superpowers/specs/2026-11-10-claim-review-design.md §1).
삭감 대응: 확정 월 조정·불능 청구의 재심 신청 기한·칩·사유 유형 규칙 제안(가상 데이터 생성 설계 §2.4)과 사람 판단
기록(ax_claim_judgments). 청구 전 점검: 대기 청구 줄의 시술 × 차트 상병이 허용 상병 밖이면 경고(§4.3).
2단계 AI 점검(요건 backend/claim_check.py 설계 §2, 추가 처치 누락 backend/claim_omission.py 설계 §2b)의 저장 결과를 청구 전 AI 칩과 요건 사유 조정 건의 판단 추천에 붙인다.
청구 건마다 DB를 부르지 않고 표마다 한 번 훑는 집합 쿼리로 센다 — 청구 수와 무관하게 쿼리 수가 같다.
master 값은 source.ax_master_values로만 읽는다(CLAUDE.md). 오늘 화면(backend/today.py)도 이 함수로 센다."""

import sqlite3
from datetime import date, timedelta

from backend import claim_check, claim_evidence, claim_omission
from backend.prep import master_values
from backend.rag_index import _sections

# 칩 순서 = 화면 순서(설계 §1). 화면 문구는 frontend/lib/claims-api.ts
CHIPS = ("all", "diagnosis", "soon", "denied", "adjusted", "expired")
# 판단 값(master.ADJUST_JUDGMENT_TARGET 키와 같은 이름) — 화면 삭감 수용·재심 신청·보완 후 재청구
JUDGMENTS = ("justified", "contestable", "resubmit")
SOON_DAYS = 30  # 남은 날이 이 이하면 위험 꼬리표·`soon` 칩(상병 착오 제외)
WATCH_DAYS = 60  # 남은 날이 이 이하면 주의 꼬리표
# 사유 유형 → 규칙 제안(가상 데이터 생성 설계 §2.4). 요건 사유는 코드로 정해지지 않는다 — 2단계 AI 몫
SUGGEST: dict[str, str | None] = {
    "diagnosis": "justified",
    "external": "justified",
    "documentation": "resubmit",
    "requirement": None,
}
# 제안 이유 한 줄 — 화면 펼침 ② "어떻게 할까"가 그대로 쓴다
SUGGEST_REASON: dict[str, str] = {
    "diagnosis": "상병 착오는 재청구할 수 없습니다 — 다음 청구 때 상병을 확인하세요",
    "external": "다른 기관 이력이라 우리 청구의 잘못이 아닙니다",
    "documentation": "빠진 기재·증빙을 채워 다시 청구할 수 있습니다",
    "requirement": "차트에 요건 근거가 있으면 재심 신청, 없으면 삭감 수용입니다",
}
CUT = ("adjusted", "denied")  # 깎인 청구(일부 조정·전액 불능)
# AI 요건 판단 → 판단 추천(설계 §2 "조정 건 판단(b)"): 근거 있음 → 재심 신청, 근거 없음 → 삭감 수용. na는 추천 없음
AI_SUGGEST = {"met": "contestable", "missing": "justified"}


def _deadline(reviewed_at: str, appeal_days: int) -> date:
    """재심 신청 기한 = 심사결과 통보일 + appeal_days(가상 데이터 생성 설계 §2.4)"""
    return date.fromisoformat(reviewed_at[:10]) + timedelta(days=appeal_days)


def _chips(status: str, type_: str, days_left: int) -> list[str]:
    """행 칩 — 기한 남음(남은 날 ≥ 0, 기한 당일 포함)이면 전체 + 상병 착오/30일 이내 + 상태, 지나면 기한 지남만"""
    if days_left < 0:
        return ["expired"]
    chips = ["all"]
    if type_ == "diagnosis":
        chips.append("diagnosis")
    elif days_left <= SOON_DAYS:
        chips.append("soon")
    return [*chips, status]


def standard_cards(conn: sqlite3.Connection) -> dict[str, dict]:
    """절 제목 → 조항 카드 {doc_title, heading, body} — 청구 기준 문서(ax_documents claim_standard)의 절(설계 §3)"""
    out = {}
    for d in conn.execute(
        "SELECT title, body FROM source.ax_documents WHERE type = 'claim_standard' ORDER BY id"
    ):
        for heading, body in _sections(d["title"], d["body"]):
            if heading != d["title"]:  # 머리말은 절이 아니다
                out[heading] = {"doc_title": d["title"], "heading": heading, "body": body.strip()}
    return out


def adjusted(conn: sqlite3.Connection, today: date) -> dict:
    """기준일까지 통보된 조정·불능 청구 전부(기한 지난 건 포함) — 행과 칩 집계(건수·못 받은 돈), 화면 꼬리표 경계.
    정렬: 기한 남은 행은 기한 이른 순, 지난 행은 그 뒤에 기한 늦은 순(최근에 지난 것 먼저)"""
    mv = master_values(conn)
    days, types, headings = mv["appeal_days"], mv["adjust_note_types"], mv["adjust_note_headings"]
    rows = []
    ai = claim_check.results(conn)
    cards = standard_cards(conn)
    quotes = claim_evidence.results(conn)
    for r in conn.execute(
        "SELECT c.id, c.chart_no, p.name, c.service_date, c.tooth_no, pr.name AS procedure_name,"
        " c.status, c.claim_amount, COALESCE(c.paid_amount, 0) AS paid_amount, c.adjust_note,"
        " c.reviewed_at, j.judgment, j.judged_by, j.judged_at"
        " FROM source.emr_claims c"
        " JOIN source.emr_patients p ON p.id = c.patient_id"
        " JOIN source.emr_procedures pr ON pr.code = c.procedure_code"
        " LEFT JOIN ax_claim_judgments j ON j.claim_id = c.id"
        " WHERE c.status IN (?, ?) AND c.reviewed_at <= ?",
        (*CUT, today.isoformat()),
    ):
        type_ = types[r["adjust_note"]]
        deadline = _deadline(r["reviewed_at"], days)
        days_left = (deadline - today).days
        suggestion, by, evidence = SUGGEST[type_], "rule" if SUGGEST[type_] else None, None
        check = ai.get(r["id"])
        if type_ == "requirement" and check and check["requirement"] in AI_SUGGEST:  # 실패는 None
            suggestion, by, evidence = AI_SUGGEST[check["requirement"]], "ai", check["evidence"]
        rows.append(
            {
                "claim_id": r["id"],
                "chart_no": r["chart_no"],
                "name": r["name"],
                "service_date": r["service_date"],
                "tooth_no": r["tooth_no"],
                "procedure_name": r["procedure_name"],
                "status": r["status"],
                "claim_amount": r["claim_amount"],
                "paid_amount": r["paid_amount"],
                "unpaid": r["claim_amount"] - r["paid_amount"],
                "adjust_note": r["adjust_note"],
                "type": type_,
                "reviewed_at": r["reviewed_at"],
                "deadline": deadline.isoformat(),
                "days_left": days_left,
                "expired": days_left < 0,
                "chips": _chips(r["status"], type_, days_left),
                "suggestion": suggestion,
                "suggest_by": by,
                "ai_evidence": evidence,
                "reason": SUGGEST_REASON[type_],
                "fill_item": headings[r["adjust_note"]] if suggestion == "resubmit" else None,
                "standard": cards.get(headings[r["adjust_note"]]),
                # 서류 사유만 — 통과한 결과가 있으면 목록(빈 답은 []), 없거나 실패면 None
                "chart_quotes": (quotes.get(r["id"]) or {}).get("quotes")
                if type_ == "documentation"
                else None,
                "judgment": (
                    {"value": r["judgment"], "by": r["judged_by"], "at": r["judged_at"]}
                    if r["judgment"]
                    else None
                ),
            }
        )
    rows.sort(
        key=lambda x: (
            (1, -x["days_left"], x["claim_id"])
            if x["expired"]
            else (0, x["days_left"], x["claim_id"])
        )
    )
    chips = {c: {"count": 0, "amount": 0} for c in CHIPS}
    for x in rows:
        for c in x["chips"]:
            chips[c]["count"] += 1
            chips[c]["amount"] += x["unpaid"]
    return {
        "date": today.isoformat(),
        "appeal_days": days,
        "soon_days": SOON_DAYS,
        "watch_days": WATCH_DAYS,
        "chips": chips,
        "rows": rows,
    }


def _ai_pending(conn, today: date) -> dict[str, int]:
    """갱신 버튼 줄 수 — 청구 전은 대기 줄 중 2a·2b 어느 쪽이든 다시 부를 줄, 삭감 대응은 2a·⑤-3 차트 문장(합집합)"""
    req = claim_check.todo_ids(conn, today)
    pending = {
        r[0] for r in conn.execute("SELECT id FROM source.emr_claims WHERE status = 'pending'")
    }
    pre = (req | claim_omission.todo_ids(conn, today)) & pending
    adjust = (req | claim_evidence.todo_ids(conn, today)) - pending
    return {"pre": len(pre), "adjust": len(adjust)}


def pre_claim(conn: sqlite3.Connection, today: date) -> dict:
    """대기(pending) 청구 줄과 상병 확인 경고 — 시술 × 차트 상병이 허용 상병 밖이면 경고, 지난 조정(통보일이 today
    전)에서 같은 조합이 상병 착오로 깎인 건수·못 받은 돈. 정렬: 경고 먼저 → 진료일 → 청구 id"""
    mv = master_values(conn)
    allowed = mv["procedure_diagnoses"]
    dx_notes = [n for n, t in mv["adjust_note_types"].items() if t == "diagnosis"]
    dx_names = dict(conn.execute("SELECT code, name FROM source.emr_diagnoses").fetchall())
    past = {
        (r[0], r[1]): (r[2], r[3])
        for r in conn.execute(
            "SELECT c.procedure_code, n.dx_code, COUNT(*),"
            " SUM(c.claim_amount - COALESCE(c.paid_amount, 0))"
            " FROM source.emr_claims c"
            " JOIN source.emr_chart_notes n ON n.treatment_id = c.treatment_id"
            f" WHERE c.status IN (?, ?) AND c.reviewed_at < ?"
            f" AND c.adjust_note IN ({', '.join('?' * len(dx_notes))})"
            " GROUP BY c.procedure_code, n.dx_code",
            (*CUT, today.isoformat(), *dx_notes),
        )
    }
    rows = []
    for r in conn.execute(
        "SELECT c.id, c.service_date, c.chart_no, p.name, c.procedure_code,"
        " pr.name AS procedure_name, c.tooth_no, c.claim_amount, n.dx_code"
        " FROM source.emr_claims c"
        " JOIN source.emr_patients p ON p.id = c.patient_id"
        " JOIN source.emr_procedures pr ON pr.code = c.procedure_code"
        " LEFT JOIN source.emr_chart_notes n ON n.treatment_id = c.treatment_id"
        " WHERE c.status = 'pending'"
    ):
        ok = allowed.get(r["procedure_code"], [])
        warning = None
        if r["dx_code"] and r["dx_code"] not in ok:
            count, amount = past.get((r["procedure_code"], r["dx_code"]), (0, 0))
            warning = {
                "allowed": [dx_names[d] for d in sorted(ok)],
                "past_count": count,
                "past_amount": amount,
            }
        rows.append(
            {
                "claim_id": r["id"],
                "service_date": r["service_date"],
                "chart_no": r["chart_no"],
                "name": r["name"],
                "procedure_name": r["procedure_name"],
                "tooth_no": r["tooth_no"],
                "claim_amount": r["claim_amount"],
                "dx_code": r["dx_code"],
                "dx_name": dx_names.get(r["dx_code"]),
                "warning": warning,
            }
        )
    # 요건(2a)과 추가 처치 누락(2b)은 따로 저장한다 — 누락은 2b 답만 쓴다(2a 답의 omissions는 보지 않음, 설계 §2b)
    req, om = claim_check.results(conn), claim_omission.results(conn)
    saved = [
        max(d[x["claim_id"]]["at"] for d in (req, om) if x["claim_id"] in d)
        for x in rows
        if x["claim_id"] in req or x["claim_id"] in om
    ]
    for x in rows:
        check, miss = req.get(x["claim_id"]), om.get(x["claim_id"])
        ok, has = bool(check and check["valid"]), bool(miss and miss["valid"])
        x["ai"] = (
            {
                "requirement": check["requirement"] if ok else None,
                "evidence": check["evidence"] if ok else [],
                "omissions": miss["omissions"] if has else [],
            }
            if ok or has
            else None
        )
    rows.sort(key=lambda x: (x["warning"] is None, x["service_date"], x["claim_id"]))
    warned = sum(x["warning"] is not None for x in rows)
    flagged = sum(
        bool(x["ai"]) and (x["ai"]["requirement"] == "missing" or bool(x["ai"]["omissions"]))
        for x in rows
    )
    return {
        "counts": {
            "lines": len(rows),
            "warning": warned,
            "ai": flagged,
            "ai_missing": sum(bool(x["ai"]) and x["ai"]["requirement"] == "missing" for x in rows),
            "ai_checked": len(saved),
        },
        "ai_at": max(saved, default=None),
        "ai_pending": _ai_pending(conn, today),
        "rows": rows,
    }


def record_judgment(
    conn: sqlite3.Connection, claim_id: int, judgment: str, by: str, at: str, today: date
) -> None:
    """사람 판단 한 줄(덮어씀) — 모르는 값, 없거나 조정·불능이 아닌 청구, 기준일 뒤 통보·기한 지난 청구, 상병 착오에 삭감 수용
    (justified)이 아닌 판단이면 ValueError(설계 "하면 실패인 것"). 커밋은 부르는 쪽"""
    if judgment not in JUDGMENTS:
        raise ValueError(f"모르는 판단: {judgment}")
    c = conn.execute(
        "SELECT status, adjust_note, reviewed_at FROM source.emr_claims WHERE id = ?", (claim_id,)
    ).fetchone()
    if c is None or c["status"] not in CUT:
        raise ValueError(f"조정·불능 청구가 아님: {claim_id}")
    if c["reviewed_at"] > today.isoformat():
        raise ValueError(f"기준일 뒤에 통보된 청구: {claim_id}")
    mv = master_values(conn)
    if _deadline(c["reviewed_at"], mv["appeal_days"]) < today:
        raise ValueError(f"재심 신청 기한 지남: {claim_id}")
    if mv["adjust_note_types"][c["adjust_note"]] == "diagnosis" and judgment != "justified":
        raise ValueError("상병 착오는 재청구할 수 없어 삭감 수용만 기록한다")
    conn.execute(
        "INSERT INTO ax_claim_judgments (claim_id, judgment, judged_by, judged_at)"
        " VALUES (?, ?, ?, ?) ON CONFLICT(claim_id) DO UPDATE SET"
        " judgment = excluded.judgment, judged_by = excluded.judged_by,"
        " judged_at = excluded.judged_at",
        (claim_id, judgment, by, at),
    )
