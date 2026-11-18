"""AI 비서 도구 = 업무 화면 숫자 (backend/assistant_tools.py, AI 비서 설계 §4.2) — 도구는 화면 함수를 감싸 칸을
고르고 거르기만 하므로, 감싼 곳이 화면과 다르게 세거나 화면이 보여 주는 칸을 빼면 여기서 걸린다."""

# ruff: noqa: F401, F811  (다른 테스트 모듈의 fixture를 가져와 쓴다)

from datetime import datetime

from backend import assistant_tools as tools
from backend import calls, missed, recon, today
from tests.backend.test_claims import NOTE, add_claim, reviewed_for
from tests.backend.test_emr_bridge import CLINIC, TODAY
from tests.backend.test_missed import Source, _record, src, three
from tests.backend.test_today import NOW, pconn, tconn


def _sheet(conn):
    return today.sheet(conn, TODAY, NOW)


def test_prep_rows_match_today_cards(pconn):
    """준비표 도구 = 오늘 화면 예약·주의·내원 확인 전 숫자"""
    got = tools._prep_rows(CLINIC, pconn, {"date": TODAY.isoformat()})
    s = _sheet(pconn)
    chips = [r["chips"] for r in got["rows"]]
    assert got["count"] == s["cards"]["appointments"]["total"]
    assert sum("alert" in c for c in chips) == s["cards"]["alert_patients"]
    assert sum("unconfirmed" in c for c in chips) == s["people"]["unconfirmed"]


def test_claim_items_match_today_lines(tmp_path):
    """재심 기한 임박·청구 전 상병 경고 = 오늘 화면 줄(청구 점검 soon 칩·경고 줄)"""
    s = Source(tmp_path / "source.sqlite")
    add_claim(s, 1, note=NOTE["documentation"][1], reviewed=reviewed_for(10))
    add_claim(s, 2, note=NOTE["diagnosis"][1], reviewed=reviewed_for(10))  # 상병 착오 — 재심 아님
    add_claim(s, 3, day="2026-11-12", status="pending", dx="D07")
    conn = s.app()
    people = _sheet(conn)["people"]
    soon = tools._claim_items(CLINIC, conn, {"kind": "appeal_soon"})
    warn = tools._claim_items(CLINIC, conn, {"kind": "pre_claim_warning"})
    assert soon["count"] == people["appeal_soon"] == 1
    assert warn["count"] == people["preclaim_warning"] == 1


def test_recon_days_unconfirmed_match_open_count(tmp_path):
    """일계표 도구의 확정 안 된 건 = 오늘 화면 일계표 불일치(recon.open_count) — 확정하면 같이 준다"""
    from tests.backend.test_recon_match import Books

    b = Books(tmp_path / "source.sqlite")
    b.pay("2026-11-03 10:00:00", 3_500, "card")
    b.sale("2026-11-03 10:00:00", 35_000, "R1")
    b.settle("2026-11-03")
    conn = b.app()

    def open_items():
        got = tools._recon_days(CLINIC, conn, {"month": "2026-11"})
        return sum(not i["confirmed"] for d in got["days"] for i in d["items"])

    assert (
        open_items() == recon.open_count(conn, TODAY) == _sheet(conn)["people"]["recon_open"] == 1
    )
    key = recon.ledger(conn, TODAY)["days"][0]["items"][0]["key"]
    recon.confirm(conn, key, "amount_typo", "데스크", "2026-12-01 09:00", TODAY)
    conn.commit()
    assert open_items() == recon.open_count(conn, TODAY) == 0


def test_find_calls_todo_matches_call_screen(tconn):
    """통화 도구의 할 일 = 통화 화면 상세의 할 일(종류·대상·내용·끝냄) — 화면에서 체크한 것도"""
    # 통화 화면 테스트 DB의 11/28 통화(김철수, 분류·할 일 둘 있음)
    cid = tconn.execute(
        "SELECT max(id) FROM ax_call_classifications WHERE call_uid = 'c-0001' AND status = 'ok'"
    ).fetchone()[0]
    calls.set_todo_done(tconn, cid, 1, part="데스크", at="2026-11-29 09:00:00", done=True)
    tconn.commit()
    got = tools._find_calls(
        tconn,
        {"date_from": "2026-11-28", "date_to": "2026-11-28", "wants_appt": None, "chart_no": None},
    )
    screen = calls.call_detail(tconn, "c-0001", TODAY)["classification"]["todo"]
    keys = ("kind", "who", "text", "done")
    assert got["calls"][0]["todo"] == [{k: t[k] for k in keys} for t in screen]
    assert [t["done"] for t in got["calls"][0]["todo"]] == [False, True]


def test_missed_patients_match_screen_with_contact(three):
    """미예약 환자 도구 = 화면 순서·칩, 최근 연락(active)까지 — 방금 연락한 사람을 다시 권하지 않게"""
    _record(three, 1, "no_answer", days_ago=0)
    got = tools._missed(CLINIC, three, {"chip": None, "limit": 10})
    rows = missed.sheet(three, TODAY, datetime.now())["rows"]
    assert [p["chart_no"] for p in got["patients"]] == [r["chart_no"] for r in rows]
    assert [p["chips"] for p in got["patients"]] == [r["chips"] for r in rows]
    by_chart = {r["chart_no"]: r["contact"] for r in rows}
    for p in got["patients"]:
        want = by_chart[p["chart_no"]]
        assert p["contact"] == (want and {k: want[k] for k in ("result", "at", "active")})
    assert any(p["contact"] for p in got["patients"])
