"""verify 테스트용 작은 정상 DB — 테스트가 한 곳씩 깨뜨려 검사가 잡는지 본다."""
import sqlite3
from datetime import date
from pathlib import Path

from generator.master import PROCEDURES
from generator.verify import Ctx

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = (ROOT / "shared" / "schema.sql").read_text(encoding="utf-8")
TRUTH_SCHEMA = (ROOT / "ground_truth" / "schema.sql").read_text(encoding="utf-8")

CTX = Ctx(today=date(2026, 12, 1), holidays=frozenset({date(2026, 11, 25)}), reschedule_rate=0.05,
          visits_per_day=26, range_start=date(2026, 10, 1), range_end=date(2026, 12, 15))

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
INSERT INTO ax_calls (id, call_uid, source, patient_id, caller_phone, direction, started_at, duration_sec, has_transcript) VALUES
  (1, 'c-0002', 'generated', 1, '010-0000-0001', 'inbound',  '2026-11-20 10:55:00', 95, 0),
  (2, 'c-0001', 'generated', NULL, '010-0000-9999', 'outbound', '2026-11-26 16:00:00', 140, 0);
"""

# 9~10월 심사 결과 정답 (가상 데이터 생성 설계 §2.4). extra_in_eob 줄은 장부에 없는 키
TRUTH_SEED = """
INSERT INTO truth_claims VALUES
  (1, 1, 'C-00001', '2026-10-06', 'U0008', '26', 84000, 'paid_full', 84000, NULL),
  (2, NULL, 'C-00001', '2026-10-06', 'U0018', '00', 24500, 'extra_in_eob', 24500, NULL);
INSERT INTO truth_gen_meta VALUES
  ('seed', '20261201'), ('scale', 'small'), ('range_start', '2026-10-01'), ('range_end', '2026-12-15');
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
    truth = sqlite3.connect(tmp_path / "truth.sqlite")
    truth.executescript(TRUTH_SCHEMA + TRUTH_SEED)
    truth.close()
    conn.execute("ATTACH DATABASE ? AS truth", (str(tmp_path / "truth.sqlite"),))
    return conn


def appt(conn, scheduled_at, type="treatment", chair=1, patient=2, duration=30, status="completed"):
    staff = {"treatment": 1, "hygiene": 2, "consult": 3}[type]
    conn.execute(
        "INSERT INTO emr_appointments (patient_id, staff_id, chair_no, scheduled_at, duration_min, type,"
        " planned_procedure_code, status, created_at) VALUES (?, ?, ?, ?, ?, ?, 'U0008', ?, '2026-01-01 09:00:00')",
        (patient, staff, chair, scheduled_at, duration, type, status),
    )
