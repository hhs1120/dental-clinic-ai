"""⑥ 경영 리포트 (기능 정의 §5 ⑥, 설계 docs/superpowers/specs/2026-11-13-report-design.md).
끝난 기간(월·주·진료일)마다 운영 숫자와 이전 기간 대비 변화 분해를 코드가 `metrics`로 모은다(설계 §2·§3).
재료비 과목은 ⑧-2 지출 증빙 줄(backend/expense.py sheet)의 과목을 그대로 쓴다. master 값은 source.ax_master_values로만."""

import json
import sqlite3
from datetime import date, timedelta

from backend import db, expense
from backend.prep import numbers

SCOPES = ("monthly", "weekly", "daily")
COUNTS = {"monthly": 6, "weekly": 8, "daily": 10}  # 화면 기간 수(설계 §1)
# 역할별 월 인건비 가정값(원, 4대보험 포함) — 실제 급여가 아니다. ax_settings labor_cost_assumed가 역할별로 덮어쓴다.
# 대표 원장(doctor)은 넣지 않는다(기능 정의 §5 ⑥)
LABOR_DEFAULT = {"hygienist": 3_800_000, "desk": 3_000_000, "consultant": 3_800_000}
MATERIAL, LAB = "재료비", "외주가공비"  # ⑧-2 계정 과목 이름(master ACCOUNTS)
NO_PCT = ("noshow_rate",)  # 이미 %인 값 — 증감률 없이 증감(%p)만
MAX_POINTS = 3
EST_MARK, EST_NOTE = (
    "[추정]",
    "대표 원장 보수·세금 전",
)  # 추정 값을 쓴 점에 글자 그대로(기능 정의 §5 ⑥)
SCHEMA = {
    "type": "object",
    "properties": {
        "points": {
            "type": "array",
            "maxItems": MAX_POINTS,
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "used": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["text", "used"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["points"],
    "additionalProperties": False,
}


def is_est(key: str) -> bool:
    """인건비 가정값이 들어간 [추정] 키 — 이전 기간·증감 키(prev.est.…)도"""
    return "est." in key


def _days(conn: sqlite3.Connection, today: date) -> list[str]:
    """진료일 — 기준일 전, 취소 뺀 예약이 있는 날"""
    return [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT substr(scheduled_at, 1, 10) d FROM source.emr_appointments"
            " WHERE status != 'cancelled' AND scheduled_at < ? ORDER BY d",
            (today.isoformat(),),
        )
    ]


def _monday(d: str) -> str:
    x = date.fromisoformat(d)
    return (x - timedelta(days=x.weekday())).isoformat()


def periods(conn: sqlite3.Connection, today: date, scope: str) -> list[str]:
    """끝난 기간 라벨, 오래된 것부터(설계 §1) — 월 "2026-10", 주 그 주 월요일, 일 진료일"""
    days = _days(conn, today)
    if scope == "monthly":
        labels = sorted({d[:7] for d in days if d[:7] < today.isoformat()[:7]})
    elif scope == "weekly":
        labels = sorted(
            {m for m in map(_monday, days) if date.fromisoformat(m) + timedelta(days=7) <= today}
        )
    elif scope == "daily":
        labels = days
    else:
        raise ValueError(f"모르는 기간 종류: {scope}")
    return labels[-COUNTS[scope] :]


def prev_period(conn: sqlite3.Connection, today: date, scope: str, label: str) -> str | None:
    """바로 앞 같은 종류 기간 — 월은 앞 달, 주는 7일 전 월요일, 일은 그 전 진료일(없으면 None)"""
    if scope == "monthly":
        return (date.fromisoformat(label + "-01") - timedelta(days=1)).isoformat()[:7]
    if scope == "weekly":
        return (date.fromisoformat(label) - timedelta(days=7)).isoformat()
    before = [d for d in _days(conn, today) if d < label]
    return before[-1] if before else None


def _range(scope: str, label: str) -> tuple[str, str]:
    """[시작, 끝) 날짜 문자열"""
    if scope == "monthly":
        start = date.fromisoformat(label + "-01")
        end = (start + timedelta(days=32)).replace(day=1)
    else:
        start = date.fromisoformat(label)
        end = start + timedelta(days=7 if scope == "weekly" else 1)
    return start.isoformat(), end.isoformat()


def _sum(conn, sql: str, *params) -> int:
    return conn.execute(sql, params).fetchone()[0] or 0


def labor(conn: sqlite3.Connection) -> dict:
    """인건비 가정값과 재직 인원 — {"wage": {역할: 원}, "headcount": {역할: 명}}"""
    over = db.get_setting(conn, "labor_cost_assumed", {}) or {}
    wage = {r: int(over.get(r, v)) for r, v in LABOR_DEFAULT.items()}
    count = dict(
        conn.execute(
            "SELECT role, COUNT(*) FROM source.emr_staff WHERE active = 1 GROUP BY role"
        ).fetchall()
    )
    return {"wage": wage, "headcount": {r: count.get(r, 0) for r in LABOR_DEFAULT}}


def _dx_notes(conn) -> list[str]:
    row = conn.execute(
        "SELECT value FROM source.ax_master_values WHERE name = 'adjust_note_types'"
    ).fetchone()
    return [n for n, t in json.loads(row[0]).items() if t == "diagnosis"]


def dx_top(conn: sqlite3.Connection, month: str) -> dict | None:
    """그 달(통보 월) 상병 착오에서 못 받은 금액이 가장 큰 시술 × 상병 조합(같으면 건수) — 기능 정의 §5 ⑥"""
    notes = _dx_notes(conn)
    rows = conn.execute(
        "SELECT p.name || ' × ' || d.name AS combo, COUNT(*) n,"
        " SUM(c.claim_amount - COALESCE(c.paid_amount, 0)) lost"
        " FROM source.emr_claims c JOIN source.emr_chart_notes cn USING (treatment_id)"
        " JOIN source.emr_procedures p ON p.code = c.procedure_code"
        " JOIN source.emr_diagnoses d ON d.code = cn.dx_code"
        f" WHERE c.adjust_note IN ({', '.join('?' * len(notes))})"
        " AND substr(c.reviewed_at, 1, 7) = ? GROUP BY combo",
        (*notes, month),
    ).fetchall()
    if not rows:
        return None
    top = max(rows, key=lambda r: (r["lost"], r["n"], r["combo"]))
    return {"name": top["combo"], "n": top["n"], "lost": top["lost"]}


def _expenses(conn, today: date, month: str) -> dict:
    """그 달 출금의 과목별 합 — 과목은 지출 증빙 줄과 같다(확정 > 자동 확정 > 같은 출금처 확정 > 거래처 기본)"""
    try:
        rows = expense.sheet(conn, today, month)["rows"]
    except ValueError:  # 출금이 없는 달
        rows = []
    open_ = [r for r in rows if r["confirmed"] is None and r["auto"] is None]
    none = [r for r in rows if r["account"] is None]
    return {
        "material": sum(r["amount"] for r in rows if r["account"] == MATERIAL),
        "lab": sum(r["amount"] for r in rows if r["account"] == LAB),
        "material_unconfirmed": sum(r["account"] in (MATERIAL, LAB) for r in open_),
        "account_none": len(none),
        "account_none_amount": sum(r["amount"] for r in none),
        "outflow": sum(r["amount"] for r in rows),
    }


def _base(conn, today: date, scope: str, label: str) -> dict[str, int]:
    """한 기간의 숫자(설계 §2) — 증감 없이"""
    s, e = _range(scope, label)
    m: dict[str, int] = {}
    cats = [
        r[0]
        for r in conn.execute("SELECT DISTINCT category FROM source.emr_procedures ORDER BY 1")
    ]
    by_cat = dict(
        conn.execute(
            "SELECT p.category, SUM(t.fee_total) FROM source.emr_treatments t"
            " JOIN source.emr_procedures p ON p.code = t.procedure_code"
            " WHERE t.performed_at >= ? AND t.performed_at < ? GROUP BY p.category",
            (s, e),
        ).fetchall()
    )
    m["sales"] = sum(by_cat.values())
    for c in cats:
        m[f"sales.{c}"] = by_cat.get(c, 0)
    m["patient_paid"] = _sum(
        conn,
        "SELECT SUM(amount_paid) FROM source.emr_payments WHERE paid_at >= ? AND paid_at < ?",
        s,
        e,
    )
    m["insurer_paid"] = _sum(
        conn,
        "SELECT SUM(paid_amount) FROM source.emr_claims WHERE reviewed_at >= ? AND reviewed_at < ?",
        s,
        e,
    )
    m["collected"] = m["patient_paid"] + m["insurer_paid"]
    m["claimed"] = _sum(
        conn,
        "SELECT SUM(claim_amount) FROM source.emr_claims"
        " WHERE submitted_at >= ? AND submitted_at < ?",
        s,
        e,
    )
    visits, noshow, days = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(status = 'noshow'), 0),"
        " COUNT(DISTINCT substr(scheduled_at, 1, 10)) FROM source.emr_appointments"
        " WHERE status != 'cancelled' AND scheduled_at >= ? AND scheduled_at < ?",
        (s, e),
    ).fetchone()
    m["visits"], m["noshow"], m["treatment_days"] = visits, noshow, days
    m["noshow_rate"] = round(noshow * 100 / visits) if visits else 0
    m["new_patients"] = _sum(
        conn,
        "SELECT COUNT(*) FROM source.emr_patients WHERE registered_at >= ? AND registered_at < ?",
        s,
        e,
    )
    if scope != "monthly":
        return m
    m |= _expenses(conn, today, label)
    m["profit_ex_labor"] = m["collected"] - m["outflow"]
    notes = _dx_notes(conn)
    n, lost = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(claim_amount - COALESCE(paid_amount, 0)), 0)"
        f" FROM source.emr_claims WHERE adjust_note IN ({', '.join('?' * len(notes))})"
        " AND substr(reviewed_at, 1, 7) = ?",
        (*notes, label),
    ).fetchone()
    m["dx_errors"], m["dx_lost"] = n, lost
    lab = labor(conn)
    m["est.labor"] = sum(lab["wage"][r] * lab["headcount"][r] for r in LABOR_DEFAULT)
    m["est.profit"] = m["profit_ex_labor"] - m["est.labor"]
    variable = m["material"] + m["lab"]
    if m["sales"] and variable < m["sales"]:
        fixed = m["outflow"] - variable + m["est.labor"]
        m["est.bep"] = round(fixed / (1 - variable / m["sales"]))
    return m


def metrics(conn: sqlite3.Connection, today: date, scope: str, label: str) -> dict[str, int]:
    """기간 숫자 + 이전 기간(prev.)·증감(diff.)·증감률(pct., 정수 %) — 설계 §3. 이전 기간에 없는 키는 증감도 없다"""
    cur = _base(conn, today, scope, label)
    p = prev_period(conn, today, scope, label)
    prev = _base(conn, today, scope, p) if p else {}
    out = dict(cur)
    for k, v in cur.items():
        if k not in prev:
            continue
        out[f"prev.{k}"] = prev[k]
        out[f"diff.{k}"] = v - prev[k]
        if prev[k] and k not in NO_PCT:  # 음수 기준이어도 부호 = 방향
            out[f"pct.{k}"] = round((v - prev[k]) * 100 / abs(prev[k]))
    return out


def label_numbers(scope: str, *labels: str) -> set[int]:
    """기간 라벨이 문장에 들어갈 숫자 — 연·월·일, 주간은 그 주 일요일의 연·월·일과 월의 몇째 주도(설계 §4)"""
    out: set[int] = set()
    for label in labels:
        if scope == "monthly":
            y, m = label.split("-")
            out |= {int(y), int(m)}
            continue
        d = date.fromisoformat(label)
        out |= {d.year, d.month, d.day}
        if scope == "weekly":
            end = d + timedelta(days=6)
            out |= {end.year, end.month, end.day, (d.day - 1) // 7 + 1}
    return out


def _allowed(values) -> set[int]:
    """값 그대로·절댓값·만 원 단위 반올림(설계 §4)"""
    out: set[int] = set()
    for v in values:
        out |= {v, abs(v), round(abs(v) / 10000) * 10000}
    return out


def point_numbers_ok(metrics: dict, point: dict, extra: set[int]) -> bool:
    """본문 숫자가 모두 그 점이 기댄 값(또는 기간 라벨)에서 나온다"""
    return numbers(point["text"]) <= _allowed(metrics[k] for k in point["used"]) | extra


def point_est_ok(metrics: dict, point: dict) -> bool:
    """추정 키를 쓰면 [추정]과 "대표 원장 보수·세금 전", 안 쓰면 [추정] 없음"""
    if any(is_est(k) for k in point["used"]):
        return EST_MARK in point["text"] and EST_NOTE in point["text"]
    return EST_MARK not in point["text"]


def check_report(metrics: dict, out: dict, extra: set[int]) -> bool:
    """후검증(설계 §4, 평가 설계 §4 단위 정규화) — 점 0~3개, 쓴 키가 모두 metrics에 있고 숫자·추정 표시가 맞다"""
    try:
        points = out["points"]
        if len(points) > MAX_POINTS:
            return False
        for p in points:
            if not p["text"].strip() or any(k not in metrics for k in p["used"]):
                return False
            if not point_numbers_ok(metrics, p, extra) or not point_est_ok(metrics, p):
                return False
    except (KeyError, TypeError, AttributeError):  # 칸이 빠진 답
        return False
    return True
