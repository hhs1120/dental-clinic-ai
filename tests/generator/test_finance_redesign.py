"""데이터 재설계 ⑧-1(설계 docs/superpowers/specs/2026-11-11-data-redesign-design.md §1) — 생성 데이터로 확인.
장부·청구·통화·문자는 그대로이고, 미수 정산 수납 누락(장부에 행 없음)이 금융에만 돈으로 남는다."""

import hashlib
import sqlite3

import pytest

from generator.master import ORAL_CARE_PRICES, UNRECORDED_SETTLEMENT_COUNT

# 재설계 전 실데이터에서 뜬 값 — 코드 단계라 생성 픽스처(가짜 LLM)와 같다
BEFORE = {
    "payments": ("select * from emr_payments order by id", "3cdcba69bb8b7925"),
    "claims": ("select * from emr_claims order by id", "28715ace5537b020"),
    "ledger_errors": (
        "select kind,payment_id,true_amount,true_paid_at,true_method from truth.truth_ledger_errors"
        " where payment_id is not null order by id",
        "7db9d51b09dfd77e",
    ),
    "claim_errors": ("select * from truth.truth_claim_errors order by 1", "87bc21825b3c3b48"),
    "messages": ("select * from emr_messages order by id", "5e035a69b6f18322"),
}


@pytest.fixture(scope="module")
def conn(generated):
    out, _ = generated("finance_redesign")
    c = sqlite3.connect(out / "source.sqlite")
    c.execute("ATTACH DATABASE ? AS truth", (str(out / "truth.sqlite"),))
    return c


@pytest.mark.parametrize("name", sorted(BEFORE))
def test_existing_truth_unchanged(conn, name):
    q, want = BEFORE[name]
    assert hashlib.sha256(repr(conn.execute(q).fetchall()).encode()).hexdigest()[:16] == want


def test_unrecorded_settlements_exist_with_patient(conn):
    rows = conn.execute(
        "select patient_id, true_amount, true_method from truth.truth_ledger_errors"
        " where payment_id is null"
    ).fetchall()
    assert len(rows) == UNRECORDED_SETTLEMENT_COUNT
    assert all(pid is not None and amount > 0 for pid, amount, _ in rows)
    sums = {a + b for a in ORAL_CARE_PRICES.values() for b in [0, *ORAL_CARE_PRICES.values()]}
    assert any(m == "card" and amount in sums for _, amount, m in rows)  # 규칙 9를 속이는 몫


def test_unrecorded_settlements_are_recon_truths(conn):
    n = conn.execute(
        "select count(*) from truth.truth_recon_mismatches m join truth.truth_ledger_errors e"
        " on e.id = m.ledger_error_id where e.payment_id is null and m.cause = 'entry_missing'"
        " and m.payment_ids = '[]'"
    ).fetchone()[0]
    assert n == UNRECORDED_SETTLEMENT_COUNT


def test_verify_unrecorded_settlements(generated):
    """verify: 수납 행 없는 누락은 환자가 있고, 금액 ≤ 그 시각 장부 잔액, 평가 달 안, ⑧-1 불일치 정답과 짝"""
    from generator import verify
    from generator.paths import ROOT
    from generator.verify.common import load_ctx

    out, _ = generated("verify_unrecorded")
    c = sqlite3.connect(out / "source.sqlite")
    c.row_factory = sqlite3.Row
    c.execute("ATTACH DATABASE ? AS truth", (str(out / "truth.sqlite"),))
    ctx = load_ctx(ROOT / "config" / "generator.yaml", c)
    assert verify.check_unrecorded_settlements(c, ctx) == []
    assert verify.check_unrecorded_settlements in verify.REQUIRED
    c.execute(
        "UPDATE truth.truth_ledger_errors SET true_amount = 99999999 WHERE id ="
        " (SELECT min(id) FROM truth.truth_ledger_errors WHERE payment_id IS NULL)"
    )
    assert verify.check_unrecorded_settlements(c, ctx)


def _sums(k):
    from itertools import combinations_with_replacement

    return {
        sum(c)
        for r in range(1, k + 1)
        for c in combinations_with_replacement(ORAL_CARE_PRICES.values(), r)
    }


def test_multi_item_non_clinical_sales(conn):
    """구강용품 3개 판매 — 규칙 9(1~2개 합)에 안 걸리고 가격표를 조합해야 풀린다"""
    from generator.master import NON_CLINICAL_MULTI_COUNT

    amounts = [
        a
        for (a,) in conn.execute(
            "select s.amount from truth.truth_recon_mismatches m, json_each(m.fin_ids) f"
            " join fin_card_sales s on s.id = json_extract(f.value, '$.id')"
            " where m.cause = 'non_clinical_sale'"
        )
    ]
    multi = [a for a in amounts if a not in _sums(2)]
    assert len(multi) == NON_CLINICAL_MULTI_COUNT
    assert all(a in _sums(3) for a in multi)


def test_latest_month_has_new_shapes(conn):
    """화면 기본 달(평가 달 마지막 달)에도 새 모양이 들어간다 — 미수 정산 누락·구강용품 3개 판매"""
    from generator.master import RECON_LATEST_MONTH_MIN

    month = conn.execute(
        "select max(substr(occurred_on, 1, 7)) from truth.truth_recon_mismatches"
    ).fetchone()[0]
    unrec = conn.execute(
        "select count(*) from truth.truth_recon_mismatches m join truth.truth_ledger_errors e"
        " on e.id = m.ledger_error_id where e.payment_id is null and substr(m.occurred_on, 1, 7) = ?",
        (month,),
    ).fetchone()[0]
    assert unrec >= RECON_LATEST_MONTH_MIN
    multi = [
        a
        for (a,) in conn.execute(
            "select s.amount from truth.truth_recon_mismatches m, json_each(m.fin_ids) f"
            " join fin_card_sales s on s.id = json_extract(f.value, '$.id')"
            " where m.cause = 'non_clinical_sale' and substr(m.occurred_on, 1, 7) = ?",
            (month,),
        )
        if a not in _sums(2)
    ]
    assert len(multi) >= RECON_LATEST_MONTH_MIN
