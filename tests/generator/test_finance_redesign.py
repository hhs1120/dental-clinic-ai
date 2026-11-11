"""데이터 재설계 ⑧-1(설계 docs/superpowers/specs/2026-11-11-data-redesign-design.md §1) — 생성 데이터로 확인.
장부·청구·통화·문자는 그대로이고, 미수 정산 수납 누락(장부에 행 없음)이 금융에만 돈으로 남는다."""

import hashlib
import sqlite3

import pytest

from generator.master import ORAL_CARE_PRICES, UNRECORDED_SETTLEMENT_COUNT

# 재설계 전 실데이터에서 뜬 값 — 코드 단계라 생성 픽스처(가짜 LLM)와 같다
BEFORE = {
    # 정산 수납 장부 실수(데이터 재설계)는 정산 행의 금액·날짜만 바꾼다 — 진료 행 전부와 정산 행의 나머지 칸은 그대로
    "payments_visit": (
        "select * from emr_payments where kind='visit' order by id",
        "02516be914b346ca",
    ),
    "payments_settlement": (
        "select id,patient_id,appointment_id,kind,amount_due,method,approval_no from emr_payments"
        " where kind='settlement' order by id",
        "c30ad02d0307e64b",
    ),
    "claims": ("select * from emr_claims order by id", "28715ace5537b020"),
    "ledger_errors": (
        "select kind,payment_id,true_amount,true_paid_at,true_method from truth.truth_ledger_errors"
        " where payment_id in (select id from emr_payments where kind='visit') order by id",
        "7db9d51b09dfd77e",
    ),
    "claim_errors": (  # 추가 처치 누락은 데이터 재설계로 더해진다 — 그 밖의 청구 오류만(test_chart_redesign이 나머지를 본다)
        "select * from truth.truth_claim_errors where kind != 'omitted_extra' order by 1",
        "1fa2be086ed75f2f",
    ),
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


# ── ⑧-2 지출(데이터 재설계 설계 §2) ─────────────────────────────


def test_expense_decoys_unattached_and_counted(conn):
    """디코이 증빙은 어느 정답에도 없고, 수는 메타 expense_decoys"""
    meta = dict(conn.execute("select key, value from truth.truth_gen_meta"))
    used = {
        int(i)
        for (ids,) in conn.execute("select document_ids from truth.truth_expense_matches")
        for i in __import__("json").loads(ids)
    }
    unused = [i for (i,) in conn.execute("select id from fin_expense_documents") if i not in used]
    assert len(unused) == int(meta["expense_decoys"]) > 0


def test_expense_aliases_and_splits_present(conn):
    from generator.master import VENDORS, vendor_aliases

    names = {v["name"] for v in VENDORS} | {v["card_name"] for v in VENDORS if v["card_name"]}
    labels = [
        m for (m,) in conn.execute("select memo from fin_bank_transactions where direction='out'")
    ]
    labels += [m for (m,) in conn.execute("select merchant from fin_card_expenses")]
    aliases = {a for v in VENDORS for a in vendor_aliases(v).values()}
    assert any(lab in aliases and lab not in names for lab in labels)
    issuers = {i for (i,) in conn.execute("select issuer from fin_expense_documents")}
    assert issuers & aliases
    two = conn.execute(
        "select count(*) from truth.truth_expense_matches where outflow_table='fin_card_expenses'"
        " and json_array_length(document_ids) = 2"
    ).fetchone()[0]
    assert two > 0


def test_mixed_orders_take_larger_account(conn):
    """쇼핑몰 혼합 주문 — 정답 과목이 거래처 기본값(소모품비)과 다른 주문이 있다(금액 큰 과목)"""
    n = conn.execute(
        "select count(*) from truth.truth_expense_matches t join fin_card_expenses c on c.id = t.outflow_id"
        " where t.outflow_table='fin_card_expenses' and c.merchant like '%모아덴탈몰%'"
        " and t.account != '소모품비'"
    ).fetchone()[0]
    assert n > 0


def test_expense_meta_words_absent(conn):
    bodies = " ".join(b for (b,) in conn.execute("select body from fin_expense_documents"))
    assert "디코이" not in bodies and "decoy" not in bodies.lower()


def test_amount_diff_reason_line(generated):
    """출금이 증빙보다 많은 건은 증빙 한 장에 이유 한 줄(master.EXPENSE_DIFF_REASONS, 차이 금액), 그 밖의 증빙에는 없다.
    verify가 줄이 빠진 건을 잡는다"""
    from generator import verify
    from generator.master import EXPENSE_DIFF_REASONS
    from generator.paths import ROOT
    from generator.verify.common import load_ctx

    out, _ = generated("expense_diff_reason")
    c = sqlite3.connect(out / "source.sqlite")
    c.row_factory = sqlite3.Row
    c.execute("ATTACH DATABASE ? AS truth", (str(out / "truth.sqlite"),))
    ctx = load_ctx(ROOT / "config" / "generator.yaml", c)
    assert verify.check_expense_matches(c, ctx) == []
    lines = [
        (i, b)
        for i, b in c.execute("select id, body from fin_expense_documents")
        if "별도 청구" in b
    ]
    assert lines and all(any(w in b for w in EXPENSE_DIFF_REASONS.values()) for _, b in lines)
    i, b = lines[0]
    c.execute(
        "UPDATE fin_expense_documents SET body = ? WHERE id = ?",
        ("\n".join(x for x in b.splitlines() if "별도 청구" not in x), i),
    )
    assert any(r["problem"] == "금액 차이 이유 줄" for r in verify.check_expense_matches(c, ctx))


def test_settlements_pay_full_balance_and_sales_avoid_balances(generated):
    """미수 정산 누락은 늘 잔액 전부(일부 납부 없음 — 읽으면 풀리게). 진짜 진료 외 매출 금액은 그날 어느 환자 잔액과도 다르다.
    verify가 둘 다 잡는다"""
    from generator import verify
    from generator.paths import ROOT
    from generator.verify.common import load_ctx

    out, _ = generated("settlement_full_balance")
    c = sqlite3.connect(out / "source.sqlite")
    c.row_factory = sqlite3.Row
    c.execute("ATTACH DATABASE ? AS truth", (str(out / "truth.sqlite"),))
    ctx = load_ctx(ROOT / "config" / "generator.yaml", c)
    assert verify.check_unrecorded_settlements(c, ctx) == []
    assert verify.check_non_clinical_sales_avoid_balances(c, ctx) == []
    assert verify.check_non_clinical_sales_avoid_balances in verify.REQUIRED
    c.execute(  # 일부 납부로 바꾸면 잡는다
        "UPDATE truth.truth_ledger_errors SET true_amount = true_amount - 100 WHERE id ="
        " (SELECT min(id) FROM truth.truth_ledger_errors WHERE payment_id IS NULL)"
    )
    assert verify.check_unrecorded_settlements(c, ctx)


def _transposed(true: int, ledger: int) -> bool:
    """ledger가 true의 이웃 두 자리를 바꾼 수(더 작음, 10배 관계 아님)"""
    a, b = str(true), str(ledger)
    if len(a) != len(b) or ledger >= true or ledger * 10 == true:
        return False
    diff = [i for i in range(len(a)) if a[i] != b[i]]
    return len(diff) == 2 and diff[1] == diff[0] + 1 and a[diff[0]] == b[diff[1]]


def test_settlement_errors(conn):
    """정산 수납 장부 실수(데이터 재설계 — 정산 행만) — 자릿수 바꿔 적기는 연동 카드, 며칠 늦게 적기는 이체(이웃 진료일보다 뒤),
    그날 뒤로 그 환자 통화 없음, 종류별 수 = master.SETTLEMENT_ERROR_COUNTS, 같은 원인의 ⑧-1 불일치 정답과 짝"""
    from generator.master import SETTLEMENT_ERROR_COUNTS

    rows = conn.execute(
        "select e.id, e.kind, e.true_amount, e.true_paid_at, e.true_method, p.amount_paid, p.paid_at,"
        " p.method, p.approval_no, p.patient_id,"
        " (select count(*) from truth.truth_recon_mismatches m where m.ledger_error_id = e.id"
        "  and m.cause = e.kind) from truth.truth_ledger_errors e join emr_payments p on p.id = e.payment_id"
        " where p.kind = 'settlement'"
    ).fetchall()
    kinds = {}
    for _eid, kind, true, t_at, t_method, paid, at, method, no, pid, paired in rows:
        kinds[kind] = kinds.get(kind, 0) + 1
        assert paired == 1 and t_method == method
        later = conn.execute(
            "select count(*) from ax_calls where patient_id = ? and started_at > ?", (pid, t_at)
        ).fetchone()[0]
        assert later == 0
        if kind == "amount_typo":
            assert method == "card" and no and at == t_at and _transposed(true, paid)
        else:
            assert kind == "date_shift" and method == "transfer" and paid == true
            assert at[:10] > t_at[:10]
    assert kinds == SETTLEMENT_ERROR_COUNTS


def test_verify_settlement_errors(generated):
    from generator import verify
    from generator.paths import ROOT
    from generator.verify.common import load_ctx

    out, _ = generated("verify_settlement_errors")
    c = sqlite3.connect(out / "source.sqlite")
    c.row_factory = sqlite3.Row
    c.execute("ATTACH DATABASE ? AS truth", (str(out / "truth.sqlite"),))
    ctx = load_ctx(ROOT / "config" / "generator.yaml", c)
    assert verify.check_settlement_errors in verify.REQUIRED
    assert verify.check_settlement_errors(c, ctx) == []
    assert verify.check_ledger_errors(c, ctx) == []
    c.execute(  # 자릿수가 아니라 아무 금액으로 바꾸면 잡는다
        "UPDATE emr_payments SET amount_paid = 1 WHERE id = (SELECT min(e.payment_id)"
        " FROM truth.truth_ledger_errors e JOIN emr_payments p ON p.id = e.payment_id"
        " WHERE p.kind = 'settlement' AND e.kind = 'amount_typo')"
    )
    assert verify.check_settlement_errors(c, ctx)
