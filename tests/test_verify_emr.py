"""verify 정형(emr_*) 필수 검사가 위반을 실제로 잡는지 — 작은 정상 DB를 만들고 한 곳씩 깨뜨린다."""
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from generator import verify
from generator.master import PROCEDURES
from generator.verify import Ctx

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = (ROOT / "shared" / "schema.sql").read_text(encoding="utf-8")

CTX = Ctx(today=date(2026, 12, 1), holidays=frozenset({date(2026, 11, 25)}), reschedule_rate=0.05)

# 2026-12-01(화) 기준. 10/06·11/03·11/17·11/24 화, 11/18 수, 12/05 토
SEED = """
INSERT INTO emr_clinic VALUES ('테스트치과', 4, '{}');
INSERT INTO emr_staff VALUES (1, '원장', 'doctor', 1), (2, '위생사', 'hygienist', 1), (3, '실장', 'consultant', 1);
INSERT INTO emr_families VALUES (1, '김');
INSERT INTO emr_patients (id, chart_no, name, birth_date, sex, phone, family_id, guardian_patient_id, registered_at) VALUES
  (1, 'C-00001', '김철수', '1980-05-10', 'M', '010-0000-0001', 1, NULL, '2025-01-01'),
  (2, 'C-00002', '김하늘', '2018-03-01', 'F', '010-0000-0002', 1, 1, '2025-01-01');
INSERT INTO emr_appointments (id, patient_id, staff_id, chair_no, scheduled_at, duration_min, type,
                              planned_procedure_code, status, rescheduled_from_id, created_at, cancelled_at) VALUES
  (1, 1, 1, 1,    '2026-10-06 10:00:00', 60, 'treatment', 'U0008', 'completed', NULL, '2026-09-30 11:00:00', NULL),
  (2, 1, 2, 2,    '2026-11-03 10:00:00', 30, 'hygiene',   'U0001', 'completed', NULL, '2026-10-20 11:00:00', NULL),
  (3, 2, 1, 1,    '2026-11-17 10:00:00', 30, 'treatment', 'U0005', 'cancelled', NULL, '2026-11-01 11:00:00', '2026-11-14 09:00:00'),
  (4, 2, 1, 1,    '2026-11-18 10:00:00', 30, 'treatment', 'U0005', 'completed', 3,    '2026-11-14 09:00:00', NULL),
  (5, 1, 1, 1,    '2026-12-01 14:00:00', 60, 'treatment', 'U0008', 'scheduled', NULL, '2026-11-20 11:00:00', NULL),
  (6, 2, 2, 2,    '2026-12-05 10:00:00', 30, 'hygiene',   'U0001', 'scheduled', NULL, '2026-11-20 11:00:00', NULL),
  (7, 1, 3, NULL, '2026-11-24 15:00:00', 30, 'consult',   'U0019', 'noshow',    NULL, '2026-11-20 11:00:00', NULL);
INSERT INTO emr_treatments VALUES
  (1, 1, 1, 'U0008', '26', '2026-10-06 10:00:00', 120000, 84000, 36000),
  (2, 2, 1, 'U0001', '00', '2026-11-03 10:00:00', 60000, 42000, 18000),
  (3, 4, 2, 'U0005', '55', '2026-11-18 10:00:00', 80000, 0, 80000);
INSERT INTO emr_payments VALUES
  (1, 1, 1, 'visit', 36000, 20000, '2026-10-06 11:00:00', 'card'),
  (2, 1, 2, 'visit', 18000, 18000, '2026-11-03 10:30:00', 'card'),
  (3, 1, 2, 'settlement', 0, 16000, '2026-11-03 10:31:00', 'card'),
  (4, 2, 4, 'visit', 80000, 80000, '2026-11-18 10:30:00', 'cash');
INSERT INTO emr_insurance_usage VALUES (1, 'scaling', 2026, 1, 1);
INSERT INTO emr_claims VALUES
  (1, 1, 1, 'C-00001', '26', '2026-10', 'monthly', '2026-10-06', 'U0008', 84000, '2026-11-05 09:00:00', 'submitted', NULL, NULL),
  (2, 2, 1, 'C-00001', '00', '2026-11', 'monthly', '2026-11-03', 'U0001', 42000, NULL, 'pending', NULL, NULL);
INSERT INTO emr_messages VALUES
  (1, 1, 'out', '2026-11-02 18:00:00', '내일 예약 안내', 'reminder', 2),
  (2, 1, 'in',  '2026-11-02 18:10:00', '네', 'reply', 2);
"""


def make_db(tmp_path, relaxed=False) -> sqlite3.Connection:
    """relaxed: 스키마 제약(tooth_no NOT NULL, 대조 키 UNIQUE)을 풀어 verify가 따로 잡는지 본다."""
    schema = SCHEMA
    if relaxed:
        assert schema.count("tooth_no       TEXT NOT NULL") == 2
        schema = schema.replace("tooth_no       TEXT NOT NULL", "tooth_no       TEXT")
        schema = schema.replace("CREATE UNIQUE INDEX", "CREATE INDEX")
    conn = sqlite3.connect(tmp_path / "app.sqlite")
    conn.row_factory = sqlite3.Row
    conn.executescript(schema)
    cols = list(PROCEDURES[0])
    conn.executemany(f"INSERT INTO emr_procedures ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                     [tuple(p[c] for c in cols) for p in PROCEDURES])
    conn.executescript(SEED)
    return conn


def appt(conn, scheduled_at, type="treatment", chair=1, patient=2, duration=30, status="completed"):
    staff = {"treatment": 1, "hygiene": 2, "consult": 3}[type]
    conn.execute(
        "INSERT INTO emr_appointments (patient_id, staff_id, chair_no, scheduled_at, duration_min, type,"
        " planned_procedure_code, status, created_at) VALUES (?, ?, ?, ?, ?, ?, 'U0008', ?, '2026-01-01 09:00:00')",
        (patient, staff, chair, scheduled_at, duration, type, status),
    )


def test_valid_db_passes_every_required_check(tmp_path):
    conn = make_db(tmp_path)
    failures = {fn.__name__: fn(conn, CTX) for fn in verify.REQUIRED}
    assert {k: v for k, v in failures.items() if v} == {}
    assert verify.run(conn, CTX) is True


# (깨뜨리는 SQL, 잡아야 하는 검사)
BREAKS = {
    "fk_dangling": ("UPDATE emr_treatments SET appointment_id = 999 WHERE id = 3", "check_foreign_keys"),
    "today_completed": ("UPDATE emr_appointments SET status = 'completed' WHERE id = 5", "check_status_from_today"),
    "future_noshow": ("UPDATE emr_appointments SET status = 'noshow' WHERE id = 6", "check_status_from_today"),
    "payment_after_today": ("UPDATE emr_payments SET paid_at = '2026-12-03 10:00:00' WHERE id = 4", "check_no_records_after_today"),
    "treatment_after_today": ("UPDATE emr_treatments SET performed_at = '2026-12-05 10:00:00' WHERE id = 3", "check_no_records_after_today"),
    "message_future": ("UPDATE emr_messages SET sent_at = '2026-12-02 09:00:00' WHERE id = 2", "check_timestamps_not_future"),
    "submitted_future": ("UPDATE emr_claims SET submitted_at = '2026-12-02 09:00:00' WHERE id = 1", "check_timestamps_not_future"),
    "last_month_submitted": ("UPDATE emr_claims SET status = 'submitted', submitted_at = '2026-12-01 09:00:00' WHERE id = 2", "check_last_month_claims_pending"),
    "recent_month_paid": ("UPDATE emr_claims SET status = 'paid', paid_amount = 84000 WHERE id = 1", "check_submitted_months_no_result"),
    "recent_month_adjust_code": ("UPDATE emr_claims SET adjust_code = 'A01' WHERE id = 1", "check_submitted_months_no_result"),
    "planned_null": ("UPDATE emr_appointments SET planned_procedure_code = NULL WHERE id = 7", "check_planned_procedure"),
    "no_reschedule": ("UPDATE emr_appointments SET rescheduled_from_id = NULL WHERE id = 4", "check_reschedule_ratio"),
    "performed_mismatch": ("UPDATE emr_treatments SET performed_at = '2026-11-18 11:00:00' WHERE id = 3", "check_performed_at"),
    "service_date_mismatch": ("UPDATE emr_claims SET service_date = '2026-10-07' WHERE id = 1", "check_service_date"),
    "uninsured_claimed": (
        "INSERT INTO emr_claims VALUES (3, 3, 2, 'C-00002', '55', '2026-11', 'monthly', '2026-11-18', 'U0005', 0, NULL, 'pending', NULL, NULL)",
        "check_uninsured_not_claimed",
    ),
    "scaling_twice": (
        "INSERT INTO emr_claims VALUES (3, 2, 1, 'C-00001', '00', '2026-05', 'monthly', '2026-05-12', 'U0001', 42000, '2026-06-05 09:00:00', 'paid', 42000, NULL)",
        "check_scaling_limit",
    ),
    "scaling_minor": (
        "INSERT INTO emr_claims VALUES (3, 3, 2, 'C-00002', '00', '2026-11', 'monthly', '2026-11-18', 'U0001', 42000, NULL, 'pending', NULL, NULL)",
        "check_scaling_limit",
    ),
    "overpaid": ("UPDATE emr_payments SET amount_paid = 40000 WHERE id = 1", "check_balance_non_negative"),
    "settled_before_debt": ("UPDATE emr_payments SET paid_at = '2026-10-01 10:00:00' WHERE id = 3", "check_balance_non_negative"),
    "visit_on_noshow": ("UPDATE emr_payments SET appointment_id = 7 WHERE id = 2", "check_visit_payment_completed"),
    "visit_without_appt": ("UPDATE emr_payments SET appointment_id = NULL WHERE id = 2", "check_visit_payment_completed"),
    "real_phone": ("UPDATE emr_patients SET phone = '010-1234-5678' WHERE id = 2", "check_fake_phone"),
    # 영업시간·휴진일 (상담 예약 7번을 옮긴다)
    "thursday": ("UPDATE emr_appointments SET scheduled_at = '2026-11-19 10:00:00' WHERE id = 7", "check_business_hours"),
    "sunday": ("UPDATE emr_appointments SET scheduled_at = '2026-11-22 10:00:00' WHERE id = 7", "check_business_hours"),
    "holiday": ("UPDATE emr_appointments SET scheduled_at = '2026-11-25 10:00:00' WHERE id = 7", "check_business_hours"),
    "before_open": ("UPDATE emr_appointments SET scheduled_at = '2026-11-24 09:00:00' WHERE id = 7", "check_business_hours"),
    "into_lunch": ("UPDATE emr_appointments SET scheduled_at = '2026-11-24 12:30:00', duration_min = 60 WHERE id = 7", "check_business_hours"),
    "past_close": ("UPDATE emr_appointments SET scheduled_at = '2026-11-24 18:00:00', duration_min = 60 WHERE id = 7", "check_business_hours"),
    "saturday_late": ("UPDATE emr_appointments SET scheduled_at = '2026-11-28 13:00:00', duration_min = 60 WHERE id = 7", "check_business_hours"),
}


@pytest.mark.parametrize("case", BREAKS)
def test_break_is_caught(tmp_path, case):
    sql, check = BREAKS[case]
    conn = make_db(tmp_path)
    conn.execute(sql)
    assert getattr(verify, check)(conn, CTX), f"{check}가 {case}를 못 잡음"


def test_doctor_slots_weekday(tmp_path):
    conn = make_db(tmp_path)
    for i in range(17):  # 60분 × 17 = 34슬롯 > 32
        appt(conn, "2026-11-24 10:00:00", chair=i, duration=60)
    assert verify.check_doctor_slots(conn, CTX)


def test_doctor_slots_saturday(tmp_path):
    conn = make_db(tmp_path)
    for i in range(9):  # 60분 × 9 = 18슬롯 > 16
        appt(conn, "2026-11-28 10:00:00", chair=i, duration=60)
    assert verify.check_doctor_slots(conn, CTX)


def test_doctor_slots_ignore_cancelled(tmp_path):
    conn = make_db(tmp_path)
    for i in range(17):
        appt(conn, "2026-11-24 10:00:00", chair=i, duration=60, status="cancelled")
    assert verify.check_doctor_slots(conn, CTX) == []


def test_chair_overlap(tmp_path):
    conn = make_db(tmp_path)
    appt(conn, "2026-10-06 10:30:00", chair=1)  # 1번 예약(10:00~11:00)과 같은 체어
    assert verify.check_chair_overlap(conn, CTX)


def test_chair_overlap_ignores_cancelled(tmp_path):
    conn = make_db(tmp_path)
    appt(conn, "2026-10-06 10:30:00", chair=1, status="cancelled")
    assert verify.check_chair_overlap(conn, CTX) == []


def test_three_treatments_at_once(tmp_path):
    conn = make_db(tmp_path)
    appt(conn, "2026-10-06 10:30:00", chair=2)
    appt(conn, "2026-10-06 10:30:00", chair=3)
    assert verify.check_concurrency(conn, CTX)


def test_hygiene_plus_treatment_over_three(tmp_path):
    conn = make_db(tmp_path)
    appt(conn, "2026-10-06 10:30:00", chair=2)  # 1번 예약과 합쳐 10:30 슬롯에 treatment 2 + hygiene 2
    appt(conn, "2026-10-06 10:30:00", type="hygiene", chair=3)
    appt(conn, "2026-10-06 10:30:00", type="hygiene", chair=4)
    assert verify.check_concurrency(conn, CTX)


def test_consult_does_not_count_for_concurrency(tmp_path):
    conn = make_db(tmp_path)
    appt(conn, "2026-10-06 10:00:00", chair=2)
    appt(conn, "2026-10-06 10:00:00", type="hygiene", chair=3)
    appt(conn, "2026-10-06 10:00:00", type="consult", chair=None)
    assert verify.check_concurrency(conn, CTX) == []


def test_patient_twice_same_day(tmp_path):
    conn = make_db(tmp_path)
    appt(conn, "2026-10-06 15:00:00", type="hygiene", chair=2, patient=1)
    assert verify.check_patient_same_day(conn, CTX)


def test_tooth_no_null(tmp_path):
    conn = make_db(tmp_path, relaxed=True)
    conn.execute("UPDATE emr_treatments SET tooth_no = NULL WHERE id = 2")
    conn.execute("UPDATE emr_claims SET tooth_no = NULL WHERE id = 2")
    assert len(verify.check_tooth_no_not_null(conn, CTX)) == 2


def test_claim_key_duplicate(tmp_path):
    conn = make_db(tmp_path, relaxed=True)
    conn.execute(
        "INSERT INTO emr_claims VALUES (3, 1, 1, 'C-00001', '26', '2026-10', 'supplementary', '2026-10-06', 'U0008',"
        " 84000, '2026-11-05 09:00:00', 'submitted', NULL, NULL)"
    )
    assert verify.check_claim_key_unique(conn, CTX)


def test_main_exit_codes(tmp_path, capsys):
    conn = make_db(tmp_path)
    conn.commit()
    db = str(tmp_path / "app.sqlite")
    assert verify.main(["--db", db]) == 0

    conn.execute("UPDATE emr_patients SET phone = '010-1234-5678' WHERE id = 2")
    conn.commit()
    assert verify.main(["--db", db]) == 1
    out = capsys.readouterr().out
    assert "check_fake_phone" in out and "1건" in out and "010-1234-5678" in out


def test_warning_failure_does_not_exit(tmp_path, monkeypatch):
    conn = make_db(tmp_path)
    monkeypatch.setattr(verify, "WARNINGS", [lambda conn, ctx: [{"x": 1}]])
    assert verify.run(conn, CTX) is True


def test_missing_db_exits(tmp_path):
    assert verify.main(["--db", str(tmp_path / "nope.sqlite")]) == 1
