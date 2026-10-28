"""재무 원본(fin_*) 검사 — ⑧-1 수입(8b단계): 카드 승인·정산 입금·통장 입금·불일치 정답
(가상 데이터 생성 설계 §2.7·§5.3 재무). 실제로 낸 돈 = 장부(emr_payments)에 장부 실수 정답(truth_ledger_errors)의
실제 값을 덮은 것 — 금융 내역은 이것에서 나오고, 장부와의 차이가 불일치다"""

import json
from collections import Counter, defaultdict
from datetime import date, timedelta
from itertools import combinations_with_replacement

from generator.master import (
    BANK_TIMES,
    CARD_ISSUERS,
    CASH_DEPOSIT_MEMO,
    DEPOSIT_DELAY_DAYS,
    LEDGER_ERROR_KINDS,
    NHIS_MEMO,
    ORAL_CARE_PRICES,
    RECON_MISMATCH_COUNTS,
    add_bank_days,
    bank_day,
    cash_deposit_day,
    recon_months,
    recon_test_months,
)
from generator.verify.common import _dt, _rows

FIN_TABLES = ("fin_card_sales", "fin_card_deposits", "fin_bank_transactions")
# 진료 외 매출 금액 — 구강용품 1~2개 합 (§2.7 non_clinical_sale)
ORAL_CARE_SUMS = {
    sum(c) for r in (1, 2) for c in combinations_with_replacement(ORAL_CARE_PRICES.values(), r)
}


def _true_payments(conn) -> list[dict]:
    """받은 금액이 있는 실제 수납 — 장부 행에 장부 실수의 실제 금액·일시·수단을 덮은 것"""
    rows = _rows(
        conn,
        "SELECT p.id, p.patient_id, p.amount_paid AS paid, p.paid_at, p.method, p.approval_no,"
        " e.kind AS error, e.true_amount, e.true_paid_at, e.true_method"
        " FROM emr_payments p LEFT JOIN truth.truth_ledger_errors e ON e.payment_id = p.id ORDER BY p.id",
    )
    for r in rows:
        if r["error"]:
            r["paid"], r["paid_at"], r["method"] = (
                r["true_amount"],
                r["true_paid_at"],
                r["true_method"],
            )
    return [r for r in rows if r["paid"] > 0]


def _mismatches(conn) -> list[dict]:
    rows = _rows(conn, "SELECT * FROM truth.truth_recon_mismatches ORDER BY id")
    for r in rows:
        r["fin"] = [(f["table"], f["id"]) for f in json.loads(r["fin_ids"])]
        r["payments"] = json.loads(r["payment_ids"])
    return rows


def _referenced(mismatches, table: str, causes=None) -> set:
    return {
        i
        for m in mismatches
        if causes is None or m["cause"] in causes
        for t, i in m["fin"]
        if t == table
    }


def _ledger_numbers(conn) -> set:
    return {
        r[0]
        for r in conn.execute("SELECT approval_no FROM emr_payments WHERE approval_no IS NOT NULL")
    }


def check_card_sales(conn, ctx):
    """카드 승인 = 실제 카드 수납 1건마다 1건(일시·금액, 연동이면 장부 승인번호) — 분할결제·취소 후 재승인 수납과
    통장·카드 쪽 불일치(분할결제·취소 후 재승인·진료 외 매출)에 든 승인·취소는 빼고 비교. 취소는 불일치에만, 카드사는 master.CARD_ISSUERS, 승인번호(취소 제외) 중복 없음"""
    ms = _mismatches(conn)
    special = {
        p for m in ms if m["cause"] in ("split_payment", "cancel_reapprove") for p in m["payments"]
    }
    in_mismatch = _referenced(
        ms, "fin_card_sales", ("split_payment", "cancel_reapprove", "non_clinical_sale")
    )  # 장부 실수 불일치가 가리키는 승인은 실제 수납의 정상 승인이다
    ledger_nos = _ledger_numbers(conn)
    sales = _rows(conn, "SELECT * FROM fin_card_sales ORDER BY id")
    want = Counter(
        (p["paid_at"], p["paid"], p["approval_no"])
        for p in _true_payments(conn)
        if p["method"] == "card" and p["id"] not in special
    )
    got = Counter(
        (
            s["approved_at"],
            s["amount"],
            s["approval_no"] if s["approval_no"] in ledger_nos else None,
        )
        for s in sales
        if s["id"] not in in_mismatch and s["cancel_of"] is None
    )
    bad = [{"missing_sale": k, "n": n} for k, n in (want - got).items()]
    bad += [{"extra_sale": k, "n": n} for k, n in (got - want).items()]
    bad += [
        {**s, "problem": "불일치에 없는 취소"}
        for s in sales
        if s["cancel_of"] is not None and s["id"] not in in_mismatch
    ]
    bad += [{**s, "problem": "카드사"} for s in sales if s["issuer"] not in CARD_ISSUERS]
    numbers = Counter(s["approval_no"] for s in sales if s["cancel_of"] is None)
    bad += [{"approval_no": k, "problem": "승인번호 중복"} for k, n in numbers.items() if n > 1]
    return bad


def check_card_deposits(conn, ctx):
    """정산 입금 = (카드사, 매출일)마다 1건 — 승인 합계 − 취소, 수수료 = 합계 × 율(내림, 수수료 차이 불일치는 다름),
    입금액 = 합계 − 수수료, 입금일 = 매출일 + 정산 지연 은행 영업일(입금 지연 불일치는 DEPOSIT_DELAY_DAYS만큼 뒤).
    입금 예정일이 TODAY 이후인 매출은 입금 없음 (§2.7)"""
    ms = _mismatches(conn)
    fee_m = _referenced(ms, "fin_card_deposits", ("fee_mismatch",))
    delay_m = _referenced(ms, "fin_card_deposits", ("deposit_delay",))
    totals = defaultdict(int)
    for s in _rows(conn, "SELECT issuer, approved_at, amount, cancel_of FROM fin_card_sales"):
        totals[(s["issuer"], s["approved_at"][:10])] += (
            -s["amount"] if s["cancel_of"] else s["amount"]
        )
    deposits = {
        (d["issuer"], d["sales_date"]): d for d in _rows(conn, "SELECT * FROM fin_card_deposits")
    }
    bad = [{**d, "problem": "승인 없는 정산"} for k, d in deposits.items() if k not in totals]
    for (issuer, day), total in sorted(totals.items()):
        issuer_info = CARD_ISSUERS.get(issuer)
        if issuer_info is None:
            continue
        expected = add_bank_days(date.fromisoformat(day), issuer_info["settle_days"], ctx.holidays)
        d = deposits.get((issuer, day))
        if expected >= ctx.today:
            if d is not None:
                bad.append({**d, "problem": "입금 예정일이 TODAY 이후인 매출의 정산"})
            continue
        if d is None:
            bad.append({"issuer": issuer, "sales_date": day, "problem": "정산 입금 없음"})
            continue
        fee = int(total * issuer_info["fee_rate"])
        on = date.fromisoformat(d["deposited_on"])
        lo, hi = (add_bank_days(expected, n, ctx.holidays) for n in DEPOSIT_DELAY_DAYS)
        if (
            d["sales_total"] != total
            or (d["fee"] == fee) == (d["id"] in fee_m)
            or d["amount"] != total - d["fee"]
            or (on == expected) == (d["id"] in delay_m)
            or (d["id"] in delay_m and not (lo <= on <= hi and bank_day(on, ctx.holidays)))
        ):
            bad.append(
                {**d, "expected_total": total, "expected_fee": fee, "expected_on": str(expected)}
            )
    return bad


def _payer_names(conn) -> dict:
    """환자 → 이체 입금자명 후보(본인, 보호자가 있으면 보호자)"""
    names = dict(conn.execute("SELECT id, name FROM emr_patients"))
    return {
        pid: {names[pid]} | ({names[g]} if g else set())
        for pid, g in conn.execute("SELECT id, guardian_patient_id FROM emr_patients")
    }


def check_bank_inflows(conn, ctx):
    """통장 입금 = 정산 입금마다 1건(입금일·입금액·카드사) + 현금 입금일마다 그 사이 실제 현금 합계 + 실제 이체마다 1건
    (그 시각·금액, 입금자 = 환자 또는 보호자) + 확정 월 공단 급여비(통보일, 은행 영업일로 미룸). 그 밖의 입금 없음 (§2.7)"""

    def t(d, kind: str) -> str:
        return f"{d} {BANK_TIMES[kind]}:00"

    want = Counter()
    for d in _rows(conn, "SELECT deposited_on, amount, issuer FROM fin_card_deposits"):
        want[(t(d["deposited_on"], "card"), d["amount"], d["issuer"])] += 1
    cash, transfers = defaultdict(int), []
    for p in _true_payments(conn):
        if p["method"] == "cash":
            cash[cash_deposit_day(_dt(p["paid_at"]).date(), ctx.holidays)] += p["paid"]
        elif p["method"] == "transfer":
            transfers.append(p)
    for day, amount in cash.items():
        if day < ctx.today:
            want[(t(day, "cash"), amount, CASH_DEPOSIT_MEMO)] += 1
    for _, amount, reviewed in conn.execute(
        "SELECT claim_month, sum(paid_amount), max(reviewed_at) FROM emr_claims"
        " WHERE status IN ('paid', 'adjusted') GROUP BY claim_month"
    ):
        day = date.fromisoformat(reviewed)
        while not bank_day(day, ctx.holidays):
            day += timedelta(days=1)
        if day < ctx.today and amount:
            want[(t(day, "nhis"), amount, NHIS_MEMO)] += 1
    rows = _rows(conn, "SELECT * FROM fin_bank_transactions WHERE direction = 'in'")
    fixed = set(CARD_ISSUERS) | {CASH_DEPOSIT_MEMO, NHIS_MEMO}
    got = Counter((r["occurred_at"], r["amount"], r["memo"]) for r in rows if r["memo"] in fixed)
    bad = [{"missing_in": k, "n": n} for k, n in (want - got).items()]
    bad += [{"extra_in": k, "n": n} for k, n in (got - want).items()]
    payers = _payer_names(conn)
    want_t = Counter((p["paid_at"], p["paid"]) for p in transfers)
    got_t = Counter((r["occurred_at"], r["amount"]) for r in rows if r["memo"] not in fixed)
    bad += [{"missing_transfer": k, "n": n} for k, n in (want_t - got_t).items()]
    bad += [{"extra_transfer": k, "n": n} for k, n in (got_t - want_t).items()]
    by_key = defaultdict(set)
    for p in transfers:
        by_key[(p["paid_at"], p["paid"])] |= payers[p["patient_id"]]
    bad += [
        {**r, "problem": "이체 입금자가 환자·보호자가 아님"}
        for r in rows
        if r["memo"] not in fixed
        and r["memo"] not in by_key.get((r["occurred_at"], r["amount"]), set())
    ]
    return bad


def check_recon_mismatches(conn, ctx):
    """불일치 정답: 원인별 건수 = 메타 recon_mismatches, 날짜는 평가 달(master.recon_months — split = master.recon_test_months면 test),
    fin·수납 id 존재. 장부 쪽 원인 ⇔ 장부 실수 1:1(같은 종류·수납·실제 날짜). 통장·카드 쪽 모양 — 분할결제 = 같은 시각 승인
    2건 합 = 장부 금액(하나는 장부 승인번호) / 취소 후 재승인 = 장부 승인·그 취소·같은 금액 새 승인 / 진료 외 매출 = 장부에
    없는 구강용품 금액 승인 / 수수료 차이·입금 지연 = 정산 입금 1건. 현금 입금 1건에 장부 실수 불일치는 최대 1건(합계 차이
    하나에 정답 하나) (§2.7)"""
    ms = _mismatches(conn)
    bad = []
    want = {k: ctx.recon_mismatches.get(k, 0) for k in RECON_MISMATCH_COUNTS}
    got = Counter(m["cause"] for m in ms)
    if {k: got.get(k, 0) for k in want} != want or set(got) - set(want):
        bad.append({"counts": dict(got), "expected": want})
    exists = {t: {r[0] for r in conn.execute(f"SELECT id FROM {t}")} for t in FIN_TABLES}
    sales = {s["id"]: s for s in _rows(conn, "SELECT * FROM fin_card_sales")}
    pays = {p["id"]: p for p in _rows(conn, "SELECT * FROM emr_payments")}
    errors = {e["id"]: e for e in _rows(conn, "SELECT * FROM truth.truth_ledger_errors")}
    ledger_nos = _ledger_numbers(conn)
    linked = Counter(m["ledger_error_id"] for m in ms if m["ledger_error_id"] is not None)
    bad += [
        {"ledger_error_id": e, "problem": "장부 실수에 불일치가 1건이 아님"}
        for e in errors
        if linked.get(e, 0) != 1
    ]
    cash_rows = {
        r[0]
        for r in conn.execute(
            "SELECT id FROM fin_bank_transactions WHERE memo = ?", (CASH_DEPOSIT_MEMO,)
        )
    }
    per_cash = Counter(
        i
        for m in ms
        if m["ledger_error_id"] is not None
        for t, i in m["fin"]
        if t == "fin_bank_transactions" and i in cash_rows
    )
    bad += [
        {"bank_id": i, "problem": "같은 현금 입금에 장부 실수 불일치가 둘 이상"}
        for i, n in sorted(per_cash.items())
        if n > 1
    ]
    for m in ms:
        why = None
        month = m["occurred_on"][:7]
        fin_sales = [sales[i] for t, i in m["fin"] if t == "fin_card_sales" and i in sales]
        if month not in recon_months(ctx.today) or m["split"] != (
            "test" if month in recon_test_months(ctx.today) else "dev"
        ):
            why = "평가 달·split"
        elif not m["fin"] or any(t not in exists or i not in exists[t] for t, i in m["fin"]):
            why = "fin id 없음"
        elif any(p not in pays for p in m["payments"]):
            why = "수납 id 없음"
        elif m["cause"] in LEDGER_ERROR_KINDS:
            e = errors.get(m["ledger_error_id"])
            if (
                e is None
                or e["kind"] != m["cause"]
                or m["payments"] != [e["payment_id"]]
                or m["occurred_on"] != e["true_paid_at"][:10]
            ):
                why = "장부 실수와 다름"
        elif m["ledger_error_id"] is not None:
            why = "통장·카드 쪽 원인인데 장부 실수 연결"
        elif m["cause"] == "split_payment":
            p = pays.get(m["payments"][0]) if len(m["payments"]) == 1 else None
            if (
                p is None
                or len(fin_sales) != 2
                or len({s["approved_at"] for s in fin_sales}) != 1
                or sum(s["amount"] for s in fin_sales) != p["amount_paid"]
                or sorted(s["approval_no"] == p["approval_no"] for s in fin_sales) != [False, True]
                or any(s["cancel_of"] for s in fin_sales)
            ):
                why = "분할결제 모양"
        elif m["cause"] == "cancel_reapprove":
            p = pays.get(m["payments"][0]) if len(m["payments"]) == 1 else None
            first = [
                s
                for s in fin_sales
                if p and s["approval_no"] == p["approval_no"] and not s["cancel_of"]
            ]
            cancel = [s for s in fin_sales if p and s["cancel_of"] == p["approval_no"]]
            again = [
                s for s in fin_sales if s["approval_no"] not in ledger_nos and not s["cancel_of"]
            ]
            if (
                len(fin_sales) != 3
                or len(first) != 1
                or len(cancel) != 1
                or len(again) != 1
                or not first[0]["amount"]
                == cancel[0]["amount"]
                == again[0]["amount"]
                == p["amount_paid"]
            ):
                why = "취소 후 재승인 모양"
        elif m["cause"] == "non_clinical_sale":
            if (
                m["payments"]
                or len(fin_sales) != 1
                or fin_sales[0]["amount"] not in ORAL_CARE_SUMS
                or fin_sales[0]["approval_no"] in ledger_nos
            ):
                why = "진료 외 매출 모양"
        elif m["cause"] in ("fee_mismatch", "deposit_delay"):
            if m["payments"] or [t for t, _ in m["fin"]].count("fin_card_deposits") != 1:
                why = "정산 입금 불일치 모양"
        if why:
            bad.append({"id": m["id"], "cause": m["cause"], "problem": why})
    return bad
