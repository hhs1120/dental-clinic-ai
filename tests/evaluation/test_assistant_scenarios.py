"""AI 비서 시나리오 평가셋 정답(evaluation/assistant_scenarios.py, AI 비서 설계 §7·§7.1).
정답 계산은 emr_bridge 테스트의 손으로 만든 source로, 구성·분할 격리는 커밋한 JSON으로 본다."""

# ruff: noqa: F401, F811  (다른 테스트 모듈의 fixture를 가져와 쓴다)

import json
from datetime import date
from types import SimpleNamespace

import pytest

from backend import assistant_tools, emr_bridge
from evaluation import assistant_scenarios as sc
from generator import verify
from generator.paths import output_paths
from tests.backend.test_emr_bridge import (
    CLINIC,
    HYGIENIST,
    MISSED,
    SCALING,
    WED,
    _src_add,
    conn,
    src,
)

SCENARIOS = output_paths()["assistant_scenarios"]


def _scenarios():
    return json.loads(SCENARIOS.read_text(encoding="utf-8"))


def _busy_hygienist(conn):
    """WED 위생사 14:00·18:00을 막는다 — 오후 빈 시작은 14:30~17:30"""
    _src_add(
        conn,
        (MISSED, HYGIENIST, f"{WED} 14:00:00", "hygiene", SCALING, 1),
        (MISSED, HYGIENIST, f"{WED} 18:00:00", "hygiene", SCALING, 2),
    )


def test_gold_booking_matches_free_slots(conn):
    _busy_hygienist(conn)
    end = sc.booking_end(conn, CLINIC, "C-00002", WED, "스케일링", ("pm",))
    free = {
        s["start"] for s in emr_bridge.free_slots(conn, CLINIC, date.fromisoformat(WED), SCALING)
    }
    lo, hi = end["draft"]["start_range"]
    assert {lo, hi} <= free
    assert (lo, hi) == ("14:30", "17:30")
    assert end["draft"] == {
        "chart_no": "C-00002",
        "date": WED,
        "start_range": ["14:30", "17:30"],
        "staff_role": "hygienist",
    }
    assert sc.booking_end(conn, CLINIC, "C-00002", WED, "치석 제거", ("am", "earliest"))["draft"][
        "start_range"
    ] == ["09:30", "09:30"]


def test_gold_booking_rejects_taken_or_same_day(conn):
    _busy_hygienist(conn)
    with pytest.raises(ValueError):  # 막힌 시각
        sc.booking_end(conn, CLINIC, "C-00002", WED, "스케일링", ("14:00",))
    with pytest.raises(ValueError):  # 그날 이미 예약 있는 환자(patient_same_day)
        sc.booking_end(conn, CLINIC, "C-00001", WED, "스케일링", ("pm",))
    with pytest.raises(ValueError):  # 휴진
        sc.booking_end(conn, CLINIC, "C-00002", "2026-12-03", "스케일링", ("am",))


def test_build_turn_gold(conn):
    """초안 턴은 앞 턴에 같은 날 free_slots가 없으면 free_slots를 더하고, 인자는 한 판에 모은다.
    초안 환자는 앞 턴 결과에 나온 환자여야 한다"""
    plan = [
        sc.S(
            "dev",
            "booking",
            sc.U("C-00002 환자 봐 줘", sc.C("query_patient", chart_no="C-00002")),
            sc.U("이영희 님 수요일 오후 스케일링으로", sc.D("C-00002", WED, "스케일링", "pm")),
        )
    ]
    [s] = sc.build(conn, CLINIC, plan)
    assert s["id"] == "as-001"
    assert s["turns"][1]["gold"] == {
        "tools": ["draft_appointment", "free_slots"],
        "args": {"chart_no": "C-00002", "date": WED, "procedure": "스케일링"},
        "end": {
            "draft": {
                "chart_no": "C-00002",
                "date": WED,
                "start_range": ["14:00", "18:00"],
                "staff_role": "hygienist",
            }
        },
    }
    unseen = [plan[0][:2] + (plan[0][3],)]  # 앞 턴 없이 초안만 — 결과에 나온 적 없는 환자
    with pytest.raises(ValueError):
        sc.build(conn, CLINIC, unseen)


def test_type_counts():
    s = _scenarios()
    ctx = SimpleNamespace(assistant_scenarios=SCENARIOS)
    assert verify.check_assistant_scenarios(None, ctx) == []
    for split in ("dev", "test"):
        got = {t: sum(x["split"] == split and x["type"] == t for x in s) for t in sc.TYPES}
        assert got == verify.ASSISTANT_TYPE_COUNTS


def test_splits_disjoint_patients_dates():
    assert verify.split_overlap(_scenarios()) == []


def test_gold_tools_in_registry():
    names = {t.name for t in assistant_tools.assistant_tools(CLINIC, None, {})} - {"done_tools"}
    for s in _scenarios():
        for t in s["turns"]:
            assert set(t["gold"]["tools"]) <= names, s["id"]


def test_gold_conn_sees_only_copied_app_tables(conn, tmp_path):
    # 직원이 앱에 남긴 기록(연락 기록)은 정답에 들지 않는다 — 시나리오 실행 DB처럼 빈 표, 옮기는 표만 앱에서
    emr_bridge.missed.record_contact(
        conn, MISSED, "booked", "call", "데스크", "2026-12-01 09:00:00", date(2026, 12, 1)
    )
    conn.execute("INSERT INTO ax_settings VALUES ('active_prompt_version', '\"v1\"')")
    conn.commit()
    gold = sc.gold_conn(tmp_path / "s.sqlite", tmp_path / "a.sqlite")
    try:
        assert gold.execute("SELECT COUNT(*) FROM ax_missed_contacts").fetchone()[0] == 0
        assert gold.execute("SELECT COUNT(*) FROM ax_settings").fetchone()[0] == 1
    finally:
        gold.close()
