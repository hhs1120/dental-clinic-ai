"""⑥ 경영 리포트 해석 채점(평가 설계 §5) — evaluation/report.py score. 저장된 ax_reports 행을 만든 때의 숫자로 다시 잰다."""

import json
from datetime import date

import pytest

from backend import db, report
from evaluation import report as report_eval
from tests.backend.test_report import make_source

TODAY = date(2026, 12, 1)
GOOD = {"text": "11월 매출은 67만 원입니다.", "used": ["sales"]}
NO_MARK = {"text": "손익은 1원입니다.", "used": ["est.profit"]}


@pytest.fixture
def conn(tmp_path):
    make_source(tmp_path / "source.sqlite")
    c = db.connect(tmp_path / "app.sqlite", tmp_path / "source.sqlite")
    db.init_schema(c)
    yield c
    c.close()


def _put(conn, period, points, valid=True, stale=False):
    m = report.metrics(conn, TODAY, "monthly", period)
    with conn:
        conn.execute(
            "INSERT INTO ax_reports VALUES ('monthly', ?, ?, ?, ?, ?, 'v1', 'm', '2026-12-01')",
            (
                period,
                json.dumps(m),
                "x" if stale else report.metrics_hash(m),
                json.dumps(points) if valid else None,
                int(valid),
            ),
        )


def test_score(conn):
    _put(conn, "2026-11", [GOOD, NO_MARK])  # 후검증을 거치지 않고 넣은 행 — 채점이 다시 잰다
    _put(conn, "2026-10", [], stale=True)
    _put(conn, "2026-09", None, valid=False)
    s = report_eval.score(conn, TODAY)
    assert s["periods"] == sum(len(report.periods(conn, TODAY, x)) for x in report.SCOPES)
    assert (s["saved"], s["valid"], s["empty"], s["stale"]) == (3, 2, 1, 1)
    assert s["points"] == 2
    assert s["numbers_ok"] == {"k": 1, "n": 2}  # "1원"은 손익 값이 아님
    assert s["est_missing"] == 1


def test_score_only_screen_periods(conn):
    """화면 기간 밖의 옛 행(기준일이 바뀐 뒤 남은 것)은 세지 않는다"""
    _put(conn, "2026-11", [GOOD])
    with conn:
        conn.execute(
            "INSERT INTO ax_reports VALUES ('monthly', '2020-01', '{}', 'x', '[]', 1, 'v1', 'm',"
            " '2026-12-01')"
        )
    assert report_eval.score(conn, TODAY)["saved"] == 1


def test_score_empty(conn):
    s = report_eval.score(conn, TODAY)
    assert (s["saved"], s["points"], s["numbers_ok"]) == (0, 0, {"k": 0, "n": 0})


def test_render(conn):
    from evaluation.eval import _render_report

    _put(conn, "2026-11", [GOOD, NO_MARK])
    text = "\n".join(_render_report(report_eval.score(conn, TODAY)))
    assert "## ⑥ 경영 리포트 해석" in text and "| [추정] 표시 어긋남 | 1 |" in text
