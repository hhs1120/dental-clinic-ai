"""verify 경고 검사(분포·치아)가 위반을 잡는지, 리포트(수치만)가 분모를 맞게 세는지 — 가상 데이터 생성 설계 §5.3."""
from dataclasses import replace

import pytest
from datetime import date

from generator import verify
from tests.verify_db import CTX, appt, make_db


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


def _only(conn, appts):
    conn.execute("DELETE FROM emr_appointments")
    for a in appts:
        appt(conn, **a)


def test_daily_fill_report_skips_ramp_up(tmp_path):
    conn = make_db(tmp_path)
    # 시작 11-10 → 처음 MAX_LEAD_DAYS(14일)는 미리 잡힌 예약이 없어 덜 찬 게 정상 → 11-24부터 센다
    # 과거 진료일 11-24·27·28·30: 19/26 · 18/26(취소 제외) · 10/13(토 — 슬롯 수 비례) · 0/26. TODAY 이후는 보지 않는다
    _only(conn, [{"scheduled_at": "2026-11-24 10:00:00"} for i in range(19)]
          + [{"scheduled_at": "2026-11-27 10:00:00"} for i in range(18)]
          + [{"scheduled_at": "2026-11-28 10:00:00"} for i in range(10)]
          + [{"scheduled_at": "2026-11-27 11:00:00", "status": "cancelled"}])
    ctx = replace(CTX, range_start=date(2026, 11, 10), range_end=date(2026, 12, 2))
    assert verify.report_daily_fill(conn, ctx) == {"days": 4, "mean": round((19 / 26 + 18 / 26 + 10 / 13) / 4, 3), "min": 0.0}


def test_visit_mix(tmp_path):
    conn = make_db(tmp_path)
    _patients(conn, {"1986-01-01": 100})
    conn.execute("DELETE FROM emr_appointments")
    for pid in range(1, 101):
        kind = "treatment" if pid <= 67 else "hygiene" if pid <= 97 else "consult"
        appt(conn, "2026-11-24 10:00:00", type=kind, patient=pid)
    assert verify.check_visit_mix(conn, CTX) == []
    conn.execute("UPDATE emr_appointments SET type = 'hygiene', staff_id = 2 WHERE patient_id BETWEEN 51 AND 70")
    assert {v["type"] for v in verify.check_visit_mix(conn, CTX)} == {"treatment", "hygiene"}


def test_visit_mix_ignores_pre_window_visits(tmp_path):
    """창 이전 내원(scheduled_at < range_start)은 내원 구성 분모에 넣지 않는다 (가상 데이터 생성 설계 §2.6 (1))"""
    conn = make_db(tmp_path)
    _patients(conn, {"1986-01-01": 100})
    conn.execute("DELETE FROM emr_appointments")
    for pid in range(1, 101):
        kind = "treatment" if pid <= 67 else "hygiene" if pid <= 97 else "consult"
        appt(conn, "2026-11-24 10:00:00", type=kind, patient=pid)
        appt(conn, "2026-09-15 10:00:00", type="hygiene", patient=pid)  # 창 이전 위생 1행씩
    assert verify.check_visit_mix(conn, CTX) == []


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
    assert verify.run(conn, CTX) is True
    assert "[INFO] report_balance_holder_share" in capsys.readouterr().out


def test_alert_holder_report(tmp_path):
    """주의사항 1개 이상 보유 비율 — 전체·연령 구간별(TODAY 나이). 시드: 1번(성인)·2번(소아) 모두 보유"""
    conn = make_db(tmp_path)
    assert verify.report_alert_holder_share(conn, CTX) == {"n": 2, "share": 1.0, "adult": 1.0, "child": 1.0}
    conn.execute("DELETE FROM emr_patient_alerts WHERE patient_id = 2")
    assert verify.report_alert_holder_share(conn, CTX) == {"n": 2, "share": 0.5, "adult": 1.0, "child": 0.0}
