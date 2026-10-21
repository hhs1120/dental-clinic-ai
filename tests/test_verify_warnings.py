"""verify 경고 검사(분포·치아)가 위반을 잡는지, 리포트(수치만)가 분모를 맞게 세는지 — 가상 데이터 생성 설계 §5.3."""
from dataclasses import replace

import pytest
from datetime import date

from generator import verify
from generator.master import ACTIVE_SHARE
from tests.verify_db import CTX, appt, make_db

LARGE = replace(CTX, range_start=date(2026, 2, 1))  # 활동 창(6개월)을 다 덮는 생성 기간


def test_valid_db_tooth_checks_pass(tmp_path):
    conn = make_db(tmp_path)
    for check in (verify.check_tooth_level, verify.check_child_procedures):
        assert check(conn, CTX) == [], check.__name__


# (깨뜨리는 SQL, 잡아야 하는 검사). 시드 진료: 1 신경치료 '26'(성인) / 2 스케일링 '00'(성인) / 3 레진 '16'(소아)
TOOTH_BREAKS = {
    "tooth_level_as_full_mouth": ("UPDATE emr_treatments SET tooth_no = '00' WHERE id = 1", "check_tooth_level"),
    "full_mouth_with_tooth": ("UPDATE emr_treatments SET tooth_no = '16' WHERE id = 2", "check_tooth_level"),
    "sealant_adult": ("UPDATE emr_treatments SET procedure_code = 'U0004' WHERE id = 1", "check_child_procedures"),
}


@pytest.mark.parametrize("case", TOOTH_BREAKS)
def test_tooth_break_is_caught(tmp_path, case):
    sql, check = TOOTH_BREAKS[case]
    conn = make_db(tmp_path)
    conn.execute(sql)
    assert getattr(verify, check)(conn, CTX), f"{check}가 {case}를 못 잡음"


def _patients(conn, n_by_birth):
    """출생일별 인원으로 환자를 채운다 (시드 2명은 지운다)."""
    conn.execute("DELETE FROM emr_patients")
    i = 0
    for birth, n in n_by_birth.items():
        for _ in range(n):
            i += 1
            conn.execute("INSERT INTO emr_patients (id, chart_no, name, birth_date, sex, phone, registered_at)"
                         " VALUES (?, ?, '환자', ?, 'F', '010-0000-0000', '2025-01-01')", (i, f"C-{i:05d}", birth))


def test_age_distribution_matches(tmp_path):
    conn = make_db(tmp_path)
    # TODAY 2026-12-01 기준: 6세 15 / 15세 8 / 40세 62 / 70세 15 (+1%p 이내 흔들림)
    _patients(conn, {"2020-01-01": 16, "2011-01-01": 8, "1986-01-01": 61, "1956-01-01": 15})
    assert verify.check_age_distribution(conn, CTX) == []


def test_age_distribution_off(tmp_path):
    conn = make_db(tmp_path)
    _patients(conn, {"2020-01-01": 15, "2011-01-01": 8, "1986-01-01": 52, "1956-01-01": 25})
    bands = {v["band"] for v in verify.check_age_distribution(conn, CTX)}
    assert bands == {"adult", "senior"}


def test_age_band_boundary_is_birthday(tmp_path):
    conn = make_db(tmp_path)
    # 2007-12-02생은 TODAY에 18세(청소년), 2007-12-01생은 19세(성인)
    _patients(conn, {"2007-12-02": 1})
    shares = {v["band"]: v["share"] for v in verify.check_age_distribution(conn, CTX)}
    assert shares["teen"] == 1.0
    _patients(conn, {"2007-12-01": 1})
    shares = {v["band"]: v["share"] for v in verify.check_age_distribution(conn, CTX)}
    assert shares["adult"] == 1.0


def test_age_distribution_skips_unborn(tmp_path):
    conn = make_db(tmp_path)
    # 미래 출생은 필수 검사(check_timestamps_not_future)가 잡고, 분포는 나머지로 계산
    _patients(conn, {"2027-01-01": 1, "2020-01-01": 15, "2011-01-01": 8, "1986-01-01": 62, "1956-01-01": 15})
    assert verify.check_age_distribution(conn, CTX) == []


def _only(conn, appts):
    conn.execute("DELETE FROM emr_appointments")
    for a in appts:
        appt(conn, **a)


def test_daily_fill_report_skips_ramp_up(tmp_path):
    conn = make_db(tmp_path)
    # 시작 11-10 → 처음 MAX_LEAD_DAYS(14일)는 미리 잡힌 예약이 없어 덜 찬 게 정상 → 11-24부터 센다
    # 과거 진료일 11-24·27·28·30: 19/26 · 18/26(취소 제외) · 10/13(토) · 0/26. TODAY 이후는 보지 않는다
    _only(conn, [{"scheduled_at": "2026-11-24 10:00:00"} for i in range(19)]
          + [{"scheduled_at": "2026-11-27 10:00:00"} for i in range(18)]
          + [{"scheduled_at": "2026-11-28 10:00:00"} for i in range(10)]
          + [{"scheduled_at": "2026-11-27 11:00:00", "status": "cancelled"}])
    ctx = replace(CTX, range_start=date(2026, 11, 10), range_end=date(2026, 12, 2))
    assert verify.report_daily_fill(conn, ctx) == {"days": 4, "mean": round((19 / 26 + 18 / 26 + 10 / 13) / 4, 3), "min": 0.0}


def _month_visits(conn, n_visits, n_new):
    """11월 completed n_visits건, 그중 n_new명은 신환(등록일 = 첫 예약 created_at 날짜)"""
    _patients(conn, {"1986-01-01": n_visits})
    conn.execute("DELETE FROM emr_appointments")
    for pid in range(1, n_visits + 1):
        appt(conn, f"2026-11-{2 + pid % 20:02d} 10:00:00", patient=pid, created_at="2026-03-03 10:00:00")
    conn.execute("UPDATE emr_patients SET registered_at = '2026-03-03' WHERE id <= ?", (n_new,))


def test_new_patient_share(tmp_path):
    conn = make_db(tmp_path)
    _month_visits(conn, 100, 12)  # 신환 등록일 03-03 = 첫 예약 날짜
    assert verify.check_new_patient_share(conn, LARGE) == []
    _month_visits(conn, 100, 20)
    assert [v["month"] for v in verify.check_new_patient_share(conn, LARGE)] == ["2026-11"]
    # 창(10-01~) 시작 전 등록이면 구환 → 신환 0명으로 경고
    assert [v["share"] for v in verify.check_new_patient_share(conn, CTX)] == [0.0]
    # 워밍업이 없으니 생성 기간 첫 달도 본다
    conn.execute("UPDATE emr_patients SET registered_at = '2026-11-02' WHERE id <= 20")
    conn.execute("UPDATE emr_appointments SET created_at = '2026-11-02 10:00:00'")
    assert [v["month"] for v in verify.check_new_patient_share(conn, replace(CTX, range_start=date(2026, 11, 1)))] == ["2026-11"]


def test_status_rates(tmp_path):
    conn = make_db(tmp_path)
    _patients(conn, {"1986-01-01": 100})
    conn.execute("DELETE FROM emr_appointments")
    for pid in range(1, 101):  # 과거 비취소 100건 중 노쇼 6
        appt(conn, "2026-11-24 10:00:00", patient=pid, status="noshow" if pid <= 6 else "completed")
    for pid in range(1, 5):  # 당일 취소 4건 (다른 날)
        appt(conn, "2026-11-27 10:00:00", patient=pid, status="cancelled")
        conn.execute("UPDATE emr_appointments SET cancelled_at = '2026-11-27 08:00:00' WHERE patient_id = ? AND status = 'cancelled'", (pid,))
    assert verify.check_status_rates(conn, CTX) == []
    conn.execute("UPDATE emr_appointments SET status = 'noshow' WHERE patient_id BETWEEN 7 AND 15 AND status = 'completed'")
    assert [v["status"] for v in verify.check_status_rates(conn, CTX)] == ["noshow"]


def test_reschedule_rate_excludes_new_appointments(tmp_path):
    conn = make_db(tmp_path)
    _patients(conn, {"1986-01-01": 100})
    conn.execute("DELETE FROM emr_appointments")
    for pid in range(1, 101):  # 예약 100건 중 5건을 변경 → 원래 5(cancelled) + 새 예약 5
        appt(conn, "2026-11-24 10:00:00", patient=pid, status="cancelled" if pid <= 5 else "completed")
    for pid in range(1, 6):
        original = conn.execute("SELECT id FROM emr_appointments WHERE patient_id = ?", (pid,)).fetchone()[0]
        appt(conn, "2026-11-27 10:00:00", patient=pid)
        conn.execute("UPDATE emr_appointments SET rescheduled_from_id = ? WHERE patient_id = ? AND id != ?",
                     (original, pid, original))
    # 분모 = 전체 105행 − 새 예약 5 = 100 → 5%
    assert verify.report_reschedule_rate(conn, CTX)["rate"] == 0.05
    for pid in range(1, 101):  # 리드 1일 예약 100건 — 변경일(created 다음 날 ~ 예약 전날)이 없어 분모에서 뺀다
        appt(conn, "2026-11-30 10:00:00", patient=pid, status="completed")
    conn.execute("UPDATE emr_appointments SET created_at = '2026-11-29 10:00:00' WHERE scheduled_at = '2026-11-30 10:00:00'")
    assert verify.report_reschedule_rate(conn, CTX)["rate"] == 0.05  # 그대로 5% (변경 불가 예약까지 넣으면 2.5%)
    conn.execute("UPDATE emr_appointments SET rescheduled_from_id = NULL")
    assert verify.report_reschedule_rate(conn, CTX)["rate"] == 0


def _five_pairs(conn):
    """변경 가능한 과거 예약 100건 중 5건 변경 (원래 5 cancelled + 새 예약 5)"""
    _patients(conn, {"1986-01-01": 300})
    conn.execute("DELETE FROM emr_appointments")
    for pid in range(1, 101):
        appt(conn, "2026-11-24 10:00:00", patient=pid, status="cancelled" if pid <= 5 else "completed")
    for pid in range(1, 6):
        original = conn.execute("SELECT id FROM emr_appointments WHERE patient_id = ?", (pid,)).fetchone()[0]
        appt(conn, "2026-11-27 10:00:00", patient=pid)
        conn.execute("UPDATE emr_appointments SET rescheduled_from_id = ? WHERE patient_id = ? AND id != ?",
                     (original, pid, original))


def test_reschedule_rate_needs_open_day_between(tmp_path):
    conn = make_db(tmp_path)
    _five_pairs(conn)
    for pid in range(101, 201):  # 수(11-18) 예약 → 금(11-20) 내원: 사이 목요일은 휴진이라 변경할 날이 없음
        appt(conn, "2026-11-20 10:00:00", patient=pid)
    conn.execute("UPDATE emr_appointments SET created_at = '2026-11-18 10:00:00' WHERE scheduled_at = '2026-11-20 10:00:00'")
    assert verify.report_reschedule_rate(conn, CTX)["rate"] == 0.05  # 5/100 (휴진일만 사이에 있는 예약까지 넣으면 2.5%)


def test_reschedule_rate_ignores_future_appointments(tmp_path):
    conn = make_db(tmp_path)
    _five_pairs(conn)
    for pid in range(101, 201):  # TODAY 이후 예약 — 시뮬레이션이 TODAY 전날 멈춰 변경이 실현되지 않음
        appt(conn, "2026-12-11 10:00:00", patient=pid, status="scheduled")
    assert verify.report_reschedule_rate(conn, CTX)["rate"] == 0.05  # 5/100 (미래 예약까지 넣으면 2.5%)


def test_confirm_rate_excludes_booked_after_reminder(tmp_path):
    conn = make_db(tmp_path)
    _patients(conn, {"1986-01-01": 150})
    conn.execute("DELETE FROM emr_appointments")
    for pid in range(1, 101):  # 11-24(화) 예약, 리마인드는 직전 진료일 11-23(월) 17:00
        appt(conn, "2026-11-24 10:00:00", patient=pid)
    conn.execute("UPDATE emr_appointments SET confirmed = 1 WHERE patient_id <= 85")
    for pid in range(101, 151):  # 리마인드 뒤(11-23 17:30)에 잡혀 리마인드를 못 받은 예약 — 분모에서 뺀다
        appt(conn, "2026-11-24 11:00:00", patient=pid)
    conn.execute("UPDATE emr_appointments SET created_at = '2026-11-23 17:30:00' WHERE patient_id > 100")
    assert verify.report_confirm_rate(conn, CTX)["rate"] == 0.85  # 85% (못 받은 예약까지 넣으면 57%)


def test_confirm_rate_saturday_reminder_before_close(tmp_path):
    conn = make_db(tmp_path)
    _patients(conn, {"1986-01-01": 150})
    conn.execute("DELETE FROM emr_appointments")
    for pid in range(1, 101):  # 11-30(월) 예약, 직전 진료일 11-28(토) — 토요일 리마인드는 마감(13:30) 전 시각
        appt(conn, "2026-11-30 10:00:00", patient=pid)
    conn.execute("UPDATE emr_appointments SET confirmed = 1 WHERE patient_id <= 85")
    for pid in range(101, 151):  # 토 13:00에 잡힌 예약 — 토요일 리마인드 뒤라 못 받음(평일 시각 17:00 기준이면 받은 것으로 셈)
        appt(conn, "2026-11-30 11:00:00", patient=pid)
    conn.execute("UPDATE emr_appointments SET created_at = '2026-11-28 13:00:00' WHERE patient_id > 100")
    assert verify.report_confirm_rate(conn, CTX)["rate"] == 0.85  # 85% (17:00 기준이면 57%)


def test_reschedule_rate_numerator_only_past_originals(tmp_path):
    conn = make_db(tmp_path)
    _five_pairs(conn)
    for pid in range(101, 106):  # 원래 예약이 TODAY 이후인 쌍 — 시뮬레이션에서 실현될 수 없어 분자에서 뺀다
        appt(conn, "2026-12-04 10:00:00", patient=pid, status="cancelled")
        original = conn.execute("SELECT max(id) FROM emr_appointments").fetchone()[0]
        conn.execute("UPDATE emr_appointments SET cancelled_at = '2026-11-24 10:00:00' WHERE id = ?", (original,))
        appt(conn, "2026-12-10 10:00:00", patient=pid, status="scheduled")
        conn.execute("UPDATE emr_appointments SET rescheduled_from_id = ? WHERE id = (SELECT max(id) FROM emr_appointments)",
                     (original,))
    assert verify.report_reschedule_rate(conn, CTX)["rate"] == 0.05  # 5/100 (미래 쌍까지 세면 10%)


def test_status_rates_denominator_excludes_reschedule_originals(tmp_path):
    conn = make_db(tmp_path)
    _patients(conn, {"1986-01-01": 200})
    conn.execute("DELETE FROM emr_appointments")
    for pid in range(1, 97):  # 비취소 96건 중 노쇼 6
        appt(conn, "2026-11-24 10:00:00", patient=pid, status="noshow" if pid <= 6 else "completed")
    for pid in range(97, 101):  # 당일 취소 4건 — 분모에 포함
        appt(conn, "2026-11-24 11:00:00", patient=pid, status="cancelled")
    conn.execute("UPDATE emr_appointments SET cancelled_at = '2026-11-24 08:00:00' WHERE status = 'cancelled'")
    for pid in range(101, 201):  # 변경 원래 예약 100건(새 예약은 TODAY 이후) — 분모에서 뺀다
        appt(conn, "2026-11-27 10:00:00", patient=pid, status="cancelled")
        original = conn.execute("SELECT max(id) FROM emr_appointments").fetchone()[0]
        conn.execute("UPDATE emr_appointments SET cancelled_at = '2026-11-20 10:00:00' WHERE id = ?", (original,))
        appt(conn, "2026-12-04 10:00:00", patient=pid, status="scheduled")
        conn.execute("UPDATE emr_appointments SET rescheduled_from_id = ? WHERE id = (SELECT max(id) FROM emr_appointments)",
                     (original,))
    # 분모 = TODAY 이전 예약 200 − 변경 원래 예약 100 = 100 → 노쇼 6%·당일 취소 4% (원래 예약까지 넣으면 3%·2%로 경고)
    assert verify.check_status_rates(conn, CTX) == []


def test_confirm_rate(tmp_path):
    conn = make_db(tmp_path)
    _patients(conn, {"1986-01-01": 100})
    conn.execute("DELETE FROM emr_appointments")
    for pid in range(1, 101):
        appt(conn, "2026-11-24 10:00:00", patient=pid)
    conn.execute("UPDATE emr_appointments SET confirmed = 1 WHERE patient_id <= 85")
    assert verify.report_confirm_rate(conn, CTX)["rate"] == 0.85
    conn.execute("UPDATE emr_appointments SET confirmed = 0 WHERE patient_id > 70")
    assert verify.report_confirm_rate(conn, CTX)["rate"] == 0.7


def test_unpaid_rate(tmp_path):
    conn = make_db(tmp_path)
    conn.execute("DELETE FROM emr_payments")
    for i in range(100):
        conn.execute("INSERT INTO emr_payments (patient_id, appointment_id, kind, amount_due, amount_paid, paid_at, method)"
                     " VALUES (1, 1, 'visit', 10000, ?, '2026-10-06 11:00:00', 'card')", (5000 if i < 15 else 10000,))
    assert verify.report_unpaid_rate(conn, CTX)["rate"] == 0.15
    for _ in range(30):  # 0원 내원(상담 등)은 미수가 생길 수 없으니 분모에서 뺀다
        conn.execute("INSERT INTO emr_payments (patient_id, appointment_id, kind, amount_due, amount_paid, paid_at, method)"
                     " VALUES (1, 1, 'visit', 0, 0, '2026-10-06 11:00:00', 'card')")
    assert verify.report_unpaid_rate(conn, CTX)["rate"] == 0.15
    conn.execute("UPDATE emr_payments SET amount_paid = 5000 WHERE id <= 30 AND amount_due > 0")
    assert verify.report_unpaid_rate(conn, CTX)["rate"] == 0.3


def test_visit_mix(tmp_path):
    conn = make_db(tmp_path)
    _patients(conn, {"1986-01-01": 100})
    conn.execute("DELETE FROM emr_appointments")
    for pid in range(1, 101):
        kind = "treatment" if pid <= 70 else "hygiene" if pid <= 95 else "consult"
        appt(conn, "2026-11-24 10:00:00", type=kind, patient=pid)
    assert verify.check_visit_mix(conn, CTX) == []
    conn.execute("UPDATE emr_appointments SET type = 'hygiene', staff_id = 2 WHERE patient_id BETWEEN 51 AND 70")
    assert {v["type"] for v in verify.check_visit_mix(conn, CTX)} == {"treatment", "hygiene"}


def _visits(conn, last_visit_by_patient):
    """환자별 completed 내원 1건만 남긴다 (None이면 내원 없음). 시드 예약은 지운다."""
    conn.execute("DELETE FROM emr_appointments")
    for pid, day in last_visit_by_patient.items():
        if day:
            appt(conn, f"{day} 10:00:00", patient=pid)


ACTIVE_N = round(ACTIVE_SHARE * 100)  # 환자 100명 중 목표 활동 인원


def _active_visits(n_active):
    """1~n_active = 06-01 이후 completed(활동), 그다음 15명 = 05-31 이전(휴면), 나머지 = 내원 없음"""
    return {i: ("2026-07-07" if i <= n_active else "2026-05-12" if i <= n_active + 15 else None) for i in range(1, 101)}


def test_active_ratio(tmp_path):
    conn = make_db(tmp_path)
    _patients(conn, {"1986-01-01": 100})
    _visits(conn, _active_visits(ACTIVE_N))
    assert verify.check_active_ratio(conn, LARGE) == []
    conn.execute("UPDATE emr_appointments SET status = 'noshow' WHERE patient_id <= 4")  # 노쇼는 내원 아님 → −4%p(허용 안)
    assert verify.check_active_ratio(conn, LARGE) == []
    conn.execute("UPDATE emr_appointments SET status = 'noshow' WHERE patient_id <= 10")  # −10%p
    assert verify.check_active_ratio(conn, LARGE)


def test_active_ratio_skipped_when_window_shorter_than_6_months(tmp_path):
    conn = make_db(tmp_path)
    _patients(conn, {"1986-01-01": 100})
    _visits(conn, {i: ("2026-08-07" if i <= ACTIVE_N - 20 else None) for i in range(1, 101)})  # 목표 −20%p — 창이 짧아 의미 없음
    small = replace(CTX, range_start=date(2026, 8, 1))
    assert verify.check_active_ratio(conn, small) == []
    assert verify.check_active_ratio(conn, replace(CTX, range_start=date(2026, 2, 1)))


def test_active_ratio_excludes_future_new_patients(tmp_path):
    conn = make_db(tmp_path)
    _patients(conn, {"1986-01-01": 110})
    _visits(conn, _active_visits(ACTIVE_N))
    for pid in range(101, 111):  # 예정 신환 10명: 창 안 등록 + 미래 예약만 → 분모에서 제외
        appt(conn, "2026-12-04 10:00:00", patient=pid, status="scheduled")
    conn.execute("UPDATE emr_patients SET registered_at = '2026-11-20' WHERE id > 100")
    assert verify.check_active_ratio(conn, LARGE) == []


def test_active_from_clips_to_month_end():
    assert verify._active_from(date(2026, 12, 1)) == date(2026, 6, 1)
    assert verify._active_from(date(2026, 8, 31)) == date(2026, 2, 28)


def test_visit_split_boundaries(tmp_path):
    conn = make_db(tmp_path)
    _patients(conn, {"1986-01-01": 4})
    # 1: 05-31 마지막 → 휴면 / 2: 06-01 → 활동 / 3: 11-30 → 활동 / 4: TODAY 예약만(scheduled) → 예정 신환, 분모 제외
    _visits(conn, {1: "2026-05-29", 2: "2026-06-01", 3: "2026-11-30"})
    conn.execute("UPDATE emr_appointments SET scheduled_at = '2026-05-31 10:00:00' WHERE patient_id = 1")
    appt(conn, "2026-12-01 10:00:00", patient=4, status="scheduled")
    conn.execute("UPDATE emr_patients SET registered_at = '2026-11-20' WHERE id = 4")  # 창 안 등록
    patients, active = verify._visit_split(conn, CTX)
    assert (patients, active) == ({1, 2, 3}, {2, 3})


def test_peak_pattern_ignores_closed_day(tmp_path):
    conn = make_db(tmp_path)
    before = verify.report_peak_pattern(conn, CTX)
    conn.execute("UPDATE emr_appointments SET scheduled_at = '2026-11-19 15:00:00' WHERE id = 7")  # 목요일
    # 휴진일 예약은 필수 검사가 잡고 리포트는 센다 — 7번(피크 시작)이 빠진 만큼만 바뀐다
    after = verify.report_peak_pattern(conn, CTX)
    assert after["n"] == before["n"] - 1


def test_peak_pattern(tmp_path):
    conn = make_db(tmp_path)
    r = verify.report_peak_pattern(conn, CTX)  # 시드: 취소 제외 6건 중 5건이 10시·15시 시작
    assert r["n"] == 6 and r["peak_share"] == round(5 / 6, 3) and r["peak_share"] > r["time_share"]
    conn.execute("UPDATE emr_appointments SET scheduled_at = substr(scheduled_at, 1, 11) || '14:00:00'")
    assert verify.report_peak_pattern(conn, CTX)["peak_share"] == 0



def test_balance_holder_report(tmp_path):
    conn = make_db(tmp_path)
    # 준비표 범위 = TODAY 포함 최근 7일(11-24~12-01)의 비취소 예약: 환자 1의 7번(11-24)·5번(12-01).
    # 11-18 예약(환자 2)은 범위 밖. 1의 미수 16,000원은 11-03에 정산 → 두 행 모두 잔액 0
    assert verify.report_balance_holder_share(conn, CTX) == {"n": 2, "share": 0.0}
    conn.execute("UPDATE emr_payments SET paid_at = '2026-11-26 10:00:00' WHERE kind = 'settlement'")
    # 잔액은 그날 기준: 11-24엔 아직 미정산(칩 켜짐), 12-01엔 정산됨 → 1/2 (범위 밖 11-18까지 세면 1/3)
    assert verify.report_balance_holder_share(conn, CTX) == {"n": 2, "share": 0.5}
    conn.execute("UPDATE emr_appointments SET status = 'cancelled', cancelled_at = '2026-11-23 10:00:00' WHERE id = 7")
    assert verify.report_balance_holder_share(conn, CTX) == {"n": 1, "share": 0.0}  # 취소된 예약은 준비표에 안 나옴


def test_reports_print_without_failing(tmp_path, capsys):
    conn = make_db(tmp_path)
    conn.execute("UPDATE emr_appointments SET confirmed = 0")  # 확인률 0% — 리포트는 실패시키지 않는다
    assert verify.run(conn, CTX) is True
    assert "[INFO] report_confirm_rate" in capsys.readouterr().out
