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
    build.generate(out / "app.sqlite", out / "truth.sqlite", CONFIG, fake_llm=True)
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


def test_unconfirmed_booking_calls_precede_booking(db):
    """예약·변경 통화: 확정이면 통화 시각 = 예약 생성 시각, 미확정이면 그날 그보다 앞(회신해서 확정)"""
    rows = db.execute("SELECT c.started_at, l.appt_confirmed, a.created_at FROM ax_calls c"
                      " JOIN truth.truth_call_labels l USING (call_uid)"
                      " JOIN emr_appointments a ON a.id = json_extract(l.used_fact, '$.appointment_id')"
                      " WHERE l.primary_purpose IN ('book', 'reschedule')").fetchall()
    assert all(t == created for t, ok, created in rows if ok)
    unconfirmed = [(t, created) for t, ok, created in rows if not ok]
    assert unconfirmed and all(t[:10] == created[:10] and t < created for t, created in unconfirmed)


def test_price_subtopic_has_topic_procedure(db):
    """부 주제가 가격 문의여도 주제 시술이 있다(가격 정보를 입력에 넣으려고) — 예약류는 그 예약의 시술"""
    rows = db.execute("SELECT l.primary_purpose, l.used_fact FROM truth.truth_call_labels l"
                      " WHERE l.accepted_purposes LIKE '%price_q%'").fetchall()
    assert rows
    for purpose, fact in rows:
        f = json.loads(fact)
        assert f.get("topic_procedure") in calls.PRICED, (purpose, f)  # 0원 시술(상담 등)의 가격은 묻지 않는다
        if "appointment" in f and f["appointment"]["procedure"] in calls.PRICED:
            assert f["topic_procedure"] == f["appointment"]["procedure"]


def test_booking_calls_not_urgent_for_later_visit(db):
    """급함 urgent = 오늘 봐야 함 — 예약·변경 통화의 예약이 그날이 아니면 urgent가 아니다(애매한 통증 노이즈도)"""
    rows = db.execute("SELECT c.started_at, l.used_fact FROM ax_calls c JOIN truth.truth_call_labels l USING (call_uid)"
                      " WHERE l.primary_purpose IN ('book', 'reschedule') AND l.urgency = 'urgent'").fetchall()
    assert all(json.loads(f)["appointment"]["date"] == t[:10] for t, f in rows)


def test_new_child_booking_is_guardian(db):
    """처음 등록하는 소아의 예약 잡기는 기존 환자인 보호자가 건다 — 처음 연락이 아니라 보호자"""
    rows = db.execute("SELECT l.counterpart FROM ax_calls c JOIN truth.truth_call_labels l USING (call_uid)"
                      " JOIN emr_patients p ON p.id = c.patient_id WHERE l.primary_purpose = 'book'"
                      " AND p.registered_at = date(c.started_at) AND p.guardian_patient_id IS NOT NULL").fetchall()
    assert all(r[0] == "guardian" for r in rows)


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
    build.generate(tmp_path / "app.sqlite", tmp_path / "truth.sqlite", CONFIG, fake_llm=True)
    assert rows(sqlite3.connect(tmp_path / "app.sqlite")) == before


def test_pool_split_halves(db):
    rows = [r for r in _labels(db) if r["in_eval_pool"]]
    split = Counter(r["split"] for r in rows)
    assert split["dev"] == (len(rows) + 1) // 2 and split["test"] == len(rows) // 2
