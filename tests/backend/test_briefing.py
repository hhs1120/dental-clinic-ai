"""⑦ 아침 브리핑 템플릿 (backend/briefing.py, 설계 docs/superpowers/specs/2026-11-16-nightly-briefing-design.md §4)."""

from datetime import datetime

from backend.briefing import day_lines, night_line

CATS = {
    "U0011": "surgery",
    "U0012": "surgery",
    "U0013": "implant",
    "U0009": "prosth",
    "U0001": "preventive",
}


def _row(time, code, procedure, name, new=False, alerts=()):
    """prep.prep_facts 행에서 브리핑이 읽는 칸만"""
    return {
        "time": time,
        "procedure_code": code,
        "procedure": procedure,
        "patient": {"name": name, "new_patient": new},
        "alerts": [{"label": label, "related": related} for label, related in alerts],
    }


def test_today_line_busy_hours_ties():
    rows = [
        _row("10:00", "U0001", "스케일링", "가"),
        _row("10:30", "U0001", "스케일링", "나"),
        _row("14:00", "U0009", "크라운", "다"),
        _row("14:30", "U0009", "크라운", "라", new=True),
        _row("17:00", "U0001", "스케일링", "마"),
    ]
    assert day_lines(rows, CATS)[0] == {
        "head": "오늘",
        "text": "예약 5건(신환 1) · 10시·14시대에 몰림",
    }


def test_big_procedures_by_category_and_overflow():
    rows = [
        _row(t, "U0011", "단순발치", n)
        for t, n in [("09:00", "가"), ("10:00", "나"), ("11:00", "다"), ("12:00", "라")]
    ]
    rows.append(_row("15:00", "U0009", "크라운", "마"))  # 60분이어도 prosth는 아님
    big = [line for line in day_lines(rows, CATS) if line["head"] == "큰 시술"][0]
    assert big["text"] == "단순발치 9:00 · 단순발치 10:00 · 단순발치 11:00 외 1건"


def test_care_patients_big_and_related_alert_only():
    rows = [
        _row(
            "10:30",
            "U0011",
            "단순발치",
            "배정웅",
            alerts=[("항응고제 복용", True), ("치과 공포", False)],
        ),
        _row("11:00", "U0001", "스케일링", "김재호", alerts=[("고혈압", True)]),  # 큰 시술 아님
        _row("15:30", "U0013", "임플란트 1차", "안서준", alerts=[("당뇨", True)]),
    ]
    care = [line["text"] for line in day_lines(rows, CATS) if line["head"] == "챙길 환자"]
    assert care == ["10:30 배정웅 단순발치 — 항응고제 복용", "15:30 안서준 임플란트 1차 — 당뇨"]


def test_empty_day():
    assert day_lines([], CATS) == []


def test_night_line_counts_and_window():
    job = {
        "status": "done",
        "finished_at": "2026-11-16T03:12:00",
        "detail": [
            {"step": "classify", "new": 5, "failed": 0, "note": None},
            {"step": "autoclose", "new": 3, "failed": 0, "note": None},
            {"step": "prep", "new": 2, "failed": 1, "note": None},
        ],
    }
    assert night_line(job, datetime(2026, 11, 16, 9, 0)) == {
        "head": "밤사이",
        "text": "통화 5건 분류 · 자동 종결 3 · 실패 1 (03:12 끝남)",
    }
    assert night_line(job, datetime(2026, 11, 16, 21, 13)) is None  # NIGHT_HOURS 18 지남


def test_night_line_budget_stop():
    job = {
        "status": "failed",
        "finished_at": "2026-11-16T03:12:00",
        "detail": [
            {"step": "classify", "new": 1, "failed": 0, "note": None},
            {"step": "judge", "new": 0, "failed": 0, "note": "상한으로 중단"},
        ],
    }
    assert night_line(job, datetime(2026, 11, 16, 9, 0))["text"].endswith(
        "(03:12 끝남 · 상한으로 중단)"
    )


def test_night_line_none():
    assert night_line(None, datetime(2026, 11, 16, 9, 0)) is None


def test_night_line_running_hidden():
    """끝나지 않은 실행은 밤사이 결과가 아니다"""
    job = {"status": "running", "finished_at": None, "detail": []}
    assert night_line(job, datetime(2026, 11, 16, 9, 0)) is None
