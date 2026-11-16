"""⑥ 경영 리포트 (기능 정의 §5 ⑥, 설계 docs/superpowers/specs/2026-11-13-report-design.md).
끝난 기간(월·주·진료일)마다 운영 숫자와 이전 기간 대비 변화 분해를 코드가 `metrics`로 모은다(설계 §2·§3).
재료비 과목은 ⑧-2 지출 증빙 줄(backend/expense.py sheet)의 과목을 그대로 쓴다. master 값은 source.ax_master_values로만."""

import argparse
import hashlib
import json
import re
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from backend import db, expense, prompts
from backend.prep import numbers

ALIAS, TASK, PROMPT_VERSION = "chat", "report", "v2"  # config/llm.yaml "③ 챗·④ 제안" 별칭
ROOT = Path(__file__).resolve().parents[1]
SCOPES = ("monthly", "weekly", "daily")
COUNTS = {"monthly": 6, "weekly": 8, "daily": 10}  # 화면 기간 수(설계 §1)
MATERIAL, LAB = "재료비", "외주가공비"  # ⑧-2 계정 과목 이름(master ACCOUNTS)
NO_PCT = ("noshow_rate",)  # 이미 %인 값 — 증감률 없이 증감(%p)만
MAX_POINTS = 3
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
    """비교할 이전 기간 — 월은 앞 달, 주는 7일 전 월요일, 일은 지난주 같은 요일(그날 진료가 없으면 None).
    일간을 바로 앞 진료일과 견주면 요일(토요일 반나절 등)이 달라 잡음이다"""
    if scope == "monthly":
        return (date.fromisoformat(label + "-01") - timedelta(days=1)).isoformat()[:7]
    week_ago = (date.fromisoformat(label) - timedelta(days=7)).isoformat()
    if scope == "weekly":
        return week_ago
    return week_ago if week_ago in _days(conn, today) else None


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
    """값 그대로·절댓값·만 원 단위 반올림(설계 §4) — .5는 올림, 1만 이상만(작은 값이 0을 허용하지 않게)"""
    out: set[int] = set()
    for v in values:
        out |= {v, abs(v)}
        if abs(v) >= 10000:
            out.add((abs(v) + 5000) // 10000 * 10000)
    return out


LABEL_UNIT = re.compile(r"(\d+)\s*(?=[년월일주])")


def point_numbers_ok(metrics: dict, point: dict, extra: set[int]) -> bool:
    """본문 숫자가 모두 그 점이 기댄 값에서 나온다. 기간 라벨 숫자는 뒤에 년·월·일·주가 붙을 때만 뺀다"""
    text = LABEL_UNIT.sub(lambda m: " " if int(m[1]) in extra else m[0], point["text"])
    return numbers(text) <= _allowed(metrics[k] for k in point["used"])


def check_report(metrics: dict, out: dict, extra: set[int]) -> bool:
    """후검증(설계 §4, 평가 설계 §4 단위 정규화) — 점 0~3개, 쓴 키가 모두 metrics에 있고 숫자가 맞다"""
    try:
        points = out["points"]
        if len(points) > MAX_POINTS:
            return False
        for p in points:
            # 기댄 키가 없는 점 = metrics에 없는 원인(설계 "하면 실패인 것")
            if not p["text"].strip() or not p["used"] or any(k not in metrics for k in p["used"]):
                return False
            if not point_numbers_ok(metrics, p, extra):
                return False
    except (KeyError, TypeError, AttributeError):  # 칸이 빠진 답
        return False
    return True


def metrics_hash(m: dict) -> str:
    return hashlib.sha256(json.dumps(m, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _input(conn, today: date, scope: str, label: str, m: dict) -> dict:
    return {
        "scope": scope,
        "period": label,
        "prev_period": prev_period(conn, today, scope, label),
        "metrics": m,
    }


def _request(facts: dict) -> dict:
    return {
        "system": prompts.load(TASK, PROMPT_VERSION)["system"],
        "messages": [
            {
                "role": "user",
                "content": "기간과 숫자:\n" + json.dumps(facts, ensure_ascii=False, indent=1),
            }
        ],
        "schema": SCHEMA,
    }


def _valid(result, facts: dict, extra: set[int]):
    """통과한 답 dict, 아니면 None"""
    if result is None or not result.ok or not isinstance(result.data, dict):
        return None
    return result.data if check_report(facts["metrics"], result.data, extra) else None


def _extra(conn, today: date, scope: str, label: str) -> set[int]:
    prev = prev_period(conn, today, scope, label)
    return label_numbers(scope, label, *([prev] if prev else []))


def _save(conn, scope: str, label: str, m: dict, out, model: str) -> None:
    with conn:  # 행마다 커밋 — 다시 부르는 동안 쓰기 잠금을 쥐지 않는다
        conn.execute(
            "INSERT INTO ax_reports (scope, period, metrics, metrics_hash, points, valid,"
            " prompt_version, model, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (scope, period) DO UPDATE SET metrics = excluded.metrics,"
            " metrics_hash = excluded.metrics_hash, points = excluded.points,"
            " valid = excluded.valid, prompt_version = excluded.prompt_version,"
            " model = excluded.model, created_at = excluded.created_at",
            (
                scope,
                label,
                json.dumps(m, ensure_ascii=False),
                metrics_hash(m),
                None if out is None else json.dumps(out["points"], ensure_ascii=False),
                int(out is not None),
                PROMPT_VERSION,
                model,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )


def _batchable(gw) -> bool:
    first = gw.config["aliases"][ALIAS][0]
    return gw.config["models"][first].get("provider", "anthropic") == "anthropic"


def _fresh(conn, scope: str, label: str, m: dict, model: str) -> bool:
    """저장된 해석을 그대로 쓰는 기간 — 숫자 해시·버전·모델이 같고 통과"""
    old = conn.execute(
        "SELECT metrics_hash, prompt_version, model, valid FROM ax_reports"
        " WHERE scope = ? AND period = ?",
        (scope, label),
    ).fetchone()
    return bool(
        old and old["valid"] and tuple(old)[:3] == (metrics_hash(m), PROMPT_VERSION, model)
    )


def todo_count(conn, today: date, model: str) -> int:
    """build가 부를 기간 수 — 같은 판정(_fresh), 쓰지 않는다(야간 실행 PLAN)"""
    return sum(
        not _fresh(conn, scope, label, metrics(conn, today, scope, label), model)
        for scope in SCOPES
        for label in periods(conn, today, scope)
    )


def build(conn, gw, today: date, *, use_batch: bool, poll_seconds: int = 30) -> dict:
    """화면 기간 전부의 해석을 만든다(설계 §5). 숫자 해시·버전·모델이 같고 통과한 기간은 부르지 않는다 — 숫자가 바뀐 기간은
    다시 부른다. 첫 답은 use_batch면 Batch 한 번, 아니면 일반 호출. Batch에서 빠진 기간은 일반 호출이 첫 답이 되고,
    첫 답이 후검증에 실패하면 일반 호출로 한 번 더, 또 실패면 해석 없음(points NULL·valid 0). 일반 호출은 차례로 — 게이트웨이 기록이 앱 연결 하나에 쓴다"""
    model = gw.config["aliases"][ALIAS][0]
    out = {"rows": 0, "called": 0, "skipped": 0, "failed": 0}
    todo = []
    for scope in SCOPES:
        for label in periods(conn, today, scope):
            out["rows"] += 1
            m = metrics(conn, today, scope, label)
            if _fresh(conn, scope, label, m, model):
                out["skipped"] += 1
                continue
            facts = _input(conn, today, scope, label, m)
            todo.append(
                (f"{scope}_{label}", scope, label, facts, _extra(conn, today, scope, label))
            )
    first = {}
    if todo and use_batch and _batchable(gw):
        bid = gw.submit_batch(
            ALIAS,
            [{"custom_id": key, **_request(facts)} for key, _, _, facts, _ in todo],
            task=TASK,
            prompt_version=PROMPT_VERSION,
        )
        while gw.batch_status(bid) != "ended":
            time.sleep(poll_seconds)
        first = gw.batch_results(
            bid, ALIAS, task=TASK, prompt_version=PROMPT_VERSION, schema=SCHEMA
        )

    def plain(facts):
        return gw.complete(ALIAS, task=TASK, prompt_version=PROMPT_VERSION, **_request(facts))

    for key, scope, label, facts, extra in todo:
        out["called"] += 1
        used = first.get(key) or plain(facts)
        got = _valid(used, facts, extra)
        if got is None:
            used = plain(facts)
            got = _valid(used, facts, extra)
        out["failed"] += got is None
        _save(conn, scope, label, facts["metrics"], got, used.model if used else model)
    return out


TREND = ("sales", "collected", "noshow_rate", "new_patients")
TREND_MONTHLY = ("material", "profit_ex_labor")


def sheet(conn: sqlite3.Connection, today: date, scope: str, period: str | None = None) -> dict:
    """화면 응답(설계 §5·§6) — LLM을 부르지 않는다. 해석은 저장된 것만, 지금 숫자와 해시가 다르면 stale.
    모르는 기간 종류·기간은 ValueError"""
    labels = periods(conn, today, scope)
    if period is None:
        if not labels:
            raise ValueError("끝난 기간이 없음")
        period = labels[-1]
    elif period not in labels:
        raise ValueError(f"모르는 기간: {period}")
    by = {p: metrics(conn, today, scope, p) for p in labels}
    m = by[period]
    row = conn.execute(
        "SELECT points, valid, metrics, metrics_hash, model, created_at FROM ax_reports"
        " WHERE scope = ? AND period = ?",
        (scope, period),
    ).fetchone()
    interp = None
    if row and row["valid"]:
        points, made = json.loads(row["points"]), json.loads(row["metrics"])
        interp = {
            "points": points,
            # 꼬리표 값 — 해석을 만든 때의 숫자(stale이면 지금 숫자와 다르다)
            "values": {k: made.get(k) for p in points for k in p["used"]},
            "model": row["model"],
            "created_at": row["created_at"],
            "stale": row["metrics_hash"] != metrics_hash(m),
        }
    keys = TREND + (TREND_MONTHLY if scope == "monthly" else ())
    monthly = scope == "monthly"
    return {
        "scope": scope,
        "periods": labels,
        "period": period,
        "prev_period": prev_period(conn, today, scope, period),
        "metrics": m,
        "interpretation": interp,
        "trend": [{"period": p} | {k: by[p].get(k) for k in keys} for p in labels],
        "dx_top": dx_top(conn, period) if monthly else None,
    }


def main(argv=None) -> None:
    from backend.harness.llm import make_gateway
    from shared.clinic import load_clinic

    p = argparse.ArgumentParser(prog="python -m backend.report")
    p.add_argument("command", choices=("run",))
    p.add_argument("--no-batch", action="store_true", help="Batch 없이 일반 호출만")
    args = p.parse_args(argv)
    conn = db.connect(ROOT / "data" / "app.sqlite", ROOT / "data" / "source.sqlite")
    db.init_schema(conn)
    print(build(conn, make_gateway(conn), load_clinic().today, use_batch=not args.no_batch))


if __name__ == "__main__":
    main()
