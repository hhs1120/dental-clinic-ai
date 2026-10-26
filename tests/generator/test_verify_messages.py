"""verify 문자 검사(8단계 — 가상 데이터 생성 설계 §2.5 '8단계 구현 결정'·§5.3)가 위반을 잡는지 — 정상 DB를 한 곳씩 깨뜨린다.
fixture 문자: 1 리마인드(2번 예약) / 2 답장 / 3 예약 확정(1번) / 4 노쇼(7번) / 5 미수 안내(1번 내원)"""
import pytest

from generator import verify
from tests.generator.verify_db import CTX, make_db

MESSAGE_CHECKS = [verify.check_message_triggers, verify.check_message_once, verify.check_confirmed_has_reminder,
                  verify.check_reply_after_out]


def test_valid_db_passes_message_checks(tmp_path):
    conn = make_db(tmp_path)
    assert {c.__name__: c(conn, CTX) for c in MESSAGE_CHECKS if c(conn, CTX)} == {}
    assert all(c in verify.REQUIRED for c in MESSAGE_CHECKS)


M = "UPDATE emr_messages SET "
INS = "INSERT INTO emr_messages (patient_id, direction, sent_at, body, \"trigger\", related_appointment_id) VALUES "
# (깨뜨리는 SQL, 잡아야 하는 검사)
BREAKS = {
    "confirm_before_created": (M + "sent_at = '2026-10-02 10:59:00' WHERE id = 3", "check_message_triggers"),
    "confirm_pre_window_appt": ("UPDATE emr_appointments SET created_at = '2026-09-28 10:00:00' WHERE id = 1",
                                "check_message_triggers"),
    "confirm_other_patient": (M + "patient_id = 2 WHERE id = 3", "check_message_triggers"),
    "confirm_twice": (INS + "(1, 'out', '2026-10-02 11:05:00', '예약 확정', 'appt_confirm', 1)", "check_message_once"),
    "reminder_wrong_day": (M + "sent_at = '2026-10-30 10:00:00' WHERE id = 1", "check_message_triggers"),
    "reminder_not_target": (INS + "(2, 'out', '2026-11-16 09:30:00', '리마인드', 'reminder', 3)", "check_message_triggers"),
    "reminder_twice": (INS + "(1, 'out', '2026-11-02 09:30:00', '리마인드', 'reminder', 2)", "check_message_once"),
    "confirmed_without_reminder": ("UPDATE emr_appointments SET confirmed = 1 WHERE id = 1", "check_confirmed_has_reminder"),
    "noshow_on_completed": (M + "related_appointment_id = 2 WHERE id = 4", "check_message_triggers"),
    "noshow_before_end": (M + "sent_at = '2026-11-24 15:20:00' WHERE id = 4", "check_message_triggers"),
    "balance_without_unpaid": (M + "related_appointment_id = 2 WHERE id = 5", "check_message_triggers"),
    "balance_before_paid": (M + "sent_at = '2026-10-06 10:30:00' WHERE id = 5", "check_message_triggers"),
    "doc_ready_without_call": (INS + "(2, 'out', '2026-11-27 10:00:00', '서류', 'doc_ready', NULL)", "check_message_triggers"),
    "out_reply": (M + "direction = 'out' WHERE id = 2", "check_message_triggers"),
    "in_not_reply": (M + "direction = 'in' WHERE id = 3", "check_message_triggers"),
    "message_on_today": (M + "sent_at = '2026-12-01 09:30:00' WHERE id = 4", "check_message_triggers"),
    "reply_other_patient": (M + "patient_id = 2 WHERE id = 2", "check_reply_after_out"),
    "reply_before_out": (M + "sent_at = '2026-11-02 17:00:00' WHERE id = 2", "check_reply_after_out"),
}


@pytest.mark.parametrize("case", BREAKS)
def test_message_break_is_caught(tmp_path, case):
    sql, check = BREAKS[case]
    conn = make_db(tmp_path)
    conn.execute(sql)
    assert getattr(verify, check)(conn, CTX), f"{check}가 {case}를 못 잡음"


def test_doc_ready_after_document_request_call(tmp_path):
    """서류 준비 문자는 같은 환자의 서류 요청 inbound 통화 뒤 — fixture 통화 c-0001(10/20 15:00, 1번 환자)을 서류 요청으로"""
    conn = make_db(tmp_path)
    conn.execute("UPDATE truth.truth_call_labels SET primary_purpose = 'document_req' WHERE call_uid = 'c-0001'")
    conn.execute("UPDATE ax_calls SET direction = 'inbound' WHERE call_uid = 'c-0001'")
    conn.execute(INS + "(1, 'out', '2026-10-21 10:00:00', '서류', 'doc_ready', NULL)")
    assert verify.check_message_triggers(conn, CTX) == []
    conn.execute("UPDATE ax_calls SET direction = 'outbound' WHERE call_uid = 'c-0001'")  # 병원이 건 통화 뒤에는 안 보냄
    assert verify.check_message_triggers(conn, CTX)
    conn.execute("UPDATE ax_calls SET direction = 'inbound' WHERE call_uid = 'c-0001'")
    conn.execute(M + "sent_at = '2026-10-20 14:00:00' WHERE \"trigger\" = 'doc_ready'")
    assert verify.check_message_triggers(conn, CTX)


def test_message_report_counts_by_trigger(tmp_path):
    conn = make_db(tmp_path)
    assert verify.report_message_counts(conn, CTX) == {"appt_confirm": 1, "balance": 1, "noshow": 1, "reminder": 1,
                                                       "reply": 1}
    assert verify.report_message_counts in verify.REPORTS
