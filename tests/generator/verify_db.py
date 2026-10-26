"""verify 테스트용 작은 정상 DB — 테스트가 한 곳씩 깨뜨려 검사가 잡는지 본다."""
import sqlite3
from datetime import date
from pathlib import Path

from generator.master import ALERT_TYPES, DIAGNOSES, PROCEDURES
from generator.verify import Ctx

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = (ROOT / "shared" / "schema.sql").read_text(encoding="utf-8")
TRUTH_SCHEMA = (ROOT / "ground_truth" / "schema.sql").read_text(encoding="utf-8")

CTX = Ctx(today=date(2026, 12, 1), holidays=frozenset({date(2026, 11, 25)}), visits_per_day=26, range_start=date(2026, 10, 1), range_end=date(2026, 12, 15),
          call_slots=4, eval_pool=2, blind_pool=1)

# 2026-12-01(화) 기준. 10/06·11/03·11/17·11/24 화, 11/18 수, 12/05 토, 10/01·11/19 목(휴진)
SEED = """
INSERT INTO emr_clinic VALUES ('테스트치과', '{}');
INSERT INTO emr_staff VALUES (1, '원장', 'doctor', 1), (2, '위생사', 'hygienist', 1), (3, '실장', 'consultant', 1);
INSERT INTO emr_families VALUES (1, '김');
INSERT INTO emr_patients (id, chart_no, name, birth_date, sex, phone, family_id, guardian_patient_id, registered_at) VALUES
  (1, 'C-00001', '김철수', '1980-05-10', 'M', '010-0000-0001', 1, NULL, '2025-01-01'),
  (2, 'C-00002', '김하늘', '2018-03-01', 'F', '010-0000-0002', 1, 1, '2025-01-01');
-- 주의사항: 1번 구환(창 이전 내원 없음 → 문진 기록일 = 등록일) 고혈압, 2번 소아 협조 어려움(13세 생일에 끝남),
-- 1번 10/06 신경치료 중 마취 잘 안 됨 발견
INSERT INTO emr_patient_alerts (id, patient_id, code, source, recorded_at, appointment_id, ends_at) VALUES
  (1, 1, 'HTN', 'intake', '2025-01-01', NULL, NULL),
  (2, 2, 'CHILD_COOP', 'intake', '2025-01-01', NULL, '2031-03-01'),
  (3, 1, 'ANES_FAIL', 'visit', '2026-10-06', 1, NULL);
INSERT INTO emr_appointments (id, patient_id, staff_id, scheduled_at, duration_min, type,
                              planned_procedure_code, status, rescheduled_from_id, booked_via, created_at, cancelled_at) VALUES
  (1, 1, 1, '2026-10-06 10:00:00', 60, 'treatment', 'U0008', 'completed', NULL, 'phone', '2026-10-02 11:00:00', NULL),
  (2, 1, 2, '2026-11-03 10:00:00', 30, 'hygiene',   'U0001', 'completed', NULL, 'desk',  '2026-10-06 11:00:00', NULL),
  (3, 2, 1, '2026-11-17 10:00:00', 30, 'treatment', 'U0005', 'cancelled', NULL, 'phone', '2026-10-30 11:00:00', '2026-11-14 10:00:00'),
  (4, 2, 1, '2026-11-18 10:00:00', 30, 'treatment', 'U0005', 'completed', 3,    'phone', '2026-11-14 10:00:00', NULL),
  (5, 1, 1, '2026-12-01 14:00:00', 60, 'treatment', 'U0008', 'scheduled', NULL, 'phone', '2026-11-20 11:00:00', NULL),
  (6, 2, 2, '2026-12-05 10:00:00', 30, 'hygiene',   'U0002', 'scheduled', NULL, 'phone', '2026-11-20 11:00:00', NULL),
  (7, 1, 3, '2026-11-24 15:00:00', 30, 'consult',   'U0019', 'noshow',    NULL, 'phone', '2026-11-20 11:00:00', NULL);
INSERT INTO emr_treatments VALUES
  (1, 1, 1, 'U0008', '26', '2026-10-06 10:00:00', 120000, 84000, 36000),
  (2, 2, 1, 'U0001', '00', '2026-11-03 10:00:00', 60000, 42000, 18000),
  (3, 4, 2, 'U0005', '16', '2026-11-18 10:00:00', 80000, 0, 80000);
INSERT INTO emr_payments VALUES
  (1, 1, 1, 'visit', 36000, 20000, '2026-10-06 11:00:00', 'card'),
  (2, 1, 2, 'visit', 18000, 18000, '2026-11-03 10:30:00', 'card'),
  (3, 1, 2, 'settlement', 0, 16000, '2026-11-03 10:31:00', 'card'),
  (4, 2, 4, 'visit', 80000, 80000, '2026-11-18 10:30:00', 'cash');
INSERT INTO emr_claims VALUES
  (1, 1, 1, 'C-00001', '26', '2026-10', '2026-10-06', 'U0008', 84000, '2026-11-02 10:00:00', 'submitted', NULL, NULL, NULL),
  (2, 2, 1, 'C-00001', '00', '2026-11', '2026-11-03', 'U0001', 42000, NULL, 'pending', NULL, NULL, NULL);
-- 차트: 1·2는 템플릿, 3(소아 레진)은 서술 디코이
INSERT INTO emr_chart_notes (treatment_id, dx_code, note) VALUES
  (1, 'D02', '26번 치아 치수염으로 신경치료를 진행했습니다. 진료 중 마취가 잘 안 됨을 확인했습니다.'),
  (2, 'D06', '치석이 많아 스케일링을 시행했습니다.'),
  (3, 'D01', '16번 치아 상아질 우식으로 레진 충전을 했습니다. 보호자와 함께 내원했습니다.');
-- 문자(8단계): 2번 예약 리마인드(직전 진료일 11/02)와 답장, 1번 예약 확정, 7번 노쇼(11/24 15:00 상담 30분 뒤),
-- 1번 내원 미수(1번 수납 16,000원 미납) 다음 진료일 안내
INSERT INTO emr_messages VALUES
  (1, 1, 'out', '2026-11-02 18:00:00', '내일 예약 안내', 'reminder', 2),
  (2, 1, 'in',  '2026-11-02 18:10:00', '네', 'reply', 2),
  (3, 1, 'out', '2026-10-02 11:03:00', '예약 확정', 'appt_confirm', 1),
  (4, 1, 'out', '2026-11-24 15:40:00', '노쇼 안내', 'noshow', 7),
  (5, 1, 'out', '2026-10-07 10:00:00', '미수 안내', 'balance', 1);
-- 통화 슬롯(6단계): 1 예약 잡기(1번 예약 created_at) / 2 수납 안내 outbound(잔액 16,000원일 때) /
-- 3 변경(4번 예약 created_at, 소아라 보호자 김철수가 건다) / 4 처음 연락 가격 문의. 행 순서 = 통화 시각순, call_uid는 셔플
INSERT INTO ax_calls (id, call_uid, source, patient_id, caller_phone, caller_name, direction, started_at, duration_sec,
                      has_transcript, in_eval_pool, eval_sort_order, judge_status, in_blind_pool, blind_sort_order) VALUES
  (1, 'c-0003', 'generated', 1, '010-0000-0001', '김철수', 'inbound',  '2026-10-02 11:00:00', 150, 0, 1, 1, 'pending', 1, 1),
  (2, 'c-0001', 'generated', 1, '010-0000-0001', '김철수', 'outbound', '2026-10-20 15:00:00', 130, 0, 0, NULL, 'none', 0, NULL),
  (3, 'c-0004', 'generated', 2, '010-0000-0002', '김철수', 'inbound',  '2026-11-14 10:00:00', 170, 0, 0, NULL, 'none', 0, NULL),
  (4, 'c-0002', 'generated', NULL, '010-0000-9999', '박민수', 'inbound', '2026-11-27 16:00:00', 140, 0, 1, 2, 'pending', 0, NULL);
"""

# 9~10월 심사 결과 정답 (가상 데이터 생성 설계 §2.4). extra_in_claim_review 줄은 장부에 없는 키
TRUTH_SEED = """
INSERT INTO truth_claims VALUES
  (1, 1, 'C-00001', '2026-10-06', 'U0008', '26', 84000, 'paid_full', 84000, NULL, NULL),
  (2, NULL, 'C-00001', '2026-10-06', 'U0018', '00', 24500, 'extra_in_claim_review', 24500, NULL, NULL);
INSERT INTO truth_chart_notes (treatment_id, kind, is_special, requirement, evidence_present, is_decoy, facts) VALUES
  (1, 'template', 0, NULL, NULL, 0, '{}'),
  (2, 'template', 0, NULL, NULL, 0, '{}'),
  (3, 'narrative', 0, NULL, NULL, 1, '{}');
INSERT INTO truth_call_labels VALUES
  ('c-0003', 'book', '["book"]', 'patient', 'normal', 0, 0, 0, 1, 1, 0, NULL,
   '{"call_date": "2026-10-02", "appointment_id": 1, "appointment": {"date": "2026-10-06", "time": "10:00", "procedure": "신경치료"}, "patient": {"id": 1, "name": "김철수", "age": 46, "sex": "M"}, "recent_visits": [], "balance": 0, "next_appointment": {"appointment_id": 1, "date": "2026-10-06", "time": "10:00", "procedure": "신경치료"}, "family": ["김하늘"], "alerts": ["고혈압"]}',
   'dev'),
  ('c-0001', 'billing_q', '["billing_q", "complaint"]', 'patient', 'normal', 0, 1, 0, 0, 0, 1, NULL,
   '{"call_date": "2026-10-20", "patient": {"id": 1, "name": "김철수", "age": 46, "sex": "M"}, "recent_visits": [{"date": "2026-10-06", "procedure": "신경치료", "tooth": "26"}], "balance": 16000, "next_appointment": {"appointment_id": 2, "date": "2026-11-03", "time": "10:00", "procedure": "스케일링"}, "family": ["김하늘"], "alerts": ["고혈압", "마취가 잘 안 됨"]}',
   'silver'),
  ('c-0004', 'reschedule', '["reschedule"]', 'guardian', 'low', 0, 0, 0, 0, 1, 1, 'guardian_confused',
   '{"call_date": "2026-11-14", "appointment_id": 4, "appointment": {"date": "2026-11-18", "time": "10:00", "procedure": "레진", "from_date": "2026-11-17", "from_time": "10:00"}, "patient": {"id": 2, "name": "김하늘", "age": 8, "sex": "F"}, "recent_visits": [], "balance": 0, "next_appointment": {"appointment_id": 4, "date": "2026-11-18", "time": "10:00", "procedure": "레진"}, "family": ["김철수"], "alerts": ["협조 어려운 소아"]}',
   'silver'),
  ('c-0002', 'price_q', '["price_q", "book"]', 'new_caller', 'normal', 0, 0, 1, 1, 0, 1, 'two_topics',
   '{"call_date": "2026-11-27", "topic_procedure": "미백"}',
   'test');
INSERT INTO truth_gen_meta VALUES
  ('seed', '20261201'), ('range_start', '2026-10-01'), ('range_end', '2026-12-15'),
  ('call_slots', '4'), ('eval_pool', '2'), ('blind_pool', '1');
"""


def make_db(tmp_path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "app.sqlite")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    cols = list(PROCEDURES[0])
    conn.executemany(f"INSERT INTO emr_procedures ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                     [tuple(p[c] for c in cols) for p in PROCEDURES])
    conn.executemany("INSERT INTO emr_diagnoses VALUES (?, ?)", sorted(DIAGNOSES.items()))
    conn.executemany("INSERT INTO emr_alert_types VALUES (?, ?, ?)",
                     [(code, a["label"], a["category"]) for code, a in ALERT_TYPES.items()])
    conn.executescript(SEED)
    truth = sqlite3.connect(tmp_path / "truth.sqlite")
    truth.executescript(TRUTH_SCHEMA + TRUTH_SEED)
    truth.close()
    conn.execute("ATTACH DATABASE ? AS truth", (str(tmp_path / "truth.sqlite"),))
    return conn


def appt(conn, scheduled_at, type="treatment", patient=2, duration=30, status="completed",
         created_at="2026-10-02 10:00:00"):
    staff = {"treatment": 1, "hygiene": 2, "consult": 3}[type]
    conn.execute(
        "INSERT INTO emr_appointments (patient_id, staff_id, scheduled_at, duration_min, type,"
        " planned_procedure_code, status, booked_via, created_at) VALUES (?, ?, ?, ?, ?, 'U0008', ?, 'phone', ?)",
        (patient, staff, scheduled_at, duration, type, status, created_at),
    )
