"""verify 필수 검사(emr_* · ax_calls · truth_claims)가 위반을 실제로 잡는지 — 정상 DB를 한 곳씩 깨뜨린다."""

import sqlite3
from dataclasses import replace
from datetime import date

import pytest

from generator import verify
from tests.generator.verify_db import CTX, ROOT, appt, make_db


def test_valid_db_passes_every_required_check(tmp_path):
    conn = make_db(tmp_path)
    failures = {fn.__name__: fn(conn, CTX) for fn in verify.REQUIRED}
    assert {k: v for k, v in failures.items() if v} == {}
    assert verify.run(conn, CTX) is True


PRE = (
    "INSERT INTO emr_appointments (patient_id, staff_id, scheduled_at, duration_min, type,"
    " planned_procedure_code, status, booked_via, created_at) VALUES "
)


def _pre(patient, at, type="hygiene", status="completed", created="2026-09-01 10:00:00"):
    staff = {"hygiene": 2, "treatment": 1}[type]
    return f"({patient}, {staff}, '{at}', 30, '{type}', 'U0001', '{status}', 'phone', '{created}')"


# (깨뜨리는 SQL, 잡아야 하는 검사)
BREAKS = {
    # 시드 소아(2번, 2018년생)의 11/18 레진 '16' — 6세 미만이면 영구치 번호의 치아 시술이 불가능
    "tooth_treatment_too_young": (
        "UPDATE emr_patients SET birth_date = '2022-01-01' WHERE id = 2",
        "check_tooth_treatment_age",
    ),
    "registered_as_infant": (
        "UPDATE emr_patients SET registered_at = '2018-06-01' WHERE id = 2",
        "check_registration_age",
    ),
    "fk_dangling": (
        "UPDATE emr_treatments SET appointment_id = 999 WHERE id = 3",
        "check_foreign_keys",
    ),
    "today_completed": (
        "UPDATE emr_appointments SET status = 'completed' WHERE id = 5",
        "check_status_from_today",
    ),
    "future_noshow": (
        "UPDATE emr_appointments SET status = 'noshow' WHERE id = 6",
        "check_status_from_today",
    ),
    "payment_after_today": (
        "UPDATE emr_payments SET paid_at = '2026-12-03 10:00:00' WHERE id = 4",
        "check_no_records_after_today",
    ),
    "treatment_after_today": (
        "UPDATE emr_treatments SET performed_at = '2026-12-05 10:00:00' WHERE id = 3",
        "check_no_records_after_today",
    ),
    "message_future": (
        "UPDATE emr_messages SET sent_at = '2026-12-02 09:00:00' WHERE id = 2",
        "check_timestamps_not_future",
    ),
    "submitted_future": (
        "UPDATE emr_claims SET submitted_at = '2026-12-02 09:00:00' WHERE id = 1",
        "check_timestamps_not_future",
    ),
    "last_month_submitted": (
        "UPDATE emr_claims SET status = 'submitted', submitted_at = '2026-12-01 09:00:00' WHERE id = 2",
        "check_last_month_claims_pending",
    ),
    "recent_month_paid": (
        "UPDATE emr_claims SET status = 'paid', paid_amount = 84000 WHERE id = 1",
        "check_submitted_months_no_result",
    ),
    "recent_month_adjust_code": (
        "UPDATE emr_claims SET adjust_code = 'A01' WHERE id = 1",
        "check_submitted_months_no_result",
    ),
    "recent_month_adjust_note": (
        "UPDATE emr_claims SET adjust_note = '재시행 간격 미충족' WHERE id = 1",
        "check_submitted_months_no_result",
    ),
    "performed_mismatch": (
        "UPDATE emr_treatments SET performed_at = '2026-11-18 11:00:00' WHERE id = 3",
        "check_performed_at",
    ),
    "service_date_mismatch": (
        "UPDATE emr_claims SET service_date = '2026-10-07' WHERE id = 1",
        "check_service_date",
    ),
    "uninsured_claimed": (
        "INSERT INTO emr_claims VALUES (3, 3, 2, 'C-00002', '16', '2026-11', '2026-11-18', 'U0005', 0, NULL, 'pending', NULL, NULL, NULL)",
        "check_uninsured_not_claimed",
    ),
    "scaling_twice": (
        "INSERT INTO emr_claims VALUES (3, 2, 1, 'C-00001', '00', '2026-05', '2026-05-12', 'U0001', 42000, '2026-06-05 09:00:00', 'paid', 42000, NULL, NULL)",
        "check_scaling_limit",
    ),
    "scaling_minor": (
        "INSERT INTO emr_claims VALUES (3, 3, 2, 'C-00002', '00', '2026-11', '2026-11-18', 'U0001', 42000, NULL, 'pending', NULL, NULL, NULL)",
        "check_scaling_limit",
    ),
    "pending_with_submitted_at": (
        "UPDATE emr_claims SET submitted_at = '2026-11-30 09:00:00' WHERE id = 2",
        "check_last_month_claims_pending",
    ),
    # 확정 월(9월 이전): 상태는 paid/adjusted/denied, 금액·사유 코드가 상태와 맞음
    "settled_still_submitted": (
        "INSERT INTO emr_claims VALUES (3, 1, 1, 'C-00001', '26', '2026-08', '2026-08-06', 'U0008', 84000, '2026-09-01 09:00:00', 'submitted', NULL, NULL, NULL)",
        "check_settled_claims",
    ),
    "paid_amount_mismatch": (
        "INSERT INTO emr_claims VALUES (3, 1, 1, 'C-00001', '26', '2026-08', '2026-08-06', 'U0008', 84000, '2026-09-01 09:00:00', 'paid', 80000, NULL, NULL)",
        "check_settled_claims",
    ),
    "adjusted_without_code": (
        "INSERT INTO emr_claims VALUES (3, 1, 1, 'C-00001', '26', '2026-08', '2026-08-06', 'U0008', 84000, '2026-09-01 09:00:00', 'adjusted', 60000, NULL, NULL)",
        "check_settled_claims",
    ),
    "adjusted_without_note": (
        "INSERT INTO emr_claims VALUES (3, 1, 1, 'C-00001', '26', '2026-08', '2026-08-06', 'U0008', 84000, '2026-09-01 09:00:00', 'adjusted', 60000, 'C', NULL)",
        "check_settled_claims",
    ),
    "paid_with_note": (
        "INSERT INTO emr_claims VALUES (3, 1, 1, 'C-00001', '26', '2026-08', '2026-08-06', 'U0008', 84000, '2026-09-01 09:00:00', 'paid', 84000, NULL, '재시행 간격 미충족')",
        "check_settled_claims",
    ),
    "denied_with_amount": (
        "INSERT INTO emr_claims VALUES (3, 1, 1, 'C-00001', '26', '2026-08', '2026-08-06', 'U0008', 84000, '2026-09-01 09:00:00', 'denied', 84000, 'A01', NULL)",
        "check_settled_claims",
    ),
    # 진료 금액: 수가대로, 급여면 본인부담률로 나눔, 비급여면 전액 본인부담
    "fee_not_master": (
        "UPDATE emr_treatments SET fee_total = 100000 WHERE id = 1",
        "check_treatment_amounts",
    ),
    "insured_split_wrong": (
        "UPDATE emr_treatments SET insured_amount = 80000, patient_amount = 40000 WHERE id = 1",
        "check_treatment_amounts",
    ),
    "uninsured_with_insured_amount": (
        "UPDATE emr_treatments SET insured_amount = 56000, patient_amount = 24000 WHERE id = 3",
        "check_treatment_amounts",
    ),
    "implant_under65_insured_amount": (  # 1번 환자는 1980년생 — 임플란트 1차는 65세 미만이면 본인부담 1.0
        "UPDATE emr_treatments SET procedure_code = 'U0013', fee_total = 1300000, insured_amount = 910000,"
        " patient_amount = 390000 WHERE id = 1",
        "check_treatment_amounts",
    ),
    # 창 안 급여 completed 진료 ↔ 청구 정확히 1줄 (환자·차트번호·코드·치아·금액·청구월 일치)
    "insured_without_claim": (
        "DELETE FROM emr_claims WHERE id = 2",
        "check_insured_claim_one_line",
    ),
    "two_claims_one_treatment": (
        "INSERT INTO emr_claims VALUES (3, 1, 1, 'C-00001', '27', '2026-10', '2026-10-06', 'U0008', 84000, '2026-11-01 09:00:00', 'submitted', NULL, NULL, NULL)",
        "check_insured_claim_one_line",
    ),
    "claim_on_uninsured_treatment": (
        "INSERT INTO emr_claims VALUES (3, 3, 2, 'C-00002', '16', '2026-11', '2026-11-18', 'U0005', 0, NULL, 'pending', NULL, NULL, NULL)",
        "check_insured_claim_one_line",
    ),
    "claim_tooth_mismatch": (
        "UPDATE emr_claims SET tooth_no = '27' WHERE id = 1",
        "check_insured_claim_one_line",
    ),
    "claim_code_mismatch": (
        "UPDATE emr_claims SET procedure_code = 'U0006' WHERE id = 1",
        "check_insured_claim_one_line",
    ),
    "claim_amount_mismatch": (
        "UPDATE emr_claims SET claim_amount = 80000 WHERE id = 1",
        "check_insured_claim_one_line",
    ),
    "claim_month_mismatch": (
        "UPDATE emr_claims SET claim_month = '2026-11' WHERE id = 1",
        "check_insured_claim_one_line",
    ),
    "claim_chart_no_mismatch": (
        "UPDATE emr_claims SET chart_no = 'C-00002' WHERE id = 1",
        "check_insured_claim_one_line",
    ),
    "claim_patient_mismatch": (
        "UPDATE emr_claims SET patient_id = 2 WHERE id = 1",
        "check_insured_claim_one_line",
    ),
    # 발치한 치아에 이후 진료 없음 (임플란트 1차 제외): 1번 10/06 단순발치 26 → 11/03 아말감 26
    "treatment_on_extracted_tooth": (
        "UPDATE emr_treatments SET procedure_code = CASE id WHEN 1 THEN 'U0011' ELSE 'U0006' END, tooth_no = '26'"
        " WHERE id IN (1, 2)",
        "check_extracted_tooth",
    ),
    "treatment_on_implant_tooth": (  # 10/06 임플란트 1차 26(이력 없는 치아) → 11/03 아말감 26
        "UPDATE emr_treatments SET procedure_code = CASE id WHEN 1 THEN 'U0013' ELSE 'U0006' END, tooth_no = '26'"
        " WHERE id IN (1, 2)",
        "check_extracted_tooth",
    ),
    "implant_twice_same_tooth": (
        "UPDATE emr_treatments SET procedure_code = 'U0013', tooth_no = '26' WHERE id IN (1, 2)",
        "check_extracted_tooth",
    ),
    "implant_on_treated_natural_tooth": (  # 10/06 신경치료 26 → 11/03 임플란트 26 (발치 없이)
        "UPDATE emr_treatments SET procedure_code = 'U0013', tooth_no = '26' WHERE id = 2",
        "check_extracted_tooth",
    ),
    "extracted_tooth_extracted_again": (
        "UPDATE emr_treatments SET procedure_code = 'U0011', tooth_no = '26' WHERE id IN (1, 2)",
        "check_extracted_tooth",
    ),
    # 확정 월 조정 사유: master.ADJUST_REASONS의 상세 사유 하나와 범주 문자·서술이 일치하고 그 시술에 맞음
    "adjust_note_unknown": (
        "INSERT INTO emr_claims VALUES (3, 1, 1, 'C-00001', '26', '2026-08', '2026-08-06', 'U0008', 84000, '2026-09-01 09:00:00', 'adjusted', 60000, 'C', '알 수 없는 사유')",
        "check_adjust_reason",
    ),
    "adjust_code_note_mismatch": (
        "INSERT INTO emr_claims VALUES (3, 1, 1, 'C-00001', '26', '2026-08', '2026-08-06', 'U0008', 84000, '2026-09-01 09:00:00', 'adjusted', 60000, 'B', '동일 부위 재시행 간격 기준 미충족')",
        "check_adjust_reason",
    ),
    "adjust_reason_wrong_procedure": (  # R02는 잇몸치료 사유
        "INSERT INTO emr_claims VALUES (3, 1, 1, 'C-00001', '26', '2026-08', '2026-08-06', 'U0008', 84000, '2026-09-01 09:00:00', 'denied', 0, 'C', '치석제거 후 후속 치주치료 확인 불가')",
        "check_adjust_reason",
    ),
    "external_reason_adjusted": (  # 외부 이력은 급여 스케일링 denied만
        "INSERT INTO emr_claims VALUES (3, 2, 1, 'C-00001', '00', '2026-08', '2026-08-04', 'U0001', 42000, '2026-09-01 09:00:00', 'adjusted', 30000, 'C', '연 1회 산정 횟수 초과(타 기관 이력)')",
        "check_adjust_reason",
    ),
    # 수납: 창 안 completed마다 visit 1행, 창 이전 내원은 수납 없음, settlement는 amount_due 0
    "completed_without_visit_payment": (
        "DELETE FROM emr_payments WHERE id = 4",
        "check_payment_per_visit",
    ),
    "two_visit_payments": (
        "INSERT INTO emr_payments VALUES (5, 2, 4, 'visit', 0, 0, '2026-11-18 10:40:00', 'card')",
        "check_payment_per_visit",
    ),
    "settlement_with_due": (
        "UPDATE emr_payments SET amount_due = 1000 WHERE id = 3",
        "check_payment_per_visit",
    ),
    "overpaid": (
        "UPDATE emr_payments SET amount_paid = 40000 WHERE id = 1",
        "check_balance_non_negative",
    ),
    "settled_before_debt": (
        "UPDATE emr_payments SET paid_at = '2026-10-01 10:00:00' WHERE id = 3",
        "check_balance_non_negative",
    ),
    "visit_on_noshow": (
        "UPDATE emr_payments SET appointment_id = 7 WHERE id = 2",
        "check_visit_payment_completed",
    ),
    "visit_without_appt": (
        "UPDATE emr_payments SET appointment_id = NULL WHERE id = 2",
        "check_visit_payment_completed",
    ),
    # ax_calls (6단계)
    "fk_dangling_call": (
        "UPDATE ax_calls SET patient_id = 999 WHERE id = 1",
        "check_foreign_keys",
    ),
    "call_future": (
        "UPDATE ax_calls SET started_at = '2026-12-02 10:00:00' WHERE id = 2",
        "check_timestamps_not_future",
    ),
    "doc_effective_future": (
        "UPDATE ax_documents SET effective_date = '2026-12-02' WHERE id = 1",
        "check_timestamps_not_future",
    ),
    "call_uid_purpose": (
        "UPDATE ax_calls SET call_uid = 'reschedule:0001' WHERE id = 1",
        "check_call_uid_format",
    ),
    "call_uid_suffix": (
        "UPDATE ax_calls SET call_uid = 'c-0001-book' WHERE id = 1",
        "check_call_uid_format",
    ),
    "call_label_column": (
        "ALTER TABLE ax_calls ADD COLUMN primary_purpose TEXT",
        "check_ax_calls_columns",
    ),
    # truth_claims (심사결과 슬롯)
    "extra_claim_review_in_ledger": (
        "UPDATE truth.truth_claims SET service_date = '2026-11-03', procedure_code = 'U0001'"
        " WHERE outcome = 'extra_in_claim_review'",
        "check_extra_claim_review_not_in_ledger",
    ),
    # TODAY 구간 (가상 데이터 생성 설계 §5.3)
    "created_future": (
        "UPDATE emr_appointments SET created_at = '2026-12-02 10:00:00' WHERE id = 6",
        "check_timestamps_not_future",
    ),
    "cancelled_future": (
        "UPDATE emr_appointments SET cancelled_at = '2026-12-02 10:00:00' WHERE id = 3",
        "check_timestamps_not_future",
    ),
    "registered_future": (
        "UPDATE emr_patients SET registered_at = '2026-12-03' WHERE id = 2",
        "check_timestamps_not_future",
    ),
    "born_future": (
        "UPDATE emr_patients SET birth_date = '2027-01-01' WHERE id = 2",
        "check_timestamps_not_future",
    ),
    "confirmed_before_reminder": (
        "UPDATE emr_appointments SET confirmed = 1 WHERE id = 6",
        "check_confirmed_not_future",
    ),
    # 리마인드 대상 = 직전 진료일 전에 잡혔고 그날까지 취소되지 않은 예약 (12-01 예약의 직전 진료일은 11-30)
    "confirmed_booked_on_reminder_day": (
        "UPDATE emr_appointments SET confirmed = 1, created_at = '2026-11-30 10:00:00' WHERE id = 5",
        "check_confirmed_reminded",
    ),
    "confirmed_cancelled_before_reminder": (
        "UPDATE emr_appointments SET confirmed = 1 WHERE id = 3",  # 11-14 변경 취소
        "check_confirmed_reminded",
    ),
    "cancel_neither_kind": (
        "UPDATE emr_appointments SET rescheduled_from_id = NULL WHERE id = 4",
        "check_cancellation_kind",
    ),
    "sameday_cancel_on_today": (
        "INSERT INTO emr_appointments (patient_id, staff_id, scheduled_at, duration_min, type,"
        " planned_procedure_code, status, booked_via, created_at, cancelled_at) VALUES"
        " (2, 2, '2026-12-01 15:00:00', 30, 'hygiene', 'U0002', 'cancelled', 'phone', '2026-11-20 11:00:00', '2026-12-01 09:00:00')",
        "check_cancellation_kind",
    ),
    "cancelled_without_time": (
        "UPDATE emr_appointments SET cancelled_at = NULL WHERE id = 3",
        "check_cancelled_at_status",
    ),
    "time_on_not_cancelled": (
        "UPDATE emr_appointments SET cancelled_at = '2026-11-20 10:00:00' WHERE id = 7",
        "check_cancelled_at_status",
    ),
    # 변경 쌍: 원래 예약은 cancelled, 새 created_at = 원래 cancelled_at
    "reschedule_original_not_cancelled": (
        "UPDATE emr_appointments SET status = 'completed' WHERE id = 3",
        "check_reschedule_pair",
    ),  # cancelled_at은 그대로 — 상태만 어김
    "reschedule_created_mismatch": (
        "UPDATE emr_appointments SET created_at = '2026-11-13 09:00:00' WHERE id = 4",
        "check_reschedule_pair",
    ),
    # 시각 순서
    "created_after_scheduled": (
        "UPDATE emr_appointments SET created_at = '2026-10-07 10:00:00' WHERE id = 1",
        "check_created_before_scheduled",
    ),
    "sameday_cancel_after_start": (
        "UPDATE emr_appointments SET cancelled_at = '2026-11-17 17:00:00' WHERE id = 3",
        "check_cancel_time",
    ),
    "cancelled_before_created": (
        "UPDATE emr_appointments SET cancelled_at = '2026-10-29 10:00:00' WHERE id = 3",
        "check_cancel_time",
    ),
    # 진료 기록 ↔ completed
    "treatment_on_noshow": (
        "UPDATE emr_treatments SET appointment_id = 7 WHERE id = 1",
        "check_treatment_on_completed",
    ),
    "completed_without_treatment": (
        "DELETE FROM emr_treatments WHERE id = 3",
        "check_completed_one_treatment",
    ),
    "completed_two_treatments": (
        "INSERT INTO emr_treatments VALUES (4, 1, 1, 'U0008', '27', '2026-10-06 10:00:00', 120000, 84000, 36000)",
        "check_completed_one_treatment",
    ),
    "implant_under65_claimed": (  # 1번 환자는 1980년생 — 임플란트 1차는 65세 미만이면 비급여
        "INSERT INTO emr_claims VALUES (3, 1, 1, 'C-00001', '36', '2026-11', '2026-11-03', 'U0013', 910000, NULL, 'pending', NULL, NULL, NULL)",
        "check_uninsured_not_claimed",
    ),
    # truth_claims ↔ 9~10월 emr_claims
    "truth_claim_missing": (
        "DELETE FROM truth.truth_claims WHERE claim_id = 1",
        "check_truth_claims_match",
    ),
    "truth_amount_mismatch": (
        "UPDATE truth.truth_claims SET claim_amount = 1 WHERE claim_id = 1",
        "check_truth_claims_match",
    ),
    "truth_points_to_pending": (
        "UPDATE truth.truth_claims SET claim_id = 2 WHERE claim_id = 1",
        "check_truth_claims_match",
    ),
    "extra_claim_review_outside_months": (
        "UPDATE truth.truth_claims SET service_date = '2026-08-06' WHERE outcome = 'extra_in_claim_review'",
        "check_truth_claims_match",
    ),
    # 창 이전 내원 (scheduled_at < range_start): 환자당 completed 위생 1행, 등록일 < 내원일
    "pre_window_twice": (
        PRE + _pre(1, "2026-09-15 10:00:00") + ", " + _pre(1, "2026-09-22 10:00:00"),
        "check_pre_window_visit",
    ),
    "pre_window_not_hygiene": (
        PRE + _pre(1, "2026-09-15 10:00:00", type="treatment"),
        "check_pre_window_visit",
    ),
    "pre_window_noshow": (
        PRE + _pre(1, "2026-09-15 10:00:00", status="noshow"),
        "check_pre_window_visit",
    ),
    "pre_window_before_registration": (
        PRE + _pre(1, "2024-12-20 10:00:00", created="2024-12-10 10:00:00"),
        "check_pre_window_visit",
    ),
    # 주의사항 (4b단계): 대상 조건(기록일 나이·성별), 출처 규칙, 기록일, 환자당 ≤ 3개
    "alert_unknown_code": (
        "UPDATE emr_patient_alerts SET code = 'NOPE' WHERE id = 1",
        "check_foreign_keys",
    ),
    "alert_future": (
        "UPDATE emr_patient_alerts SET recorded_at = '2026-12-02' WHERE id = 1",
        "check_timestamps_not_future",
    ),
    "alert_htn_child": (
        "UPDATE emr_patient_alerts SET code = 'HTN' WHERE id = 2",
        "check_alert_target",
    ),
    "alert_pregnant_male": (
        "UPDATE emr_patient_alerts SET code = 'PREGNANT' WHERE id = 1",
        "check_alert_target",
    ),
    "alert_child_coop_adult": (
        "UPDATE emr_patient_alerts SET code = 'CHILD_COOP' WHERE id = 1",
        "check_alert_target",
    ),
    "alert_before_registration": (
        "UPDATE emr_patient_alerts SET recorded_at = '2024-12-01' WHERE id = 1",
        "check_alert_recorded_at",
    ),
    "intake_not_on_rule_date": (
        "UPDATE emr_patient_alerts SET recorded_at = '2026-10-06' WHERE id = 1",
        "check_alert_recorded_at",
    ),
    "intake_with_appointment": (
        "UPDATE emr_patient_alerts SET appointment_id = 1 WHERE id = 1",
        "check_alert_source",
    ),
    "intake_code_as_visit": (
        "UPDATE emr_patient_alerts SET source = 'visit', recorded_at = '2026-10-06', appointment_id = 1"
        " WHERE id = 1",
        "check_alert_source",
    ),
    "visit_code_as_intake": (
        "UPDATE emr_patient_alerts SET source = 'intake', recorded_at = '2025-01-01', appointment_id = NULL"
        " WHERE id = 3",
        "check_alert_source",
    ),
    "visit_alert_unrelated_procedure": (
        "UPDATE emr_patient_alerts SET appointment_id = 2, recorded_at = '2026-11-03' WHERE id = 3",
        "check_alert_source",
    ),  # 스케일링은 마취 시술 아님
    "visit_alert_other_day": (
        "UPDATE emr_patient_alerts SET recorded_at = '2026-10-07' WHERE id = 3",
        "check_alert_source",
    ),
    "visit_alert_on_noshow": (
        "UPDATE emr_patient_alerts SET appointment_id = 7, recorded_at = '2026-11-24' WHERE id = 3",
        "check_alert_source",
    ),
    "visit_alert_without_appointment": (
        "UPDATE emr_patient_alerts SET appointment_id = NULL WHERE id = 3",
        "check_alert_source",
    ),
    "visit_alert_other_patient": (
        "UPDATE emr_patient_alerts SET patient_id = 2 WHERE id = 3",
        "check_alert_source",
    ),
    "child_coop_without_end": (
        "UPDATE emr_patient_alerts SET ends_at = NULL WHERE id = 2",
        "check_alert_ends",
    ),
    "child_coop_wrong_end": (
        "UPDATE emr_patient_alerts SET ends_at = '2031-02-28' WHERE id = 2",
        "check_alert_ends",
    ),
    "lasting_alert_with_end": (
        "UPDATE emr_patient_alerts SET ends_at = '2026-06-01' WHERE id = 1",
        "check_alert_ends",
    ),
    "visit_alert_with_end": (
        "UPDATE emr_patient_alerts SET ends_at = '2027-01-01' WHERE id = 3",
        "check_alert_ends",
    ),
    "four_alerts": (
        "INSERT INTO emr_patient_alerts (patient_id, code, source, recorded_at) VALUES"
        " (1, 'PENICILLIN', 'intake', '2025-01-01'), (1, 'FEAR', 'intake', '2025-01-01')",
        "check_alerts_per_patient",
    ),
    "caller_phone_real": (
        "UPDATE ax_calls SET caller_phone = '010-1234-5678' WHERE id = 2",
        "check_fake_phone",
    ),
    "real_phone": (
        "UPDATE emr_patients SET phone = '010-1234-5678' WHERE id = 2",
        "check_fake_phone",
    ),
}


@pytest.mark.parametrize("case", BREAKS)
def test_break_is_caught(tmp_path, case):
    sql, check = BREAKS[case]
    conn = make_db(tmp_path)
    conn.execute(sql)
    assert getattr(verify, check)(conn, CTX), f"{check}가 {case}를 못 잡음"


def test_patient_twice_same_day(tmp_path):
    conn = make_db(tmp_path)
    appt(conn, "2026-10-06 15:00:00", type="hygiene", patient=1)
    assert verify.check_patient_same_day(conn, CTX)


def test_confirmed_uses_previous_open_day(tmp_path):
    conn = make_db(tmp_path)
    appt(
        conn, "2026-12-04 10:00:00", status="scheduled"
    )  # 금요일 — 직전 진료일은 12-02(수), 12-03은 목요일 휴진
    conn.execute(
        "UPDATE emr_appointments SET confirmed = 1 WHERE scheduled_at = '2026-12-04 10:00:00'"
    )
    assert verify.check_confirmed_not_future(conn, CTX)  # TODAY 12-01: 리마인드 아직
    assert (
        verify.check_confirmed_not_future(conn, replace(CTX, today=date(2026, 12, 2))) == []
    )  # 12-02에 리마인드 발송


def test_main_exit_codes(tmp_path, capsys):
    conn = make_db(tmp_path)
    conn.commit()
    args = ["--db", str(tmp_path / "app.sqlite"), "--truth", str(tmp_path / "truth.sqlite")]
    assert verify.main(args) == 0

    conn.execute("UPDATE emr_patients SET phone = '010-1234-5678' WHERE id = 2")
    conn.commit()
    assert verify.main(args) == 1
    out = capsys.readouterr().out
    assert "check_fake_phone" in out and "1건" in out and "010-1234-5678" in out


def test_warning_failure_does_not_exit(tmp_path, monkeypatch):
    conn = make_db(tmp_path)
    monkeypatch.setattr(verify, "WARNINGS", [lambda conn, ctx: [{"x": 1}]])
    assert verify.run(conn, CTX) is True


def test_missing_db_exits(tmp_path):
    assert verify.main(["--db", str(tmp_path / "nope.sqlite")]) == 1


def test_missing_truth_exits(tmp_path):
    make_db(tmp_path).commit()
    assert (
        verify.main(
            ["--db", str(tmp_path / "app.sqlite"), "--truth", str(tmp_path / "nope.sqlite")]
        )
        == 1
    )


def test_missing_gen_meta_exits(tmp_path, capsys):
    conn = make_db(tmp_path)
    conn.execute("DELETE FROM truth.truth_gen_meta WHERE key = 'range_start'")
    conn.commit()
    assert (
        verify.main(
            ["--db", str(tmp_path / "app.sqlite"), "--truth", str(tmp_path / "truth.sqlite")]
        )
        == 1
    )
    assert "truth_gen_meta" in capsys.readouterr().out


def test_missing_gen_meta_table_exits(tmp_path, capsys):
    make_db(tmp_path).commit()
    truth = sqlite3.connect(tmp_path / "truth.sqlite")
    truth.execute("DROP TABLE truth_gen_meta")
    truth.commit()
    assert (
        verify.main(
            ["--db", str(tmp_path / "app.sqlite"), "--truth", str(tmp_path / "truth.sqlite")]
        )
        == 1
    )
    assert "truth_gen_meta" in capsys.readouterr().out


def test_load_ctx_reads_range_from_gen_meta(tmp_path):
    conn = make_db(tmp_path)
    ctx = verify.load_ctx(ROOT / "config" / "generator.yaml", conn)
    assert (ctx.range_start, ctx.range_end) == (date(2026, 10, 1), date(2026, 12, 15))


def test_confirmed_allowed_when_reminder_sent_by_today(tmp_path):
    conn = make_db(tmp_path)
    # 12-01(TODAY) 예약과 12-02 예약은 전날 리마인드가 TODAY 이전·당일이라 confirmed 가능
    conn.execute("UPDATE emr_appointments SET confirmed = 1 WHERE id = 5")
    appt(conn, "2026-12-02 10:00:00", status="scheduled")
    conn.execute(
        "UPDATE emr_appointments SET confirmed = 1 WHERE scheduled_at = '2026-12-02 10:00:00'"
    )
    assert verify.check_confirmed_not_future(conn, CTX) == []


def test_run_survives_closed_day_and_unborn_patient(tmp_path, capsys):
    conn = make_db(tmp_path)
    conn.execute(
        "UPDATE emr_appointments SET scheduled_at = '2026-11-19 10:00:00' WHERE id = 7"
    )  # 목요일
    conn.execute("UPDATE emr_patients SET birth_date = '2027-01-01' WHERE id = 2")
    assert verify.run(conn, CTX) is False
    out = capsys.readouterr().out
    assert "check_timestamps_not_future" in out


def test_truth_checks_skip_until_truth_claims_exist(tmp_path):
    conn = make_db(tmp_path)
    conn.execute("DELETE FROM truth.truth_claims")  # 심사결과 슬롯 착수 전에는 비어 있다
    assert verify.check_truth_claims_match(conn, CTX) == []
    assert verify.check_extra_claim_review_not_in_ledger(conn, CTX) == []


def test_truth_claims_extra_must_have_no_claim_id(tmp_path):
    conn = make_db(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE truth.truth_claims SET claim_id = 1 WHERE outcome = 'extra_in_claim_review'"
        )


def _two_dbs(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    return make_db(a), make_db(b)


def test_canonical_hash_same_content(tmp_path):
    a, b = _two_dbs(tmp_path)
    assert verify.canonical_hash(a) == verify.canonical_hash(b)


def test_canonical_hash_detects_change(tmp_path):
    a, b = _two_dbs(tmp_path)
    b.execute("UPDATE emr_payments SET amount_paid = 19990 WHERE id = 1")
    assert verify.canonical_hash(a) != verify.canonical_hash(b)


def test_canonical_hash_ignores_insertion_order(tmp_path):
    a, b = _two_dbs(tmp_path)
    # emr_procedures는 TEXT PK라 rowid가 삽입 순서를 따른다 → 정렬 없이는 순서가 갈림
    rows = [tuple(r) for r in b.execute("SELECT * FROM emr_procedures ORDER BY code")]
    b.execute("DELETE FROM emr_procedures")
    b.executemany(
        f"INSERT INTO emr_procedures VALUES ({', '.join('?' * len(rows[0]))})", rows[::-1]
    )
    assert verify.canonical_hash(a) == verify.canonical_hash(b)


def test_canonical_hash_truth_schema(tmp_path):
    a, b = _two_dbs(tmp_path)
    assert verify.canonical_hash(a, "truth") == verify.canonical_hash(b, "truth")
    b.execute("UPDATE truth.truth_gen_meta SET value = '1' WHERE key = 'seed'")
    assert verify.canonical_hash(a, "truth") != verify.canonical_hash(b, "truth")
    assert verify.canonical_hash(a) == verify.canonical_hash(b)  # main은 그대로


def test_canonical_hash_ignores_indexed_at(tmp_path):
    a, b = _two_dbs(tmp_path)
    b.execute("UPDATE ax_calls SET indexed_at = '2026-12-01 09:00:00'")
    assert verify.canonical_hash(a) == verify.canonical_hash(b)


def test_rag_checks_skipped_without_chunks(tmp_path, capsys):
    conn = make_db(tmp_path)
    assert verify.run(conn, CTX) is True  # 앱 색인 코드 전 — ax_chunks가 없어도 실패가 아니다
    assert "[SKIP] RAG" in capsys.readouterr().out
    # 청크가 생겨도 RAG 검사가 구현되기 전에는 건너뜀 — 검사 없이 통과한 것처럼 보이지 않게
    conn.execute("CREATE TABLE ax_chunks (id INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO ax_chunks VALUES (1)")
    verify.run(conn, CTX)
    assert "[SKIP] RAG" in capsys.readouterr().out


def test_pre_window_visit_valid(tmp_path):
    conn = make_db(tmp_path)
    conn.execute(PRE + _pre(1, "2026-09-15 10:00:00") + ", " + _pre(2, "2026-09-16 10:00:00"))
    assert verify.check_pre_window_visit(conn, CTX) == []


def test_pre_window_visit_has_no_payment(tmp_path):
    conn = make_db(tmp_path)
    conn.execute(PRE + _pre(1, "2026-09-15 10:00:00"))
    assert verify.check_payment_per_visit(conn, CTX) == []
    pre_id = conn.execute("SELECT max(id) FROM emr_appointments").fetchone()[0]
    conn.execute(
        "INSERT INTO emr_payments VALUES (5, 1, ?, 'visit', 18000, 18000, '2026-09-15 10:40:00', 'card')",
        (pre_id,),
    )
    assert verify.check_payment_per_visit(conn, CTX)


def _settled_claim(conn, code, tooth, status, paid, adjust_code, note, service_date="2026-08-06"):
    conn.execute(
        "INSERT INTO emr_claims (treatment_id, patient_id, chart_no, tooth_no, claim_month, service_date,"
        " procedure_code, claim_amount, submitted_at, status, paid_amount, adjust_code, adjust_note)"
        " VALUES (1, 1, 'C-00001', ?, '2026-08', ?, ?, 84000, '2026-09-01 09:00:00', ?, ?, ?, ?)",
        (tooth, service_date, code, status, paid, adjust_code, note),
    )


def test_adjust_reason_valid(tmp_path):
    """사유 표대로 쓴 조정·불능 줄과 외부 이력 스케일링 불능은 통과, paid 줄은 사유를 보지 않는다"""
    conn = make_db(tmp_path)
    _settled_claim(
        conn, "U0008", "26", "adjusted", 60000, "C", "동일 부위 재시행 간격 기준 미충족"
    )
    _settled_claim(
        conn,
        "U0008",
        "26",
        "denied",
        0,
        "K",
        "부위·산정 내역 기재 누락",
        service_date="2026-08-07",
    )
    _settled_claim(conn, "U0001", "00", "denied", 0, "C", "연 1회 산정 횟수 초과(타 기관 이력)")
    _settled_claim(conn, "U0006", "16", "paid", 84000, None, None)
    assert verify.check_adjust_reason(conn, CTX) == []


def _pre_window_scaling(conn, at, claim_status=None):
    """창 이전 위생 내원 1행 + 진료 기록(성인 스케일링). claim_status를 주면 청구 1줄도"""
    conn.execute(PRE + _pre(1, at))
    appt_id = conn.execute("SELECT max(id) FROM emr_appointments").fetchone()[0]
    conn.execute(
        "INSERT INTO emr_treatments (appointment_id, patient_id, procedure_code, tooth_no, performed_at,"
        " fee_total, insured_amount, patient_amount) VALUES (?, 1, 'U0001', '00', ?, 60000, 42000, 18000)",
        (appt_id, at),
    )
    treatment_id = conn.execute("SELECT max(id) FROM emr_treatments").fetchone()[0]
    if claim_status:
        paid = 42000 if claim_status == "paid" else None
        conn.execute(
            "INSERT INTO emr_claims (treatment_id, patient_id, chart_no, tooth_no, claim_month, service_date,"
            " procedure_code, claim_amount, submitted_at, status, paid_amount)"
            " VALUES (?, 1, 'C-00001', '00', ?, ?, 'U0001', 42000, '2026-09-01 09:00:00', ?, ?)",
            (treatment_id, at[:7], at[:10], claim_status, paid),
        )


def test_pre_window_scaling_this_year_claimed_paid(tmp_path):
    """창 이전 내원은 TODAY와 같은 해 급여 스케일링만 청구 1줄 paid (§2.6 (1))"""
    conn = make_db(tmp_path)
    _pre_window_scaling(conn, "2026-08-04 10:00:00", claim_status="paid")
    assert verify.check_insured_claim_one_line(conn, CTX) == []


def test_pre_window_scaling_this_year_without_claim(tmp_path):
    conn = make_db(tmp_path)
    _pre_window_scaling(conn, "2026-08-04 10:00:00")
    assert verify.check_insured_claim_one_line(conn, CTX)


def test_pre_window_scaling_claim_not_paid(tmp_path):
    conn = make_db(tmp_path)
    _pre_window_scaling(conn, "2026-08-04 10:00:00", claim_status="submitted")
    assert verify.check_insured_claim_one_line(conn, CTX)


def test_pre_window_scaling_last_year_not_claimed(tmp_path):
    conn = make_db(tmp_path)
    _pre_window_scaling(conn, "2025-12-02 10:00:00")
    assert verify.check_insured_claim_one_line(conn, CTX) == []
    _pre_window_scaling(conn, "2025-11-04 10:00:00", claim_status="paid")
    assert verify.check_insured_claim_one_line(conn, CTX)


def test_intake_alert_of_returning_patient_on_pre_window_visit(tmp_path):
    """구환 문진 기록일 = 창 이전 마지막 내원일 (그 내원이 있으면). 대상 나이도 그날 기준"""
    conn = make_db(tmp_path)
    conn.execute(PRE + _pre(1, "2026-09-15 10:00:00"))
    assert verify.check_alert_recorded_at(conn, CTX)  # 등록일(2025-01-01) 기록은 이제 어긋남
    conn.execute("UPDATE emr_patient_alerts SET recorded_at = '2026-09-15' WHERE id = 1")
    assert verify.check_alert_recorded_at(conn, CTX) == []


def test_intake_alert_of_new_patient_on_first_visit(tmp_path):
    """신환(등록일 ≥ 창 시작) 문진 기록일 = 첫 completed 내원일 — 등록(전화 예약)일이 아니다"""
    conn = make_db(tmp_path)
    conn.execute("UPDATE emr_patients SET registered_at = '2026-10-02' WHERE id = 1")
    conn.execute(
        "UPDATE emr_patient_alerts SET recorded_at = '2026-10-06' WHERE id = 1"
    )  # 첫 내원 = 10/06 신경치료
    assert verify.check_alert_recorded_at(conn, CTX) == []
    conn.execute("UPDATE emr_patient_alerts SET recorded_at = '2026-10-02' WHERE id = 1")
    assert verify.check_alert_recorded_at(conn, CTX)


def test_no_intake_alert_for_new_patient_never_visited(tmp_path):
    """내원한 적 없는 신환은 문진 주의사항이 없다"""
    conn = make_db(tmp_path)
    conn.execute("UPDATE emr_patients SET registered_at = '2026-10-30' WHERE id = 2")
    conn.execute(
        "UPDATE emr_patient_alerts SET recorded_at = '2026-11-18' WHERE id = 2"
    )  # 첫 내원 11/18 레진
    assert verify.check_alert_recorded_at(conn, CTX) == []
    conn.execute("UPDATE emr_appointments SET status = 'noshow' WHERE id = 4")
    assert verify.check_alert_recorded_at(conn, CTX)


def _pregnant(conn, recorded, ends):
    conn.execute(
        "INSERT INTO emr_patients (id, chart_no, name, birth_date, sex, phone, registered_at)"
        " VALUES (3, 'C-00003', '이수진', '1993-04-01', 'F', '010-0000-0003', '2025-01-01')"
    )
    conn.execute(
        "INSERT INTO emr_patient_alerts (patient_id, code, source, recorded_at, ends_at)"
        " VALUES (3, 'PREGNANT', 'intake', ?, ?)",
        (recorded, ends),
    )


def test_pregnant_ends_within_lasting_days(tmp_path):
    """임신 종료일 = 기록일 + master.PREGNANCY_LEFT_DAYS 범위 안 (남은 기간)"""
    conn = make_db(tmp_path)
    _pregnant(conn, "2025-01-01", "2025-06-01")
    assert verify.check_alert_ends(conn, CTX) == []


@pytest.mark.parametrize("ends", [None, "2025-12-01", "2025-01-01"])
def test_pregnant_end_out_of_range(tmp_path, ends):
    conn = make_db(tmp_path)
    _pregnant(conn, "2025-01-01", ends)
    assert verify.check_alert_ends(conn, CTX)


def test_implant_on_extracted_tooth_allowed(tmp_path):
    """발치한 치아에도 임플란트 1차는 된다"""
    conn = make_db(tmp_path)
    conn.execute(
        "UPDATE emr_treatments SET procedure_code = CASE id WHEN 1 THEN 'U0011' ELSE 'U0013' END,"
        " tooth_no = '26' WHERE id IN (1, 2)"
    )
    assert verify.check_extracted_tooth(conn, CTX) == []


def test_visit_alert_not_on_pre_window_visit(tmp_path):
    """진료 중 발견은 창 안 completed 내원만 (창 이전 내원은 대상 아님 — §2.6 (1))"""
    conn = make_db(tmp_path)
    conn.execute(PRE + _pre(1, "2026-09-15 10:00:00"))  # 창 이전 스케일링 — 구역반사 관련 시술
    pre_id = conn.execute("SELECT max(id) FROM emr_appointments").fetchone()[0]
    conn.execute(
        "INSERT INTO emr_patient_alerts (patient_id, code, source, recorded_at, appointment_id)"
        " VALUES (1, 'GAG', 'visit', '2026-09-15', ?)",
        (pre_id,),
    )
    assert verify.check_alert_source(conn, CTX)


def test_alert_unique_per_patient_code(tmp_path):
    conn = make_db(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO emr_patient_alerts (patient_id, code, source, recorded_at) VALUES (1, 'HTN', 'intake', '2025-01-01')"
        )
