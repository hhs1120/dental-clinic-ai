"""자동 종결 (기능 정의 §4 ①·§3 원칙 1, 앱 설계 §3b) — 조건 함수·실행·풀기."""

from backend.autoclose import autoclose_reason

CLEAN = {
    "primary_purpose": "confirm",
    "counterpart": "patient",
    "urgency": "normal",
    "emergency": 0,
    "complaint": 0,
    "price_inquiry": 0,
    "wants_appt": 0,
    "appt_confirmed": 0,
    "has_todo": 0,
}


def _r(ok=True, fallback=False, **over):
    return {"labels": CLEAN | over, "ok": ok, "fallback": fallback}


def test_clean_pair_closes():
    assert autoclose_reason(_r(), _r())


def test_todo_blocks():
    assert autoclose_reason(_r(has_todo=1), _r()) is None
    assert autoclose_reason(_r(), _r(has_todo=1)) is None


def test_emergency_complaint_urgent_block():
    for over in ({"emergency": 1}, {"complaint": 1}, {"urgency": "urgent"}):
        assert autoclose_reason(_r(**over), _r(**over)) is None
        assert autoclose_reason(_r(), _r(**over)) is None


def test_disagreement_blocks():
    assert autoclose_reason(_r(primary_purpose="book"), _r()) is None
    assert autoclose_reason(
        _r(urgency="low"), _r()
    )  # 급함은 urgent 여부만 — normal↔low는 엇갈림 아님


def test_missing_failed_fallback_block():
    assert autoclose_reason(_r(), None) is None
    assert autoclose_reason(None, _r()) is None
    assert autoclose_reason(_r(ok=False), _r()) is None


def test_fallback_blocks():
    assert autoclose_reason(_r(), _r(fallback=True)) is None
    assert autoclose_reason(_r(fallback=True), _r()) is None
