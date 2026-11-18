"""EMR 연동 모듈 — 예약 슬롯 검사·빈 시간·예약 초안·승인·취소, 원본 + AX 예약 합쳐 읽기, 예약 원함 미예약
(AI 비서 설계 §5). emr_*에는 쓰지 않고 ax_emr_writes에만 쓴다(§5.1). 실제 도입 때는 이 모듈의 쓰기만 EMR API로 바꾼다.

슬롯 검사는 원본 예약이 지키는 운영 규칙 전부(가상 데이터 생성 설계 §1 (3)·(4)) — 영업시간·점심·휴진일, 기준일
이후, 시술 소요 시간, 담당 역할, 담당 동시 건수, 체어 한 시각 1건(상담은 체어 없음), 같은 환자 같은 날 1건.
원본(취소 제외)과 승인된 AX 예약을 합쳐 세고, 초안은 자리를 막지 않는다(체어는 승인 때 다시 고른다).
종이 차트 이관(차트 OCR 설계 §설계 3)도 여기서 — 초안·승인·반려는 ax_chart_imports에만.
값은 source.ax_master_values(prep.master_values)."""

import json
import sqlite3
from datetime import date, datetime, time, timedelta

from backend import calls, missed, prep
from backend.emr import _hours
from shared.clinic import Clinic

ROLE_FOR_TYPE = {"treatment": "doctor", "hygiene": "hygienist", "consult": "consultant"}
PATIENT_RULES = ("patient_same_day", "unknown_patient")  # free_slots는 환자 없이 본다


class Conflict(Exception):
    """초안이 아닌 행을 승인·취소하려 함 — 이미 처리됐다(두 번 누름·다른 파트가 먼저)"""


def _staff(conn: sqlite3.Connection) -> list[dict]:
    """재직 중인 원장·위생사·상담실장, id 순"""
    return [
        dict(r)
        for r in conn.execute(
            "SELECT id, name, role FROM source.emr_staff WHERE active = 1 AND role IN (?, ?, ?)"
            " ORDER BY id",
            tuple(ROLE_FOR_TYPE.values()),
        )
    ]


def appointment_type(conn: sqlite3.Connection, procedure_code: str) -> str:
    """원본 예약에서 그 시술에 가장 많이 쓴 예약 종류(같으면 이름순) — 없으면 ValueError"""
    row = conn.execute(
        "SELECT type FROM source.emr_appointments WHERE planned_procedure_code = ?"
        " GROUP BY type ORDER BY COUNT(*) DESC, type LIMIT 1",
        (procedure_code,),
    ).fetchone()
    if row is None:
        raise ValueError(f"예약 기록이 없는 시술: {procedure_code}")
    return row[0]


def _duration(conn: sqlite3.Connection, procedure_code: str) -> int:
    row = conn.execute(
        "SELECT duration_min FROM source.emr_procedures WHERE code = ?", (procedure_code,)
    ).fetchone()
    if row is None:
        raise ValueError(f"모르는 시술: {procedure_code}")
    return row[0]


def _busy(conn: sqlite3.Connection, day: date, exclude_id: int | None = None) -> list[dict]:
    """그날 자리를 차지한 예약 — 원본(취소 제외 — 노쇼도 그 시각엔 잡혀 있던 자리, verify와 같다) + 승인된 AX 예약.
    exclude_id = 빼고 볼 AX 행"""
    rows = conn.execute(
        "SELECT patient_id, staff_id, chair_no, scheduled_at, duration_min"
        " FROM source.emr_appointments WHERE status != 'cancelled' AND date(scheduled_at) = :d"
        " UNION ALL"
        " SELECT patient_id, staff_id, chair_no, scheduled_at, duration_min FROM ax_emr_writes"
        " WHERE status = 'approved' AND date(scheduled_at) = :d AND id IS NOT :x",
        {"d": day.isoformat(), "x": exclude_id},
    ).fetchall()
    out = []
    for r in rows:
        start = datetime.fromisoformat(r["scheduled_at"])
        out.append(
            {**dict(r), "start": start, "end": start + timedelta(minutes=r["duration_min"])}
        )
    return out


def _check(
    conn: sqlite3.Connection,
    clinic: Clinic,
    mv: dict,
    *,
    patient_id: int | None,
    staff: dict | None,
    start: datetime,
    type_: str,
    duration: int,
    busy: list[dict],
) -> tuple[list[str], list[int]]:
    """(어긋난 규칙 코드, 그 시각 빈 체어) — patient_id None이면 환자 규칙을 건너뛴다"""
    day, end = start.date(), start + timedelta(minutes=duration)
    h = _hours(conn, day)
    codes = []
    if h["closed"] or day.isoformat() in mv["bank_holidays"]:  # 공휴일도 휴진(config holidays)
        codes.append("closed_day")

    def at(hhmm: str) -> datetime:
        return datetime.combine(day, time.fromisoformat(hhmm))

    if start < at(h["open"]) or end > at(h["close"]):
        codes.append("outside_hours")
    # 원본 예약은 개원 시각부터 slot_min 격자에서만 시작한다(생성기 starts) — 지어낸 시각이 초안이 되지 않게
    grid = timedelta(minutes=mv["slot_min"])
    if start.second or start.microsecond or (start - at(h["open"])) % grid:
        codes.append("off_slot")
    if h["lunch"] and start < at(h["lunch"][1]) and end > at(h["lunch"][0]):
        codes.append("lunch")
    if day < clinic.today:
        codes.append("before_today")
    if staff is None or staff["role"] != ROLE_FOR_TYPE[type_]:
        codes.append("wrong_role")
    overlap = [b for b in busy if b["start"] < end and start < b["end"]]
    cap = {"treatment": mv["doctor_concurrency"], "hygiene": 1}.get(type_)  # 상담은 제약 없음
    # 담당 동시 건수 = 새 예약 안 각 시작 시각(새 시작·겹치는 예약의 시작)에 진행 중인 건수 + 새 예약 —
    # 동시 건수의 최대는 어떤 시작 시각에 나온다(verify schedule._over_capacity와 같은 셈)
    mine = [b for b in overlap if staff and b["staff_id"] == staff["id"]]
    points = [start] + [b["start"] for b in mine if start <= b["start"]]
    if cap is not None and any(
        1 + sum(b["start"] <= t < b["end"] for b in mine) > cap for t in points
    ):
        codes.append("staff_busy")
    used = {b["chair_no"] for b in overlap}
    chairs = [c for c in range(1, mv["chairs"] + 1) if c not in used]
    if type_ != "consult" and not chairs:
        codes.append("no_chair")
    if patient_id is not None:
        known = conn.execute(
            "SELECT 1 FROM source.emr_patients WHERE id = ?", (patient_id,)
        ).fetchone()
        if known is None:
            codes.append("unknown_patient")
        elif any(b["patient_id"] == patient_id for b in busy):
            codes.append("patient_same_day")
    return codes, chairs


def _slot(conn, clinic, *, patient_id, staff_id, start, procedure_code, exclude_id=None):
    type_ = appointment_type(conn, procedure_code)
    duration = _duration(conn, procedure_code)
    staff = next((s for s in _staff(conn) if s["id"] == staff_id), None)
    codes, chairs = _check(
        conn,
        clinic,
        prep.master_values(conn),
        patient_id=patient_id,
        staff=staff,
        start=start,
        type_=type_,
        duration=duration,
        busy=_busy(conn, start.date(), exclude_id),
    )
    return codes, chairs, type_, duration


def check_slot(
    conn: sqlite3.Connection,
    clinic: Clinic,
    *,
    patient_id: int,
    staff_id: int,
    start: datetime,
    procedure_code: str,
    exclude_id: int | None = None,
) -> list[str]:
    """어긋난 규칙 코드 목록 — 빈 목록이면 통과(AI 비서 설계 §5.2 승인 검사)"""
    return _slot(
        conn,
        clinic,
        patient_id=patient_id,
        staff_id=staff_id,
        start=start,
        procedure_code=procedure_code,
        exclude_id=exclude_id,
    )[0]


def free_slots(
    conn: sqlite3.Connection,
    clinic: Clinic,
    day: date,
    procedure_code: str,
    staff_id: int | None = None,
) -> list[dict]:
    """그날 그 시술이 들어갈 시작 시각 × 담당 — master slot_min 간격, 환자 규칙을 뺀 check_slot 통과분(§5.2).
    chairs = 그 시각에 비어 있는 체어 번호(배정은 초안·승인 때 이 중 앞 번호)"""
    type_ = appointment_type(conn, procedure_code)
    duration = _duration(conn, procedure_code)
    mv, busy, h = prep.master_values(conn), _busy(conn, day), _hours(conn, day)
    staff = [
        s
        for s in _staff(conn)
        if (staff_id is None and s["role"] == ROLE_FOR_TYPE[type_]) or s["id"] == staff_id
    ]
    out = []
    start = datetime.combine(day, time.fromisoformat(h["open"]))
    close = datetime.combine(day, time.fromisoformat(h["close"]))
    while start + timedelta(minutes=duration) <= close:
        for s in staff:
            codes, chairs = _check(
                conn,
                clinic,
                mv,
                patient_id=None,
                staff=s,
                start=start,
                type_=type_,
                duration=duration,
                busy=busy,
            )
            if not codes:
                end = start + timedelta(minutes=duration)
                out.append(
                    {
                        "staff_id": s["id"],
                        "staff_name": s["name"],
                        "start": start.strftime("%H:%M"),
                        "end": end.strftime("%H:%M"),
                        "chairs": chairs,
                    }
                )
        start += timedelta(minutes=mv["slot_min"])
    return out


def _row(conn: sqlite3.Connection, write_id: int) -> dict | None:
    r = conn.execute("SELECT * FROM ax_emr_writes WHERE id = ?", (write_id,)).fetchone()
    return dict(r) if r else None


def create_draft(
    conn: sqlite3.Connection,
    clinic: Clinic,
    *,
    patient_id: int,
    staff_id: int,
    start: datetime,
    procedure_code: str,
    source_call_uid: str | None,
    conversation_id: int | None,
    turn: int | None,
) -> dict:
    """검사를 통과하면 draft 행 하나(체어는 그 시각 빈 체어 중 가장 작은 번호, 상담은 NULL)와 그 행,
    아니면 {error: [코드]}(행 없음). 커밋은 부르는 쪽"""
    codes, chairs, type_, duration = _slot(
        conn,
        clinic,
        patient_id=patient_id,
        staff_id=staff_id,
        start=start,
        procedure_code=procedure_code,
    )
    if codes:
        return {"error": codes}
    cur = conn.execute(
        "INSERT INTO ax_emr_writes (kind, patient_id, staff_id, scheduled_at, duration_min, type,"
        " procedure_code, chair_no, source_call_uid, conversation_id, turn, created_at)"
        " VALUES ('appointment', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            patient_id,
            staff_id,
            start.isoformat(sep=" ", timespec="seconds"),
            duration,
            type_,
            procedure_code,
            None if type_ == "consult" else chairs[0],
            source_call_uid,
            conversation_id,
            turn,
            datetime.now().isoformat(sep=" ", timespec="seconds"),
        ),
    )
    return _row(conn, cur.lastrowid)


def approve(
    conn: sqlite3.Connection,
    clinic: Clinic,
    draft_id: int,
    *,
    by: str,
    at: str,
    missed_day: date,
) -> dict:
    """사람의 승인(§5.2) — draft가 아니면 Conflict. 다시 검사해 걸리면 rejected(reason = 코드를 쉼표로)와
    {ok: False, reasons}, 통과하면 approved와 — 그 환자가 그날 ⑨ 미예약 환자면 연락 기록 booked(채널 call,
    §5.3). 한 트랜잭션 — 열린 트랜잭션이 없으면 BEGIN IMMEDIATE로 쓰기 잠금부터 잡아, 두 연결이 같은 자리의 다른
    초안을 동시에 승인해도 상태 확인·재검사가 앞 승인의 커밋 뒤에 돈다. 커밋은 부르는 쪽(Conflict면 이 함수가 연
    트랜잭션은 되돌린다)"""
    began = not conn.in_transaction
    if began:
        conn.execute("BEGIN IMMEDIATE")
    row = _row(conn, draft_id)
    if row is None or row["status"] != "draft":
        if began:
            conn.rollback()
        raise Conflict(f"초안이 아님: {draft_id}")
    codes, chairs, _, _ = _slot(
        conn,
        clinic,
        patient_id=row["patient_id"],
        staff_id=row["staff_id"],
        start=datetime.fromisoformat(row["scheduled_at"]),
        procedure_code=row["procedure_code"],
        exclude_id=draft_id,
    )
    status = "rejected" if codes else "approved"
    # 초안 때 고른 체어를 같은 시각의 다른 초안이 먼저 승인돼 차지했으면 지금 빈 체어 중 가장 작은 번호로(상담은 NULL)
    chair = row["chair_no"]
    if not codes and chair is not None and chair not in chairs:
        chair = chairs[0]
    cur = conn.execute(
        "UPDATE ax_emr_writes SET status = ?, reason = ?, decided_by = ?, decided_at = ?,"
        " chair_no = ? WHERE id = ? AND status = 'draft'",
        (status, ",".join(codes) or None, by, at, chair, draft_id),
    )
    if cur.rowcount == 0:
        raise Conflict(f"초안이 아님: {draft_id}")
    if codes:
        return {"ok": False, "reasons": codes}
    contact = any(f["patient_id"] == row["patient_id"] for f in missed.facts(conn, missed_day))
    if contact:
        missed.record_contact(conn, row["patient_id"], "booked", "call", by, at, missed_day)
    return {"ok": True, "id": draft_id, "contact": contact}


def cancel(conn: sqlite3.Connection, draft_id: int, *, by: str, at: str) -> None:
    """초안만 cancelled — 아니면 Conflict. 커밋은 부르는 쪽"""
    cur = conn.execute(
        "UPDATE ax_emr_writes SET status = 'cancelled', decided_by = ?, decided_at = ?"
        " WHERE id = ? AND status = 'draft'",
        (by, at, draft_id),
    )
    if cur.rowcount == 0:
        raise Conflict(f"초안이 아님: {draft_id}")


def ax_appointments(conn: sqlite3.Connection, date_from: date, date_to: date) -> list[dict]:
    """기간(양 끝 포함)의 승인된 AX 예약 — 원본 예약 행(emr_appointments)과 같은 키 + 환자·시술 이름(§5.3).
    id는 "ax-<id>", 상태는 scheduled, 잡은 시각(created_at)은 승인 시각"""
    rows = conn.execute(
        "SELECT w.*, p.chart_no, p.name, r.name AS procedure_name,"
        " pc.started_at AS pasted_call_started_at FROM ax_emr_writes w"
        " JOIN source.emr_patients p ON p.id = w.patient_id"
        " LEFT JOIN ax_pasted_calls pc ON pc.call_uid = w.source_call_uid"
        " JOIN source.emr_procedures r ON r.code = w.procedure_code"
        " WHERE w.status = 'approved' AND date(w.scheduled_at) BETWEEN ? AND ?"
        " ORDER BY w.scheduled_at, w.id",
        (date_from.isoformat(), date_to.isoformat()),
    ).fetchall()
    return [
        {
            "id": f"ax-{r['id']}",
            "patient_id": r["patient_id"],
            "staff_id": r["staff_id"],
            "scheduled_at": r["scheduled_at"],
            "duration_min": r["duration_min"],
            "type": r["type"],
            "planned_procedure_code": r["procedure_code"],
            "status": "scheduled",
            "rescheduled_from_id": None,
            "booked_via": "ax",
            "confirmed": 0,
            "created_at": r["decided_at"],
            "cancelled_at": None,
            "chair_no": r["chair_no"],
            "plan_item_id": None,
            "source": "ax",
            "source_call_uid": r["source_call_uid"],
            "conversation_id": r["conversation_id"],
            "decided_by": r["decided_by"],
            "decided_at": r["decided_at"],
            "chart_no": r["chart_no"],
            "name": r["name"],
            "procedure_name": r["procedure_name"],
            "pasted_call_started_at": r[
                "pasted_call_started_at"
            ],  # 출처가 붙여 넣은 통화면 그 시각
        }
        for r in rows
    ]


def unbooked_requests(conn: sqlite3.Connection, date_from: date, date_to: date) -> list[dict]:
    """예약 원함 미예약(AI 비서 설계 §5.3) — 기간(통화일, 양 끝 포함) 안 활성 분류의 wants_appt가 참인 통화 중
    그 환자에게 통화 시각 뒤로 살아 있는 예약(원본 scheduled·completed 또는 승인된 AX 예약)이 없는 것.
    같은 환자는 최근 통화 하나, 차트에 없는 통화(patient_id 없음)는 통화마다 한 줄에 no_chart. 최근 통화 순"""
    version, _ = calls.active_versions(conn)
    rows = conn.execute(
        "SELECT c.call_uid, c.started_at, c.patient_id, p.chart_no,"
        " COALESCE(p.name, c.caller_name) AS name, k.summary, c.in_eval_pool"
        " FROM source.ax_calls c"
        " JOIN ax_call_classifications k ON k.call_uid = c.call_uid"
        "  AND k.model_alias = 'classify' AND k.prompt_version = :v AND k.status = 'ok'"
        " LEFT JOIN source.emr_patients p ON p.id = c.patient_id"
        " WHERE json_extract(k.labels, '$.wants_appt') = 1"
        " AND date(c.started_at) BETWEEN :f AND :t"
        " AND NOT EXISTS (SELECT 1 FROM source.emr_appointments a WHERE a.patient_id = c.patient_id"
        "  AND a.status IN ('scheduled', 'completed') AND a.scheduled_at > c.started_at)"
        " AND NOT EXISTS (SELECT 1 FROM ax_emr_writes w WHERE w.patient_id = c.patient_id"
        "  AND w.status = 'approved' AND w.scheduled_at > c.started_at)"
        " ORDER BY c.started_at DESC, c.call_uid",
        {"v": version, "f": date_from.isoformat(), "t": date_to.isoformat()},
    ).fetchall()
    out, seen = [], set()
    for r in rows:
        if r["patient_id"] is not None:
            if r["patient_id"] in seen:
                continue
            seen.add(r["patient_id"])
        out.append({**dict(r), "no_chart": r["patient_id"] is None})
    return out


def match_patient(conn: sqlite3.Connection, name: str, birth_date: str) -> int | None:
    """이름·생년월일이 정확히 한 명과 맞으면 그 환자 id — 없거나 동명이인이면 None(사람이 고른다)"""
    rows = conn.execute(
        "SELECT id FROM source.emr_patients WHERE name = ? AND birth_date = ?",
        (name.strip(), birth_date.strip()),
    ).fetchall()
    return rows[0][0] if len(rows) == 1 else None


def chart_import_draft(
    conn: sqlite3.Connection,
    *,
    filename: str,
    image: bytes,
    ocr: dict,
    fields: dict,
    at: str,
) -> int:
    """종이 차트 한 장의 초안 — 환자는 match_patient로 미리 채운다. 커밋은 부르는 쪽"""
    pid = match_patient(conn, fields.get("patient_name", ""), fields.get("birth_date", ""))
    cur = conn.execute(
        "INSERT INTO ax_chart_imports (filename, image, ocr, fields, patient_id, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            filename,
            image,
            json.dumps(ocr, ensure_ascii=False),
            json.dumps(fields, ensure_ascii=False),
            pid,
            at,
        ),
    )
    return cur.lastrowid


def chart_import(conn: sqlite3.Connection, import_id: int) -> dict | None:
    """이관 한 행 — JSON 칸은 풀고 이미지는 뺀다(따로 읽는다)"""
    r = conn.execute(
        "SELECT id, filename, ocr, fields, final, patient_id, status, decided_by, decided_at,"
        " created_at FROM ax_chart_imports WHERE id = ?",
        (import_id,),
    ).fetchone()
    if r is None:
        return None
    out = dict(r)
    for k in ("ocr", "fields", "final"):
        out[k] = json.loads(out[k]) if out[k] else None
    return out


def approve_chart_import(
    conn: sqlite3.Connection,
    import_id: int,
    *,
    final: dict,
    patient_id: int,
    by: str,
    at: str,
) -> None:
    """사람이 고친 항목과 환자로 승인 — 모르는 환자면 ValueError, 초안이 아니면 Conflict. 커밋은 부르는 쪽"""
    if not conn.execute(
        "SELECT 1 FROM source.emr_patients WHERE id = ?", (patient_id,)
    ).fetchone():
        raise ValueError(f"모르는 환자: {patient_id}")
    cur = conn.execute(
        "UPDATE ax_chart_imports SET status = 'approved', final = ?, patient_id = ?,"
        " decided_by = ?, decided_at = ? WHERE id = ? AND status = 'draft'",
        (json.dumps(final, ensure_ascii=False), patient_id, by, at, import_id),
    )
    if cur.rowcount == 0:
        raise Conflict(f"초안이 아님: {import_id}")


def reject_chart_import(conn: sqlite3.Connection, import_id: int, *, by: str, at: str) -> None:
    """초안만 rejected — 아니면 Conflict. 커밋은 부르는 쪽"""
    cur = conn.execute(
        "UPDATE ax_chart_imports SET status = 'rejected', decided_by = ?, decided_at = ?"
        " WHERE id = ? AND status = 'draft'",
        (by, at, import_id),
    )
    if cur.rowcount == 0:
        raise Conflict(f"초안이 아님: {import_id}")


def chart_imports(conn: sqlite3.Connection, patient_id: int) -> list[dict]:
    """그 환자의 승인된 이관 진료 줄 — 날짜순(같으면 이관 순) {import_id, date, teeth, fdi, treatment}"""
    out = []
    for r in conn.execute(
        "SELECT id, final FROM ax_chart_imports WHERE patient_id = ? AND status = 'approved'"
        " ORDER BY id",
        (patient_id,),
    ):
        for v in json.loads(r["final"]).get("visits", []):
            out.append(
                {
                    "import_id": r["id"],
                    "date": v.get("date"),
                    "teeth": v.get("teeth"),
                    "fdi": v.get("fdi"),
                    "treatment": v.get("treatment", ""),
                }
            )
    return sorted(out, key=lambda x: (x["date"] or "", x["import_id"]))
