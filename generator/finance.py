"""8b단계 재무 수입 — fin_card_sales·fin_card_deposits·fin_bank_transactions(입금)와 ⑧-1 불일치 정답
(가상 데이터 생성 설계 §2.7). 금융 내역은 실제로 낸 돈(장부 + 장부 실수의 실제 값)에서 파생해, 장부 실수는 저절로
장부와 어긋난다. 통장·카드 쪽 불일치(분할결제·취소 후 재승인·수수료 차이·입금 지연·진료 외 매출)는 여기서 심는다.
- 카드 승인: 실제 카드 수납 1건 = 1건(수납 시각, 카드사는 CARD_ISSUERS 비중). 연동일은 장부 승인번호, 아니면 새 번호
- 정산 입금: (카드사, 매출일)마다 — 승인 합계 − 취소, 수수료 = 합계 × 율(내림), 입금일 = 매출일 + 정산 지연 은행 영업일
- 통장 입금: 정산 입금, 현금 입금(cash_deposit_day마다 그 사이 실제 현금 합계), 환자 이체(입금자 = 환자, 보호자가 있으면
  GUARDIAN_PAYER_SHARE만큼 보호자), 공단 급여비(확정 월 지급액 합, 통보일 — 은행 영업일로 미룸). TODAY 이후 행은 없다
"""

import json
from collections import defaultdict
from datetime import date, datetime, timedelta

from generator.master import (
    BANK_TIMES,
    CARD_ISSUERS,
    CASH_DEPOSIT_MEMO,
    DEPOSIT_DELAY_DAYS,
    FEE_MISMATCH_RATE_DIFF,
    GUARDIAN_PAYER_SHARE,
    HOURS,
    MAX_SETTLE_DAYS,
    NHIS_MEMO,
    ORAL_CARE_PRICES,
    RECON_MISMATCH_COUNTS,
    SATURDAY,
    SPLIT_MIN_AMOUNT,
    add_bank_days,
    bank_day,
    cash_deposit_day,
    recon_months,
    recon_test_months,
)

FMT = "%Y-%m-%d %H:%M:%S"
ISSUERS = sorted(CARD_ISSUERS)


def _pick(rng, items: list, n: int, what: str) -> list:
    if len(items) < n:
        raise ValueError(f"{what} 후보 {len(items)}건 < {n}건")
    return [items[int(i)] for i in sorted(rng.choice(len(items), size=n, replace=False))]


def _true_payments(conn) -> list[dict]:
    """받은 금액이 있는 실제 수납 — 장부 행에 장부 실수(truth_ledger_errors)의 실제 금액·일시·수단을 덮은 것"""
    out = []
    for (
        pid,
        pay_id,
        paid,
        paid_at,
        method,
        no,
        err_id,
        kind,
        t_amount,
        t_at,
        t_method,
    ) in conn.execute(
        "SELECT p.patient_id, p.id, p.amount_paid, p.paid_at, p.method, p.approval_no, e.id, e.kind,"
        " e.true_amount, e.true_paid_at, e.true_method FROM emr_payments p"
        " LEFT JOIN truth.truth_ledger_errors e ON e.payment_id = p.id ORDER BY p.id"
    ):
        if err_id is not None:
            paid, paid_at, method = t_amount, t_at, t_method
        if paid > 0:
            out.append(
                {
                    "id": pay_id,
                    "pid": pid,
                    "paid": paid,
                    "at": datetime.strptime(paid_at, FMT),
                    "method": method,
                    "approval_no": no,
                    "error_id": err_id,
                    "error": kind,
                }
            )
    return out


def stage_income(run, rng, fake) -> None:
    conn, today, holidays = run.conn, run.today, run.holidays
    pays = _true_payments(conn)
    used = {p["approval_no"] for p in pays if p["approval_no"]}

    def new_no() -> str:
        while (no := f"{int(rng.integers(10**7, 10**8))}") in used:
            pass
        used.add(no)
        return no

    def weighted_issuer() -> str:
        w = [CARD_ISSUERS[i]["weight"] for i in ISSUERS]
        return ISSUERS[int(rng.choice(len(ISSUERS), p=[x / sum(w) for x in w]))]

    # 카드 승인 — 실제 카드 수납마다
    sales = []  # dict: at, issuer, amount, approval_no, cancel_of, pay
    by_pay = {}
    for p in pays:
        if p["method"] == "card":
            s = {
                "at": p["at"],
                "issuer": weighted_issuer(),
                "amount": p["paid"],
                "approval_no": p["approval_no"] or new_no(),
                "cancel_of": None,
            }
            sales.append(s)
            by_pay[p["id"]] = s

    # 통장·카드 쪽 불일치 — 평가 달 중 정산(지연 포함)·현금 입금이 TODAY 전에 보이는 연동일
    def ok(d: date) -> bool:
        return (
            d.strftime("%Y-%m") in recon_months(today)
            and add_bank_days(d, MAX_SETTLE_DAYS + DEPOSIT_DELAY_DAYS[1], holidays) < today
        )

    failure = set(run.state["link_failure_days"])
    linked = [
        p
        for p in pays
        if p["method"] == "card"
        and p["approval_no"]
        and p["error"] is None
        and p["paid"] >= SPLIT_MIN_AMOUNT
        and ok(p["at"].date())
    ]
    mismatches = []  # dict: on, cause, fin(list of row dicts), payments, ledger
    split = _pick(rng, linked, RECON_MISMATCH_COUNTS["split_payment"], "분할결제")
    for p in split:
        a = by_pay[p["id"]]
        first = int(p["paid"] * rng.uniform(0.3, 0.7)) // 10 * 10
        a["amount"] = first
        b = {
            "at": p["at"],
            "issuer": weighted_issuer(),
            "amount": p["paid"] - first,
            "approval_no": new_no(),
            "cancel_of": None,
        }
        sales.append(b)
        mismatches.append(
            {"on": p["at"].date(), "cause": "split_payment", "fin": [a, b], "payments": [p["id"]]}
        )
    rest = [p for p in linked if p not in split]
    for p in _pick(rng, rest, RECON_MISMATCH_COUNTS["cancel_reapprove"], "취소 후 재승인"):
        a = by_pay[p["id"]]
        cancel = {**a, "at": a["at"] + timedelta(minutes=1), "cancel_of": a["approval_no"]}
        again = {**a, "at": a["at"] + timedelta(minutes=2), "approval_no": new_no()}
        sales += [cancel, again]
        mismatches.append(
            {
                "on": p["at"].date(),
                "cause": "cancel_reapprove",
                "fin": [a, cancel, again],
                "payments": [p["id"]],
            }
        )
    days = sorted(
        {p["at"].date() for p in pays if ok(p["at"].date()) and p["at"].date() not in failure}
    )
    prices = list(ORAL_CARE_PRICES.values())
    for d in _pick(rng, days, RECON_MISMATCH_COUNTS["non_clinical_sale"], "진료 외 매출 날"):
        close = HOURS["saturday_close" if d.weekday() == SATURDAY else "weekday_close"]
        open_ = datetime.combine(d, datetime.strptime(HOURS["open"], "%H:%M").time())
        span = (
            datetime.combine(d, datetime.strptime(close, "%H:%M").time()) - open_
        ).seconds // 60
        items = int(rng.integers(1, 3))
        s = {
            "at": open_ + timedelta(minutes=int(rng.integers(span))),
            "issuer": weighted_issuer(),
            "amount": sum(prices[int(rng.integers(len(prices)))] for _ in range(items)),
            "approval_no": new_no(),
            "cancel_of": None,
        }
        sales.append(s)
        mismatches.append({"on": d, "cause": "non_clinical_sale", "fin": [s], "payments": []})

    # 정산 입금
    groups = defaultdict(list)
    for s in sales:
        groups[(s["issuer"], s["at"].date())].append(s)
    touched = {id(s) for m in mismatches for s in m["fin"]} | {
        id(by_pay[p["id"]]) for p in pays if p["error"] and p["id"] in by_pay
    }
    deposits = {}
    for (issuer, d), group in sorted(groups.items()):
        expected = add_bank_days(d, CARD_ISSUERS[issuer]["settle_days"], holidays)
        if expected >= today:
            continue
        total = sum(-s["amount"] if s["cancel_of"] else s["amount"] for s in group)
        fee = int(total * CARD_ISSUERS[issuer]["fee_rate"])
        deposits[(issuer, d)] = {
            "on": expected,
            "issuer": issuer,
            "sales_date": d,
            "total": total,
            "fee": fee,
            "clean": ok(d) and not any(id(s) in touched for s in group),
        }
    clean = [k for k, v in deposits.items() if v["clean"]]
    fee_keys = _pick(rng, clean, RECON_MISMATCH_COUNTS["fee_mismatch"], "수수료 차이")
    delay_keys = _pick(
        rng,
        [k for k in clean if k not in fee_keys],
        RECON_MISMATCH_COUNTS["deposit_delay"],
        "입금 지연",
    )
    for k in fee_keys:
        v = deposits[k]
        v["fee"] = int(
            v["total"] * (CARD_ISSUERS[v["issuer"]]["fee_rate"] + FEE_MISMATCH_RATE_DIFF)
        )
    for k in delay_keys:
        v = deposits[k]
        lo, hi = DEPOSIT_DELAY_DAYS
        v["on"] = add_bank_days(v["on"], int(rng.integers(lo, hi + 1)), holidays)

    # 통장 입금
    bank = []

    def deposit_row(day: date, kind: str, amount: int, memo: str) -> dict:
        row = {
            "at": datetime.combine(day, datetime.strptime(BANK_TIMES[kind], "%H:%M").time()),
            "amount": amount,
            "memo": memo,
        }
        bank.append(row)
        return row

    for v in deposits.values():
        v["bank"] = deposit_row(v["on"], "card", v["total"] - v["fee"], v["issuer"])
    cash = defaultdict(int)
    for p in pays:
        if p["method"] == "cash":
            cash[cash_deposit_day(p["at"].date(), holidays)] += p["paid"]
    cash_rows = {
        day: deposit_row(day, "cash", amount, CASH_DEPOSIT_MEMO)
        for day, amount in sorted(cash.items())
        if day < today
    }
    names = dict(conn.execute("SELECT id, name FROM emr_patients"))
    guardian = dict(conn.execute("SELECT id, guardian_patient_id FROM emr_patients"))
    transfer_rows = {}
    for p in pays:
        if p["method"] == "transfer":
            g = guardian[p["pid"]]
            payer = g if g and rng.random() < GUARDIAN_PAYER_SHARE else p["pid"]
            row = {"at": p["at"], "amount": p["paid"], "memo": names[payer]}
            bank.append(row)
            transfer_rows[p["id"]] = row
    for amount, reviewed in conn.execute(
        "SELECT sum(paid_amount), max(reviewed_at) FROM emr_claims"
        " WHERE status IN ('paid', 'adjusted') GROUP BY claim_month ORDER BY claim_month"
    ):
        day = date.fromisoformat(reviewed)
        while not bank_day(day, holidays):
            day += timedelta(days=1)
        if day < today and amount:
            deposit_row(day, "nhis", amount, NHIS_MEMO)

    for k, cause in [(k, "fee_mismatch") for k in fee_keys] + [
        (k, "deposit_delay") for k in delay_keys
    ]:
        v = deposits[k]
        mismatches.append(
            {"on": v["sales_date"], "cause": cause, "fin": [v, v["bank"]], "payments": []}
        )

    # 장부 쪽 불일치 — 장부 실수마다 그 돈이 보이는 금융 행
    ledger = {
        pid: (datetime.strptime(at, FMT), method)
        for pid, at, method in conn.execute("SELECT id, paid_at, method FROM emr_payments")
    }
    for p in pays:
        if p["error"] is None:
            continue
        fin = []
        if p["method"] == "card":
            fin.append(by_pay[p["id"]])
        elif p["method"] == "transfer":
            fin.append(transfer_rows[p["id"]])
        else:
            fin.append(cash_rows[cash_deposit_day(p["at"].date(), holidays)])
            shifted = cash_rows.get(cash_deposit_day(ledger[p["id"]][0].date(), holidays))
            if p["error"] == "date_shift" and shifted is not None:
                fin.append(shifted)
        if (
            p["error"] == "method_wrong" and ledger[p["id"]][1] == "cash"
        ):  # 장부엔 현금 — 그 현금 입금도
            row = cash_rows.get(cash_deposit_day(p["at"].date(), holidays))
            if row is not None:
                fin.append(row)
        mismatches.append(
            {
                "on": p["at"].date(),
                "cause": p["error"],
                "fin": fin,
                "payments": [p["id"]],
                "ledger": p["error_id"],
            }
        )

    # 적재 — 행 id는 시각순
    sales.sort(key=lambda s: (s["at"], s["approval_no"], s["cancel_of"] or ""))
    for i, s in enumerate(sales, 1):
        s["id"] = i
    conn.executemany(
        "INSERT INTO fin_card_sales (id, approved_at, issuer, amount, approval_no, cancel_of) VALUES (?, ?, ?, ?, ?, ?)",
        [
            (
                s["id"],
                s["at"].strftime(FMT),
                s["issuer"],
                s["amount"],
                s["approval_no"],
                s["cancel_of"],
            )
            for s in sales
        ],
    )
    deps = sorted(deposits.values(), key=lambda v: (v["on"], v["issuer"], v["sales_date"]))
    for i, v in enumerate(deps, 1):
        v["id"] = i
    conn.executemany(
        "INSERT INTO fin_card_deposits (id, deposited_on, issuer, sales_date, sales_total, fee, amount)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (
                v["id"],
                v["on"].isoformat(),
                v["issuer"],
                v["sales_date"].isoformat(),
                v["total"],
                v["fee"],
                v["total"] - v["fee"],
            )
            for v in deps
        ],
    )
    bank.sort(key=lambda r: (r["at"], r["memo"], r["amount"]))
    for i, r in enumerate(bank, 1):
        r["id"] = i
    conn.executemany(
        "INSERT INTO fin_bank_transactions (id, occurred_at, direction, amount, memo) VALUES (?, ?, 'in', ?, ?)",
        [(r["id"], r["at"].strftime(FMT), r["amount"], r["memo"]) for r in bank],
    )

    def ref(row) -> dict:
        table = (
            "fin_card_sales"
            if "approval_no" in row
            else "fin_card_deposits"
            if "sales_date" in row
            else "fin_bank_transactions"
        )
        return {"table": table, "id": row["id"]}

    mismatches.sort(key=lambda m: (m["on"], m["cause"], m["payments"]))
    conn.executemany(
        "INSERT INTO truth.truth_recon_mismatches (occurred_on, cause, fin_ids, payment_ids, split, ledger_error_id)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [
            (
                m["on"].isoformat(),
                m["cause"],
                json.dumps([ref(r) for r in m["fin"]]),
                json.dumps(m["payments"]),
                "test" if m["on"].strftime("%Y-%m") in recon_test_months(today) else "dev",
                m.get("ledger"),
            )
            for m in mismatches
        ],
    )
