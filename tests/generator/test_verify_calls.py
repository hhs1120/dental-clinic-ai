"""verify 통화 슬롯 검사(6단계 — 가상 데이터 생성 설계 §4.1·§5.3)가 위반을 잡는지 — 정상 DB를 한 곳씩 깨뜨린다.
fixture 통화: id 1 예약 잡기(c-0003) / 2 수납 안내 outbound(c-0001) / 3 변경·보호자(c-0004) / 4 처음 연락 가격 문의(c-0002)"""

import json
from dataclasses import replace

import pytest

from generator import verify
from tests.generator.verify_db import CTX, make_db

CALL_CHECKS = [
    verify.check_call_labels_match,
    verify.check_call_label_values,
    verify.check_call_slot_count,
    verify.check_call_day,
    verify.check_call_duration,
    verify.check_call_row_order,
    verify.check_booking_call_event,
    verify.check_call_counterpart,
    verify.check_call_facts_as_of,
    verify.check_eval_pool,
    verify.check_blind_pool,
    verify.check_eval_pool_strata,
]


def test_valid_db_passes_call_checks(tmp_path):
    conn = make_db(tmp_path)
    assert {c.__name__: c(conn, CTX) for c in CALL_CHECKS if c(conn, CTX)} == {}
    assert all(c in verify.REQUIRED for c in CALL_CHECKS)


def test_unconfirmed_booking_call_precedes_booking(tmp_path):
    """미확정 예약류 통화는 그날 예약 생성보다 앞 — 통화 뒤 회신해서 확정 (§4.1)"""
    conn = make_db(tmp_path)
    conn.execute("UPDATE truth.truth_call_labels SET appt_confirmed = 0 WHERE call_uid = 'c-0003'")
    conn.execute("UPDATE ax_calls SET started_at = '2026-10-02 10:00:00' WHERE id = 1")
    assert verify.check_booking_call_event(conn, CTX) == []


def _fact(conn, uid, **change):
    fact = json.loads(
        conn.execute(
            "SELECT used_fact FROM truth.truth_call_labels WHERE call_uid = ?", (uid,)
        ).fetchone()[0]
    )
    fact.update(change)
    conn.execute(
        "UPDATE truth.truth_call_labels SET used_fact = ? WHERE call_uid = ?",
        (json.dumps(fact, ensure_ascii=False), uid),
    )


L = "UPDATE truth.truth_call_labels SET "
# (깨뜨리는 SQL, 잡아야 하는 검사)
BREAKS = {
    # 라벨 1:1
    "label_missing": (
        "DELETE FROM truth.truth_call_labels WHERE call_uid = 'c-0001'",
        "check_call_labels_match",
    ),
    "label_orphan": (
        "UPDATE truth.truth_call_labels SET call_uid = 'c-0099' WHERE call_uid = 'c-0001'",
        "check_call_labels_match",
    ),
    # 라벨 값
    "purpose_enum": (
        L
        + "primary_purpose = 'chitchat', accepted_purposes = '[\"chitchat\"]' WHERE call_uid = 'c-0001'",
        "check_call_label_values",
    ),
    "counterpart_enum": (
        L + "counterpart = 'doctor' WHERE call_uid = 'c-0001'",
        "check_call_label_values",
    ),
    "urgency_enum": (L + "urgency = 'high' WHERE call_uid = 'c-0001'", "check_call_label_values"),
    "noise_enum": (
        L + "noise_type = 'static' WHERE call_uid = 'c-0001'",
        "check_call_label_values",
    ),
    "booking_urgent_later_appt": (
        L + "urgency = 'urgent' WHERE call_uid = 'c-0003'",
        "check_call_label_values",
    ),
    "implied_flag_off": (
        L + "wants_appt = 0 WHERE call_uid = 'c-0003'",
        "check_call_label_values",
    ),
    "accepted_not_primary_first": (
        L + "accepted_purposes = '[\"complaint\", \"billing_q\"]' WHERE call_uid = 'c-0001'",
        "check_call_label_values",
    ),
    "accepted_without_flag": (
        L + "complaint = 0 WHERE call_uid = 'c-0001'",
        "check_call_label_values",
    ),
    "flag_without_accepted": (
        L + "accepted_purposes = '[\"billing_q\"]' WHERE call_uid = 'c-0001'",
        "check_call_label_values",
    ),
    "two_topics_single": (
        L + "accepted_purposes = '[\"price_q\"]', wants_appt = 0 WHERE call_uid = 'c-0002'",
        "check_call_label_values",
    ),
    "two_topics_outbound": (
        "UPDATE ax_calls SET direction = 'outbound' WHERE id = 4",
        "check_call_label_values",
    ),
    "emergency_not_urgent": (
        L + 'emergency = 1, accepted_purposes = \'["billing_q", "complaint", "emergency"]\''
        " WHERE call_uid = 'c-0001'",
        "check_call_label_values",
    ),
    "subtopic_not_allowed": (
        L
        + "accepted_purposes = '[\"reschedule\", \"book\"]', wants_appt = 1 WHERE call_uid = 'c-0004'",
        "check_call_label_values",
    ),
    "guardian_noise_on_patient": (
        L + "noise_type = 'guardian_confused' WHERE call_uid = 'c-0003'",
        "check_call_label_values",
    ),
    "pain_noise_wrong_purpose": (
        L + "noise_type = 'ambiguous_pain' WHERE call_uid = 'c-0004'",
        "check_call_label_values",
    ),
    "outbound_wrong_purpose": (
        "UPDATE ax_calls SET direction = 'outbound' WHERE id = 1",
        "check_call_label_values",
    ),
    # 건수
    "extra_slot": (
        "INSERT INTO ax_calls (call_uid, source, direction, started_at, duration_sec)"
        " VALUES ('c-0005', 'generated', 'inbound', '2026-11-27 17:00:00', 150)",
        "check_call_slot_count",
    ),
    # 날짜: 진료일·창 안·TODAY 전
    "call_on_holiday": (
        "UPDATE ax_calls SET started_at = '2026-11-25 10:00:00' WHERE id = 4",
        "check_call_day",
    ),
    "call_on_thursday": (
        "UPDATE ax_calls SET started_at = '2026-11-19 10:00:00' WHERE id = 4",
        "check_call_day",
    ),
    "call_today": (
        "UPDATE ax_calls SET started_at = '2026-12-01 10:00:00' WHERE id = 4",
        "check_call_day",
    ),
    "call_before_window": (
        "UPDATE ax_calls SET started_at = '2026-09-29 10:00:00' WHERE id = 1",
        "check_call_day",
    ),
    "duration_short": (
        "UPDATE ax_calls SET duration_sec = 30 WHERE id = 4",
        "check_call_duration",
    ),
    # 행 순서 = 통화 시각순
    "row_order": (
        "UPDATE ax_calls SET started_at = '2026-11-28 10:00:00' WHERE id = 3",
        "check_call_row_order",
    ),
    # 예약류 ↔ 이벤트
    "book_time_mismatch": (
        "UPDATE ax_calls SET started_at = '2026-10-02 11:05:00' WHERE id = 1",
        "check_booking_call_event",
    ),
    "unconfirmed_call_at_booking": (
        L + "appt_confirmed = 0 WHERE call_uid = 'c-0003'",
        "check_booking_call_event",
    ),
    "book_on_desk_appt": (
        "UPDATE emr_appointments SET booked_via = 'desk' WHERE id = 1",
        "check_booking_call_event",
    ),
    "reschedule_not_pair": (
        "UPDATE emr_appointments SET rescheduled_from_id = NULL WHERE id = 4",
        "check_booking_call_event",
    ),
    "booking_other_patient": (
        "UPDATE ax_calls SET patient_id = 1, caller_phone = '010-0000-0001' WHERE id = 3",
        "check_booking_call_event",
    ),
    "book_without_appointment": (
        L + "used_fact = '{\"call_date\": \"2026-10-02\"}' WHERE call_uid = 'c-0003'",
        "check_booking_call_event",
    ),
    # 상대 ↔ 환자·발신자
    "guardian_for_adult": (
        L + "counterpart = 'guardian' WHERE call_uid = 'c-0003'",
        "check_call_counterpart",
    ),
    "patient_for_child": (
        L + "counterpart = 'patient' WHERE call_uid = 'c-0004'",
        "check_call_counterpart",
    ),
    "side_without_patient": (
        "UPDATE ax_calls SET patient_id = NULL WHERE id = 2",
        "check_call_counterpart",
    ),
    "caller_phone_not_patient": (
        "UPDATE ax_calls SET caller_phone = '010-0000-0555' WHERE id = 2",
        "check_call_counterpart",
    ),
    "outsider_with_patient": (
        "UPDATE ax_calls SET patient_id = 1 WHERE id = 4",
        "check_call_counterpart",
    ),
    "outsider_patient_phone": (
        "UPDATE ax_calls SET caller_phone = '010-0000-0001' WHERE id = 4",
        "check_call_counterpart",
    ),
    "outbound_to_outsider": (
        "UPDATE truth.truth_call_labels SET counterpart = 'insurer' WHERE call_uid = 'c-0001'",
        "check_call_counterpart",
    ),
    "new_caller_book_old_patient": (
        L + "counterpart = 'new_caller' WHERE call_uid = 'c-0003'",
        "check_call_counterpart",
    ),
    # 통화 시점 사실
    "pool_marking_without_label_split": (
        L + "split = 'silver' WHERE call_uid = 'c-0003'",
        "check_eval_pool",
    ),
    "pool_order_gap": ("UPDATE ax_calls SET eval_sort_order = 3 WHERE id = 4", "check_eval_pool"),
    "pool_test_first": (
        "UPDATE ax_calls SET eval_sort_order = 3 - eval_sort_order WHERE in_eval_pool = 1",
        "check_eval_pool",
    ),
    "pool_too_big": (
        "UPDATE ax_calls SET in_eval_pool = 1, eval_sort_order = 3 WHERE id = 2",
        "check_eval_pool",
    ),
    "blind_outside_pool": (
        "UPDATE ax_calls SET in_blind_pool = 1, blind_sort_order = 2 WHERE id = 2",
        "check_blind_pool",
    ),
    "blind_order_missing": (
        "UPDATE ax_calls SET blind_sort_order = NULL WHERE id = 1",
        "check_blind_pool",
    ),
}


@pytest.mark.parametrize("case", BREAKS)
def test_call_break_is_caught(tmp_path, case):
    sql, check = BREAKS[case]
    conn = make_db(tmp_path)
    conn.execute(sql)
    assert getattr(verify, check)(conn, CTX), f"{check}가 {case}를 못 잡음"


# 사실 깨뜨리기 — 통화 시각 이후 사실, 장부와 다른 잔액·내원·다음 예약·주의사항
FACT_BREAKS = {
    "balance_after_call": ("c-0003", {"balance": 16000}),  # 10/06 진료 미수는 10/02 통화 뒤
    "balance_wrong": ("c-0001", {"balance": 0}),
    "visit_after_call": (
        "c-0003",
        {"recent_visits": [{"date": "2026-10-06", "procedure": "신경치료", "tooth": "26"}]},
    ),
    "next_appt_not_yet_booked": (
        "c-0001",
        {
            "next_appointment": {
                "appointment_id": 5,
                "date": "2026-12-01",
                "time": "14:00",
                "procedure": "신경치료",
            }
        },
    ),
    "next_appt_cancelled": (
        "c-0004",
        {
            "next_appointment": {
                "appointment_id": 3,
                "date": "2026-11-17",
                "time": "10:00",
                "procedure": "레진",
            }
        },
    ),
    "alert_not_yet": ("c-0003", {"alerts": ["고혈압", "마취가 잘 안 됨"]}),
    "patient_mismatch": ("c-0001", {"patient": {"id": 2, "name": "김하늘", "age": 8, "sex": "F"}}),
    "call_date_mismatch": ("c-0001", {"call_date": "2026-10-21"}),
    "appointment_detail_wrong": (
        "c-0003",
        {"appointment": {"date": "2026-10-07", "time": "10:00", "procedure": "신경치료"}},
    ),
    "reschedule_from_wrong": (
        "c-0004",
        {
            "appointment": {
                "date": "2026-11-18",
                "time": "10:00",
                "procedure": "레진",
                "from_date": "2026-11-16",
                "from_time": "10:00",
            }
        },
    ),
}


@pytest.mark.parametrize("case", FACT_BREAKS)
def test_fact_break_is_caught(tmp_path, case):
    uid, change = FACT_BREAKS[case]
    conn = make_db(tmp_path)
    _fact(conn, uid, **change)
    assert verify.check_call_facts_as_of(conn, CTX), f"{case}를 못 잡음"


def test_patient_facts_required_when_patient_linked(tmp_path):
    """환자가 연결된 통화(환자 측·예약류)에 환자 사실이 없으면 실패"""
    conn = make_db(tmp_path)
    conn.execute(L + "used_fact = '{\"call_date\": \"2026-10-20\"}' WHERE call_uid = 'c-0001'")
    assert verify.check_call_facts_as_of(conn, CTX)


def test_confirm_call_on_reminder_day_after_message(tmp_path):
    """확인 통화: confirmed 예약, 시각은 리마인드 날(직전 진료일) 개원 뒤, 리마인드 문자 sent_at 이후"""
    conn = make_db(tmp_path)
    conn.execute(
        "UPDATE emr_appointments SET confirmed = 1 WHERE id = 2"
    )  # 11/03 예약 → 리마인드 날 11/02
    conn.execute(
        "UPDATE ax_calls SET started_at = '2026-11-02 18:30:00', direction = 'outbound' WHERE id = 3"
    )
    conn.execute(
        L
        + "primary_purpose = 'confirm', accepted_purposes = '[\"confirm\"]', counterpart = 'patient',"
        " noise_type = NULL WHERE call_uid = 'c-0004'"
    )
    conn.execute("UPDATE ax_calls SET patient_id = 1 WHERE id = 3")
    _fact(
        conn,
        "c-0004",
        appointment_id=2,
        call_date="2026-11-02",
        patient={"id": 1, "name": "김철수", "age": 46, "sex": "M"},
        recent_visits=[{"date": "2026-10-06", "procedure": "신경치료", "tooth": "26"}],
        balance=16000,
        next_appointment={
            "appointment_id": 2,
            "date": "2026-11-03",
            "time": "10:00",
            "procedure": "스케일링",
        },
        family=["김하늘"],
        alerts=["고혈압", "마취가 잘 안 됨"],
        open_plan_items=[
            {"procedure": "신경치료", "tooth": "26"},
            {"procedure": "크라운", "tooth": "26"},
        ],
        appointment={"date": "2026-11-03", "time": "10:00", "procedure": "스케일링"},
    )
    # 순서가 바뀌므로(11/02 < 10/20 아님) 행 순서 검사는 보지 않는다
    assert verify.check_booking_call_event(conn, CTX) == []
    assert verify.check_call_facts_as_of(conn, CTX) == []
    conn.execute(
        "UPDATE ax_calls SET started_at = '2026-11-02 17:59:00' WHERE id = 3"
    )  # 문자(18:00) 전
    assert verify.check_booking_call_event(conn, CTX)
    conn.execute(
        "UPDATE ax_calls SET started_at = '2026-10-30 18:30:00' WHERE id = 3"
    )  # 리마인드 날 아님
    assert verify.check_booking_call_event(conn, CTX)
    conn.execute("UPDATE ax_calls SET started_at = '2026-11-02 18:30:00' WHERE id = 3")
    conn.execute("UPDATE emr_appointments SET confirmed = 0 WHERE id = 2")
    assert verify.check_booking_call_event(conn, CTX)


def test_booking_event_used_once(tmp_path):
    """이벤트 하나에 통화 최대 1건 — 같은 예약을 두 통화가 쓰면 실패"""
    conn = make_db(tmp_path)
    conn.execute(
        "INSERT INTO ax_calls (id, call_uid, source, patient_id, caller_phone, caller_name, direction,"
        " started_at, duration_sec) VALUES (5, 'c-0005', 'generated', 1, '010-0000-0001', '김철수', 'inbound',"
        " '2026-10-02 11:00:00', 150)"
    )
    row = conn.execute(
        "SELECT * FROM truth.truth_call_labels WHERE call_uid = 'c-0003'"
    ).fetchone()
    conn.execute(
        f"INSERT INTO truth.truth_call_labels VALUES ({', '.join('?' * len(row))})",
        ("c-0005", *tuple(row)[1:-1], "silver"),
    )
    assert verify.check_booking_call_event(conn, CTX)


def test_eval_pool_strata(tmp_path):
    """층화: 희귀 카테고리는 전사의 EVAL_RARE_SHARE(반올림), 카테고리마다 dev·test 차이 ≤ 1"""
    conn = make_db(tmp_path)
    assert verify.check_eval_pool_strata(conn, CTX) == []
    # 희귀(불만) 5건 중 풀 3건이어야 함 — 2건만 넣으면 실패
    conn.execute("DELETE FROM ax_calls")
    conn.execute("DELETE FROM truth.truth_call_labels")
    for i in range(1, 6):
        pool = i <= 2
        conn.execute(
            "INSERT INTO ax_calls (id, call_uid, source, direction, started_at, duration_sec, in_eval_pool,"
            " eval_sort_order) VALUES (?, ?, 'generated', 'inbound', '2026-11-27 10:00:00', 150,"
            " ?, ?)",
            (i, f"c-{i:04d}", int(pool), i if pool else None),
        )
        conn.execute(
            "INSERT INTO truth.truth_call_labels VALUES (?, 'complaint', '[\"complaint\"]', 'patient',"
            " 'normal', 0, 1, 0, 0, 0, 1, NULL, '{}', ?)",
            (f"c-{i:04d}", {1: "dev", 2: "test"}.get(i, "silver")),
        )
    assert verify.check_eval_pool_strata(conn, replace(CTX, eval_pool=2))
    # 3건이면 통과(dev 2·test 1)
    conn.execute("UPDATE ax_calls SET in_eval_pool = 1, eval_sort_order = 3 WHERE id = 3")
    conn.execute(L + "split = 'dev' WHERE call_uid = 'c-0003'")
    assert verify.check_eval_pool_strata(conn, replace(CTX, eval_pool=3)) == []
    # dev 3·test 0이면 실패
    conn.execute(L + "split = 'dev' WHERE call_uid = 'c-0002'")
    assert verify.check_eval_pool_strata(conn, replace(CTX, eval_pool=3))
    # 일반 카테고리(예약 잡기) 4건 이상인데 split당 1건이면 최소(EVAL_MIN_PER_SPLIT) 미달
    conn.execute(
        L
        + "primary_purpose = 'book', accepted_purposes = '[\"book\"]', complaint = 0, wants_appt = 1"
    )
    conn.execute(L + "split = 'silver' WHERE call_uid = 'c-0003'")
    conn.execute("UPDATE ax_calls SET in_eval_pool = 0, eval_sort_order = NULL WHERE id = 3")
    assert any(
        v["problem"] == "split당 최소 건수 미달"
        for v in verify.check_eval_pool_strata(conn, replace(CTX, eval_pool=2))
    )


def test_call_mix_warnings(tmp_path):
    """목적·노이즈 분포 경고 — fixture 4건은 목표와 멀어 둘 다 위반, 리포트는 상대·방향 비율"""
    conn = make_db(tmp_path)
    assert verify.check_call_purpose_mix(conn, CTX)
    assert verify.check_call_noise_mix(conn, CTX)
    assert (
        verify.check_call_purpose_mix in verify.WARNINGS
        and verify.check_call_noise_mix in verify.WARNINGS
    )
    # fixture는 창 앞뒤 절반에 2건씩 — 최근 가중(뒤 절반 비중 > 1/2)과 멀다
    assert verify.check_call_recency(conn, CTX) and verify.check_call_recency in verify.WARNINGS
    report = verify.report_call_mix(conn, CTX)
    assert report["counterpart"] == {"patient": 0.5, "guardian": 0.25, "new_caller": 0.25}
    assert report["outbound"] == 0.25
    assert report["dev_n"] == {"book": 1}  # 평가 설계 §2.1 — 카테고리별 dev n


def test_call_recency_passes_near_target(tmp_path, monkeypatch):
    """가중치가 1(고르게)이면 기대값이 진료일 비율(≈ 1/2)이라 fixture 앞뒤 2건씩이 허용치(CALL_RECENCY_TOLERANCE) 안"""
    from generator import master

    conn = make_db(tmp_path)
    monkeypatch.setattr(master, "CALL_RECENCY_WEIGHT", 1)
    assert verify.check_call_recency(conn, CTX) == []


# ── 통화 전사 (7단계, §4.1 '7단계 구현 결정') ──────────────────


def test_transcripts_filled_required(tmp_path):
    conn = make_db(tmp_path)
    assert verify.check_call_transcripts(conn, CTX) == []
    assert verify.check_call_transcripts in verify.REQUIRED
    conn.execute("UPDATE ax_calls SET has_transcript = 0, transcript = NULL WHERE id = 2")
    assert [v["id"] for v in verify.check_call_transcripts(conn, CTX)] == [2]


def test_transcript_warnings(tmp_path):
    """화자 형식·꼭 넣을 사실·라벨 어휘 비율 — 경고"""
    conn = make_db(tmp_path)
    checks = (
        verify.check_transcript_speakers,
        verify.check_transcript_facts,
        verify.check_transcript_label_words,
    )
    assert {c.__name__: c(conn, CTX) for c in checks if c(conn, CTX)} == {}
    assert all(c in verify.WARNINGS for c in checks)
    conn.execute("UPDATE ax_calls SET transcript = '손님: 안녕하세요' WHERE id = 1")
    assert [v["id"] for v in verify.check_transcript_speakers(conn, CTX)] == [1]
    conn.execute(
        "UPDATE ax_calls SET transcript = '환자: 하루 미룰 수 있을까요?\n직원: 네, 11월 18일로요.' WHERE id = 3"
    )
    assert [v["id"] for v in verify.check_transcript_facts(conn, CTX)] == [
        1,
        3,
    ]  # 1은 위에서 덮어씀, 3은 원래 날짜 11월 17일 없음
    conn.execute("UPDATE ax_calls SET transcript = '환자: 불만이 있어요\n직원: 네' WHERE id = 2")
    assert verify.check_transcript_label_words(conn, CTX)  # 4건 중 1건 = 25% ≥ 20%
    conn.execute(
        "UPDATE ax_calls SET transcript = '환자: 예약변경 하려고요\n직원: 네' WHERE id = 2"
    )
    assert verify.check_transcript_label_words(conn, CTX)  # 띄어쓰기만 다른 '예약 변경'


def test_label_tokens_in_stored_text(tmp_path):
    """저장 텍스트(전사·차트)에 영문 라벨 값(two_topics·urgent·R01 등)이 없음 — 경고"""
    conn = make_db(tmp_path)
    assert (
        verify.check_label_tokens(conn, CTX) == [] and verify.check_label_tokens in verify.WARNINGS
    )
    conn.execute(
        "UPDATE ax_calls SET transcript = '환자: 안녕하세요 [noise: two_topics]\n직원: 네' WHERE id = 2"
    )
    conn.execute("UPDATE emr_chart_notes SET note = note || ' (R01)' WHERE treatment_id = 3")
    assert {(v["table"], v["token"]) for v in verify.check_label_tokens(conn, CTX)} == {
        ("ax_calls", "two_topics"),
        ("emr_chart_notes", "R01"),
    }


# 이탈·리콜 문의·경과 확인 (§2.2·§4.1): 시드 c-0001 = 1번 환자 10/20 수납 문의 발신
def test_churn_patient_calls(tmp_path):
    """이탈일 뒤 이탈 환자의 통화는 없다 — 수납 문의(10/20)는 이탈일(10/10) 뒤라 잡히고, 이탈일이 통화 뒤면 통과"""
    conn = make_db(tmp_path)
    conn.execute("INSERT INTO truth.truth_patient_churn VALUES (1, 'churn', '2026-10-10')")
    assert verify.check_churn_calls(conn, CTX)
    conn.execute("UPDATE truth.truth_patient_churn SET churned_on = '2026-10-21'")
    assert verify.check_churn_calls(conn, CTX) == []


def _recall_q(conn):
    conn.execute("UPDATE ax_calls SET direction = 'inbound' WHERE call_uid = 'c-0001'")
    conn.execute(
        "UPDATE truth.truth_call_labels SET primary_purpose = 'recall_q',"
        " accepted_purposes = '[\"recall_q\"]', complaint = 0, noise_type = NULL"
        " WHERE call_uid = 'c-0001'"
    )


@pytest.mark.parametrize(
    "sent_at, fact_at, ok",
    [
        (None, "2026-10-15 09:00:00", False),  # 리콜 문자 없음
        ("2026-10-15 09:00:00", "2026-10-15 09:00:00", True),  # 5일 전
        ("2026-10-20 09:00:00", "2026-10-20 09:00:00", True),  # 그날 아침
        ("2026-10-01 09:00:00", "2026-10-01 09:00:00", False),  # 19일 전 — 규칙 창 밖
        ("2026-10-20 16:00:00", "2026-10-20 16:00:00", False),  # 통화(15:00) 뒤
        ("2026-10-15 09:00:00", None, False),  # 사실에 받은 문자 없음
        ("2026-10-15 09:00:00", "2026-10-14 09:00:00", False),  # 사실의 문자 ≠ 보낸 문자
    ],
)
def test_recall_q_follows_recall_text(tmp_path, sent_at, fact_at, ok):
    """리콜 문의(수신)는 통화 전 규칙 창(master.CALL_PATIENT_RULES) 안에 그 환자가 받은 리콜 문자가 있고, 사실
    recall_text가 그 문자를 가리킨다"""
    conn = make_db(tmp_path)
    _recall_q(conn)
    if sent_at:
        conn.execute(
            "INSERT INTO emr_messages VALUES (9, 1, 'out', ?, '리콜', 'recall', NULL)", (sent_at,)
        )
    if fact_at:
        conn.execute(
            "UPDATE truth.truth_call_labels SET used_fact = json_set(used_fact, '$.recall_text',"
            " json_object('sent_at', ?, 'kind', 'after', 'due_month', 9)) WHERE call_uid = 'c-0001'",
            (fact_at,),
        )
    assert (verify.check_recall_q_calls(conn, CTX) == []) == ok
    assert verify.check_recall_q_calls in verify.REQUIRED


def test_recall_q_is_inbound(tmp_path):
    """리콜 문의는 발신 목적이 아니다 — 수신이면 통과, 발신이면 라벨 검사에 잡힌다"""
    conn = make_db(tmp_path)
    _recall_q(conn)
    assert verify.check_call_label_values(conn, CTX) == []
    conn.execute("UPDATE ax_calls SET direction = 'outbound' WHERE call_uid = 'c-0001'")
    assert any(
        v["problem"] == "outbound가 아닌 목적" for v in verify.check_call_label_values(conn, CTX)
    )


@pytest.mark.parametrize(
    "started_at, ok", [("2026-10-08 15:00:00", True), ("2026-10-20 15:00:00", False)]
)
def test_followup_topic_is_recent_treatment(tmp_path, started_at, ok):
    """경과 확인 발신의 주제 = 1~7일 전 진료(1번 환자 10/06 신경치료) — 10/20이면 창 안 진료가 없어 잡힌다"""
    conn = make_db(tmp_path)
    conn.execute("UPDATE ax_calls SET started_at = ? WHERE call_uid = 'c-0001'", (started_at,))
    conn.execute(
        "UPDATE truth.truth_call_labels SET primary_purpose = 'treatment_q',"
        " used_fact = json_set(used_fact, '$.topic_procedure', '신경치료') WHERE call_uid = 'c-0001'"
    )
    assert (verify.check_followup_topic(conn, CTX) == []) == ok


def test_already_paid_calls_lower_bound(tmp_path):
    """메타 ledger_debt_calls만큼 already_paid 수납 문의 발신이 있어야 — 시드엔 없어 1이면 잡힌다"""
    conn = make_db(tmp_path)
    assert verify.check_already_paid_calls(conn, CTX) == []
    assert verify.check_already_paid_calls(conn, replace(CTX, ledger_debt_calls=1))
