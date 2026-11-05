"""dev 판정 풀·dev 블라인드 조건 (backend/pools.py, 앱 설계 §6)."""

import sqlite3
from datetime import date

from backend import pools
from shared.clinic import Clinic
from tests.backend.conftest import SOURCE_SCHEMA

CLINIC = Clinic(today=date(2026, 12, 1), range_start=date(2026, 1, 1), eval_pool_size=4)


def test_dev_pool_conditions():
    """판정 풀 4건 중 정렬 앞 절반(⌈4/2⌉ = 2)이 dev, dev 블라인드는 그중 블라인드 표시된 것"""
    c = sqlite3.connect(":memory:")
    c.executescript(SOURCE_SCHEMA.read_text(encoding="utf-8"))
    c.executemany(
        "INSERT INTO ax_calls (call_uid, source, direction, started_at, duration_sec, has_transcript,"
        " in_eval_pool, eval_sort_order, in_blind_pool) VALUES (?, 'generated', 'inbound',"
        " '2026-07-01 10:00:00', 60, 0, ?, ?, ?)",
        [
            ("c-1", 1, 1, 1),
            ("c-2", 1, 2, 0),
            ("c-3", 1, 3, 1),
            ("c-4", 1, 4, 0),
            ("c-5", 0, None, 0),
        ],
    )
    pick = lambda sql: [  # noqa: E731
        r[0] for r in c.execute(f"SELECT call_uid FROM ax_calls a WHERE {sql} ORDER BY call_uid")
    ]
    assert pick(pools.dev_eval_sql(CLINIC)) == ["c-1", "c-2"]
    assert pick(pools.dev_blind_sql(CLINIC)) == ["c-1"]
