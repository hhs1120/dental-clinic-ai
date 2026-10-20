"""verify 경고 검사(분포·치아)가 위반을 잡는지 — 가상 데이터 생성 설계 §5.3 경고 표."""
from dataclasses import replace

import pytest
from datetime import date

from generator import verify
from tests.verify_db import CTX, appt, make_db


def test_valid_db_tooth_checks_pass(tmp_path):
    conn = make_db(tmp_path)
    for check in (verify.check_tooth_level, verify.check_child_procedures):
        assert check(conn, CTX) == [], check.__name__


# (깨뜨리는 SQL, 잡아야 하는 검사). 시드 진료: 1 신경치료 '26'(성인) / 2 스케일링 '00'(성인) / 3 레진 '55'(소아)
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


def test_daily_fill_past_days(tmp_path):
    conn = make_db(tmp_path)
    # 범위 11-24(화)~12-02: 과거 진료일 11-24·27·28·30. 11-24 19건(73%)·11-27 18건(69%)·11-28(토) 10건(77%)·11-30 0건
    # TODAY 이후(12-01·12-02)는 덜 찬 게 정상이라 보지 않는다
    _only(conn, [{"scheduled_at": "2026-11-24 10:00:00", "chair": i} for i in range(19)]
          + [{"scheduled_at": "2026-11-27 10:00:00", "chair": i} for i in range(18)]
          + [{"scheduled_at": "2026-11-28 10:00:00", "chair": i} for i in range(10)]
          + [{"scheduled_at": "2026-11-27 11:00:00", "chair": 99, "status": "cancelled"}])
    ctx = replace(CTX, range_start=date(2026, 11, 24), range_end=date(2026, 12, 2))
    assert [v["date"] for v in verify.check_daily_fill(conn, ctx)] == ["2026-11-27", "2026-11-30"]


def _month_visits(conn, n_visits, n_new):
    """11월 completed n_visits건, 그중 n_new명은 신환(등록일 = 첫 예약 created_at 날짜)"""
    _patients(conn, {"1986-01-01": n_visits})
    conn.execute("DELETE FROM emr_appointments")
    for pid in range(1, n_visits + 1):
        appt(conn, f"2026-11-{2 + pid % 20:02d} 10:00:00", patient=pid, chair=pid)
    conn.execute("UPDATE emr_patients SET registered_at = '2026-01-01' WHERE id <= ?", (n_new,))


def test_new_patient_share(tmp_path):
    conn = make_db(tmp_path)
    _month_visits(conn, 100, 12)
    assert verify.check_new_patient_share(conn, CTX) == []
    _month_visits(conn, 100, 20)
    assert [v["month"] for v in verify.check_new_patient_share(conn, CTX)] == ["2026-11"]


def test_status_rates(tmp_path):
    conn = make_db(tmp_path)
    _patients(conn, {"1986-01-01": 100})
    conn.execute("DELETE FROM emr_appointments")
    for pid in range(1, 101):  # 과거 비취소 100건 중 노쇼 6
        appt(conn, "2026-11-24 10:00:00", patient=pid, chair=pid, status="noshow" if pid <= 6 else "completed")
    for pid in range(1, 5):  # 당일 취소 4건 (다른 날)
        appt(conn, "2026-11-27 10:00:00", patient=pid, chair=pid, status="cancelled")
        conn.execute("UPDATE emr_appointments SET cancelled_at = '2026-11-27 08:00:00' WHERE patient_id = ? AND status = 'cancelled'", (pid,))
    assert verify.check_status_rates(conn, CTX) == []
    conn.execute("UPDATE emr_appointments SET status = 'noshow' WHERE patient_id BETWEEN 7 AND 15 AND status = 'completed'")
    assert [v["status"] for v in verify.check_status_rates(conn, CTX)] == ["noshow"]


def _visits(conn, last_visit_by_patient):
    """환자별 completed 내원 1건만 남긴다 (None이면 내원 없음). 시드 예약은 지운다."""
    conn.execute("DELETE FROM emr_appointments")
    for pid, day in last_visit_by_patient.items():
        if day:
            appt(conn, f"{day} 10:00:00", patient=pid, chair=pid)


def test_active_ratio(tmp_path):
    conn = make_db(tmp_path)
    _patients(conn, {"1986-01-01": 100})
    # 활동 70 = 06-01 이후 completed, 휴면 30 = 05-31 이전 또는 내원 없음
    _visits(conn, {i: ("2026-07-07" if i <= 70 else "2026-05-12" if i <= 85 else None) for i in range(1, 101)})
    assert verify.check_active_ratio(conn, CTX) == []
    conn.execute("UPDATE emr_appointments SET status = 'noshow' WHERE patient_id <= 4")  # 노쇼는 내원 아님 → 66%(±5%p 안)
    assert verify.check_active_ratio(conn, CTX) == []
    conn.execute("UPDATE emr_appointments SET status = 'noshow' WHERE patient_id <= 10")  # 60%
    assert verify.check_active_ratio(conn, CTX)


def test_active_ratio_excludes_future_new_patients(tmp_path):
    conn = make_db(tmp_path)
    _patients(conn, {"1986-01-01": 110})
    visits = {i: ("2026-07-07" if i <= 70 else "2026-05-12" if i <= 85 else None) for i in range(1, 101)}
    _visits(conn, visits)
    for pid in range(101, 111):  # 예정 신환 10명: 미래 예약만 → 분모에서 제외
        appt(conn, "2026-12-04 10:00:00", patient=pid, chair=pid, status="scheduled")
    assert verify.check_active_ratio(conn, CTX) == []


def test_active_from_clips_to_month_end():
    assert verify._active_from(date(2026, 12, 1)) == date(2026, 6, 1)
    assert verify._active_from(date(2026, 8, 31)) == date(2026, 2, 28)


def test_visit_split_boundaries(tmp_path):
    conn = make_db(tmp_path)
    _patients(conn, {"1986-01-01": 4})
    # 1: 05-31 마지막 → 휴면 / 2: 06-01 → 활동 / 3: 11-30 → 활동 / 4: TODAY 예약만(scheduled) → 예정 신환, 분모 제외
    _visits(conn, {1: "2026-05-29", 2: "2026-06-01", 3: "2026-11-30"})
    conn.execute("UPDATE emr_appointments SET scheduled_at = '2026-05-31 10:00:00' WHERE patient_id = 1")
    appt(conn, "2026-12-01 10:00:00", patient=4, chair=4, status="scheduled")
    patients, active = verify._visit_split(conn, CTX)
    assert (patients, active) == ({1, 2, 3}, {2, 3})


def test_peak_pattern_ignores_closed_day(tmp_path):
    conn = make_db(tmp_path)
    conn.execute("UPDATE emr_appointments SET scheduled_at = '2026-11-19 15:00:00' WHERE id = 7")  # 목요일
    assert verify.check_peak_pattern(conn, CTX) == []  # 휴진일 예약은 필수 검사가 잡는다


def test_peak_pattern(tmp_path):
    conn = make_db(tmp_path)
    assert verify.check_peak_pattern(conn, CTX) == []  # 시드: 취소 제외 6건 중 5건이 10시·15시 시작
    conn.execute("UPDATE emr_appointments SET scheduled_at = substr(scheduled_at, 1, 11) || '14:00:00'")
    assert verify.check_peak_pattern(conn, CTX)


