"""verify 문자 검사(8단계 — 가상 데이터 생성 설계 §2.5 '8단계 구현 결정'·§5.3)가 위반을 잡는지 — 정상 DB를 한 곳씩 깨뜨린다.
fixture 문자: 1 리마인드(2번 예약) / 2 답장 / 3 예약 확정(1번) / 4 노쇼(7번) / 5 미수 안내(1번 내원)"""

import pytest

from generator import verify
from generator.master import (
    CLINIC_NAME,
    MESSAGE_REPLIES_PAID,
    MESSAGE_REPLIES_PAID_METHOD,
    RECALL_TEMPLATES,
)
from tests.generator.verify_db import CTX, make_db

MESSAGE_CHECKS = [
    verify.check_message_triggers,
    verify.check_message_once,
    verify.check_confirmed_has_reminder,
    verify.check_reply_after_out,
]


def test_valid_db_passes_message_checks(tmp_path):
    conn = make_db(tmp_path)
    assert {c.__name__: c(conn, CTX) for c in MESSAGE_CHECKS if c(conn, CTX)} == {}
    assert all(c in verify.REQUIRED for c in MESSAGE_CHECKS)


M = "UPDATE emr_messages SET "
INS = 'INSERT INTO emr_messages (patient_id, direction, sent_at, body, "trigger", related_appointment_id) VALUES '
# (깨뜨리는 SQL, 잡아야 하는 검사)
BREAKS = {
    "confirm_before_created": (
        M + "sent_at = '2026-10-02 10:59:00' WHERE id = 3",
        "check_message_triggers",
    ),
    "confirm_pre_window_appt": (
        "UPDATE emr_appointments SET created_at = '2026-09-28 10:00:00' WHERE id = 1",
        "check_message_triggers",
    ),
    "confirm_other_patient": (M + "patient_id = 2 WHERE id = 3", "check_message_triggers"),
    "confirm_twice": (
        INS + "(1, 'out', '2026-10-02 11:05:00', '예약 확정', 'appt_confirm', 1)",
        "check_message_once",
    ),
    "reminder_wrong_day": (
        M + "sent_at = '2026-10-30 10:00:00' WHERE id = 1",
        "check_message_triggers",
    ),
    "reminder_not_target": (
        INS + "(2, 'out', '2026-11-16 09:30:00', '리마인드', 'reminder', 3)",
        "check_message_triggers",
    ),
    "reminder_twice": (
        INS + "(1, 'out', '2026-11-02 09:30:00', '리마인드', 'reminder', 2)",
        "check_message_once",
    ),
    "confirmed_without_reminder": (
        "UPDATE emr_appointments SET confirmed = 1 WHERE id = 1",
        "check_confirmed_has_reminder",
    ),
    "noshow_on_completed": (
        M + "related_appointment_id = 2 WHERE id = 4",
        "check_message_triggers",
    ),
    "noshow_before_end": (
        M + "sent_at = '2026-11-24 15:20:00' WHERE id = 4",
        "check_message_triggers",
    ),
    "balance_without_unpaid": (
        M + "related_appointment_id = 2 WHERE id = 5",
        "check_message_triggers",
    ),
    "balance_before_paid": (
        M + "sent_at = '2026-10-06 10:30:00' WHERE id = 5",
        "check_message_triggers",
    ),
    "doc_ready_without_call": (
        INS + "(2, 'out', '2026-11-27 10:00:00', '서류', 'doc_ready', NULL)",
        "check_message_triggers",
    ),
    "out_reply": (M + "direction = 'out' WHERE id = 2", "check_message_triggers"),
    "in_not_reply": (M + "direction = 'in' WHERE id = 3", "check_message_triggers"),
    "message_on_today": (
        M + "sent_at = '2026-12-01 09:30:00' WHERE id = 4",
        "check_message_triggers",
    ),
    "reply_other_patient": (M + "patient_id = 2 WHERE id = 2", "check_reply_after_out"),
    "reply_before_out": (
        M + "sent_at = '2026-11-02 17:00:00' WHERE id = 2",
        "check_reply_after_out",
    ),
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
    conn.execute(
        "UPDATE truth.truth_call_labels SET primary_purpose = 'document_req' WHERE call_uid = 'c-0001'"
    )
    conn.execute("UPDATE ax_calls SET direction = 'inbound' WHERE call_uid = 'c-0001'")
    conn.execute(INS + "(1, 'out', '2026-10-21 10:00:00', '서류', 'doc_ready', NULL)")
    assert verify.check_message_triggers(conn, CTX) == []
    conn.execute(
        "UPDATE ax_calls SET direction = 'outbound' WHERE call_uid = 'c-0001'"
    )  # 병원이 건 통화 뒤에는 안 보냄
    assert verify.check_message_triggers(conn, CTX)
    conn.execute("UPDATE ax_calls SET direction = 'inbound' WHERE call_uid = 'c-0001'")
    conn.execute(M + "sent_at = '2026-10-20 14:00:00' WHERE \"trigger\" = 'doc_ready'")
    assert verify.check_message_triggers(conn, CTX)


def test_message_report_counts_by_trigger(tmp_path):
    conn = make_db(tmp_path)
    assert verify.report_message_counts(conn, CTX) == {
        "appt_confirm": 1,
        "balance": 1,
        "noshow": 1,
        "reminder": 1,
        "reply": 1,
    }
    assert verify.report_message_counts in verify.REPORTS


# 리콜(§2.5): 2번 환자에게 창 이전 위생 내원을 주면 기한 = 그날 + 리콜 주기(12개월). 10월 첫 진료일 = 10/02(금 — 10/01 목
# 휴진) 개원 09:30, 그때 2번 환자의 잡힌 예약은 없다(3번 예약은 10/30에 잡힘)
def _pre_hygiene(conn, day):
    conn.execute(
        "INSERT INTO emr_appointments (patient_id, staff_id, scheduled_at, duration_min, type,"
        " planned_procedure_code, status, booked_via, created_at, chair_no) VALUES"
        " (2, 2, ?, 30, 'hygiene', 'U0002', 'completed', 'phone', '2025-01-02 10:00:00', 2)",
        (f"{day} 10:00:00",),
    )


RECALL = INS + "(2, 'out', ?, ?, 'recall', NULL)"
NAME = "김하늘"


def _body(kind, month=None):
    return RECALL_TEMPLATES[kind].format(clinic=CLINIC_NAME, name=NAME, month=month)


@pytest.mark.parametrize(
    "hygiene, body",
    [("2025-11-10", _body("before", 11)), ("2025-09-10", _body("after"))],
)  # 기한 11월 → 10월에 다가옴 / 기한 9월 → 10월에 지남
def test_recall_valid(tmp_path, hygiene, body):
    conn = make_db(tmp_path)
    _pre_hygiene(conn, hygiene)
    conn.execute(RECALL, ("2026-10-02 09:30:00", body))
    assert verify.check_message_triggers(conn, CTX) == []


@pytest.mark.parametrize(
    "hygiene, sent_at, body",
    [
        ("2025-11-10", "2026-10-02 09:30:00", _body("after")),  # 다가옴인데 지남 문구
        ("2025-11-10", "2026-10-02 09:30:00", _body("before", 12)),  # 달이 틀림
        ("2025-10-10", "2026-10-02 09:30:00", _body("before", 10)),  # 기한이 이번 달 — 보내지 않음
        ("2025-11-10", "2026-10-05 09:30:00", _body("before", 11)),  # 첫 진료일 아님
        ("2025-11-10", "2026-10-02 10:00:00", _body("before", 11)),  # 개원 시각 아님
        (
            "2025-10-10",
            "2026-11-02 09:30:00",
            _body("after"),
        ),  # 11/02엔 3번 예약(11/17)이 잡혀 있음
    ],
)
def test_recall_break(tmp_path, hygiene, sent_at, body):
    conn = make_db(tmp_path)
    _pre_hygiene(conn, hygiene)
    conn.execute(RECALL, (sent_at, body))
    assert verify.check_message_triggers(conn, CTX)


def test_recall_without_hygiene(tmp_path):
    conn = make_db(tmp_path)  # 2번 환자는 completed 위생 내원이 없다
    conn.execute(RECALL, ("2026-10-02 09:30:00", _body("after")))
    assert verify.check_message_triggers(conn, CTX)


@pytest.fixture
def recall_db(tmp_path):
    conn = make_db(tmp_path)
    _pre_hygiene(conn, "2025-09-10")
    return conn


def test_recall_twice_a_month(recall_db):
    recall_db.execute(RECALL, ("2026-10-02 09:30:00", _body("after")))
    recall_db.execute(RECALL, ("2026-10-02 09:30:00", _body("after")))
    assert verify.check_recall_once_a_month(recall_db, CTX)


def test_churn_reply(recall_db):
    """이탈 환자는 이탈일부터 어떤 문자에도 답장하지 않는다 — 이탈일 전 답장은 괜찮다"""
    recall_db.execute(RECALL, ("2026-10-02 09:30:00", _body("after")))
    recall_db.execute(INS + "(2, 'in', '2026-10-02 09:40:00', '네', 'reply', NULL)")
    recall_db.execute("INSERT INTO truth.truth_patient_churn VALUES (2, 'churn', '2026-10-03')")
    assert verify.check_churn_no_reply(recall_db, CTX) == []
    recall_db.execute("UPDATE truth.truth_patient_churn SET churned_on = '2026-10-01'")
    assert verify.check_churn_no_reply(recall_db, CTX)


def test_paid_reply_only_on_balance(tmp_path):
    """이미 냈다는 답장은 미수 안내(5번 문자, 1번 예약) 답장에만 — 리마인드(1번 문자) 답장(2번)을 그 문구로 바꾸면 잡힌다"""
    conn = make_db(tmp_path)
    paid = MESSAGE_REPLIES_PAID[0]
    conn.execute(INS + "(1, 'in', '2026-10-07 10:10:00', ?, 'reply', 1)", (paid,))
    assert verify.check_paid_replies(conn, CTX) == []
    conn.execute(M + "body = ? WHERE id = 2", (paid,))
    assert verify.check_paid_replies(conn, CTX)


def test_ledger_error_balance_reply_is_paid(tmp_path):
    """장부 실수 미수의 안내 답장은 이미 냈다는 문구 — 1번 수납을 입력 누락으로 두면 '다음에 갈 때 낼게요'는 잡힌다"""
    conn = make_db(tmp_path)
    conn.execute(
        "INSERT INTO truth.truth_ledger_errors (kind, payment_id, true_amount, true_paid_at, true_method)"
        " VALUES ('entry_missing', 1, 36000, '2026-10-06 11:00:00', 'card')"
    )
    conn.execute(INS + "(1, 'in', '2026-10-07 10:10:00', '다음에 갈 때 낼게요', 'reply', 1)")
    assert verify.check_paid_replies(conn, CTX)


def test_paid_reply_method_matches(tmp_path):
    """결제수단을 말하는 이미 냈다는 답장은 그 내원의 실제 수단 — 1번 내원은 카드라 이체 문구면 잡힌다"""
    conn = make_db(tmp_path)
    conn.execute(
        INS + "(1, 'in', '2026-10-07 10:10:00', ?, 'reply', 1)",
        (MESSAGE_REPLIES_PAID_METHOD["card"],),
    )
    assert verify.check_paid_replies(conn, CTX) == []
    conn.execute(
        "UPDATE emr_messages SET body = ? WHERE direction = 'in' AND related_appointment_id = 1 AND sent_at = '2026-10-07 10:10:00'",
        (MESSAGE_REPLIES_PAID_METHOD["transfer"],),
    )
    assert verify.check_paid_replies(conn, CTX)
