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
    # ax_calls (6단계)
    "fk_dangling_call": ("UPDATE ax_calls SET patient_id = 999 WHERE id = 1", "check_foreign_keys"),
    "call_future": ("UPDATE ax_calls SET started_at = '2026-12-02 10:00:00' WHERE id = 2", "check_timestamps_not_future"),
    "call_uid_purpose": ("UPDATE ax_calls SET call_uid = 'reschedule:0001' WHERE id = 1", "check_call_uid_format"),
    "call_uid_suffix": ("UPDATE ax_calls SET call_uid = 'c-0001-book' WHERE id = 1", "check_call_uid_format"),
    "call_label_column": ("ALTER TABLE ax_calls ADD COLUMN primary_purpose TEXT", "check_ax_calls_columns"),
    # truth_claims (심사결과 슬롯)
    "extra_claim_review_in_ledger": (
        "UPDATE truth.truth_claims SET service_date = '2026-11-03', procedure_code = 'U0001'"
        " WHERE outcome = 'extra_in_claim_review'",
        "check_extra_claim_review_not_in_ledger",
    ),
    # TODAY 구간 (가상 데이터 생성 설계 §5.3)
    "created_future": ("UPDATE emr_appointments SET created_at = '2026-12-02 10:00:00' WHERE id = 6", "check_timestamps_not_future"),
    "cancelled_future": ("UPDATE emr_appointments SET cancelled_at = '2026-12-02 10:00:00' WHERE id = 3", "check_timestamps_not_future"),
    "registered_future": ("UPDATE emr_patients SET registered_at = '2026-12-03' WHERE id = 2", "check_timestamps_not_future"),
    "born_future": ("UPDATE emr_patients SET birth_date = '2027-01-01' WHERE id = 2", "check_timestamps_not_future"),
    "confirmed_before_reminder": ("UPDATE emr_appointments SET confirmed = 1 WHERE id = 6", "check_confirmed_not_future"),
    # 리마인드 대상 = 직전 진료일 전에 잡혔고 그날까지 취소되지 않은 예약 (12-01 예약의 직전 진료일은 11-30)
    "confirmed_booked_on_reminder_day": ("UPDATE emr_appointments SET confirmed = 1, created_at = '2026-11-30 10:00:00' WHERE id = 5",
                                         "check_confirmed_reminded"),
    "confirmed_cancelled_before_reminder": ("UPDATE emr_appointments SET confirmed = 1 WHERE id = 3",  # 11-14 변경 취소
                                            "check_confirmed_reminded"),
    "cancel_neither_kind": ("UPDATE emr_appointments SET rescheduled_from_id = NULL WHERE id = 4", "check_cancellation_kind"),
    "sameday_cancel_on_today": (
        "INSERT INTO emr_appointments (patient_id, staff_id, scheduled_at, duration_min, type,"
        " planned_procedure_code, status, booked_via, created_at, cancelled_at) VALUES"
        " (2, 2, '2026-12-01 15:00:00', 30, 'hygiene', 'U0002', 'cancelled', 'phone', '2026-11-20 11:00:00', '2026-12-01 09:00:00')",
        "check_cancellation_kind",
    ),
    "cancelled_without_time": ("UPDATE emr_appointments SET cancelled_at = NULL WHERE id = 3", "check_cancelled_at_status"),
    "time_on_not_cancelled": ("UPDATE emr_appointments SET cancelled_at = '2026-11-20 10:00:00' WHERE id = 7", "check_cancelled_at_status"),
    # 변경 쌍: 원래 예약은 cancelled, 새 created_at = 원래 cancelled_at
    "reschedule_original_not_cancelled": ("UPDATE emr_appointments SET status = 'completed' WHERE id = 3",
                                          "check_reschedule_pair"),  # cancelled_at은 그대로 — 상태만 어김
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
    # truth_claims ↔ 9~10월 emr_claims
    "truth_claim_missing": ("DELETE FROM truth.truth_claims WHERE claim_id = 1", "check_truth_claims_match"),
    "truth_amount_mismatch": ("UPDATE truth.truth_claims SET claim_amount = 1 WHERE claim_id = 1", "check_truth_claims_match"),
    "truth_points_to_pending": ("UPDATE truth.truth_claims SET claim_id = 2 WHERE claim_id = 1", "check_truth_claims_match"),
    "extra_claim_review_outside_months": (
        "UPDATE truth.truth_claims SET service_date = '2026-08-06' WHERE outcome = 'extra_in_claim_review'", "check_truth_claims_match"),
    "caller_phone_real": ("UPDATE ax_calls SET caller_phone = '010-1234-5678' WHERE id = 2", "check_fake_phone"),
    "real_phone": ("UPDATE emr_patients SET phone = '010-1234-5678' WHERE id = 2", "check_fake_phone"),
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
    appt(conn, "2026-12-04 10:00:00", status="scheduled")  # 금요일 — 직전 진료일은 12-02(수), 12-03은 목요일 휴진
    conn.execute("UPDATE emr_appointments SET confirmed = 1 WHERE scheduled_at = '2026-12-04 10:00:00'")
    assert verify.check_confirmed_not_future(conn, CTX)  # TODAY 12-01: 리마인드 아직
    assert verify.check_confirmed_not_future(conn, replace(CTX, today=date(2026, 12, 2))) == []  # 12-02에 리마인드 발송


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
    assert "check_timestamps_not_future" in out


def test_truth_checks_skip_until_truth_claims_exist(tmp_path):
    conn = make_db(tmp_path)
    conn.execute("DELETE FROM truth.truth_claims")  # 심사결과 슬롯 착수 전에는 비어 있다
    assert verify.check_truth_claims_match(conn, CTX) == []
    assert verify.check_extra_claim_review_not_in_ledger(conn, CTX) == []


def test_truth_claims_extra_must_have_no_claim_id(tmp_path):
    conn = make_db(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE truth.truth_claims SET claim_id = 1 WHERE outcome = 'extra_in_claim_review'")


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


def test_rag_checks_skipped_without_chunks(tmp_path, capsys):
    conn = make_db(tmp_path)
    assert verify.run(conn, CTX) is True  # 앱 색인 코드 전 — ax_chunks가 없어도 실패가 아니다
    assert "[SKIP] RAG" in capsys.readouterr().out
    # 청크가 생겨도 RAG 검사가 구현되기 전에는 건너뜀 — 검사 없이 통과한 것처럼 보이지 않게
    conn.execute("CREATE TABLE ax_chunks (id INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO ax_chunks VALUES (1)")
    verify.run(conn, CTX)
    assert "[SKIP] RAG" in capsys.readouterr().out
