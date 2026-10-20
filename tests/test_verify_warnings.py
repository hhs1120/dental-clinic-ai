"""verify 경고 검사(분포·치아)가 위반을 잡는지 — 가상 데이터 생성 설계 §5.3 경고 표."""
from dataclasses import replace

import pytest
from datetime import date

from generator import verify
from tests.verify_db import CTX, appt, make_db


def test_valid_db_tooth_checks_pass(tmp_path):
    conn = make_db(tmp_path)
    for check in (verify.check_tooth_fdi, verify.check_tooth_level, verify.check_primary_teeth_child,
                  verify.check_child_procedures):
        assert check(conn, CTX) == [], check.__name__


# (깨뜨리는 SQL, 잡아야 하는 검사). 시드 진료: 1 신경치료 '26'(성인) / 2 스케일링 '00'(성인) / 3 레진 '55'(소아)
TOOTH_BREAKS = {
    "fdi_invalid": ("UPDATE emr_treatments SET tooth_no = '19' WHERE id = 1", "check_tooth_fdi"),
    "fdi_invalid_claim": ("UPDATE emr_claims SET tooth_no = '90' WHERE id = 1", "check_tooth_fdi"),
    "tooth_level_as_full_mouth": ("UPDATE emr_treatments SET tooth_no = '00' WHERE id = 1", "check_tooth_level"),
    "full_mouth_with_tooth": ("UPDATE emr_treatments SET tooth_no = '16' WHERE id = 2", "check_tooth_level"),
    "primary_tooth_adult": ("UPDATE emr_treatments SET tooth_no = '55' WHERE id = 1", "check_primary_teeth_child"),
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


def test_daily_visits_within_30pct(tmp_path):
    conn = make_db(tmp_path)
    # 11-27(금) 19건 = 26의 -27%, 11-28(토) 16건 = 13의 +23%. 취소는 세지 않는다
    _only(conn, [{"scheduled_at": "2026-11-27 10:00:00", "chair": i} for i in range(19)]
          + [{"scheduled_at": "2026-11-28 10:00:00", "chair": i} for i in range(16)]
          + [{"scheduled_at": "2026-11-27 11:00:00", "chair": 99 + i, "status": "cancelled"} for i in range(20)])
    ctx = replace(CTX, range_start=date(2026, 11, 27), range_end=date(2026, 11, 28))
    assert verify.check_daily_visits(conn, ctx) == []


def test_daily_visits_too_few(tmp_path):
    conn = make_db(tmp_path)
    _only(conn, [{"scheduled_at": "2026-11-24 10:00:00", "chair": i} for i in range(18)])  # -31%
    ctx = replace(CTX, range_start=date(2026, 11, 24), range_end=date(2026, 11, 24))
    assert [v["date"] for v in verify.check_daily_visits(conn, ctx)] == ["2026-11-24"]


def test_daily_visits_empty_open_day_in_range(tmp_path):
    conn = make_db(tmp_path)
    # 범위 11-24(화)~12-01(화): 11-24·11-27·12-01만 26건. 11-28(토)·11-30(월)은 진료일인데 0건
    # 11-25는 테스트 공휴일, 11-26 목·11-29 일은 휴진. 맨 앞(11-24)도 범위 안이라 비면 잡혀야 한다
    _only(conn, [{"scheduled_at": "2026-11-27 10:00:00", "chair": i} for i in range(26)]
          + [{"scheduled_at": "2026-12-01 10:00:00", "chair": i} for i in range(26)])
    ctx = replace(CTX, range_start=date(2026, 11, 24), range_end=date(2026, 12, 1))
    assert [v["date"] for v in verify.check_daily_visits(conn, ctx)] == ["2026-11-24", "2026-11-28", "2026-11-30"]


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
    conn.execute("UPDATE emr_appointments SET status = 'noshow' WHERE patient_id <= 10")  # 노쇼는 내원 아님 → 60%
    assert verify.check_active_ratio(conn, CTX)


def test_active_ratio_excludes_future_new_patients(tmp_path):
    conn = make_db(tmp_path)
    _patients(conn, {"1986-01-01": 110})
    visits = {i: ("2026-07-07" if i <= 70 else "2026-05-12" if i <= 85 else None) for i in range(1, 101)}
    _visits(conn, visits)
    for pid in range(101, 111):  # 예정 신환 10명: 미래 예약만 → 분모에서 제외
        appt(conn, "2026-12-04 10:00:00", patient=pid, chair=pid, status="scheduled")
    assert verify.check_active_ratio(conn, CTX) == []


def test_dormant_a_ratio_scales_with_window(tmp_path):
    conn = make_db(tmp_path)
    _patients(conn, {"1986-01-01": 100})
    # large 창(02-01~): 기대 30% × 50% = 15% → 15명이 05-31 이전에만 내원
    _visits(conn, {i: ("2026-07-07" if i <= 70 else "2026-05-12" if i <= 85 else None) for i in range(1, 101)})
    large = replace(CTX, range_start=date(2026, 2, 1))
    assert verify.check_dormant_a_ratio(conn, large) == []
    # small 창(08-01~): 06-01 이전 구간이 없어 기대 0% → 15%는 경고
    small = replace(CTX, range_start=date(2026, 8, 1))
    assert verify.check_dormant_a_ratio(conn, small)


def test_peak_pattern_ignores_closed_day(tmp_path):
    conn = make_db(tmp_path)
    conn.execute("UPDATE emr_appointments SET scheduled_at = '2026-11-19 15:00:00' WHERE id = 7")  # 목요일
    assert verify.check_peak_pattern(conn, CTX) == []  # 휴진일 예약은 필수 검사가 잡는다


def test_peak_pattern(tmp_path):
    conn = make_db(tmp_path)
    assert verify.check_peak_pattern(conn, CTX) == []  # 시드: 취소 제외 6건 중 5건이 10시·15시 시작
    conn.execute("UPDATE emr_appointments SET scheduled_at = substr(scheduled_at, 1, 11) || '14:00:00'")
    assert verify.check_peak_pattern(conn, CTX)


def test_long_procedures_peak_share(tmp_path):
    conn = make_db(tmp_path)
    # 시드 60분 예약: 1번 10:00(피크), 5번 14:00 → 피크 50%
    assert verify.check_long_procedures_peak(conn, CTX)
    conn.execute("UPDATE emr_appointments SET scheduled_at = '2026-10-06 11:00:00' WHERE id = 1")
    assert verify.check_long_procedures_peak(conn, CTX) == []
