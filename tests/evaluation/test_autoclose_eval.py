"""자동 종결 오류율 (평가 설계 §5) — fixture는 eval_db 머리말. 분류 v2 × 판정기 v1:
통화 1(dev 풀) book=book·통화 4(silver) book=book은 조건을 넘고, 통화 2·3은 목적이 엇갈려, 통화 5는 분류 실패라 못 넘는다."""

import pytest

from evaluation import autoclose, metrics
from evaluation.splits import silver_split
from tests.evaluation.eval_db import make_db


@pytest.fixture
def conn(tmp_path):
    return make_db(tmp_path)


def _validation(conn) -> set:
    rows = [
        dict(r)
        for r in conn.execute(
            "SELECT call_uid, primary_purpose, emergency, complaint FROM truth.truth_call_labels"
            " WHERE split = 'silver'"
        )
    ]
    return {u for u, part in silver_split(rows).items() if part == "validation"}


def test_wilson():
    assert metrics.wilson(0, 0) is None
    lo, hi = metrics.wilson(1, 10)
    assert 0 < lo < 0.1 < hi < 0.5


def test_dev_is_silver_validation(conn):
    r = autoclose.evaluate(conn, "dev", "v2", "v1")
    assert r["pair"] == "v2×v1" and r["n"] == len(_validation(conn))


def test_error_counts_gold_todo(conn):
    """두 모델 모두 할 일 없음이라 조건을 넘었는데 골드는 할 일 있음 → 오류"""
    conn.execute("UPDATE truth.truth_call_labels SET has_todo = 1 WHERE split = 'silver'")
    r = autoclose.evaluate(conn, "dev", "v2", "v1")
    closed = 1 if "c-0004" in _validation(conn) else 0
    assert r["closed"] == closed
    assert r["errors"]["k"] == closed and r["by_item"]["has_todo"] == closed


def test_test_split_is_test_pool_only(conn):
    r = autoclose.evaluate(conn, "test", "v2", "v1")
    assert r["n"] == 1 and r["closed"] == 0  # 통화 3 — 목적 엇갈림
