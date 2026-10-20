"""verify 경고 검사(분포·치아)가 위반을 잡는지 — 데이터 설계 §5.3 경고 표."""
import pytest

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
    assert verify.check_daily_visits(conn, CTX) == []


def test_daily_visits_too_few(tmp_path):
    conn = make_db(tmp_path)
    _only(conn, [{"scheduled_at": "2026-11-24 10:00:00", "chair": i} for i in range(18)])  # -31%
    assert [v["date"] for v in verify.check_daily_visits(conn, CTX)] == ["2026-11-24"]


def test_daily_visits_empty_open_day_in_span(tmp_path):
    conn = make_db(tmp_path)
    # 11-27(금)~12-01(화) 사이: 11-28(토)·11-30(월)은 진료일인데 0건, 11-29(일)은 휴진
    _only(conn, [{"scheduled_at": "2026-11-27 10:00:00", "chair": i} for i in range(26)]
          + [{"scheduled_at": "2026-12-01 10:00:00", "chair": i} for i in range(26)])
    assert [v["date"] for v in verify.check_daily_visits(conn, CTX)] == ["2026-11-28", "2026-11-30"]


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
