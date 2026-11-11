"""④ 진료 준비표 (기능 정의 §4 ④, 설계 docs/superpowers/specs/2026-11-10-prep-sheet-design.md).
그날 예약마다 EMR에 흩어진 사실을 코드가 한 행으로 모으고(prep_facts) 칩을 켠다. 사실은 그날 개원 전 기준 —
지난 날짜도 그날 아침에 보였을 값으로 다시 계산한다. master 값은 source.ax_master_values로만 읽는다(CLAUDE.md)."""

import argparse
import hashlib
import json
import re
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from backend import db, prompts
from shared.llm import load_config

# 칩 순서 = 화면 순서(기능 정의 §4 ④)
CHIPS = (
    "appointment",
    "insurance",
    "balance",
    "family_recall",
    "alert",
    "open_plan",
    "unconfirmed",
)
DAYS_PER_MONTH = (
    365.25 / 12
)  # 리콜 주기(개월) → 일 — backend/query_tools.py _family_recall과 같은 기준
FULL_MOUTH = "00"  # 치아 단위가 아닌 시술의 치아 번호(가상 데이터 생성 설계 §2.3)
ALIAS, TASK, PROMPT_VERSION = "chat", "prep", "v1"  # config/llm.yaml "③ 챗·④ 제안"
DAYS = 8  # 날짜 탭 — 기준일 포함 최근 8일(기능 정의 §4 ④)
ROOT = Path(__file__).resolve().parents[1]
SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["suggest", "none"]},
        "text": {"type": "string"},
        "used_facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"key": {"type": "string"}, "value": {"type": "string"}},
                "required": ["key", "value"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["action", "text", "used_facts"],
    "additionalProperties": False,
}


def master_values(conn: sqlite3.Connection) -> dict:
    """ax_master_values — 주의사항 관련 시술·행동, 보험 잔여 칩 기준"""
    got = {
        n: json.loads(v)
        for n, v in conn.execute("SELECT name, value FROM source.ax_master_values")
    }
    got["alert_rules"] = {
        code: {"action": r["action"], "procedures": set(r["procedures"])}
        for code, r in got["alert_rules"].items()
    }
    return got


def _age(birth: str, day: date) -> int:
    b = date.fromisoformat(birth)
    return day.year - b.year - ((day.month, day.day) < (b.month, b.day))


def _one(conn, sql: str, *params):
    return conn.execute(sql, params).fetchone()[0]


def _alerts(conn, pid: int, day: str, planned: str, rules: dict) -> list[dict]:
    """그날 보이는 주의사항(기록일 ≤ 그날 < 끝나는 날 — 평가 설계 §4), 그날 예정 시술과 관련 있는지"""
    out = []
    for code, label in conn.execute(
        "SELECT a.code, t.label FROM source.emr_patient_alerts a"
        " JOIN source.emr_alert_types t ON t.code = a.code WHERE a.patient_id = ?"
        " AND a.recorded_at <= ? AND (a.ends_at IS NULL OR ? < a.ends_at) ORDER BY a.id",
        (pid, day, day),
    ):
        rule = rules[code]
        out.append(
            {
                "code": code,
                "label": label,
                "related": planned in rule["procedures"],
                "action": rule["action"],
            }
        )
    return out


def _scaling_unused(conn, p, day: date, planned: str, mv: dict) -> bool:
    """올해 스케일링 급여 미사용 성인 — 그날 예정 시술이 스케일링이면 끈다(설계 §2)"""
    if _age(p["birth_date"], day) < mv["scaling_min_age"] or planned == mv["scaling_code"]:
        return False
    return not _one(
        conn,
        "SELECT EXISTS (SELECT 1 FROM source.emr_claims WHERE patient_id = ? AND procedure_code = ?"
        " AND substr(service_date, 1, 4) = ? AND service_date < ?)",
        p["id"],
        mv["scaling_code"],
        str(day.year),
        day.isoformat(),
    )


def _family_recall(conn, p, day: date) -> list[dict]:
    """리콜이 지난 가족 — 그날 전 마지막 위생 내원 + 리콜 주기 < 그날, 그날 아침 이후 위생 예약이 잡혀 있던 사람은 뺀다"""
    if p["family_id"] is None:
        return []
    d = day.isoformat()
    out = []
    for m in conn.execute(
        "SELECT f.id, f.chart_no, f.name, f.recall_cycle_months AS months,"
        " (SELECT MAX(date(scheduled_at)) FROM source.emr_appointments WHERE patient_id = f.id"
        "  AND status = 'completed' AND type = 'hygiene' AND date(scheduled_at) < ?) AS last,"
        # 그날 아침 잡혀 있던 위생 예약 — 그날 전에 잡았고 그날 전에 취소되지 않음(뒤의 노쇼·취소도 그날엔 잡힌 것)
        " EXISTS (SELECT 1 FROM source.emr_appointments WHERE patient_id = f.id AND type = 'hygiene'"
        "  AND date(scheduled_at) >= ? AND created_at < ?"
        "  AND (cancelled_at IS NULL OR date(cancelled_at) >= ?)) AS booked"
        " FROM source.emr_patients f WHERE f.family_id = ? AND f.id != ? ORDER BY f.id",
        (d, d, d, d, p["family_id"], p["id"]),
    ):
        if m["last"] is None or m["booked"]:
            continue
        due = date.fromisoformat(m["last"]) + timedelta(days=round(m["months"] * DAYS_PER_MONTH))
        if due < day:
            out.append({"chart_no": m["chart_no"], "name": m["name"], "last_hygiene": m["last"]})
    return out


def _open_plan(conn, pid: int, day: date, today_item: int | None) -> list[dict]:
    """그날 아침 열린 치료계획 항목 — 지금 status가 아니라 그날 기준으로 다시 계산한다.
    끝남 = 그날 전 completed 연결 예약, 대체됨 = 계획을 만든 뒤·그날 전에 다른 계획(또는 계획 밖)의 completed
    진료가 같은 치아에(전체 구강 시술은 같은 시술코드) 들어감(가상 데이터 생성 설계 검사 "치료계획"과 같은 규칙).
    그날 만든 계획은 넣지 않고, 그날 예정 항목은 뺀다"""
    d = day.isoformat()
    out = []
    for it in conn.execute(
        "SELECT i.id, i.tooth_no, i.est_fee, i.procedure_code, r.name, pl.created_at, pl.id AS plan"
        " FROM source.emr_treatment_plan_items i JOIN source.emr_treatment_plans pl ON pl.id = i.plan_id"
        " JOIN source.emr_procedures r ON r.code = i.procedure_code"
        " WHERE pl.patient_id = ? AND date(pl.created_at) < ? ORDER BY pl.id, i.seq",
        (pid, d),
    ):
        if it["id"] == today_item:
            continue
        done = _one(
            conn,
            "SELECT EXISTS (SELECT 1 FROM source.emr_appointments WHERE plan_item_id = ?"
            " AND status = 'completed' AND date(scheduled_at) < ?)",
            it["id"],
            d,
        )
        same = "t.procedure_code = ?" if it["tooth_no"] == FULL_MOUTH else "t.tooth_no = ?"
        replaced = _one(
            conn,
            "SELECT EXISTS (SELECT 1 FROM source.emr_treatments t"
            " JOIN source.emr_appointments a ON a.id = t.appointment_id"
            " LEFT JOIN source.emr_treatment_plan_items o ON o.id = a.plan_item_id"
            f" WHERE t.patient_id = ? AND {same} AND t.performed_at > ? AND date(t.performed_at) < ?"
            " AND (o.plan_id IS NULL OR o.plan_id != ?))",
            pid,
            it["procedure_code"] if it["tooth_no"] == FULL_MOUTH else it["tooth_no"],
            it["created_at"],
            d,
            it["plan"],
        )
        if not done and not replaced:
            out.append(
                {"procedure": it["name"], "tooth": it["tooth_no"], "est_fee": it["est_fee"]}
            )
    return out


def chips(row: dict, day: date, today: date) -> list[str]:
    on = {
        "appointment": True,
        "insurance": row["scaling_unused"],
        "balance": row["balance"] > 0,
        "family_recall": bool(row["family_recall"]),
        "alert": any(a["related"] for a in row["alerts"]),
        "open_plan": bool(row["open_plan"]),
        "unconfirmed": day >= today and not row["confirmed"],  # 지난 날짜는 확인 여부가 의미 없다
    }
    return [c for c in CHIPS if on[c]]


def prep_facts(conn: sqlite3.Connection, day: date, today: date) -> list[dict]:
    """그날 예약(취소 제외) 한 건 = 한 행, 시각순. 사실은 그날 개원 전 기준. today는 내원 확인 전 칩 판단에만"""
    mv, d = master_values(conn), day.isoformat()
    out = []
    for a in conn.execute(
        "SELECT a.id, a.patient_id, a.scheduled_at, a.duration_min, a.chair_no, a.type,"
        " a.planned_procedure_code AS code, r.name AS procedure, s.name AS staff, a.status,"
        " a.confirmed, a.plan_item_id FROM source.emr_appointments a"
        " JOIN source.emr_procedures r ON r.code = a.planned_procedure_code"
        " JOIN source.emr_staff s ON s.id = a.staff_id"
        " WHERE date(a.scheduled_at) = ? AND a.status != 'cancelled' ORDER BY a.scheduled_at, a.id",
        (d,),
    ).fetchall():
        p = conn.execute(
            "SELECT id, chart_no, name, birth_date, sex, family_id FROM source.emr_patients"
            " WHERE id = ?",
            (a["patient_id"],),
        ).fetchone()
        last = _one(
            conn,
            "SELECT MAX(date(scheduled_at)) FROM source.emr_appointments WHERE patient_id = ?"
            " AND status = 'completed' AND date(scheduled_at) < ?",
            p["id"],
            d,
        )
        balance = _one(
            conn,
            "SELECT COALESCE(SUM(amount_due - amount_paid), 0) FROM source.emr_payments"
            " WHERE patient_id = ? AND date(paid_at) < ?",
            p["id"],
            d,
        )
        row = {
            "appointment_id": a["id"],
            "patient_id": p["id"],
            "time": a["scheduled_at"][11:16],
            "duration_min": a["duration_min"],
            "chair_no": a["chair_no"],
            "type": a["type"],
            "procedure": a["procedure"],
            "procedure_code": a["code"],
            "staff": a["staff"],
            "status": a["status"],
            "confirmed": bool(a["confirmed"]),
            "patient": {
                "name": p["name"],
                "chart_no": p["chart_no"],
                "age": _age(p["birth_date"], day),
                "sex": p["sex"],
                "new_patient": last is None,
            },
            "last_visit": last,
            "balance": max(balance, 0),  # 더 낸 금액은 미수가 아니다
            "alerts": _alerts(conn, p["id"], d, a["code"], mv["alert_rules"]),
            "scaling_unused": _scaling_unused(conn, p, day, a["code"], mv),
            "family_recall": _family_recall(conn, p, day),
            "open_plan": _open_plan(conn, p["id"], day, a["plan_item_id"]),
        }
        row["chips"] = chips(row, day, today)
        out.append(row)
    return out


# ── 제안 (설계 §3) ─────────────────────────────────────────────


def llm_facts(row: dict) -> dict[str, str | int]:
    """제안 입력 — 평평한 키. 내원 확인 전·화면용 id·환자 이름은 넣지 않는다(기능 정의 §4 ④). 관련 주의사항만 행동 문구를 붙인다"""
    f: dict[str, str | int] = {
        "procedure": row["procedure"],
        "type": row["type"],
        "last_visit": row["last_visit"] or "첫 내원",
    }
    if row["balance"] > 0:
        f["balance"] = row["balance"]
    if row["scaling_unused"]:
        f["scaling_unused"] = "올해 스케일링 급여 미사용"
    for a in row["alerts"]:
        f[f"alert.{a['code']}"] = f"{a['label']} — {a['action']}" if a["related"] else a["label"]
    for i, p in enumerate(row["open_plan"], 1):
        f[f"plan.{i}"] = f"{p['procedure']} {p['tooth']} {p['est_fee']}원"
    for m in row["family_recall"]:
        f[f"family.{m['chart_no']}"] = f"{m['name']} 마지막 스케일링 {m['last_hygiene']}"
    return f


UNIT = re.compile(r"(\d+(?:\.\d+)?)\s*만(?:\s*(\d+)\s*천)?|(\d+)\s*천")


def numbers(text: str) -> set[int]:
    """글 속 숫자 — 쉼표를 떼고 "3만"·"3만5천"·"5천"을 원 단위로 편다(평가 설계 §4 단위 정규화)"""
    out: set[int] = set()

    def unit(m: re.Match) -> str:
        if m[1]:
            out.add(round(float(m[1]) * 10000 + (int(m[2]) * 1000 if m[2] else 0)))
        else:
            out.add(int(m[3]) * 1000)
        return " "

    rest = UNIT.sub(unit, text.replace(",", ""))
    return out | {int(n) for n in re.findall(r"\d+", rest)}


def check_suggestion(facts: dict, s: dict) -> bool:
    """후검증(평가 설계 §4) — 근거가 모두 입력 사실과 같고, 본문 숫자가 모두 근거 값에 있다. none은 근거가 비어야 통과"""
    if s["action"] == "none":
        return not s["used_facts"] and not s["text"].strip()
    if not s["used_facts"] or not s["text"].strip():
        return False
    if any(u["key"] not in facts or str(facts[u["key"]]) != u["value"] for u in s["used_facts"]):
        return False
    allowed = set().union(*(numbers(u["value"]) for u in s["used_facts"]))
    return numbers(s["text"]) <= allowed


def _request(facts: dict) -> dict:
    return {
        "system": prompts.load(TASK, PROMPT_VERSION)["system"],
        "messages": [
            {
                "role": "user",
                "content": "사실 목록:\n" + json.dumps(facts, ensure_ascii=False, indent=1),
            }
        ],
        "schema": SCHEMA,
    }


def _hash(facts: dict) -> str:
    return hashlib.sha256(
        json.dumps(facts, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def _valid(result, facts: dict):
    """통과한 제안 dict, 아니면 None"""
    if result is None or not result.ok or not isinstance(result.data, dict):
        return None
    try:
        return result.data if check_suggestion(facts, result.data) else None
    except (
        KeyError,
        TypeError,
        AttributeError,
    ):  # 칸이 빠진 답 — 그 행만 실패(스키마를 강제하지 않는 공급자)
        return None


def _save(
    conn,
    day: date,
    row: dict,
    facts_hash: str,
    suggestion,
    model: str,
    created_at: str | None = None,
) -> None:
    """created_at이 있으면 그대로(건너뛴 행 — 제안을 만든 시각을 지킨다)"""
    with conn:  # 행마다 커밋 — 다시 부르는 동안 쓰기 잠금을 쥐지 않는다
        conn.execute(
            "INSERT INTO ax_prep_sheets (sheet_date, patient_id, appointment_id, facts, facts_hash,"
            " suggestion, suggestion_valid, prompt_version, model, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (sheet_date, patient_id, appointment_id) DO UPDATE SET facts = excluded.facts,"
            " facts_hash = excluded.facts_hash, suggestion = excluded.suggestion,"
            " suggestion_valid = excluded.suggestion_valid, prompt_version = excluded.prompt_version,"
            " model = excluded.model, created_at = excluded.created_at",
            (
                day.isoformat(),
                row["patient_id"],
                row["appointment_id"],
                json.dumps(row, ensure_ascii=False),
                facts_hash,
                None if suggestion is None else json.dumps(suggestion, ensure_ascii=False),
                int(suggestion is not None),
                PROMPT_VERSION,
                model,
                created_at or datetime.now().isoformat(timespec="seconds"),
            ),
        )


def _batchable(gw) -> bool:
    first = gw.config["aliases"][ALIAS][0]
    return gw.config["models"][first].get("provider", "anthropic") == "anthropic"


def build(
    conn, gw, days: list[date], today: date, *, use_batch: bool, poll_seconds: int = 30
) -> dict:
    """그날들 준비표를 만든다. 해시·버전·모델이 같고 통과한 행은 사실만 새로 쓰고 부르지 않는다.
    첫 답은 use_batch면 Batch 한 번, 아니면 일반 호출. 후검증에 실패하거나 Batch에서 빠진 행만 일반 호출로 한 번 더,
    또 실패면 제안 NULL. 일반 호출은 차례로 — 게이트웨이 기록이 앱 연결 하나에 쓴다"""
    model = gw.config["aliases"][ALIAS][0]
    out = {"rows": 0, "called": 0, "skipped": 0, "failed": 0}
    todo = []
    for day in days:
        rows = prep_facts(conn, day, today)
        with conn:  # 취소·이동으로 없어진 예약의 행
            conn.execute(
                "DELETE FROM ax_prep_sheets WHERE sheet_date = ? AND appointment_id NOT IN"
                f" ({', '.join('?' * len(rows))})",
                (day.isoformat(), *[r["appointment_id"] for r in rows]),
            )
        for r in rows:
            out["rows"] += 1
            facts = llm_facts(r)
            h = _hash(facts)
            old = conn.execute(
                "SELECT facts_hash, prompt_version, model, suggestion, suggestion_valid, created_at"
                " FROM ax_prep_sheets WHERE sheet_date = ? AND patient_id = ? AND appointment_id = ?",
                (day.isoformat(), r["patient_id"], r["appointment_id"]),
            ).fetchone()
            if _reusable(old, h, model):
                _save(conn, day, r, h, json.loads(old["suggestion"]), model, old["created_at"])
                out["skipped"] += 1
                continue
            todo.append((f"{day.isoformat()}_{r['appointment_id']}", day, r, facts, h))
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

    for key, day, r, facts, h in todo:
        out["called"] += 1
        used = first.get(key) or plain(facts)
        got = _valid(used, facts)
        if got is None:
            used = plain(facts)
            got = _valid(used, facts)
        out["failed"] += got is None
        _save(conn, day, r, h, got, used.model if used else model)  # 실제로 답한 모델(폴백 포함)
    return out


def tab_days(today: date) -> list[date]:
    return [today - timedelta(days=k) for k in range(DAYS - 1, -1, -1)]


def _reusable(old, h: str, model: str) -> bool:
    """저장된 제안을 그대로 쓰는 행 — 해시·버전·모델이 같고 통과(build는 부르지 않고, 화면은 갱신 대상이 아님)"""
    return bool(
        old
        and old["suggestion_valid"]
        and (old["facts_hash"], old["prompt_version"], old["model"]) == (h, PROMPT_VERSION, model)
    )


def _changed(before: dict, after: dict) -> list[dict]:
    """제안 입력 키별 전후 값 — 지금 키 순서, 그다음 사라진 키. 남은 치료계획(plan.n)은 순번 키라 값으로 비교해
    빠진 값·생긴 값을 key "plan"으로"""
    plan = [str(v) for k, v in before.items() if k.startswith("plan.")]
    now = [str(v) for k, v in after.items() if k.startswith("plan.")]
    before = {k: v for k, v in before.items() if not k.startswith("plan.")}
    after = {k: v for k, v in after.items() if not k.startswith("plan.")}
    keys = list(after) + [k for k in before if k not in after]
    out = [
        {
            "key": k,
            "before": None if k not in before else str(before[k]),
            "after": None if k not in after else str(after[k]),
        }
        for k in keys
        if before.get(k) != after.get(k)
    ]
    out += [{"key": "plan", "before": v, "after": None} for v in plan if v not in now]
    out += [{"key": "plan", "before": None, "after": v} for v in now if v not in plan]
    return out


def _old_changes(saved_facts: str, now: dict) -> list[dict]:
    """저장된 facts가 지금 llm_facts가 읽는 키를 갖지 않으면(입력 필드가 늘어난 뒤의 옛 행) 비운다 — 화면은 뜬다"""
    try:
        return _changed(llm_facts(json.loads(saved_facts)), now)
    except KeyError:
        return []


def sheet(conn, day: date, today: date, model: str | None = None) -> dict:
    """화면 한 장 — 지금 사실(prep_facts) + 저장된 제안. refresh = 갱신하면 다시 부르는 행(build와 같은 조건),
    stale = 저장 뒤 제안 입력이 바뀐 행이고 changed에 바뀐 키와 전후 값(없으면 None)을 단다"""
    model = model or load_config()["aliases"][ALIAS][0]
    saved = {
        r["appointment_id"]: r
        for r in conn.execute(
            "SELECT appointment_id, facts, facts_hash, prompt_version, model, suggestion,"
            " suggestion_valid, created_at FROM ax_prep_sheets WHERE sheet_date = ?",
            (day.isoformat(),),
        )
    }
    rows = []
    for r in prep_facts(conn, day, today):
        s = saved.get(r["appointment_id"])
        now = llm_facts(r)
        h = _hash(now)
        stale = bool(s and s["facts_hash"] != h)
        rows.append(
            r
            | {
                "generated": s is not None,
                "suggestion": json.loads(s["suggestion"]) if s and s["suggestion"] else None,
                "suggestion_valid": bool(s and s["suggestion_valid"]),
                "refresh": not _reusable(s, h, model),
                "stale": stale,
                "changed": _old_changes(s["facts"], now) if stale else [],
                "made_at": s["created_at"] if s else None,
            }
        )
    tabs = tab_days(today)
    counts = dict(
        conn.execute(
            "SELECT date(scheduled_at), COUNT(*) FROM source.emr_appointments"
            " WHERE status != 'cancelled' AND date(scheduled_at) BETWEEN ? AND ?"
            " GROUP BY date(scheduled_at)",
            (tabs[0].isoformat(), tabs[-1].isoformat()),
        ).fetchall()
    )
    return {
        "date": day.isoformat(),
        "generated_at": max((s["created_at"] for s in saved.values()), default=None),
        "days": [{"date": d.isoformat(), "count": counts.get(d.isoformat(), 0)} for d in tabs],
        "rows": rows,
    }


def main(argv=None) -> None:
    from backend.harness.llm import make_gateway
    from shared.clinic import load_clinic

    p = argparse.ArgumentParser(prog="python -m backend.prep")
    p.add_argument("command", choices=("run",))
    p.add_argument("--date", type=date.fromisoformat, help="하루만(기본은 기준일 포함 최근 8일)")
    p.add_argument("--no-batch", action="store_true", help="Batch 없이 일반 호출만")
    args = p.parse_args(argv)
    conn = db.connect(ROOT / "data" / "app.sqlite", ROOT / "data" / "source.sqlite")
    db.init_schema(conn)
    today = load_clinic().today
    days = [args.date] if args.date else tab_days(today)
    print(build(conn, make_gateway(conn), days, today, use_batch=not args.no_batch))


if __name__ == "__main__":
    main()
