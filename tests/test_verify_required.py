"""verify 필수 검사(emr_* · ax_calls · truth_claims)가 위반을 실제로 잡는지 — 정상 DB를 한 곳씩 깨뜨린다."""
import sqlite3
from dataclasses import replace
from datetime import date

import pytest

from generator import verify
from tests.verify_db import CTX, ROOT, appt, make_db


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
    "performed_mismatch": ("UPDATE emr_treatments SET performed_at = '2026-11-18 11:00:00' WHERE id = 3", "check_performed_at"),
    "service_date_mismatch": ("UPDATE emr_claims SET service_date = '2026-10-07' WHERE id = 1", "check_service_date"),
    "uninsured_claimed": (
        "INSERT INTO emr_claims VALUES (3, 3, 2, 'C-00002', '16', '2026-11', 'monthly', '2026-11-18', 'U0005', 0, NULL, 'pending', NULL, NULL)",
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
    # 예약 경로·창 (워밍업 없음 — §2.6 ③)
    # 각 케이스는 조건 하나만 어긴다: 완료되지 않은 내원 / 다른 환자의 내원 / 변경으로 생긴 새 예약
    "desk_after_noshow": ("UPDATE emr_appointments SET booked_via = 'desk', created_at = '2026-11-24 15:30:00' WHERE id = 5",
                          "check_booked_via"),  # 환자 1 자기 노쇼(7번) 종료 시각
    "desk_at_other_patient_visit_end": (
        "UPDATE emr_appointments SET booked_via = 'desk', created_at = '2026-10-06 11:00:00' WHERE id = 6",
        "check_booked_via"),  # 환자 2 예약을 환자 1의 1번 내원 종료 시각에
    "rescheduled_by_desk": (
        "INSERT INTO emr_appointments (patient_id, staff_id, scheduled_at, duration_min, type, planned_procedure_code,"
        " status, rescheduled_from_id, booked_via, created_at) VALUES"
        " (1, 1, '2026-10-08 10:00:00', 30, 'treatment', 'U0008', 'scheduled', 1, 'desk', '2026-10-06 11:00:00')",
        "check_booked_via"),  # 자기 completed 종료 시각이지만 변경 새 예약
    # 사건 시각은 진료일 영업시간 안 (당일 취소만 예외)
    "created_on_closed_day": ("UPDATE emr_appointments SET created_at = '2026-11-19 11:00:00' WHERE id = 5", "check_event_times_in_hours"),
    "created_on_holiday": ("UPDATE emr_appointments SET created_at = '2026-11-25 11:00:00' WHERE id = 6", "check_event_times_in_hours"),
    "created_after_saturday_close": ("UPDATE emr_appointments SET created_at = '2026-11-21 14:00:00' WHERE id = 6",
                                     "check_event_times_in_hours"),  # 토요일은 13:30 마감
    "call_on_closed_day": ("UPDATE ax_calls SET started_at = '2026-11-19 10:00:00' WHERE id = 1", "check_event_times_in_hours"),
    "message_out_after_close": ("UPDATE emr_messages SET sent_at = '2026-11-02 19:00:00' WHERE id = 1", "check_event_times_in_hours"),
    "created_after_close": ("UPDATE emr_appointments SET created_at = '2026-11-20 19:00:00' WHERE id = 6", "check_event_times_in_hours"),
    "reschedule_cancel_before_open": ("UPDATE emr_appointments SET cancelled_at = '2026-11-14 08:00:00' WHERE id = 3",
                                      "check_event_times_in_hours"),
    "created_before_window": ("UPDATE emr_appointments SET created_at = '2026-09-30 11:00:00' WHERE id = 1",
                              "check_created_in_window"),
    # ax_calls (6단계)
    "fk_dangling_call": ("UPDATE ax_calls SET patient_id = 999 WHERE id = 1", "check_foreign_keys"),
    "call_future": ("UPDATE ax_calls SET started_at = '2026-12-02 10:00:00' WHERE id = 2", "check_timestamps_not_future"),
    "call_uid_purpose": ("UPDATE ax_calls SET call_uid = 'reschedule:0001' WHERE id = 1", "check_call_uid_format"),
    "call_uid_suffix": ("UPDATE ax_calls SET call_uid = 'c-0001-book' WHERE id = 1", "check_call_uid_format"),
    "call_label_column": ("ALTER TABLE ax_calls ADD COLUMN primary_purpose TEXT", "check_ax_calls_columns"),
    # truth_claims (EOB 슬롯)
    "extra_eob_in_ledger": (
        "UPDATE truth.truth_claims SET service_date = '2026-11-03', procedure_code = 'U0001'"
        " WHERE outcome = 'extra_in_eob'",
        "check_extra_eob_not_in_ledger",
    ),
    # TODAY 구간 (가상 데이터 생성 설계 §5.3)
    "created_future": ("UPDATE emr_appointments SET created_at = '2026-12-02 10:00:00' WHERE id = 6", "check_timestamps_not_future"),
    "cancelled_future": ("UPDATE emr_appointments SET cancelled_at = '2026-12-02 10:00:00' WHERE id = 3", "check_timestamps_not_future"),
    "registered_future": ("UPDATE emr_patients SET registered_at = '2026-12-03' WHERE id = 2", "check_timestamps_not_future"),
    "born_future": ("UPDATE emr_patients SET birth_date = '2027-01-01' WHERE id = 2", "check_timestamps_not_future"),
    "confirmed_before_reminder": ("UPDATE emr_appointments SET confirmed = 1 WHERE id = 6", "check_confirmed_not_future"),
    "cancel_neither_kind": ("UPDATE emr_appointments SET rescheduled_from_id = NULL WHERE id = 4", "check_cancellation_kind"),
    "sameday_cancel_on_today": (
        "INSERT INTO emr_appointments (patient_id, staff_id, scheduled_at, duration_min, type,"
        " planned_procedure_code, status, booked_via, created_at, cancelled_at) VALUES"
        " (2, 2, '2026-12-01 15:00:00', 30, 'hygiene', 'U0002', 'cancelled', 'phone', '2026-11-20 11:00:00', '2026-12-01 09:00:00')",
        "check_cancellation_kind",
    ),
    "cancelled_without_time": ("UPDATE emr_appointments SET cancelled_at = NULL WHERE id = 3", "check_cancelled_at_status"),
    "time_on_not_cancelled": ("UPDATE emr_appointments SET cancelled_at = '2026-11-20 10:00:00' WHERE id = 7", "check_cancelled_at_status"),
    # 변경 쌍: 새 예약은 원래 날짜 뒤 1~14일, 같은 유형·시술, 새 created_at = 원래 cancelled_at
    "reschedule_too_far": ("UPDATE emr_appointments SET scheduled_at = '2026-12-02 10:00:00' WHERE id = 4", "check_reschedule_pair"),
    "reschedule_earlier": ("UPDATE emr_appointments SET scheduled_at = '2026-11-16 10:00:00' WHERE id = 4", "check_reschedule_pair"),
    "reschedule_other_procedure": ("UPDATE emr_appointments SET planned_procedure_code = 'U0006' WHERE id = 4", "check_reschedule_pair"),
    "reschedule_created_mismatch": ("UPDATE emr_appointments SET created_at = '2026-11-13 09:00:00' WHERE id = 4", "check_reschedule_pair"),
    # 시각 순서
    "created_after_scheduled": ("UPDATE emr_appointments SET created_at = '2026-10-07 10:00:00' WHERE id = 1", "check_created_before_scheduled"),
    "sameday_cancel_after_start": ("UPDATE emr_appointments SET cancelled_at = '2026-11-17 17:00:00' WHERE id = 3", "check_cancel_time"),
    "cancelled_before_created": ("UPDATE emr_appointments SET cancelled_at = '2026-10-29 10:00:00' WHERE id = 3", "check_cancel_time"),
    # 진료 기록 ↔ completed
    "treatment_on_noshow": ("UPDATE emr_treatments SET appointment_id = 7 WHERE id = 1", "check_treatment_on_completed"),
    "completed_without_treatment": ("DELETE FROM emr_treatments WHERE id = 3", "check_completed_one_treatment"),
    "completed_two_treatments": (
        "INSERT INTO emr_treatments VALUES (4, 1, 1, 'U0008', '27', '2026-10-06 10:00:00', 120000, 84000, 36000)",
        "check_completed_one_treatment",
    ),
    "implant_under65_claimed": (  # 1번 환자는 1980년생 — 임플란트 1차는 65세 미만이면 비급여
        "INSERT INTO emr_claims VALUES (3, 1, 1, 'C-00001', '36', '2026-11', 'monthly', '2026-11-03', 'U0013', 910000, NULL, 'pending', NULL, NULL)",
        "check_uninsured_not_claimed",
    ),
    "reschedule_chain": (  # 4번은 3번을 변경한 새 예약 — 다시 변경하지 않는다
        "INSERT INTO emr_appointments (patient_id, staff_id, scheduled_at, duration_min, type, planned_procedure_code,"
        " status, rescheduled_from_id, booked_via, created_at) VALUES (2, 1, '2026-11-20 10:00:00', 30, 'treatment', 'U0005', 'scheduled', 4, 'phone', '2026-11-14 10:00:00')",
        "check_reschedule_once",
    ),
    "reschedule_original_twice": (
        "INSERT INTO emr_appointments (patient_id, staff_id, scheduled_at, duration_min, type, planned_procedure_code,"
        " status, rescheduled_from_id, booked_via, created_at) VALUES (2, 1, '2026-11-20 10:00:00', 30, 'treatment', 'U0005', 'scheduled', 3, 'phone', '2026-11-14 10:00:00')",
        "check_reschedule_once",
    ),
    # 30분 격자
    "off_grid_start": ("UPDATE emr_appointments SET scheduled_at = '2026-11-24 15:15:00' WHERE id = 7", "check_slot_grid"),
    "off_grid_duration": ("UPDATE emr_appointments SET duration_min = 45 WHERE id = 7", "check_slot_grid"),
    # truth_claims ↔ 9~10월 emr_claims
    "truth_claim_missing": ("DELETE FROM truth.truth_claims WHERE claim_id = 1", "check_truth_claims_match"),
    "truth_amount_mismatch": ("UPDATE truth.truth_claims SET claim_amount = 1 WHERE claim_id = 1", "check_truth_claims_match"),
    "truth_points_to_pending": ("UPDATE truth.truth_claims SET claim_id = 2 WHERE claim_id = 1", "check_truth_claims_match"),
    "extra_eob_outside_months": (
        "UPDATE truth.truth_claims SET service_date = '2026-08-06' WHERE outcome = 'extra_in_eob'", "check_truth_claims_match"),
    "caller_phone_real": ("UPDATE ax_calls SET caller_phone = '010-1234-5678' WHERE id = 2", "check_fake_phone"),
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


# 11-24(화) 진료 시작 시각 16개 × 3건 — 하루 상한 검사용으로 시각대 제한(3)을 넘지 않게 채운다
SLOTS_1124 = [f"2026-11-24 {h}:{m}:00" for h, m in
              [("09", "30"), ("10", "00"), ("10", "30"), ("11", "00"), ("11", "30"), ("12", "00"), ("12", "30"),
               ("14", "00"), ("14", "30"), ("15", "00"), ("15", "30"), ("16", "00"), ("16", "30"), ("17", "00"),
               ("17", "30"), ("18", "00")] for _ in range(3)]


def test_daily_cap_weekday(tmp_path):
    conn = make_db(tmp_path)  # 11-24에는 시드 상담 예약 1건(noshow)
    for at in SLOTS_1124[:25]:
        appt(conn, at)
    assert verify.check_daily_cap(conn, CTX) == []
    appt(conn, SLOTS_1124[25])  # 27건 > 26
    assert [v["date"] for v in verify.check_daily_cap(conn, CTX)] == ["2026-11-24"]


def test_daily_cap_saturday_and_cancelled(tmp_path):
    conn = make_db(tmp_path)
    sat = [f"2026-11-28 {h}:{m}:00" for h, m in [("09", "30"), ("10", "00"), ("10", "30"), ("11", "00"), ("11", "30")]
           for _ in range(3)]
    for at in sat[:13]:
        appt(conn, at)
    for at in sat[13:]:
        appt(conn, at, status="cancelled")  # 취소는 세지 않음
    assert verify.check_daily_cap(conn, CTX) == []
    conn.execute("UPDATE emr_appointments SET status = 'completed' WHERE status = 'cancelled' AND date(scheduled_at) = '2026-11-28'")
    assert [v["date"] for v in verify.check_daily_cap(conn, CTX)] == ["2026-11-28"]


def test_slot_cap(tmp_path):
    conn = make_db(tmp_path)
    for _ in range(3):
        appt(conn, "2026-11-24 10:00:00")
    appt(conn, "2026-11-24 10:00:00", status="cancelled")
    assert verify.check_slot_cap(conn, CTX) == []
    appt(conn, "2026-11-24 10:00:00")
    assert [v["at"] for v in verify.check_slot_cap(conn, CTX)] == ["2026-11-24 10:00:00"]


def test_patient_twice_same_day(tmp_path):
    conn = make_db(tmp_path)
    appt(conn, "2026-10-06 15:00:00", type="hygiene", patient=1)
    assert verify.check_patient_same_day(conn, CTX)


def test_planned_procedure_not_null(tmp_path):
    with pytest.raises(sqlite3.IntegrityError):
        make_db(tmp_path).execute("UPDATE emr_appointments SET planned_procedure_code = NULL WHERE id = 7")
    relaxed = tmp_path / "relaxed"
    relaxed.mkdir()
    conn = make_db(relaxed, relaxed=True)
    conn.execute("UPDATE emr_appointments SET planned_procedure_code = NULL WHERE id = 7")
    assert verify.check_planned_procedure(conn, CTX)


def test_confirmed_uses_previous_open_day(tmp_path):
    conn = make_db(tmp_path)
    appt(conn, "2026-12-04 10:00:00", status="scheduled")  # 금요일 — 직전 진료일은 12-02(수), 12-03은 목요일 휴진
    conn.execute("UPDATE emr_appointments SET confirmed = 1 WHERE scheduled_at = '2026-12-04 10:00:00'")
    assert verify.check_confirmed_not_future(conn, CTX)  # TODAY 12-01: 리마인드 아직
    assert verify.check_confirmed_not_future(conn, replace(CTX, today=date(2026, 12, 2))) == []  # 12-02에 리마인드 발송


def test_tooth_no_null(tmp_path):
    conn = make_db(tmp_path, relaxed=True)
    conn.execute("UPDATE emr_treatments SET tooth_no = NULL WHERE id = 2")
    conn.execute("UPDATE emr_claims SET tooth_no = NULL WHERE id = 2")
    assert len(verify.check_tooth_no_not_null(conn, CTX)) == 2


def test_claim_key_duplicate(tmp_path):
    conn = make_db(tmp_path, relaxed=True)
    conn.execute(
        "INSERT INTO emr_claims VALUES (3, 1, 1, 'C-00001', '26', '2026-10', 'supplementary', '2026-10-06', 'U0008',"
        " 84000, '2026-11-02 10:00:00', 'submitted', NULL, NULL)"
    )
    assert verify.check_claim_key_unique(conn, CTX)


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
    assert verify.main(["--db", str(tmp_path / "app.sqlite"), "--truth", str(tmp_path / "nope.sqlite")]) == 1


def test_missing_gen_meta_exits(tmp_path, capsys):
    conn = make_db(tmp_path)
    conn.execute("DELETE FROM truth.truth_gen_meta WHERE key = 'range_start'")
    conn.commit()
    assert verify.main(["--db", str(tmp_path / "app.sqlite"), "--truth", str(tmp_path / "truth.sqlite")]) == 1
    assert "truth_gen_meta" in capsys.readouterr().out


def test_missing_gen_meta_table_exits(tmp_path, capsys):
    make_db(tmp_path).commit()
    truth = sqlite3.connect(tmp_path / "truth.sqlite")
    truth.execute("DROP TABLE truth_gen_meta")
    truth.commit()
    assert verify.main(["--db", str(tmp_path / "app.sqlite"), "--truth", str(tmp_path / "truth.sqlite")]) == 1
    assert "truth_gen_meta" in capsys.readouterr().out


def _future_new_patient(conn, n_appts):
    """첫 예약이 TODAY 이후인 신환(예정 신환) — 비취소 예약 n_appts건"""
    conn.execute("INSERT INTO emr_patients (id, chart_no, name, birth_date, sex, phone, registered_at)"
                 " VALUES (3, 'C-00003', '이신환', '1990-01-01', 'F', '010-0000-0003', '2026-11-28')")
    for day in ("2026-12-04", "2026-12-11")[:n_appts]:
        appt(conn, f"{day} 10:00:00", patient=3, status="scheduled")
    conn.execute("UPDATE emr_appointments SET created_at = '2026-11-28 10:00:00' WHERE patient_id = 3")


def test_future_new_patient_single_appointment(tmp_path):
    conn = make_db(tmp_path)
    _future_new_patient(conn, 1)
    assert verify.check_future_new_patient_single(conn, CTX) == []


def test_future_new_patient_two_appointments(tmp_path):
    conn = make_db(tmp_path)
    _future_new_patient(conn, 2)
    assert [v["patient_id"] for v in verify.check_future_new_patient_single(conn, CTX)] == [3]


def test_old_patient_with_only_future_appointments_is_not_future_new(tmp_path):
    conn = make_db(tmp_path)
    _future_new_patient(conn, 2)
    # 창 시작(10-01) 전에 등록한 구환 — 리콜·치료로 미래 예약만 2건이어도 정상
    conn.execute("UPDATE emr_patients SET registered_at = '2019-03-01' WHERE id = 3")
    assert verify.check_future_new_patient_single(conn, CTX) == []
    assert verify.check_future_new_patient_registered(conn, CTX) == []


def test_future_new_patient_registered_at_first_booking(tmp_path):
    conn = make_db(tmp_path)
    _future_new_patient(conn, 1)
    assert verify.check_future_new_patient_registered(conn, CTX) == []
    # 창 안에서 등록했는데 등록일이 첫 예약 날짜와 다르면 위반
    conn.execute("UPDATE emr_patients SET registered_at = '2026-11-20' WHERE id = 3")
    assert [v["patient_id"] for v in verify.check_future_new_patient_registered(conn, CTX)] == [3]


def test_scale_mismatch_exits(tmp_path, capsys):
    conn = make_db(tmp_path)
    conn.execute("UPDATE truth.truth_gen_meta SET value = 'small' WHERE key = 'scale'")
    conn.commit()
    args = ["--db", str(tmp_path / "app.sqlite"), "--truth", str(tmp_path / "truth.sqlite")]
    assert verify.main(args + ["--scale", "small"]) == 0
    assert verify.main(args + ["--scale", "large"]) == 1  # small DB를 large로 검사하려 함 — 섞인 DB
    assert "scale" in capsys.readouterr().out


def test_unknown_scale_is_argparse_error():
    with pytest.raises(SystemExit):
        verify.main(["--scale", "smal"])


def test_load_ctx_reads_range_from_gen_meta(tmp_path):
    conn = make_db(tmp_path)
    ctx = verify.load_ctx(ROOT / "config" / "generator.yaml", conn)
    assert (ctx.range_start, ctx.range_end) == (date(2026, 10, 1), date(2026, 12, 15))


def test_confirmed_allowed_when_reminder_sent_by_today(tmp_path):
    conn = make_db(tmp_path)
    # 12-01(TODAY) 예약과 12-02 예약은 전날 리마인드가 TODAY 이전·당일이라 confirmed 가능
    conn.execute("UPDATE emr_appointments SET confirmed = 1 WHERE id = 5")
    appt(conn, "2026-12-02 10:00:00", status="scheduled")
    conn.execute("UPDATE emr_appointments SET confirmed = 1 WHERE scheduled_at = '2026-12-02 10:00:00'")
    assert verify.check_confirmed_not_future(conn, CTX) == []


def test_run_survives_closed_day_and_unborn_patient(tmp_path, capsys):
    conn = make_db(tmp_path)
    conn.execute("UPDATE emr_appointments SET scheduled_at = '2026-11-19 10:00:00' WHERE id = 7")  # 목요일
    conn.execute("UPDATE emr_patients SET birth_date = '2027-01-01' WHERE id = 2")
    assert verify.run(conn, CTX) is False
    out = capsys.readouterr().out
    assert "check_business_hours" in out and "check_timestamps_not_future" in out


def test_truth_checks_skip_until_truth_claims_exist(tmp_path):
    conn = make_db(tmp_path)
    conn.execute("DELETE FROM truth.truth_claims")  # EOB 슬롯 착수 전에는 비어 있다
    assert verify.check_truth_claims_match(conn, CTX) == []
    assert verify.check_extra_eob_not_in_ledger(conn, CTX) == []


def test_truth_claims_extra_must_have_no_claim_id(tmp_path):
    conn = make_db(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE truth.truth_claims SET claim_id = 1 WHERE outcome = 'extra_in_eob'")


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
    b.executemany(f"INSERT INTO emr_procedures VALUES ({', '.join('?' * len(rows[0]))})", rows[::-1])
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


def test_event_times_boundaries(tmp_path):
    conn = make_db(tmp_path)
    # 당일 취소는 영업 전이어도 된다(문자·자동응답) — 그날 예약 시각 전이면
    conn.execute("UPDATE emr_appointments SET status = 'cancelled', cancelled_at = '2026-11-24 07:00:00' WHERE id = 7")
    # 점심시간·마감 시각 정각도 영업시간 안
    conn.execute("UPDATE emr_appointments SET created_at = '2026-11-20 13:30:00' WHERE id = 6")
    conn.execute("UPDATE emr_appointments SET created_at = '2026-11-20 18:30:00' WHERE id = 5")
    conn.execute("UPDATE emr_appointments SET created_at = '2026-11-21 13:30:00' WHERE id = 3")  # 토요일 마감 정각
    conn.execute("UPDATE emr_messages SET sent_at = '2026-11-02 23:00:00' WHERE id = 2")  # 환자 답장(in)은 아무 때나
    assert verify.check_event_times_in_hours(conn, CTX) == []
    # 영업 전 취소 허용은 그날 예약에만 — 전날 밤 취소는 위반
    conn.execute("UPDATE emr_appointments SET cancelled_at = '2026-11-23 22:00:00' WHERE id = 7")
    assert [v["id"] for v in verify.check_event_times_in_hours(conn, CTX)] == [7]


def test_rag_checks_skipped_without_chunks(tmp_path, capsys):
    conn = make_db(tmp_path)
    assert verify.run(conn, CTX) is True  # 앱 색인 코드 전 — ax_chunks가 없어도 실패가 아니다
    assert "[SKIP] RAG" in capsys.readouterr().out
    # 청크가 생겨도 RAG 검사가 구현되기 전에는 건너뜀 — 검사 없이 통과한 것처럼 보이지 않게
    conn.execute("CREATE TABLE ax_chunks (id INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO ax_chunks VALUES (1)")
    verify.run(conn, CTX)
    assert "[SKIP] RAG" in capsys.readouterr().out
