"""⑥ 경영 리포트 해석 채점 (평가 설계 §5, 설계 docs/superpowers/specs/2026-11-13-report-design.md §7).
저장된 ax_reports 행을 만든 때의 숫자(metrics)로 다시 잰다 — 해석 저장률·후검증 통과율·본문 숫자 존재율.
정답이 없는 코드 채점이라 분할이 없고, 원인 정확도는 3단계(시나리오 DB)."""

import json
from datetime import date

from backend import report


def score(conn, today: date) -> dict:
    """{periods, saved, valid, empty, stale, points, numbers_ok: {k, n}} — 점 단위로 다시 잰다"""
    out = {
        "periods": sum(len(report.periods(conn, today, s)) for s in report.SCOPES),
        "saved": 0,
        "valid": 0,
        "empty": 0,
        "stale": 0,
        "points": 0,
        "numbers_ok": {"k": 0, "n": 0},
    }
    screen = {(s, p) for s in report.SCOPES for p in report.periods(conn, today, s)}
    for r in conn.execute("SELECT * FROM ax_reports"):
        if (r["scope"], r["period"]) not in screen:  # 기준일이 바뀐 뒤 남은 옛 기간
            continue
        out["saved"] += 1
        if not r["valid"]:
            continue
        out["valid"] += 1
        m, points = json.loads(r["metrics"]), json.loads(r["points"])
        out["empty"] += not points
        now = report.metrics(conn, today, r["scope"], r["period"])
        out["stale"] += r["metrics_hash"] != report.metrics_hash(now)
        prev = report.prev_period(conn, today, r["scope"], r["period"])
        extra = report.label_numbers(r["scope"], r["period"], *([prev] if prev else []))
        for p in points:
            out["points"] += 1
            out["numbers_ok"]["n"] += 1
            try:
                out["numbers_ok"]["k"] += report.point_numbers_ok(m, p, extra)
            except KeyError:  # metrics에 없는 키
                pass
    return out
