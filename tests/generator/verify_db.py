"""verify 테스트용 작은 정상 DB — 테스트가 한 곳씩 깨뜨려 검사가 잡는지 본다."""

import json
import sqlite3
from datetime import date
from pathlib import Path

from generator.clinic import master_values_rows
from generator.doc_catalog import CLAIM_HEADINGS
from generator.master import ALERT_TYPES, DIAGNOSES, PROCEDURES
from generator.verify import Ctx

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = (ROOT / "shared" / "source_schema.sql").read_text(encoding="utf-8")
TRUTH_SCHEMA = (ROOT / "ground_truth" / "schema.sql").read_text(encoding="utf-8")

CTX = Ctx(
    today=date(2026, 12, 1),
    holidays=frozenset({date(2026, 11, 25)}),
    visits_per_day=26,
    range_start=date(2026, 10, 1),
    range_end=date(2026, 12, 15),
    call_slots=4,
    eval_pool=2,
    blind_pool=1,
    documents_core=3,
    documents_distractor=3,
    link_failure_days=0,
    ledger_errors={},
    recon_mismatches={},
    ledger_debt_calls=0,
)

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
                              planned_procedure_code, status, rescheduled_from_id, booked_via, created_at, cancelled_at, chair_no, plan_item_id) VALUES
  (1, 1, 1, '2026-10-06 10:00:00', 60, 'treatment', 'U0008', 'completed', NULL, 'phone', '2026-10-02 11:00:00', NULL, 1, 1),
  (2, 1, 2, '2026-11-03 10:00:00', 30, 'hygiene',   'U0001', 'completed', NULL, 'desk',  '2026-10-06 11:00:00', NULL, 2, NULL),
  (3, 2, 1, '2026-11-17 10:00:00', 30, 'treatment', 'U0005', 'cancelled', NULL, 'phone', '2026-10-30 11:00:00', '2026-11-14 10:00:00', 1, NULL),
  (4, 2, 1, '2026-11-18 10:00:00', 30, 'treatment', 'U0005', 'completed', 3,    'phone', '2026-11-14 10:00:00', NULL, 1, NULL),
  (5, 1, 1, '2026-12-01 14:00:00', 60, 'treatment', 'U0008', 'scheduled', NULL, 'phone', '2026-11-20 11:00:00', NULL, 1, 2),
  (6, 2, 2, '2026-12-05 10:00:00', 30, 'hygiene',   'U0002', 'scheduled', NULL, 'phone', '2026-11-20 11:00:00', NULL, 2, NULL),
  (7, 1, 3, '2026-11-24 15:00:00', 30, 'consult',   'U0019', 'noshow',    NULL, 'phone', '2026-11-20 11:00:00', NULL, NULL, NULL);
-- 2번 예약은 리마인드(문자 1)에 '네'라고 답했다(문자 2) — confirmed(리마인드 응답)
UPDATE emr_appointments SET confirmed = 1 WHERE id = 2;
-- 치료계획: 1번 예약(신경치료 코스 첫 내원)이 만든 계획 — 항목 1 완료, 항목 2는 5번 예약(12/01), 항목 3 크라운 열림
INSERT INTO emr_treatment_plans VALUES (1, 1, '2026-10-06 11:00:00', 1);
INSERT INTO emr_treatment_plan_items VALUES
  (1, 1, 1, 'U0008', '26', 36000, 'done'),
  (2, 1, 2, 'U0008', '26', 36000, 'open'),
  (3, 1, 3, 'U0009', '26', 550000, 'open');
INSERT INTO emr_treatments VALUES
  (1, 1, 1, 'U0008', '26', '2026-10-06 10:00:00', 120000, 84000, 36000),
  (2, 2, 1, 'U0001', '00', '2026-11-03 10:00:00', 60000, 42000, 18000),
  (3, 4, 2, 'U0005', '16', '2026-11-18 10:00:00', 80000, 0, 80000);
-- 카드 수납은 연동 단말기 승인번호(시드 창에는 연동 실패일·장부 실수 없음 — 메타 0)
INSERT INTO emr_payments (id, patient_id, appointment_id, kind, amount_due, amount_paid, paid_at, method, approval_no) VALUES
  (1, 1, 1, 'visit', 36000, 20000, '2026-10-06 11:00:00', 'card', '10000001'),
  (2, 1, 2, 'visit', 18000, 18000, '2026-11-03 10:30:00', 'card', '10000002'),
  (3, 1, 2, 'settlement', 0, 16000, '2026-11-03 10:31:00', 'card', '10000003'),
  (4, 2, 4, 'visit', 80000, 80000, '2026-11-18 10:30:00', 'cash', NULL);
-- 재무(8b): 카드 수납 3건 = 가온카드 승인(정산 2영업일·율 1.2%), 10/06분 10/08·11/03분 11/05 입금,
-- 11/18 현금은 다음 월요일 11/23 입금. 확정 월 청구가 없어 공단 입금 없음
INSERT INTO fin_card_sales (id, approved_at, issuer, amount, approval_no, cancel_of) VALUES
  (1, '2026-10-06 11:00:00', '가온카드', 20000, '10000001', NULL),
  (2, '2026-11-03 10:30:00', '가온카드', 18000, '10000002', NULL),
  (3, '2026-11-03 10:31:00', '가온카드', 16000, '10000003', NULL);
INSERT INTO fin_card_deposits (id, deposited_on, issuer, sales_date, sales_total, fee, amount) VALUES
  (1, '2026-10-08', '가온카드', '2026-10-06', 20000, 240, 19760),
  (2, '2026-11-05', '가온카드', '2026-11-03', 34000, 408, 33592);
INSERT INTO fin_bank_transactions (id, occurred_at, direction, amount, memo) VALUES
  (1, '2026-10-08 08:00:00', 'in', 19760, '가온카드'),
  (2, '2026-11-05 08:00:00', 'in', 33592, '가온카드'),
  (3, '2026-11-23 13:10:00', 'in', 80000, '현금 입금');
INSERT INTO emr_claims (id, treatment_id, patient_id, chart_no, tooth_no, claim_month, service_date, procedure_code, claim_amount, submitted_at, status, paid_amount, adjust_code, adjust_note) VALUES
  (1, 1, 1, 'C-00001', '26', '2026-10', '2026-10-06', 'U0008', 84000, '2026-11-02 10:00:00', 'submitted', NULL, NULL, NULL),
  (2, 2, 1, 'C-00001', '00', '2026-11', '2026-11-03', 'U0001', 42000, NULL, 'pending', NULL, NULL, NULL);
-- 차트: 1·2는 템플릿, 3(소아 레진)은 서술 디코이
INSERT INTO emr_chart_notes (treatment_id, dx_code, note) VALUES
  (1, 'D02', '26번 치아 치수염으로 신경치료를 진행했습니다. 진료 중 마취가 잘 안 됨을 확인했습니다.'),
  (2, 'D06', '치석이 많아 스케일링을 시행했습니다.'),
  (3, 'D01', '16번 치아 상아질 우식으로 레진 충전함. 보호자와 함께 내원.');
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
                      has_transcript, transcript, in_eval_pool, eval_sort_order, in_blind_pool, blind_sort_order) VALUES
  (1, 'c-0003', 'generated', 1, '010-0000-0001', '김철수', 'inbound',  '2026-10-02 11:00:00', 150, 1,
   '환자: 이가 너무 시려서요, 진료 받을 수 있을까요?
직원: 네, 10월 6일 오전 10시에 신경치료로 잡아 드릴게요.', 1, 1, 1, 1),
  (2, 'c-0001', 'generated', 1, '010-0000-0001', '김철수', 'outbound', '2026-10-20 15:00:00', 130, 1,
   '직원: 지난번 진료비가 일부 남아 있어 연락드렸어요.
환자: 아, 그게 왜 남았는지 모르겠네요. 좀 기분이 안 좋네요.', 0, NULL, 0, NULL),
  (3, 'c-0004', 'generated', 2, '010-0000-0002', '김철수', 'inbound',  '2026-11-14 10:00:00', 170, 1,
   '환자: 아이가 11월 17일에 가기로 했는데 하루 미룰 수 있을까요?
직원: 네, 11월 18일 오전 10시로 옮겨 드릴게요.', 0, NULL, 0, NULL),
  (4, 'c-0002', 'generated', NULL, '010-0000-9999', '박민수', 'inbound', '2026-11-27 16:00:00', 140, 1,
   '환자: 미백 하면 얼마 정도 나와요? 괜찮으면 날 잡고 싶어서요.
직원: 상담 후 안내드리는데, 이번 주에 한번 오실 수 있으세요?', 1, 2, 0, NULL);
"""

TRUTH_SEED = """
INSERT INTO truth_chart_notes (treatment_id, kind, is_special, requirement, evidence_present, is_decoy, facts) VALUES
  (1, 'template', 0, NULL, NULL, 0, '{}'),
  (2, 'template', 0, NULL, NULL, 0, '{}'),
  (3, 'narrative', 0, NULL, NULL, 1, '{"alerts": [], "cc": "이에 까만 점이 생겼어요", "discovered": [], "findings": null, "plan": null, "procedure": "레진", "requirement": null, "tooth": "16"}');
INSERT INTO truth_call_labels VALUES
  ('c-0003', 'book', '["book"]', 'patient', 'normal', 0, 0, 0, 1, 1, 0, NULL,
   '{"call_date": "2026-10-02", "appointment_id": 1, "appointment": {"date": "2026-10-06", "time": "10:00", "procedure": "신경치료"}, "patient": {"id": 1, "name": "김철수", "age": 46, "sex": "M"}, "recent_visits": [], "balance": 0, "next_appointment": null, "family": ["김하늘"], "alerts": ["고혈압"], "open_plan_items": []}',
   'dev'),
  ('c-0001', 'billing_q', '["billing_q", "complaint"]', 'patient', 'normal', 0, 1, 0, 0, 0, 1, NULL,
   '{"call_date": "2026-10-20", "patient": {"id": 1, "name": "김철수", "age": 46, "sex": "M"}, "recent_visits": [{"date": "2026-10-06", "procedure": "신경치료", "tooth": "26"}], "balance": 16000, "next_appointment": {"appointment_id": 2, "date": "2026-11-03", "time": "10:00", "procedure": "스케일링"}, "family": ["김하늘"], "alerts": ["고혈압", "마취가 잘 안 됨"], "open_plan_items": [{"procedure": "신경치료", "tooth": "26"}, {"procedure": "크라운", "tooth": "26"}]}',
   'silver'),
  ('c-0004', 'reschedule', '["reschedule"]', 'guardian', 'low', 0, 0, 0, 0, 1, 1, 'guardian_confused',
   '{"call_date": "2026-11-14", "appointment_id": 4, "appointment": {"date": "2026-11-18", "time": "10:00", "procedure": "레진", "from_date": "2026-11-17", "from_time": "10:00"}, "patient": {"id": 2, "name": "김하늘", "age": 8, "sex": "F"}, "recent_visits": [], "balance": 0, "next_appointment": {"appointment_id": 3, "date": "2026-11-17", "time": "10:00", "procedure": "레진"}, "family": ["김철수"], "alerts": ["협조 어려운 소아"], "open_plan_items": []}',
   'silver'),
  ('c-0002', 'price_q', '["price_q", "book"]', 'new_caller', 'normal', 0, 0, 1, 1, 0, 1, 'two_topics',
   '{"call_date": "2026-11-27", "topic_procedure": "미백"}',
   'test');
INSERT INTO truth_gen_meta VALUES
  ('seed', '20261201'), ('today', '2026-12-01'), ('range_start', '2026-10-01'), ('range_end', '2026-12-15'),
  ('call_slots', '4'), ('eval_pool', '2'), ('blind_pool', '1'), ('documents_core', '3'), ('documents_distractor', '3'),
  ('link_failure_days', '0'), ('ledger_errors', '{}'), ('recon_mismatches', '{}'), ('ledger_debt_calls', '0');
"""


def _fact(fid, text, *words, reason=None):
    return {"id": fid, "text": text, "words": list(words), "reason": reason}


# RAG 문서(9단계): 1 매뉴얼 / 2 비슷한 주제(1의 짝, 1의 사실 없음) / 3 최신 공지(4를 대체) / 4 옛 공지(생성 기간 전) /
# 5 청구 기준(사유마다 절 하나) / 6 타 병원(1의 주제를 다른 값으로)
_CLAIM_BODY = "# 청구 기준\n\n" + "\n\n".join(
    f"## {h}\n\n기준을 적는다." for h in CLAIM_HEADINGS.values()
)
DOCS = [
    (
        1,
        "manual",
        "예약 부도 관리",
        "1",
        None,
        "docs/manual/001.md",
        "# 예약 부도 관리\n\n예약 부도가 3회 쌓이면 3개월 동안 전화 예약만 받습니다.",
    ),
    (
        2,
        "manual",
        "예약 접수 기본",
        "1",
        None,
        "docs/manual/002.md",
        "# 예약 접수 기본\n\n전화로 예약을 받습니다.",
    ),
    (
        3,
        "notice",
        "미백 비용 변경",
        "2",
        "2026-09-01",
        "docs/notice/003.md",
        "미백 비용은 300,000원입니다.",
    ),
    (
        4,
        "notice",
        "미백 비용 변경",
        "1",
        "2025-01-02",
        "docs/notice/004.md",
        "미백 비용은 250,000원입니다.",
    ),
    (5, "claim_standard", "공통 청구 기준", "1", None, "docs/claim_standard/005.md", _CLAIM_BODY),
    (
        6,
        "external",
        "다른 치과 부도 규정",
        "1",
        None,
        "docs/external/006.md",
        "부도 2회면 6개월간 예약을 받지 않습니다.",
    ),
]
DOC_META = [  # (doc_key, kind, pair_doc_id, supersedes_doc_id, avoid_doc_ids, planted_facts) — 2는 3(미백 공지)의 사실도 피함
    (
        "manual-noshow",
        "core",
        None,
        None,
        [],
        [_fact("noshow.limit", "부도 3회 → 3개월 전화 예약만", "3회", "3개월")],
    ),
    ("sim-booking-basics", "similar", 1, None, [3], []),
    (
        "notice-whitening",
        "core",
        None,
        4,
        [],
        [_fact("notice.white", "미백 300,000원", "300,000원")],
    ),
    (
        "old-whitening",
        "old_version",
        3,
        None,
        [],
        [_fact("old.white", "미백 250,000원", "250,000원")],
    ),
    (
        "claim-all",
        "core",
        None,
        None,
        [],
        [_fact(f"claim.{r}.heading", h, f"## {h}", reason=r) for r, h in CLAIM_HEADINGS.items()],
    ),
    (
        "ext-noshow",
        "external",
        1,
        None,
        [],
        [_fact("ext.noshow", "부도 2회 → 6개월", "2회", "6개월")],
    ),
]


def make_db(tmp_path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "source.sqlite")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    cols = list(PROCEDURES[0])
    conn.executemany(
        f"INSERT INTO emr_procedures ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        [tuple(p[c] for c in cols) for p in PROCEDURES],
    )
    conn.executemany("INSERT INTO emr_diagnoses VALUES (?, ?)", sorted(DIAGNOSES.items()))
    conn.executemany(
        "INSERT INTO emr_alert_types VALUES (?, ?, ?)",
        [(code, a["label"], a["category"]) for code, a in ALERT_TYPES.items()],
    )
    conn.executemany(
        "INSERT INTO ax_master_values VALUES (?, ?)", master_values_rows(CTX.holidays)
    )
    conn.executescript(SEED)
    conn.executemany(
        "INSERT INTO ax_documents VALUES (?, ?, ?, ?, ?, ?, ?)", [d[:7] for d in DOCS]
    )
    truth = sqlite3.connect(tmp_path / "truth.sqlite")
    truth.executescript(TRUTH_SCHEMA + TRUTH_SEED)
    truth.executemany(
        "INSERT INTO truth_doc_meta VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                d[0],
                key,
                kind,
                int(kind != "core"),
                pair,
                sup,
                json.dumps(avoid),
                json.dumps(facts, ensure_ascii=False),
            )
            for d, (key, kind, pair, sup, avoid, facts) in zip(DOCS, DOC_META, strict=True)
        ],
    )
    truth.commit()
    truth.close()
    conn.execute("ATTACH DATABASE ? AS truth", (str(tmp_path / "truth.sqlite"),))
    return conn


def appt(
    conn,
    scheduled_at,
    type="treatment",
    patient=2,
    duration=30,
    status="completed",
    created_at="2026-10-02 10:00:00",
    chair=None,
):
    staff = {"treatment": 1, "hygiene": 2, "consult": 3}[type]
    if chair is None and type != "consult":
        chair = 3  # 고정 예약(체어 1·2)과 겹치지 않게
    conn.execute(
        "INSERT INTO emr_appointments (patient_id, staff_id, scheduled_at, duration_min, type,"
        " planned_procedure_code, status, booked_via, created_at, chair_no)"
        " VALUES (?, ?, ?, ?, ?, 'U0008', ?, 'phone', ?, ?)",
        (patient, staff, scheduled_at, duration, type, status, created_at, chair),
    )
