"""재무 원본(fin_*) 검사 — ⑧-1 수입(8b단계): 카드 승인·정산 입금·통장 입금·불일치 정답 / ⑧-2 지출(8c단계): 출금·증빙·정답
(가상 데이터 생성 설계 §2.7·§5.3 재무). 실제로 낸 돈 = 장부(emr_payments)에 장부 실수 정답(truth_ledger_errors)의
실제 값을 덮은 것 — 금융 내역은 이것에서 나오고, 장부와의 차이가 불일치다"""

import json
import re
from collections import Counter, defaultdict
from datetime import date, timedelta
from itertools import combinations_with_replacement

from generator.master import (
    ACCOUNTS,
    BANK_TIMES,
    CARD_ISSUERS,
    CASH_DEPOSIT_MEMO,
    DEPOSIT_DELAY_DAYS,
    EXPENSE_AMOUNT_DIFF_RANGE,
    EXPENSE_AMOUNT_DIFF_RATE,
    EXPENSE_DIFF_REASONS,
    EXPENSE_ITEMS,
    EXPENSE_NO_DOC_RATE,
    FEE_MISMATCH_RATE_DIFF,
    FULL_MOUTH,
    HOURS,
    LAB_FEES,
    LEDGER_ERROR_KINDS,
    MAX_SETTLE_DAYS,
    NHIS_MEMO,
    NON_CLINICAL_MAX_ITEMS,
    ORAL_CARE_PRICES,
    PROCEDURES,
    RECON_MISMATCH_COUNTS,
    SETTLEMENT_LATE_OPEN_DAYS,
    VENDORS,
    add_bank_days,
    bank_day,
    cash_deposit_day,
    recon_months,
    recon_test_months,
    vendor_aliases,
)
from generator.verify.common import _dt, _rows
from generator.verify.records import _close, _next_open_day

FIN_TABLES = ("fin_card_sales", "fin_card_deposits", "fin_bank_transactions")
# 진료 외 매출 금액 — 구강용품 1~NON_CLINICAL_MAX_ITEMS개 합 (§2.7 non_clinical_sale, 3개는 데이터 재설계)
ORAL_CARE_SMALL_SUMS = {
    a + b for a in ORAL_CARE_PRICES.values() for b in [0, *ORAL_CARE_PRICES.values()]
}
ORAL_CARE_SUMS = {
    sum(c)
    for r in range(1, NON_CLINICAL_MAX_ITEMS + 1)
    for c in combinations_with_replacement(ORAL_CARE_PRICES.values(), r)
}


def _true_payments(conn) -> list[dict]:
    """받은 금액이 있는 실제 수납 — 장부 행에 장부 실수의 실제 금액·일시·수단을 덮은 것"""
    rows = _rows(
        conn,
        "SELECT p.id, p.patient_id, p.amount_paid AS paid, p.paid_at, p.method, p.approval_no,"
        " e.kind AS error, e.true_amount, e.true_paid_at, e.true_method"
        " FROM emr_payments p LEFT JOIN truth.truth_ledger_errors e ON e.payment_id = p.id ORDER BY p.id",
    )
    # 장부에 행 없는 미수 정산 누락 — 금융에만 있는 실제 수납(데이터 재설계 설계 §1)
    rows += _rows(
        conn,
        "SELECT NULL AS id, patient_id, true_amount AS paid, true_paid_at AS paid_at, true_method AS method,"
        " NULL AS approval_no, kind AS error, true_amount, true_paid_at, true_method"
        " FROM truth.truth_ledger_errors WHERE payment_id IS NULL ORDER BY id",
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


def _ledger_fin(e, ledger, m, sales, bank, cash_by_day, fixed, ctx) -> bool:
    """장부 실수 불일치의 금융 행이 실제 돈의 행인지 — 카드: 실제 시각·금액 승인 1건 / 이체: 실제 시각·금액 입금 1건 /
    현금: 실제 날짜의 현금 입금(날짜 밀림이면 장부 날짜의 입금도, 결제수단 오입력이 장부에 현금이면 그날 현금 입금도).
    틀리면 True"""
    t, amount = e["true_paid_at"], e["true_amount"]
    got_sales = [sales[i] for tb, i in m["fin"] if tb == "fin_card_sales" and i in sales]
    got_bank = {i for tb, i in m["fin"] if tb == "fin_bank_transactions"}
    day = date.fromisoformat(t[:10])
    want_bank = set()
    if e["true_method"] == "card":
        if len(got_sales) != 1 or (got_sales[0]["approved_at"], got_sales[0]["amount"]) != (
            t,
            amount,
        ):
            return True
    elif got_sales:
        return True
    if e["true_method"] == "transfer":
        hits = [i for i in got_bank if (bank[i]["occurred_at"], bank[i]["amount"]) == (t, amount)]
        if len(got_bank) != 1 or len(hits) != 1 or bank[hits[0]]["memo"] in fixed:
            return True
        return False
    cash_days = []
    if e["true_method"] == "cash":
        cash_days.append(cash_deposit_day(day, ctx.holidays))
        if e["kind"] == "date_shift":
            cash_days.append(cash_deposit_day(_dt(ledger["paid_at"]).date(), ctx.holidays))
    if e["kind"] == "method_wrong" and ledger["method"] == "cash":
        cash_days.append(cash_deposit_day(day, ctx.holidays))
    want_bank = {cash_by_day[d.isoformat()] for d in cash_days if d.isoformat() in cash_by_day}
    return got_bank != want_bank


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
            or (
                d["id"] in fee_m
                and d["fee"] != int(total * (issuer_info["fee_rate"] + FEE_MISMATCH_RATE_DIFF))
            )
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


def check_unrecorded_settlements(conn, ctx):
    """미수 정산 수납 누락(장부에 행 없음 — 데이터 재설계 설계 §1): 환자가 있고, 금액 = 그 시각 장부 잔액(> 0 — 일부 납부
    없음, 금액이 구강용품 합이어도 잔액으로 풀리게),
    실제 일시가 평가 달(master.recon_months) 안, 이체·카드, ⑧-1 입력 누락 정답(수납 id 없음)과 하나씩 짝. 건수 =
    메타 unrecorded_settlements(= master.UNRECORDED_SETTLEMENT_COUNT), 환자는 서로 다르고 그날 장부 행·그날 뒤 정산 행·장부
    실수가 없음"""
    rows = conn.execute(
        "SELECT e.id, e.patient_id, e.true_amount, e.true_paid_at, e.true_method,"
        " (SELECT count(*) FROM truth.truth_recon_mismatches m WHERE m.ledger_error_id = e.id"
        "  AND m.cause = 'entry_missing' AND m.payment_ids = '[]') AS paired"
        " FROM truth.truth_ledger_errors e WHERE e.payment_id IS NULL"
    ).fetchall()
    bad = []
    for eid, pid, amount, at, method, paired in rows:
        bal = conn.execute(
            "SELECT coalesce(sum(amount_due - amount_paid), 0) FROM emr_payments"
            " WHERE patient_id = ? AND paid_at <= ?",
            (pid, at),
        ).fetchone()[0]
        if (
            pid is None
            or not 0 < amount == bal
            or at[:7] not in recon_months(ctx.today)
            or method not in ("transfer", "card")
            or paired != 1
        ):
            bad.append(
                {"id": eid, "patient_id": pid, "amount": amount, "balance": bal, "paired": paired}
            )
    pids = [r[1] for r in rows]
    if len(set(pids)) != len(pids):
        bad.append({"problem": "미수 정산 누락 환자가 겹침"})
    for (
        eid,
        pid,
        _,
        at,
        _,
        _,
    ) in rows:  # 그날 장부 행 없음·그날 뒤 정산 행 없음·장부 실수 없음(장부 잔액 = 실제 잔액)
        same_day, later, erred = conn.execute(
            "SELECT sum(date(paid_at) = date(?)), sum(kind = 'settlement' AND paid_at > ?),"
            " (SELECT count(*) FROM truth.truth_ledger_errors e JOIN emr_payments q ON q.id = e.payment_id"
            "  WHERE q.patient_id = ?) FROM emr_payments WHERE patient_id = ?",
            (at, at, pid, pid),
        ).fetchone()
        if same_day or later or erred:
            bad.append({"id": eid, "problem": "그날 장부 행·뒤 정산 행·장부 실수가 있는 환자"})
    if len(rows) != ctx.unrecorded_settlements:
        bad.append({"count": len(rows), "expected": ctx.unrecorded_settlements})
    return bad


def check_non_clinical_sales_avoid_balances(conn, ctx):
    """진짜 진료 외 매출 승인 금액은 그날 어느 환자의 장부 잔액(전날까지·그날까지)과도 다르다 — 미수 정산 누락(금액 = 잔액)과
    거꾸로 헷갈리지 않게(데이터 재설계 설계 §1)"""
    sales = conn.execute(
        "SELECT m.id, date(s.approved_at), s.amount FROM truth.truth_recon_mismatches m"
        " JOIN fin_card_sales s ON s.id = json_extract(m.fin_ids, '$[0].id')"
        " WHERE m.cause = 'non_clinical_sale'"
    ).fetchall()
    pays = conn.execute(
        "SELECT patient_id, date(paid_at), amount_due - amount_paid FROM emr_payments ORDER BY paid_at, id"
    ).fetchall()
    bad = []
    for mid, day, amount in sales:
        before, through = defaultdict(int), defaultdict(int)
        for pid, d, owe in pays:
            if d > day:
                break
            through[pid] += owe
            if d < day:
                before[pid] += owe
        if amount in set(before.values()) | set(through.values()):
            bad.append({"mismatch_id": mid, "day": day, "amount": amount})
    return bad


def _transposed(true: int, ledger: int) -> bool:
    """ledger가 true의 이웃 두 자리를 바꾼 더 작은 수(10배 관계 아님, 끝자리 그대로)"""
    a, b = str(true), str(ledger)
    if len(a) != len(b) or ledger >= true or ledger * 10 == true or a[-1] != b[-1]:
        return False
    diff = [i for i in range(len(a)) if a[i] != b[i]]
    return len(diff) == 2 and diff[1] == diff[0] + 1 and a[diff[0]] == b[diff[1]]


def _seen(d: date, ctx) -> bool:
    """그날 카드 정산·입금이 TODAY 전에 보임 — 대조 대기(pending)가 아닌 날"""
    return add_bank_days(d, MAX_SETTLE_DAYS + DEPOSIT_DELAY_DAYS[1], ctx.holidays) < ctx.today


def check_settlement_errors(conn, ctx):
    """정산 수납 장부 실수(정산 행만 — 데이터 재설계 설계 §1): 실제 일시가 평가 달 안, 정산 행 하나에 실수 하나, 그 환자는 실제
    일시 뒤로 통화 없고 TODAY부터 예약 없음. 자릿수 바꿔 적기(amount_typo) = 연동 카드(승인번호)·실제 일시 그대로·장부 금액이 실제 금액의 이웃 두
    자리를 바꾼 더 작은 수 / 며칠 늦게 적기(date_shift) = 이체·금액 같음·장부 날짜가 실제 날 뒤 SETTLEMENT_LATE_OPEN_DAYS
    범위의 진료일·영업시간. 실제 날과 장부 날 모두 정산·입금이 TODAY 전에 보임. 종류별 수 = 메타 settlement_errors"""
    rows = _rows(
        conn,
        "SELECT e.id, e.kind, e.true_amount, e.true_paid_at, e.true_method, p.id AS pay_id, p.patient_id,"
        " p.amount_paid, p.paid_at, p.method, p.approval_no FROM truth.truth_ledger_errors e"
        " JOIN emr_payments p ON p.id = e.payment_id WHERE p.kind = 'settlement' ORDER BY e.id",
    )
    bad, counts = [], Counter(r["kind"] for r in rows)
    lo, hi = SETTLEMENT_LATE_OPEN_DAYS
    for r in rows:
        true_day = _dt(r["true_paid_at"]).date()
        later = conn.execute(
            "SELECT count(*) FROM ax_calls WHERE patient_id = ? AND started_at > ?",
            (r["patient_id"], r["true_paid_at"]),
        ).fetchone()[0]
        future = conn.execute(
            "SELECT count(*) FROM emr_appointments WHERE patient_id = ? AND date(scheduled_at) >= ?",
            (r["patient_id"], ctx.today.isoformat()),
        ).fetchone()[0]
        ok = r["true_method"] == r["method"] and not later and not future
        ok = ok and true_day.strftime("%Y-%m") in recon_months(ctx.today)
        ok = ok and _seen(true_day, ctx)
        if r["kind"] == "amount_typo":
            ok = ok and r["method"] == "card" and bool(r["approval_no"])
            ok = ok and r["paid_at"] == r["true_paid_at"]
            ok = ok and _transposed(r["true_amount"], r["amount_paid"])
        elif r["kind"] == "date_shift":
            days, d = [], true_day
            for _ in range(hi):
                d = _next_open_day(d, ctx.holidays)
                days.append(d)
            at = _dt(r["paid_at"])
            ok = ok and r["method"] == "transfer" and r["amount_paid"] == r["true_amount"]
            ok = ok and at.date() in days[lo - 1 :] and _seen(at.date(), ctx)
            ok = ok and HOURS["open"] <= at.strftime("%H:%M") <= _close(at.date())
        else:
            ok = False
        if not ok:
            bad.append(r)
    if len({r["pay_id"] for r in rows}) != len(rows):
        bad.append({"problem": "정산 행 하나에 실수 둘 이상"})
    if dict(counts) != {k: v for k, v in (ctx.settlement_errors or {}).items() if v}:
        bad.append({"counts": dict(counts), "expected": ctx.settlement_errors})
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
    want["entry_missing"] += ctx.unrecorded_settlements  # 장부에 행 없는 누락도 입력 누락 정답
    for k, v in (ctx.settlement_errors or {}).items():  # 정산 수납 장부 실수도 그 원인의 정답
        want[k] += v
    want["non_clinical_sale"] += (
        ctx.non_clinical_multi
    )  # 구강용품 여러 개 판매도 진료 외 매출 정답
    got = Counter(m["cause"] for m in ms)
    if {k: got.get(k, 0) for k in want} != want or set(got) - set(want):
        bad.append({"counts": dict(got), "expected": want})
    multi = sum(  # 구강용품 NON_CLINICAL_MAX_ITEMS개 판매(1~2개 합과 다른 금액) 수 = 메타
        1
        for m in ms
        if m["cause"] == "non_clinical_sale"
        for t, i in m["fin"][:1]
        if t == "fin_card_sales"
        and conn.execute("SELECT amount FROM fin_card_sales WHERE id = ?", (i,)).fetchone()[0]
        not in ORAL_CARE_SMALL_SUMS
    )
    if multi != ctx.non_clinical_multi:
        bad.append({"non_clinical_multi": multi, "expected": ctx.non_clinical_multi})
    exists = {t: {r[0] for r in conn.execute(f"SELECT id FROM {t}")} for t in FIN_TABLES}
    sales = {s["id"]: s for s in _rows(conn, "SELECT * FROM fin_card_sales")}
    pays = {p["id"]: p for p in _rows(conn, "SELECT * FROM emr_payments")}
    errors = {e["id"]: e for e in _rows(conn, "SELECT * FROM truth.truth_ledger_errors")}
    ledger_nos = _ledger_numbers(conn)
    linked_days = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT date(paid_at) FROM emr_payments WHERE approval_no IS NOT NULL"
        )
    }
    bank = {
        b["id"]: b
        for b in _rows(conn, "SELECT * FROM fin_bank_transactions WHERE direction = 'in'")
    }
    cash_by_day = {
        b["occurred_at"][:10]: i for i, b in bank.items() if b["memo"] == CASH_DEPOSIT_MEMO
    }
    fixed = set(CARD_ISSUERS) | {CASH_DEPOSIT_MEMO, NHIS_MEMO}
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
                or m["payments"] != ([e["payment_id"]] if e["payment_id"] is not None else [])
                or m["occurred_on"] != e["true_paid_at"][:10]
            ):
                why = "장부 실수와 다름"
            elif (
                e["payment_id"] is None
            ):  # 장부에 행 없는 누락 — 모양은 check_unrecorded_settlements가 본다
                pass
            elif _ledger_fin(e, pays[e["payment_id"]], m, sales, bank, cash_by_day, fixed, ctx):
                why = "장부 실수 불일치가 가리키는 금융 행이 실제 돈의 행이 아님"
        elif m["ledger_error_id"] is not None:
            why = "통장·카드 쪽 원인인데 장부 실수 연결"
        elif (
            add_bank_days(
                date.fromisoformat(m["occurred_on"]),
                MAX_SETTLE_DAYS + DEPOSIT_DELAY_DAYS[1],
                ctx.holidays,
            )
            >= ctx.today
        ):
            why = "정산(지연 포함)이 TODAY 전에 오지 않는 날"
        elif m["cause"] in ("split_payment", "cancel_reapprove", "non_clinical_sale") and (
            m["occurred_on"] not in linked_days
        ):
            why = "연동일이 아닌 날의 카드 쪽 불일치"
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


# ── ⑧-2 지출 (8c단계) ───────────────────────────────────────

_LAB_LINE = re.compile(r"^- (\S+) (?:(\d{2})번 )?(" + "|".join(LAB_FEES) + r") ([\d,]+)원$", re.M)
_VENDOR = {v["name"]: v for v in VENDORS} | {
    vendor_aliases(v)["memo"]: v for v in VENDORS
}  # 은행 표기(끝 글자 잘림)도 그 거래처
_CARD_NAMES = {v["card_name"] or v["name"]: v for v in VENDORS if v["pay"] == "card"} | {
    vendor_aliases(v)["card"]: v for v in VENDORS if v["pay"] == "card"
}  # 결제대행사 표기도


def _outflows(conn) -> list[dict]:
    """출금 — 통장 출금(적요 = 거래처) + 병원 카드 사용(가맹점명 → 거래처), 각각 정답 행·증빙을 붙인다"""
    truth = {
        (r["outflow_table"], r["outflow_id"]): r
        for r in _rows(conn, "SELECT * FROM truth.truth_expense_matches")
    }
    docs = {d["id"]: d for d in _rows(conn, "SELECT * FROM fin_expense_documents")}
    rows = [
        {
            "table": "fin_bank_transactions",
            "id": r["id"],
            "at": r["occurred_at"],
            "amount": r["amount"],
            "vendor": _VENDOR.get(r["memo"]),
            "label": r["memo"],
        }
        for r in _rows(conn, "SELECT * FROM fin_bank_transactions WHERE direction = 'out'")
    ] + [
        {
            "table": "fin_card_expenses",
            "id": r["id"],
            "at": r["used_at"],
            "amount": r["amount"],
            "vendor": _CARD_NAMES.get(r["merchant"]),
            "label": r["merchant"],
        }
        for r in _rows(conn, "SELECT * FROM fin_card_expenses")
    ]
    for r in rows:
        r["truth"] = truth.get((r["table"], r["id"]))
        ids = json.loads(r["truth"]["document_ids"]) if r["truth"] else []
        r["docs"] = [docs[i] for i in ids if i in docs]
        r["missing_docs"] = [i for i in ids if i not in docs]
    return rows


def check_expense_matches(conn, ctx):
    """⑧-2 정답: 출금마다 truth_expense_matches 1행, 증빙은 정확히 한 출금에(발행처 = 그 거래처, 발행일 ≤ 출금일,
    본문에 금액), 계정 과목 ∈ master.ACCOUNTS(이체·자동이체 = 거래처 과목, 카드 = 증빙 품목의 과목 — 증빙 없으면 거래처 과목),
    split = 평가 달만(⑧-1과 같은 달 기준). 통장 출금 적요 = 이체·자동이체 거래처, 카드 가맹점명 = 카드 거래처(표기 차이 포함).
    증빙 합계(명세서 등) = 출금액 — 다른 출금은 증빙 있는 출금의 EXPENSE_AMOUNT_DIFF_RATE건, 차이는 100원 ~ 출금액 × 최대 비율,
    그 출금의 증빙 한 장에만 이유 줄(EXPENSE_DIFF_REASONS[증빙 종류] 차이 금액 — 데이터 재설계 설계 §2).
    세금계산서 = 그 출금 명세서 합계. 증빙 없음은 카드 사용에만, EXPENSE_NO_DOC_RATE건 (§2.7)"""
    rows = _outflows(conn)
    bad = []
    keys = {(r["table"], r["id"]) for r in rows}
    bad += [
        {"outflow": k, "problem": "없는 출금의 정답"}
        for k in conn.execute("SELECT outflow_table, outflow_id FROM truth.truth_expense_matches")
        if tuple(k) not in keys
    ]
    used = Counter(d["id"] for r in rows for d in r["docs"])
    bad += [
        {"document_id": i, "problem": "증빙이 두 출금 이상에 속함"}
        for (i,) in conn.execute("SELECT id FROM fin_expense_documents")
        if used.get(i, 0) > 1
    ]
    # 디코이(어느 출금에도 안 붙은 증빙 — 취소 주문·견적) 수 = 메타 expense_decoys(데이터 재설계 설계 §2)
    unused = [i for (i,) in conn.execute("SELECT id FROM fin_expense_documents") if i not in used]
    meta = dict(conn.execute("SELECT key, value FROM truth.truth_gen_meta"))
    if len(unused) != int(meta.get("expense_decoys", 0)):
        bad.append(
            {"unattached_documents": len(unused), "expected": meta.get("expense_decoys", 0)}
        )
    with_docs = diffs = cards = no_docs = 0
    hi = EXPENSE_AMOUNT_DIFF_RANGE[1]
    for r in rows:
        v, t = r["vendor"], r["truth"]
        month = r["at"][:7]
        why = None
        if t is None:
            why = "정답 없음"
        elif v is None or (r["table"] == "fin_card_expenses") != (v["pay"] == "card"):
            why = "적요·가맹점명이 거래처가 아님"
        elif r["missing_docs"]:
            why = "없는 증빙"
        elif t["account"] not in ACCOUNTS:
            why = "계정 과목"
        elif t["split"] != (
            None
            if month not in recon_months(ctx.today)
            else "test"
            if month in recon_test_months(ctx.today)
            else "dev"
        ):
            why = "split"
        elif any(
            d["issuer"] not in (v["name"], vendor_aliases(v)["issuer"])
            or d["issued_on"] > r["at"][:10]
            or f"{d['amount']:,}원" not in d["body"]
            for d in r["docs"]
        ):
            why = "증빙 발행처·발행일·본문 금액"
        if why:
            bad.append({"outflow": (r["table"], r["id"]), "problem": why})
            continue
        if r["table"] == "fin_card_expenses":
            cards += 1
            items = {
                acct
                for item, acct in EXPENSE_ITEMS[v["kind"]]
                for d in r["docs"]
                if item in d["body"]
            }
            want = items if r["docs"] else {v["account"]}
            if len(want) > 1:  # 혼합 주문 — 금액이 큰 품목의 과목(데이터 재설계 설계 §2)
                spent = Counter()
                for d in r["docs"]:
                    for item, acct in EXPENSE_ITEMS[v["kind"]]:
                        m = re.search(rf"- {re.escape(item)} ([\d,]+)원", d["body"])
                        if m:
                            spent[acct] += int(m[1].replace(",", ""))
                want = {spent.most_common(1)[0][0]}
            if want != {t["account"]}:
                bad.append(
                    {"outflow": (r["table"], r["id"]), "problem": "카드 계정 과목 ≠ 품목 과목"}
                )
        elif t["account"] != v["account"]:
            bad.append({"outflow": (r["table"], r["id"]), "problem": "계정 과목 ≠ 거래처 과목"})
        if not r["docs"]:
            no_docs += 1
            if r["table"] != "fin_card_expenses":
                bad.append(
                    {"outflow": (r["table"], r["id"]), "problem": "이체·자동이체에 증빙 없음"}
                )
            continue
        with_docs += 1
        main = [d for d in r["docs"] if d["kind"] != "tax_invoice"] or r["docs"]
        total = sum(d["amount"] for d in main)
        if any(
            d["amount"] != total for d in r["docs"] if d["kind"] == "tax_invoice" and d not in main
        ):
            bad.append({"outflow": (r["table"], r["id"]), "problem": "세금계산서 ≠ 명세서 합계"})
        diff = abs(total - r["amount"])
        reasons = [
            d
            for d in r["docs"]
            if any(
                re.search(rf"{w} [\d,]+원 별도 청구", d["body"])
                for w in EXPENSE_DIFF_REASONS.values()
            )
        ]
        if (bool(diff) != bool(reasons)) or (
            diff
            and (
                len(reasons) != 1
                or f"{EXPENSE_DIFF_REASONS[reasons[0]['kind']]} {diff:,}원 별도 청구"
                not in reasons[0]["body"]
            )
        ):
            bad.append({"outflow": (r["table"], r["id"]), "problem": "금액 차이 이유 줄"})
        if diff:
            diffs += 1
            if not (100 <= diff <= r["amount"] * hi):
                bad.append(
                    {
                        "outflow": (r["table"], r["id"]),
                        "problem": "증빙 금액 차이가 범위 밖",
                        "diff": diff,
                    }
                )
    if diffs != round(EXPENSE_AMOUNT_DIFF_RATE * with_docs):
        bad.append(
            {"amount_diffs": diffs, "expected": round(EXPENSE_AMOUNT_DIFF_RATE * with_docs)}
        )
    if no_docs != round(EXPENSE_NO_DOC_RATE * cards):
        bad.append({"no_docs": no_docs, "expected": round(EXPENSE_NO_DOC_RATE * cards)})
    return bad


def check_lab_statements(conn, ctx):
    """기공소 명세서 줄 = 창 안·TODAY가 속한 달 전의 completed 인레이·크라운·브릿지 진료 1건(환자 이름·치아 — 브릿지는 치아 없음·보철 종류),
    줄 금액 = master.LAB_FEES, 명세서 금액 = 줄 합계 (§2.7)"""
    lab = {
        n for v in VENDORS if v["kind"] == "lab" for n in (v["name"], vendor_aliases(v)["issuer"])
    }
    codes = {p["code"]: p["name"] for p in PROCEDURES if p["name"] in LAB_FEES}
    want = Counter(
        (name, None if tooth == FULL_MOUTH else tooth, codes[code])
        for name, tooth, code in conn.execute(
            "SELECT p.name, t.tooth_no, t.procedure_code FROM emr_treatments t"
            " JOIN emr_patients p ON p.id = t.patient_id"
            f" WHERE t.procedure_code IN ({', '.join('?' * len(codes))}) AND date(t.performed_at) >= ?"
            " AND date(t.performed_at) < ?",
            (*codes, ctx.range_start.isoformat(), ctx.today.replace(day=1).isoformat()),
        )
    )
    got, bad = Counter(), []
    for d in _rows(conn, "SELECT * FROM fin_expense_documents"):
        if d["issuer"] not in lab:
            continue
        lines = _LAB_LINE.findall(d["body"])
        total = 0
        for name, tooth, kind, fee in lines:
            got[(name, tooth or None, kind)] += 1
            total += int(fee.replace(",", ""))
            if int(fee.replace(",", "")) != LAB_FEES[kind]:
                bad.append(
                    {"document_id": d["id"], "problem": "기공 단가", "line": (name, kind, fee)}
                )
        if not lines or total != d["amount"]:
            bad.append({"document_id": d["id"], "problem": "명세서 금액 ≠ 줄 합계"})
    bad += [{"missing_line": k, "n": n} for k, n in (want - got).items()]
    bad += [{"extra_line": k, "n": n} for k, n in (got - want).items()]
    return bad
