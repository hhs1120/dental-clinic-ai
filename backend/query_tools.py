"""조회 도구 (앱 설계 §3 — 시그니처 고정, 평가 설계 §3.2 채점 기준). source.sqlite를 읽기만 한다.
숫자는 코드가 낸다(기능 정의 §3 원칙 2). 요약 칸을 목록보다 앞에 두어 결과가 잘려도 건수가 남는다."""

import json
from collections import Counter
from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from backend.harness.tools import Tool
from backend.prep import master_values
from shared.clinic import Clinic

SETTLED = ("paid", "adjusted", "denied")  # 심사 결과가 있는 청구


class PatientArgs(BaseModel):
    chart_no: str | None = None
    name: str | None = None

    @model_validator(mode="after")
    def one_required(self):
        if not (self.chart_no or self.name):
            raise ValueError("chart_no 또는 name 필수")
        return self


class ScheduleArgs(BaseModel):
    date_from: date
    date_to: date
    status: Literal["scheduled", "completed", "cancelled", "noshow"] | None = None
    type: Literal["treatment", "hygiene", "consult"] | None = None
    has_unpaid_balance: bool | None = None
    is_new_patient: bool | None = None


class ClaimsArgs(BaseModel):
    date_from: date | None = None
    date_to: date | None = None
    chart_no: str | None = None
    procedure_code: str | None = None
    status: Literal["pending", "submitted", "paid", "adjusted", "denied"] | None = None


class ChartArgs(BaseModel):
    procedure: str = Field(min_length=1)
    prev_procedure: str | None = None
    min_gap_days: int | None = Field(default=None, ge=0)
    max_gap_days: int | None = Field(default=None, ge=0)
    same_tooth: bool = False
    date_from: date | None = None
    date_to: date | None = None
    chart_no: str | None = None
    limit: int = Field(default=10, ge=1, le=20)


class RefuseArgs(BaseModel):
    reason: Literal["not_found", "restricted"]


def _rows(conn, sql: str, *args) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def _balance(conn, patient_id: int, before: str) -> int:
    """그날 개원 전 잔액 (앱 설계 §3 has_unpaid_balance)"""
    return conn.execute(
        "SELECT COALESCE(SUM(amount_due - amount_paid), 0) FROM source.emr_payments"
        " WHERE patient_id = ? AND date(paid_at) < ?",
        (patient_id, before),
    ).fetchone()[0]


# include_ax일 때 예약 원본 행 자리에 쓰는 합친 표 — 원본 + 승인된 AX 예약(ax_emr_writes, AI 비서 설계 §5.3)
APPOINTMENTS_WITH_AX = (
    "(SELECT id, patient_id, staff_id, scheduled_at, type, status, confirmed, planned_procedure_code,"
    " rescheduled_from_id, 'emr' AS source FROM source.emr_appointments"
    " UNION ALL SELECT 'ax-' || id, patient_id, staff_id, scheduled_at, type, 'scheduled', 0,"
    " procedure_code, NULL, 'ax' FROM ax_emr_writes WHERE status = 'approved')"
)


def _schedule(clinic: Clinic, conn, args: dict, include_ax: bool = False) -> dict:
    where = ["date(a.scheduled_at) BETWEEN ? AND ?"]
    params = [args["date_from"], args["date_to"]]
    for name in ("status", "type"):
        if args[name]:
            where.append(f"a.{name} = ?")
            params.append(args[name])
    if args["is_new_patient"] is not None:
        where.append(f"p.registered_at {'>=' if args['is_new_patient'] else '<'} ?")
        params.append(clinic.range_start.isoformat())
    if args["has_unpaid_balance"] is not None:
        where.append(
            "(SELECT COALESCE(SUM(y.amount_due - y.amount_paid), 0) FROM source.emr_payments y"
            f" WHERE y.patient_id = a.patient_id AND date(y.paid_at) < date(a.scheduled_at))"
            f" {'>' if args['has_unpaid_balance'] else '<='} 0"
        )
    rows = _rows(
        conn,
        "SELECT a.id, a.scheduled_at, a.type, a.status, a.confirmed, p.chart_no,"
        + (" a.source," if include_ax else "")
        + " p.name AS patient, r.name AS procedure,"
        " EXISTS (SELECT 1 FROM source.emr_appointments n WHERE n.rescheduled_from_id = a.id)"
        " AS rescheduled"
        f" FROM {APPOINTMENTS_WITH_AX if include_ax else 'source.emr_appointments'} a"
        " JOIN source.emr_patients p ON p.id = a.patient_id"
        " JOIN source.emr_procedures r ON r.code = a.planned_procedure_code"
        f" WHERE {' AND '.join(where)} ORDER BY a.scheduled_at, a.id",
        *params,
    )
    by_status = Counter("rescheduled" if r["rescheduled"] else r["status"] for r in rows)
    for r in rows:
        del r["rescheduled"]
    return {
        "count": len(rows),
        "by_status": dict(by_status),
        "by_type": dict(Counter(r["type"] for r in rows)),
        "appointments": rows,
    }


def _family_recall(conn, patient: dict, today: date, days_per_month: float) -> list[dict]:
    if patient["family_id"] is None:
        return []
    out = []
    for m in _rows(
        conn,
        "SELECT p.chart_no, p.name, p.recall_cycle_months AS months, MAX(date(a.scheduled_at))"
        " AS last_hygiene FROM source.emr_patients p LEFT JOIN source.emr_appointments a"
        " ON a.patient_id = p.id AND a.status = 'completed' AND a.type = 'hygiene'"
        " WHERE p.family_id = ? AND p.id != ? GROUP BY p.id ORDER BY p.id",
        patient["family_id"],
        patient["id"],
    ):
        last = m["last_hygiene"]
        due = last and date.fromisoformat(last) + timedelta(
            days=round(m["months"] * days_per_month)
        )
        out.append(
            {
                "chart_no": m["chart_no"],
                "name": m["name"],
                "last_hygiene": last,
                "overdue": bool(due and due < today),
            }
        )
    return out


def _summary(clinic: Clinic, conn, p: dict, include_ax: bool = False) -> dict:
    today = clinic.today.isoformat()
    last = conn.execute(
        "SELECT MAX(date(scheduled_at)) FROM source.emr_appointments"
        " WHERE patient_id = ? AND status = 'completed'",
        (p["id"],),
    ).fetchone()[0]
    upcoming = _rows(
        conn,
        "SELECT a.scheduled_at, a.type, r.name AS procedure FROM"
        f" {APPOINTMENTS_WITH_AX if include_ax else 'source.emr_appointments'} a"
        " JOIN source.emr_procedures r ON r.code = a.planned_procedure_code"
        " WHERE a.patient_id = ? AND a.status = 'scheduled' AND date(a.scheduled_at) >= ?"
        " ORDER BY a.scheduled_at LIMIT 1",
        p["id"],
        today,
    )
    mv = master_values(conn)
    scaling = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM source.emr_claims c WHERE c.patient_id = ?"
        " AND c.procedure_code = ? AND substr(c.service_date, 1, 4) = ?)",
        (p["id"], mv["scaling_code"], today[:4]),
    ).fetchone()[0]
    return {
        "chart_no": p["chart_no"],
        "name": p["name"],
        "birth_date": p["birth_date"],
        "sex": p["sex"],
        "phone": p["phone"],
        "alerts": _rows(
            conn,
            "SELECT t.label, t.category FROM source.emr_patient_alerts a"
            " JOIN source.emr_alert_types t ON t.code = a.code WHERE a.patient_id = ?"
            " AND a.recorded_at <= ? AND (a.ends_at IS NULL OR ? < a.ends_at) ORDER BY a.id",
            p["id"],
            today,
            today,
        ),
        "last_visit": last,
        "balance": _balance(conn, p["id"], today),
        "next_appointment": upcoming[0] if upcoming else None,
        "family_recall": _family_recall(conn, p, clinic.today, mv["days_per_month"]),
        "scaling_claimed_this_year": bool(scaling),
        "open_plan_items": _rows(
            conn,
            "SELECT r.name AS procedure, i.tooth_no, i.est_fee FROM"
            " source.emr_treatment_plan_items i"
            " JOIN source.emr_treatment_plans t ON t.id = i.plan_id"
            " JOIN source.emr_procedures r ON r.code = i.procedure_code"
            " WHERE t.patient_id = ? AND i.status = 'open' ORDER BY i.plan_id, i.seq",
            p["id"],
        ),
    }


def _patient(clinic: Clinic, conn, args: dict, include_ax: bool = False) -> dict:
    key, value = ("chart_no", args["chart_no"]) if args["chart_no"] else ("name", args["name"])
    found = _rows(
        conn, f"SELECT * FROM source.emr_patients WHERE {key} = ? ORDER BY chart_no", value
    )
    out = {
        "matches": [
            {"chart_no": p["chart_no"], "name": p["name"], "birth_date": p["birth_date"]}
            for p in found
        ]
    }
    if len(found) == 1:
        out["summary"] = _summary(clinic, conn, found[0], include_ax)
    return out


def _claims(conn, args: dict) -> dict:
    where, params = ["1 = 1"], []
    for name, sql in (
        ("date_from", "service_date >= ?"),
        ("date_to", "service_date <= ?"),
        ("chart_no", "chart_no = ?"),
        ("procedure_code", "procedure_code = ?"),
        ("status", "status = ?"),
    ):
        if args[name]:
            where.append(sql)
            params.append(args[name])
    rows = _rows(
        conn,
        "SELECT chart_no, service_date, procedure_code, tooth_no, claim_amount, status,"
        " paid_amount, adjust_code, adjust_note, reviewed_at FROM source.emr_claims"
        f" WHERE {' AND '.join(where)} ORDER BY service_date, id",
        *params,
    )
    for r in rows:
        if r["status"] not in SETTLED:  # 확정 월만 결과·사유
            for name in ("paid_amount", "adjust_code", "adjust_note", "reviewed_at"):
                del r[name]
    return {
        "count": len(rows),
        "total_amount": sum(r["claim_amount"] for r in rows),
        "by_status": dict(Counter(r["status"] for r in rows)),
        "claims": rows,
    }


def _procedure_codes(conn, name: str) -> tuple[list[str], list[str], list[str]]:
    """시술 이름·별칭 → (코드, 맞춘 시술 이름, 모호한 별칭의 후보). 띄어쓰기는 무시한다. 순서: 이름 일치 → 별칭 일치(둘 이상이면
    코드 없이 후보만 — 별칭 오탐 방지, AI 실험 기록 §4·§5) → 이름에 그 말이 든 시술 전부('발치' → 단순발치·사랑니 발치).
    이름은 emr_procedures에서 읽어 상수를 박지 않는다(앱 설계 §8)"""
    key = name.replace(" ", "")
    rows = _rows(conn, "SELECT code, name, aliases FROM source.emr_procedures ORDER BY code")
    flat = {r["code"]: r["name"].replace(" ", "") for r in rows}
    exact = [r for r in rows if flat[r["code"]] == key]
    alias = [
        r for r in rows if key in {a.replace(" ", "") for a in json.loads(r["aliases"] or "[]")}
    ]
    if not exact and len(alias) > 1:
        return [], [], [r["name"] for r in alias]
    hits = exact or alias or [r for r in rows if key in flat[r["code"]]]
    return [r["code"] for r in hits], [r["name"] for r in hits], []


def _charts(clinic: Clinic, conn, args: dict) -> dict:
    """범위 조건 차트 조회 (설계 2026-11-09 챗 "query_charts") — 같은 환자 직전 앞 시술과의 날짜 차(일)"""
    codes, matched, candidates = _procedure_codes(conn, args["procedure"])
    prev_codes, prev_cand = [], []
    if args["prev_procedure"]:
        prev_codes, _, prev_cand = _procedure_codes(conn, args["prev_procedure"])
    if candidates or prev_cand:
        return {"count": 0, "results": [], "candidates": candidates or prev_cand}
    unknown = [
        n
        for n, c in ((args["procedure"], codes), (args["prev_procedure"], prev_codes))
        if n and not c
    ]
    if unknown:  # 없는 이름 — 모델이 이름을 고쳐 다시 부르게 알린다
        return {"count": 0, "results": [], "unknown": unknown}
    sql = [
        "SELECT t.id, t.patient_id, t.tooth_no, t.performed_at, p.chart_no, pr.name AS procedure,"
        " n.dx_code, n.note FROM source.emr_treatments t"
        " JOIN source.emr_patients p ON p.id = t.patient_id"
        " JOIN source.emr_procedures pr ON pr.code = t.procedure_code"
        " JOIN source.emr_chart_notes n ON n.treatment_id = t.id"
        f" WHERE t.procedure_code IN ({','.join('?' * len(codes))}) AND date(t.performed_at) <= ?"
    ]
    params = [*codes, clinic.today.isoformat()]
    for clause, value in (
        ("date(t.performed_at) >= ?", args["date_from"]),
        ("date(t.performed_at) <= ?", args["date_to"]),
        ("p.chart_no = ?", args["chart_no"]),
    ):
        if value:
            sql.append(f"AND {clause}")
            params.append(value)
    rows = _rows(conn, " ".join(sql) + " ORDER BY t.performed_at DESC, t.id DESC", *params)
    out = []
    for r in rows:
        item = {
            "id": f"chart:{r['id']}",
            "chart_no": r["chart_no"],
            "performed_at": r["performed_at"],
            "procedure": r["procedure"],
            "tooth_no": r["tooth_no"],
            "dx_code": r["dx_code"],
            "note": r["note"],
        }
        if prev_codes:
            prev = conn.execute(
                "SELECT pr.name, t.performed_at,"
                " CAST(julianday(date(?)) - julianday(date(t.performed_at)) AS INTEGER)"
                " FROM source.emr_treatments t JOIN source.emr_procedures pr"
                " ON pr.code = t.procedure_code"
                f" WHERE t.patient_id = ? AND t.procedure_code IN ({','.join('?' * len(prev_codes))})"
                " AND t.performed_at < ?"
                + (" AND t.tooth_no = ?" if args["same_tooth"] else "")
                + " ORDER BY t.performed_at DESC LIMIT 1",
                [r["performed_at"], r["patient_id"], *prev_codes, r["performed_at"]]
                + ([r["tooth_no"]] if args["same_tooth"] else []),
            ).fetchone()
            if prev is None:
                continue
            gap = prev[2]
            if (args["min_gap_days"] is not None and gap < args["min_gap_days"]) or (
                args["max_gap_days"] is not None and gap > args["max_gap_days"]
            ):
                continue
            item["prev"] = {"procedure": prev[0], "performed_at": prev[1], "gap_days": gap}
        out.append(item)
    return {"count": len(out), "matched": matched, "results": out[: args["limit"]]}


def query_tools(clinic: Clinic, include_ax: bool = False) -> list[Tool]:
    """include_ax = query_schedule·query_patient(다음 예약)가 승인된 AX 예약도 본다 — ③ 챗은 원본만(기본)"""
    today = clinic.today.isoformat()
    return [
        Tool(
            "query_patient",
            "환자 한 명의 기본정보·주의사항·마지막 내원·잔액·다음 예약·가족 리콜·열린 치료계획."
            " 동명이인이면 후보만 돌려준다. chart_no 또는 name 중 하나 필수.",
            PatientArgs,
            lambda ctx, args: _patient(clinic, ctx.store.conn, args, include_ax),
        ),
        Tool(
            "query_schedule",
            f"예약 건수·목록. 날짜는 YYYY-MM-DD, 오늘은 {today}. 단일 날짜는 date_from과 date_to에"
            " 같은 값. count는 조건에 맞는 전체 행이고 status를 주지 않으면 취소·변경도 든다.",
            ScheduleArgs,
            lambda ctx, args: _schedule(clinic, ctx.store.conn, args, include_ax),
        ),
        Tool(
            "query_claims",
            f"보험 청구 건수·금액·목록. 날짜는 진료일 기준 YYYY-MM-DD, 오늘은 {today}."
            " 확정 월은 결과와 사유가 함께 나온다.",
            ClaimsArgs,
            lambda ctx, args: _claims(ctx.store.conn, args),
        ),
        Tool(
            "query_charts",
            "기간·간격 조건이 있는 차트 기록 조회(예: 스케일링하고 닷새도 안 돼서 치주치료). 시술 이름·별칭,"
            " 바로 앞 시술과 그 간격(일), 같은 치아, 진료 기간, 차트 번호로 거른다. 결과 id(chart:…)는 인용할 수 있다."
            f" 날짜는 YYYY-MM-DD, 오늘은 {today}. 환자 이름은 나오지 않는다.",
            ChartArgs,
            lambda ctx, args: _charts(clinic, ctx.store.conn, args),
        ),
        Tool(
            "refuse",
            "근거가 없거나(not_found) 비공개 행정정보(restricted)면 부른다. 호출하면 턴이 끝난다.",
            RefuseArgs,
            lambda ctx, args: None,
            ends_run=lambda args: f"refuse:{args['reason']}",
        ),
    ]
