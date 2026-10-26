"""6단계 통화 슬롯 — 할당량·풀 배분 규칙, 생성 결과 (가상 데이터 생성 설계 §4.1, 평가 설계 §2.1).
필수 검사 전부는 tests/generator/test_records.py가 본다."""
import json
import sqlite3
from collections import Counter

import pytest

from generator import build, calls
from generator.master import (CALL_NOISE_SHARE, CALL_OUTBOUND_SHARE, CALL_PURPOSE_SHARE, EVAL_MIN_PER_SPLIT,
                              EVAL_RARE_PURPOSES, EVAL_RARE_SHARE, OUTBOUND_MULTI_SHARE)
from generator.paths import ROOT

CONFIG = ROOT / "config" / "generator.yaml"


def test_quota_sums_to_n_and_rounds_by_largest_remainder():
    assert calls.quota({"a": 0.5, "b": 0.3, "c": 0.2}, 7) == {"a": 4, "b": 2, "c": 1}  # 3.5·2.1·1.4 → 잔여 큰 a
    q = calls.quota(CALL_PURPOSE_SHARE, 431)
    assert sum(q.values()) == 431 and all(abs(q[k] - v * 431) < 1 for k, v in CALL_PURPOSE_SHARE.items())


def _by(counts: dict) -> dict:
    return {c: [f"{c}-{i}" for i in range(n)] for c, n in counts.items()}


def test_allocate_oversamples_rare_and_lifts_small_categories():
    """1000건·풀 150: 희귀는 60%, 'other'(30건)처럼 비례가 split당 2건 미만이면 4건까지 올린다"""
    by = _by(calls.quota(CALL_PURPOSE_SHARE, 1000))
    n = calls._allocate(by, 150)
    assert sum(n.values()) == 150
    assert all(n[c] == round(EVAL_RARE_SHARE * len(by[c])) for c in EVAL_RARE_PURPOSES)
    assert all(n[c] >= 2 * EVAL_MIN_PER_SPLIT for c in by)


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    out = tmp_path_factory.mktemp("calls")
    build.generate(out / "app.sqlite", out / "truth.sqlite", CONFIG)
    conn = sqlite3.connect(out / "app.sqlite")
    conn.row_factory = sqlite3.Row
    conn.execute("ATTACH DATABASE ? AS truth", (str(out / "truth.sqlite"),))
    return conn


def _labels(conn):
    return [dict(r) for r in conn.execute("SELECT c.direction, c.in_eval_pool, c.eval_sort_order, l.* FROM ax_calls c"
                                          " JOIN truth.truth_call_labels l USING (call_uid)")]


def test_purpose_direction_noise_follow_quota(db):
    rows = _labels(db)
    n = len(rows)
    purposes = Counter(r["primary_purpose"] for r in rows)
    assert purposes == Counter(calls.quota(CALL_PURPOSE_SHARE, n))
    noise = Counter(r["noise_type"] for r in rows)
    assert {k: noise[k] for k in CALL_NOISE_SHARE} == {k: v for k, v in calls.quota(
        {**CALL_NOISE_SHARE, None: 1 - sum(CALL_NOISE_SHARE.values())}, n).items() if k}
    for purpose, k in purposes.items():
        out = [r for r in rows if r["primary_purpose"] == purpose and r["direction"] == "outbound"]
        share = CALL_OUTBOUND_SHARE.get(purpose, 0) / CALL_PURPOSE_SHARE[purpose]
        assert len(out) == calls.quota({"o": share, "i": 1 - share}, k)["o"], purpose
        # outbound 복합 주제(구조) — noise_type 없음
        multi = [r for r in out if len(json.loads(r["accepted_purposes"])) > 1]
        assert len(multi) == calls.quota({"m": OUTBOUND_MULTI_SHARE, "s": 1 - OUTBOUND_MULTI_SHARE}, len(out))["m"]
        assert all(r["noise_type"] != "two_topics" for r in out)


def _adjacent_same(purposes: list) -> tuple[int, float]:
    """이웃한 쌍 중 같은 카테고리 수와 셔플(무작위 순열)일 때의 기대값 Σ n_c(n_c − 1) / n"""
    same = sum(a == b for a, b in zip(purposes, purposes[1:]))
    return same, sum(k * (k - 1) for k in Counter(purposes).values()) / len(purposes)


def test_pool_order_and_call_uid_are_shuffled(db):
    """평가 설계 §2.1 — 순번이 층화 순서대로면 카테고리가 드러난다. 판정 풀 split 안 순번·call_uid 순서 모두
    같은 카테고리가 이웃하는 쌍이 셔플 기대값 근처(카테고리별로 몰리면 거의 n − 카테고리 수)"""
    rows = sorted((r for r in _labels(db) if r["in_eval_pool"]), key=lambda r: r["eval_sort_order"])
    for split in ("dev", "test"):
        same, expected = _adjacent_same([r["primary_purpose"] for r in rows if r["split"] == split])
        assert same <= 2 * expected + 5, (split, same, expected)
    same, expected = _adjacent_same([r["primary_purpose"] for r in sorted(_labels(db), key=lambda r: r["call_uid"])])
    assert same <= 1.5 * expected + 10, (same, expected)


def test_call_rows_independent_of_noise_shares(db, tmp_path, monkeypatch):
    """노이즈 비중만 바꿔도 통화 행(call_uid·환자·시각·길이)과 풀 소속은 그대로 — call_uid는 7단계 LLM 캐시 키"""
    def rows(conn):
        return sorted(tuple(r) for r in conn.execute(
            "SELECT call_uid, patient_id, started_at, duration_sec, in_eval_pool FROM ax_calls"))
    before = rows(db)
    monkeypatch.setattr(calls, "CALL_NOISE_SHARE", {"two_topics": 0.08, "interrupted": 0.10, "ambiguous_pain": 0.03,
                                                    "guardian_confused": 0.02})
    build.generate(tmp_path / "app.sqlite", tmp_path / "truth.sqlite", CONFIG)
    assert rows(sqlite3.connect(tmp_path / "app.sqlite")) == before


def test_pool_split_halves(db):
    rows = [r for r in _labels(db) if r["in_eval_pool"]]
    split = Counter(r["split"] for r in rows)
    assert split["dev"] == (len(rows) + 1) // 2 and split["test"] == len(rows) // 2
