"""EMR 연동 모듈 — 슬롯 검사·예약 초안·승인·합쳐 읽기·예약 원함 미예약(backend/emr_bridge.py, AI 비서 설계 §5).
손으로 만든 source(실제 스키마 + master 값, 직원 3명·시술 3개)로 운영 규칙마다 걸림·통과를 본다."""

import json
import sqlite3
from datetime import date, datetime

import pytest

from backend import db, emr_bridge
from generator.clinic import master_values_rows
from generator.master import HOURS, PROCEDURES
from shared.clinic import Clinic
from tests.backend.call_db import LABELS, classify_row
from tests.backend.conftest import SOURCE_SCHEMA

TODAY = date(2026, 12, 1)  # 화
CLINIC = Clinic(TODAY, date(2026, 6, 1), 40)
WED = "2026-12-02"
SCALING, RESIN, CONSULT = "U0001", "U0005", "U0019"  # hygiene·treatment·consult
DOCTOR, HYGIENIST, CONSULTANT = 1, 2, 3
MISSED, NEW = 1, 2  # 1 = 리콜 지난 우리 환자(⑨ facts에 있음), 2 = 내원 기록 없음
BY, AT = "데스크", "2026-12-01 08:50:00"


def _appt(s, pid, staff, at, type_, code, chair, status="scheduled", dur=30):
    s.execute(
        "INSERT INTO emr_appointments (patient_id, staff_id, scheduled_at, duration_min, type,"
        " planned_procedure_code, status, booked_via, created_at, chair_no)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, 'phone', '2025-01-01 09:00:00', ?)",
        (pid, staff, at, dur, type_, code, status, chair),
    )


@pytest.fixture
def src(tmp_path):
    path = tmp_path / "s.sqlite"
    s = sqlite3.connect(path)
    s.executescript(SOURCE_SCHEMA.read_text(encoding="utf-8"))
    s.execute("INSERT INTO emr_clinic VALUES ('치과', ?)", (json.dumps(HOURS),))
    cols = list(PROCEDURES[0])
    s.executemany(
        f"INSERT INTO emr_procedures ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        [tuple(p[c] for c in cols) for p in PROCEDURES if p["code"] in (SCALING, RESIN, CONSULT)],
    )
    s.executemany("INSERT INTO ax_master_values VALUES (?, ?)", master_values_rows())
    s.executemany(
        "INSERT INTO emr_staff (id, name, role) VALUES (?, ?, ?)",
        [
            (DOCTOR, "강태현", "doctor"),
            (HYGIENIST, "임재윤", "hygienist"),
            (CONSULTANT, "오지수", "consultant"),
        ],
    )
    s.executemany(
        "INSERT INTO emr_patients (id, chart_no, name, birth_date, sex, phone, registered_at,"
        " recall_cycle_months) VALUES (?, ?, ?, '1980-01-01', 'F', ?, '2020-01-01', 6)",
        [(MISSED, "C-00001", "김철수", "010-1"), (NEW, "C-00002", "이영희", "010-2")],
    )
    # 지난 내원 — 시술별 예약 종류(appointment_type)와 ⑨ 리콜 칩의 재료
    _appt(s, MISSED, HYGIENIST, "2025-01-06 10:00:00", "hygiene", SCALING, 1, "completed")
    _appt(s, MISSED, DOCTOR, "2025-01-13 10:00:00", "treatment", RESIN, 1, "completed")
    _appt(s, MISSED, CONSULTANT, "2025-01-20 10:00:00", "consult", CONSULT, None, "completed")
    _appt(s, MISSED, DOCTOR, "2025-01-27 10:00:00", "treatment", CONSULT, 1, "completed")
    _appt(s, MISSED, CONSULTANT, "2025-02-03 10:00:00", "consult", CONSULT, None, "completed")
    s.commit()
    return s


@pytest.fixture
def conn(src, tmp_path):
    src.close()
    c = db.connect(tmp_path / "a.sqlite", tmp_path / "s.sqlite")
    db.init_schema(c)
    yield c
    c.close()


def _src_add(conn, *rows):
    """source는 읽기 전용으로 붙어 있어 따로 열어 더한다"""
    path = conn.execute("SELECT file FROM pragma_database_list WHERE name = 'source'").fetchone()[
        0
    ]
    s = sqlite3.connect(path)
    for r in rows:
        _appt(s, *r)
    s.commit()
    s.close()


def _check(conn, at, code=RESIN, staff=DOCTOR, patient=NEW, **kw):
    return emr_bridge.check_slot(
        conn,
        CLINIC,
        patient_id=patient,
        staff_id=staff,
        start=datetime.fromisoformat(at),
        procedure_code=code,
        **kw,
    )


def _draft(conn, at, code=RESIN, staff=DOCTOR, patient=NEW):
    return emr_bridge.create_draft(
        conn,
        CLINIC,
        patient_id=patient,
        staff_id=staff,
        start=datetime.fromisoformat(at),
        procedure_code=code,
        source_call_uid="c-0001",
        conversation_id=1,
        turn=1,
    )


def _approve(conn, draft_id):
    return emr_bridge.approve(conn, CLINIC, draft_id, by=BY, at=AT, missed_day=TODAY)


def test_appointment_type_most_used(conn):
    assert emr_bridge.appointment_type(conn, SCALING) == "hygiene"
    assert emr_bridge.appointment_type(conn, CONSULT) == "consult"  # 2:1
    with pytest.raises(ValueError):
        emr_bridge.appointment_type(conn, "U0099")


def test_slot_ok(conn):
    assert _check(conn, f"{WED} 10:00") == []


def test_slot_closed_weekday(conn):
    assert _check(conn, "2026-12-03 10:00") == ["closed_day"]


def test_slot_outside_hours(conn):
    assert _check(conn, f"{WED} 18:30") == ["outside_hours"]
    assert _check(conn, f"{WED} 09:00") == ["outside_hours"]
    assert _check(conn, "2026-12-05 13:30") == ["outside_hours"]  # 토요일 마감


def test_slot_lunch(conn):
    assert _check(conn, f"{WED} 13:00") == ["lunch"]
    assert _check(conn, "2026-12-05 13:00") == []  # 토요일은 점심 없음


def test_slot_before_today(conn):
    assert "before_today" in _check(conn, "2026-11-30 10:00")
    assert _check(conn, "2026-12-01 09:30") == []  # 기준일 당일은 영업시간 전체


def test_slot_wrong_role(conn):
    assert _check(conn, f"{WED} 10:00", code=SCALING, staff=DOCTOR) == ["wrong_role"]


def test_slot_unknown_patient(conn):
    assert _check(conn, f"{WED} 10:00", patient=999) == ["unknown_patient"]


def test_slot_staff_busy_hygienist(conn):
    _src_add(conn, (MISSED, HYGIENIST, f"{WED} 10:00:00", "hygiene", SCALING, 1))
    assert _check(conn, f"{WED} 10:00", code=SCALING, staff=HYGIENIST) == ["staff_busy"]
    assert _check(conn, f"{WED} 10:30", code=SCALING, staff=HYGIENIST) == []


def test_slot_doctor_concurrency(conn):
    cap = emr_bridge.prep.master_values(conn)["doctor_concurrency"]
    rows = [(MISSED, DOCTOR, f"{WED} 10:00:00", "treatment", RESIN, c) for c in range(1, cap)]
    _src_add(conn, *rows)
    assert _check(conn, f"{WED} 10:00") == []
    _src_add(conn, (MISSED, DOCTOR, f"{WED} 10:00:00", "treatment", RESIN, cap))
    assert _check(conn, f"{WED} 10:00") == ["staff_busy"]


def test_slot_no_chair(conn):
    chairs = emr_bridge.prep.master_values(conn)["chairs"]
    _src_add(
        conn,
        *[
            (MISSED, HYGIENIST, f"{WED} 10:00:00", "hygiene", SCALING, c)
            for c in range(1, chairs + 1)
        ],
    )
    assert _check(conn, f"{WED} 10:00") == ["no_chair"]
    # 상담은 체어 없이 잡는다
    assert _check(conn, f"{WED} 10:00", code=CONSULT, staff=CONSULTANT) == []


def test_slot_patient_same_day(conn):
    _src_add(conn, (NEW, HYGIENIST, f"{WED} 15:00:00", "hygiene", SCALING, 1))
    assert _check(conn, f"{WED} 10:00") == ["patient_same_day"]
    # 취소된 예약은 세지 않는다
    _src_add(conn, (MISSED, HYGIENIST, "2026-12-04 15:00:00", "hygiene", SCALING, 1, "cancelled"))
    assert _check(conn, "2026-12-04 10:00", patient=MISSED) == []


def test_slot_counts_approved_ax_not_drafts(conn):
    first = _draft(conn, f"{WED} 10:00", code=SCALING, staff=HYGIENIST, patient=MISSED)
    assert _check(conn, f"{WED} 10:00", code=SCALING, staff=HYGIENIST) == []
    _approve(conn, first["id"])
    assert _check(conn, f"{WED} 10:00", code=SCALING, staff=HYGIENIST) == ["staff_busy"]


def test_free_slots_excludes_busy_and_lunch(conn):
    _src_add(conn, (MISSED, HYGIENIST, f"{WED} 10:00:00", "hygiene", SCALING, 1))
    slots = emr_bridge.free_slots(conn, CLINIC, date(2026, 12, 2), SCALING)
    starts = [s["start"] for s in slots]
    assert {s["staff_id"] for s in slots} == {HYGIENIST}
    assert slots[0] == {
        "staff_id": HYGIENIST,
        "staff_name": "임재윤",
        "start": "09:30",
        "end": "10:00",
    }
    assert "10:00" not in starts and "10:30" in starts
    assert "13:00" not in starts and "13:30" not in starts and "14:00" in starts
    assert starts[-1] == "18:00"
    assert emr_bridge.free_slots(conn, CLINIC, date(2026, 12, 3), SCALING) == []
    assert emr_bridge.free_slots(conn, CLINIC, date(2026, 12, 2), RESIN, staff_id=HYGIENIST) == []


def test_create_draft_assigns_lowest_free_chair(conn):
    _src_add(
        conn,
        (MISSED, HYGIENIST, f"{WED} 10:00:00", "hygiene", SCALING, 1),
        (MISSED, DOCTOR, f"{WED} 09:30:00", "treatment", RESIN, 2, "scheduled", 60),
    )
    got = _draft(conn, f"{WED} 10:00")
    assert got["chair_no"] == 3 and got["status"] == "draft" and got["type"] == "treatment"
    assert got["duration_min"] == 30 and got["scheduled_at"] == f"{WED} 10:00:00"
    consult = _draft(conn, f"{WED} 11:00", code=CONSULT, staff=CONSULTANT)
    assert consult["chair_no"] is None and consult["type"] == "consult"


def test_create_draft_rejects_without_row(conn):
    assert _draft(conn, "2026-12-03 10:00") == {"error": ["closed_day"]}
    assert conn.execute("SELECT COUNT(*) FROM ax_emr_writes").fetchone()[0] == 0


def _contacts(conn):
    return conn.execute("SELECT patient_id, result, channel FROM ax_missed_contacts").fetchall()


def test_approve_writes_contact_when_in_missed(conn):
    d = _draft(conn, f"{WED} 10:00", code=SCALING, staff=HYGIENIST, patient=MISSED)
    got = _approve(conn, d["id"])
    assert got["ok"] is True
    row = conn.execute("SELECT * FROM ax_emr_writes WHERE id = ?", (d["id"],)).fetchone()
    assert (row["status"], row["decided_by"], row["decided_at"]) == ("approved", BY, AT)
    assert [tuple(r) for r in _contacts(conn)] == [(MISSED, "booked", "call")]


def test_approve_patient_not_in_missed_skips_contact(conn):
    d = _draft(conn, f"{WED} 10:00")
    assert _approve(conn, d["id"])["ok"] is True
    assert _contacts(conn) == []


def test_approve_rechecks_and_rejects_conflict(conn):
    a = _draft(conn, f"{WED} 10:00", code=SCALING, staff=HYGIENIST, patient=MISSED)
    b = _draft(conn, f"{WED} 10:00", code=SCALING, staff=HYGIENIST, patient=NEW)
    assert _approve(conn, a["id"])["ok"] is True
    got = _approve(conn, b["id"])
    assert got == {"ok": False, "reasons": ["staff_busy"]}
    row = conn.execute("SELECT status, reason FROM ax_emr_writes WHERE id = ?", (b["id"],))
    assert tuple(row.fetchone()) == ("rejected", "staff_busy")


def test_approve_twice_is_idempotent_conflict(conn):
    d = _draft(conn, f"{WED} 10:00", code=SCALING, staff=HYGIENIST, patient=MISSED)
    _approve(conn, d["id"])
    with pytest.raises(emr_bridge.Conflict):
        _approve(conn, d["id"])
    assert len(_contacts(conn)) == 1


def test_cancel_only_draft(conn):
    d = _draft(conn, f"{WED} 10:00")
    emr_bridge.cancel(conn, d["id"], by=BY, at=AT)
    row = conn.execute("SELECT status, decided_by FROM ax_emr_writes WHERE id = ?", (d["id"],))
    assert tuple(row.fetchone()) == ("cancelled", BY)
    e = _draft(conn, f"{WED} 11:00")
    _approve(conn, e["id"])
    with pytest.raises(emr_bridge.Conflict):
        emr_bridge.cancel(conn, e["id"], by=BY, at=AT)
    status = conn.execute("SELECT status FROM ax_emr_writes WHERE id = ?", (e["id"],)).fetchone()
    assert status[0] == "approved"


def test_ax_appointments_shape(conn):
    d = _draft(conn, f"{WED} 10:00", code=SCALING, staff=HYGIENIST, patient=MISSED)
    _draft(conn, f"{WED} 11:00")  # 초안은 나오지 않는다
    _approve(conn, d["id"])
    got = emr_bridge.ax_appointments(conn, date(2026, 12, 2), date(2026, 12, 2))
    assert len(got) == 1
    a = got[0]
    assert a["id"] == f"ax-{d['id']}" and a["source"] == "ax" and a["booked_via"] == "ax"
    assert (a["name"], a["chart_no"], a["procedure_name"]) == ("김철수", "C-00001", "스케일링")
    assert (a["scheduled_at"], a["type"], a["status"]) == (
        f"{WED} 10:00:00",
        "hygiene",
        "scheduled",
    )
    assert a["planned_procedure_code"] == SCALING and a["chair_no"] == 1
    assert emr_bridge.ax_appointments(conn, date(2026, 12, 3), date(2026, 12, 9)) == []


def _call(conn, uid, at, patient, wants=1):
    path = conn.execute("SELECT file FROM pragma_database_list WHERE name = 'source'").fetchone()[
        0
    ]
    s = sqlite3.connect(path)
    s.execute(
        "INSERT INTO ax_calls (call_uid, source, patient_id, caller_phone, caller_name, direction,"
        " started_at, duration_sec, has_transcript, transcript, in_eval_pool)"
        " VALUES (?, 'generated', ?, '010-9', '모르는분', 'inbound', ?, 60, 1, '네', 0)",
        (uid, patient, at),
    )
    s.commit()
    s.close()
    classify_row(
        conn, uid, version="v1", labels={**LABELS, "wants_appt": wants}, summary=f"{uid} 요약"
    )


def test_unbooked_requests_definition(conn):
    _call(conn, "c-1", "2026-11-25 10:00:00", MISSED)  # 뒤에 원본 예약 → 빠짐
    _src_add(conn, (MISSED, DOCTOR, "2026-11-26 10:00:00", "treatment", RESIN, 1, "completed"))
    _call(conn, "c-2", "2026-11-26 15:00:00", NEW)  # 같은 환자 두 통화 → 최근 하나
    _call(conn, "c-3", "2026-11-27 11:00:00", NEW)
    _call(conn, "c-4", "2026-11-27 12:00:00", None)  # 차트 없음
    _call(conn, "c-5", "2026-11-28 10:00:00", MISSED, wants=0)  # 예약 원함 아님
    got = emr_bridge.unbooked_requests(conn, date(2026, 11, 24), date(2026, 11, 30))
    assert [r["call_uid"] for r in got] == ["c-4", "c-3"]
    nc = got[0]
    assert nc["no_chart"] is True and nc["patient_id"] is None and nc["chart_no"] is None
    assert nc["name"] == "모르는분" and nc["summary"] == "c-4 요약" and nc["in_eval_pool"] == 0
    assert got[1]["no_chart"] is False and got[1]["name"] == "이영희"
    # 통화 전 예약만 있는 환자는 남는다
    _call(conn, "c-6", "2026-11-27 09:00:00", MISSED)
    got = emr_bridge.unbooked_requests(conn, date(2026, 11, 24), date(2026, 11, 30))
    assert [r["call_uid"] for r in got] == ["c-4", "c-3", "c-6"]
    # 승인된 AX 예약 → 빠짐(초안은 아님)
    d = _draft(conn, f"{WED} 10:00")
    assert len(emr_bridge.unbooked_requests(conn, date(2026, 11, 24), date(2026, 11, 30))) == 3
    _approve(conn, d["id"])
    got = emr_bridge.unbooked_requests(conn, date(2026, 11, 24), date(2026, 11, 30))
    assert [r["call_uid"] for r in got] == ["c-4", "c-6"]
    # 기간 밖 통화는 없다
    assert emr_bridge.unbooked_requests(conn, date(2026, 11, 28), date(2026, 11, 30)) == []


def test_slot_off_grid(conn):
    assert _check(conn, f"{WED} 10:10") == ["off_slot"]
    assert _check(conn, f"{WED} 10:00:37") == ["off_slot"]
    assert _check(conn, f"{WED} 10:30") == []
    assert _draft(conn, f"{WED} 10:10") == {"error": ["off_slot"]}


def test_slot_holiday_is_closed(conn):
    path = conn.execute("SELECT file FROM pragma_database_list WHERE name = 'source'").fetchone()[
        0
    ]
    s = sqlite3.connect(path)
    s.execute(
        "UPDATE ax_master_values SET value = '[\"2026-12-25\"]' WHERE name = 'bank_holidays'"
    )
    s.commit()
    s.close()
    assert _check(conn, "2026-12-25 10:00") == ["closed_day"]  # 금요일 공휴일
    assert _check(conn, "2026-12-24 10:00") == ["closed_day"]  # 목요일 휴진(그대로)
    assert _check(conn, "2026-12-23 10:00") == []


def test_approve_locks_then_second_connection_rejected(conn, tmp_path):
    a = _draft(conn, f"{WED} 10:00", code=SCALING, staff=HYGIENIST, patient=MISSED)
    b = _draft(conn, f"{WED} 10:00", code=SCALING, staff=HYGIENIST, patient=NEW)
    conn.commit()
    other = db.connect(tmp_path / "a.sqlite", tmp_path / "s.sqlite")
    try:
        assert _approve(conn, a["id"])["ok"] is True  # 아직 커밋 전 — 쓰기 잠금을 쥐고 있다
        other.execute("PRAGMA busy_timeout = 0")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            _approve(other, b["id"])  # 상태 확인·재검사 전에 막힌다
        assert not other.in_transaction
        conn.commit()
        assert _approve(other, b["id"]) == {"ok": False, "reasons": ["staff_busy"]}
        other.commit()
    finally:
        other.close()
    row = conn.execute("SELECT status FROM ax_emr_writes WHERE id = ?", (b["id"],)).fetchone()
    assert row[0] == "rejected"
