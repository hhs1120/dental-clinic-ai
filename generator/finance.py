"""8b단계 재무 수입 — fin_card_sales·fin_card_deposits·fin_bank_transactions(입금)와 ⑧-1 불일치 정답, 8c단계 지출(아래
stage_expenses)
(가상 데이터 생성 설계 §2.7). 금융 내역은 실제로 낸 돈(장부 + 장부 실수의 실제 값)에서 파생해, 장부 실수는 저절로
장부와 어긋난다. 통장·카드 쪽 불일치(분할결제·취소 후 재승인·수수료 차이·입금 지연·진료 외 매출)는 여기서 심는다.
- 카드 승인: 실제 카드 수납 1건 = 1건(수납 시각, 카드사는 CARD_ISSUERS 비중). 연동일은 장부 승인번호, 아니면 새 번호
- 정산 입금: (카드사, 매출일)마다 — 승인 합계 − 취소, 수수료 = 합계 × 율(내림), 입금일 = 매출일 + 정산 지연 은행 영업일
- 통장 입금: 정산 입금, 현금 입금(cash_deposit_day마다 그 사이 실제 현금 합계), 환자 이체(입금자 = 환자, 보호자가 있으면
  GUARDIAN_PAYER_SHARE만큼 보호자), 공단 급여비(확정 월 지급액 합, 통보일 — 은행 영업일로 미룸). TODAY 이후 행은 없다
"""

import json
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from itertools import combinations_with_replacement

from generator.master import (
    BANK_TIMES,
    CARD_ISSUERS,
    CASH_DEPOSIT_MEMO,
    DEPOSIT_DELAY_DAYS,
    EXPENSE_ALIAS_RATE,
    EXPENSE_AMOUNT_DIFF_RANGE,
    EXPENSE_AMOUNT_DIFF_RATE,
    EXPENSE_DECOY_RATE,
    EXPENSE_DIFF_REASONS,
    EXPENSE_DOC_LEAD_DAYS,
    EXPENSE_ISSUER_ALIAS_RATE,
    EXPENSE_ITEMS,
    EXPENSE_MIXED_ORDER_RATE,
    EXPENSE_NO_DOC_RATE,
    EXPENSE_SPLIT_DOC_RATE,
    FEE_MISMATCH_RATE_DIFF,
    FULL_MOUTH,
    GUARDIAN_PAYER_SHARE,
    HOURS,
    LAB_FEES,
    MAX_SETTLE_DAYS,
    NHIS_MEMO,
    NON_CLINICAL_MAX_ITEMS,
    NON_CLINICAL_MULTI_COUNT,
    ORAL_CARE_PRICES,
    PROCEDURES,
    RECON_LATEST_MONTH_MIN,
    RECON_MISMATCH_COUNTS,
    SATURDAY,
    SETTLEMENT_ERROR_COUNTS,
    SETTLEMENT_LATE_OPEN_DAYS,
    SPLIT_MIN_AMOUNT,
    UNRECORDED_ORAL_SHARE,
    UNRECORDED_SETTLEMENT_COUNT,
    VENDORS,
    add_bank_days,
    bank_day,
    cash_deposit_day,
    open_slots,
    recon_months,
    recon_test_months,
    vendor_aliases,
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


def _transpose(n: int) -> int | None:
    """이웃 두 자리를 바꾼 더 작은 수 — 앞자리부터(끝자리 제외), 0이 맨 앞에 오거나 10배 관계가 되면 다음 자리"""
    s = str(n)
    for i in range(
        len(s) - 2
    ):  # 끝자리는 그대로 — 10원 단위가 아닌 금액은 그것만으로 오입력이 드러남
        if s[i] > s[i + 1] and not (i == 0 and s[i + 1] == "0"):
            t = int(s[:i] + s[i + 1] + s[i] + s[i + 2 :])
            if t * 10 != n:
                return t
    return None


def stage_settlement_errors(run, rng, fake) -> None:
    """정산 수납 장부 실수(데이터 재설계 설계 §1) — 8b 앞. 통화·문자가 다 만들어진 뒤라 장부를 바꿔도 번지지 않고, 말이 어긋나지
    않게 정산 행만(진료 행은 문자·통화가 읽음)·그 환자는 그날 뒤로 통화 없음·앞으로 예약 없음. 자릿수 바꿔 적기는 연동 카드,
    며칠 늦게 적기는 이체(SETTLEMENT_LATE_OPEN_DAYS번째 진료일). 실제 값은 truth_ledger_errors에 — 8b가 금융 행을 실제
    값으로 만들고 불일치 정답을 붙인다. 종류마다 평가 달 마지막 달 먼저(RECON_LATEST_MONTH_MIN), 그다음 dev·test 번갈아"""
    conn, today, holidays = run.conn, run.today, run.holidays
    calls = defaultdict(list)
    for pid, at in conn.execute(
        "SELECT patient_id, started_at FROM ax_calls WHERE patient_id IS NOT NULL"
    ):
        calls[pid].append(at)
    future = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT patient_id FROM emr_appointments WHERE date(scheduled_at) >= ?",
            (today.isoformat(),),
        )
    }
    erred = {
        r[0]
        for r in conn.execute(
            "SELECT payment_id FROM truth.truth_ledger_errors WHERE payment_id IS NOT NULL"
        )
    }
    months, test, last = recon_months(today), recon_test_months(today), recon_months(today)[-1]
    rows = [
        dict(zip(("id", "pid", "paid", "at", "method", "no"), r, strict=True))
        for r in conn.execute(
            "SELECT id, patient_id, amount_paid, paid_at, method, approval_no FROM emr_payments"
            " WHERE kind = 'settlement' AND amount_paid > 0 ORDER BY id"
        )
    ]

    def seen(d: date) -> bool:
        """그날 카드 정산·입금이 TODAY 전에 보임 — 대조 대기(pending)가 아닌 날(8b의 ok와 같은 기준)"""
        return add_bank_days(d, MAX_SETTLE_DAYS + DEPOSIT_DELAY_DAYS[1], holidays) < today

    rows = [
        r
        for r in rows
        if r["at"][:7] in months
        and r["id"] not in erred
        and r["pid"] not in future
        and not any(t > r["at"] for t in calls[r["pid"]])
        and seen(date.fromisoformat(r["at"][:10]))
    ]
    lo, hi = SETTLEMENT_LATE_OPEN_DAYS

    def late_day(r) -> date | None:
        d = date.fromisoformat(r["at"][:10])
        for _ in range(int(rng.integers(lo, hi + 1))):
            d += timedelta(days=1)
            while not open_slots(d.weekday()) or d in holidays:
                d += timedelta(days=1)
        return d if seen(d) else None

    shapes = {
        "amount_typo": [
            r for r in rows if r["method"] == "card" and r["no"] and _transpose(r["paid"])
        ],
        "date_shift": [r for r in rows if r["method"] == "transfer"],
    }
    taken = set()
    for kind, n in SETTLEMENT_ERROR_COUNTS.items():
        order = [shapes[kind][int(i)] for i in rng.permutation(len(shapes[kind]))]
        order = [r for r in order if r["pid"] not in taken]
        head = [r for r in order if r["at"][:7] == last][:RECON_LATEST_MONTH_MIN]
        pools = {
            s: [r for r in order if r not in head and (r["at"][:7] in test) == s]
            for s in (False, True)
        }
        done = Counter((r["at"][:7] in test) for r in head)
        ordered = list(head)
        while any(pools.values()):
            s = min((x for x in pools if pools[x]), key=lambda x: done[x])
            ordered.append(pools[s].pop(0))
            done[s] += 1
        made = 0
        for r in ordered:
            if made == n:
                break
            if r["pid"] in taken:
                continue
            if kind == "amount_typo":
                conn.execute(
                    "UPDATE emr_payments SET amount_paid = ? WHERE id = ?",
                    (_transpose(r["paid"]), r["id"]),
                )
            else:
                d = late_day(r)
                if d is None:
                    continue
                close = HOURS["saturday_close" if d.weekday() == SATURDAY else "weekday_close"]
                open_ = datetime.combine(d, datetime.strptime(HOURS["open"], "%H:%M").time())
                span = (
                    datetime.combine(d, datetime.strptime(close, "%H:%M").time()) - open_
                ).seconds // 60
                at = open_ + timedelta(minutes=int(rng.integers(span)))
                conn.execute(
                    "UPDATE emr_payments SET paid_at = ? WHERE id = ?", (at.strftime(FMT), r["id"])
                )
            conn.execute(
                "INSERT INTO truth.truth_ledger_errors (kind, payment_id, true_amount, true_paid_at,"
                " true_method) VALUES (?, ?, ?, ?, ?)",
                (kind, r["id"], r["paid"], r["at"], r["method"]),
            )
            taken.add(r["pid"])
            made += 1


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

    # 미수 정산 수납 누락(데이터 재설계) — 장부는 그대로, 금융에만 돈. 새 자식 난수(주 난수 흐름 밖)
    unrecorded_rng, multi_rng = rng.spawn(2)  # spawn은 앞 자식을 그대로 둔다 — 뒤에 더하기만
    unrecorded = _unrecorded_settlements(conn, unrecorded_rng, days, failure, used, today)
    for u in unrecorded:
        if u["method"] == "card":
            u["row"] = {
                "at": u["at"],
                "issuer": u["issuer"],
                "amount": u["amount"],
                "approval_no": u["approval_no"],
                "cancel_of": None,
            }
            sales.append(u["row"])
            mismatches.append(
                {
                    "on": u["at"].date(),
                    "cause": "entry_missing",
                    "fin": [u["row"]],
                    "payments": [],
                    "unrecorded": u,
                }
            )

    # 구강용품 여러 개 판매(데이터 재설계) — 규칙 9가 모르는 금액
    for s_ in _multi_item_sales(multi_rng, days, today, used):
        sales.append(s_)
        mismatches.append(
            {"on": s_["at"].date(), "cause": "non_clinical_sale", "fin": [s_], "payments": []}
        )

    # 진짜 진료 외 매출 금액이 그날 어느 환자 잔액과 같으면 겹치지 않는 구강용품 합으로 — 미수 정산 누락(금액 = 잔액)과
    # 거꾸로 헷갈리지 않게(데이터 재설계 설계 §1). 난수 없이 고른다(뒤 난수 흐름 그대로)
    _avoid_balances(conn, [m["fin"][0] for m in mismatches if m["cause"] == "non_clinical_sale"])

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
    for u in unrecorded:
        if u["method"] == "transfer":
            u["row"] = {"at": u["at"], "amount": u["amount"], "memo": names[u["patient_id"]]}
            bank.append(u["row"])
        if u["method"] != "card":
            mismatches.append(
                {
                    "on": u["at"].date(),
                    "cause": "entry_missing",
                    "fin": [u["row"]],
                    "payments": [],
                    "unrecorded": u,
                }
            )
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

    for m in mismatches:  # 장부에 행이 없는 누락의 정답 행 — 수납 id 없이 환자로
        if "unrecorded" in m:
            u = m["unrecorded"]
            m["ledger"] = conn.execute(
                "INSERT INTO truth.truth_ledger_errors (kind, payment_id, true_amount, true_paid_at,"
                " true_method, patient_id) VALUES ('entry_missing', NULL, ?, ?, ?, ?)",
                (u["amount"], u["at"].strftime(FMT), u["method"], u["patient_id"]),
            ).lastrowid
    mismatches.sort(key=lambda m: (m["on"], m["cause"], m["payments"], m.get("ledger") or 0))
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


def _latest_first(rng, pool: list[date]) -> list[date]:
    """뽑을 날 순서 — 평가 달 마지막 달(화면 기본 달)에서 RECON_LATEST_MONTH_MIN일을 먼저, 나머지는 섞어서(데이터 재설계 §4b)"""
    order = [pool[int(i)] for i in rng.permutation(len(pool))]
    last = max(d.strftime("%Y-%m") for d in pool)
    head = [d for d in order if d.strftime("%Y-%m") == last][:RECON_LATEST_MONTH_MIN]
    return head + [d for d in order if d not in head]


def _multi_item_sales(rng, days: list[date], today: date, used: set) -> list[dict]:
    """구강용품 NON_CLINICAL_MAX_ITEMS개 판매 승인 — 1~2개 합과 다른 금액만, 그날 영업시간(진료 외 매출과 같은 꼴)"""
    prices = list(ORAL_CARE_PRICES.values())
    small = {a + b for a in prices for b in [0, *prices]}
    issuers = list(CARD_ISSUERS)
    w = [CARD_ISSUERS[i]["weight"] for i in issuers]
    out = []
    for d in _latest_first(rng, days)[:NON_CLINICAL_MULTI_COUNT]:
        while (
            amount := sum(
                prices[int(rng.integers(len(prices)))] for _ in range(NON_CLINICAL_MAX_ITEMS)
            )
        ) in small:
            pass
        close = HOURS["saturday_close" if d.weekday() == SATURDAY else "weekday_close"]
        open_ = datetime.combine(d, datetime.strptime(HOURS["open"], "%H:%M").time())
        span = (
            datetime.combine(d, datetime.strptime(close, "%H:%M").time()) - open_
        ).seconds // 60
        while (no := f"{int(rng.integers(10**7, 10**8))}") in used:
            pass
        used.add(no)
        out.append(
            {
                "at": open_ + timedelta(minutes=int(rng.integers(span))),
                "issuer": issuers[int(rng.choice(len(issuers), p=[x / sum(w) for x in w]))],
                "amount": amount,
                "approval_no": no,
                "cancel_of": None,
            }
        )
    return out


def _avoid_balances(conn, sales: list[dict]) -> None:
    """승인 금액이 그날 어느 환자 장부 잔액(전날까지·그날까지)과 같으면, 같은 묶음(구강용품 1~2개 합 / NON_CLINICAL_MAX_ITEMS개
    합)에서 잔액과 안 겹치는 가장 작은 금액으로 바꾼다"""
    pays = conn.execute(
        "SELECT patient_id, date(paid_at), amount_due - amount_paid FROM emr_payments"
        " ORDER BY paid_at, id"
    ).fetchall()
    prices = list(ORAL_CARE_PRICES.values())
    small = sorted({a + b for a in prices for b in [0, *prices]})
    multi = sorted(
        {sum(c) for c in combinations_with_replacement(prices, NON_CLINICAL_MAX_ITEMS)}
        - set(small)
    )
    for s in sales:
        day = s["at"].date().isoformat()
        before, through = defaultdict(int), defaultdict(int)
        for pid, d, owe in pays:
            if d > day:
                break
            through[pid] += owe
            if d < day:
                before[pid] += owe
        taken = set(before.values()) | set(through.values())
        if s["amount"] in taken:
            s["amount"] = next(
                a for a in (small if s["amount"] in small else multi) if a not in taken
            )


def _unrecorded_settlements(
    conn, rng, days: list[date], failure: set, used: set, today: date
) -> list[dict]:
    """미수 정산 수납 누락 후보(데이터 재설계 설계 §1) — 평가 달 진료일 중 장부 잔액이 남고 그날 장부 행이 없는 환자가 그날
    영업시간에 잔액 전부를 낸 돈(일부 납부 없음 — 잔액을 보면 풀리게). 장부 실수가 있는 환자(장부 잔액 ≠ 실제 잔액)와
    그날 뒤로 정산 행이 있는 환자(같은 잔액을 또 냄)는 뺀다. UNRECORDED_ORAL_SHARE만큼은 잔액이 구강용품 1~2개 합과
    같은 환자의 카드(규칙 9가 진료 외 매출로 틀림), 나머지는 이체·카드(현금은 한 입금에 기존 현금 장부 실수와 겹쳐 원인이 둘이
    될 수 있어 쓰지 않는다). 환자는 서로 다름"""
    ledger = defaultdict(list)
    for pid, at, owe in conn.execute(
        "SELECT patient_id, paid_at, amount_due - amount_paid FROM emr_payments ORDER BY paid_at, id"
    ):
        ledger[pid].append((at, owe))
    erred = {
        r[0]
        for r in conn.execute(
            "SELECT p.patient_id FROM truth.truth_ledger_errors e JOIN emr_payments p ON p.id = e.payment_id"
        )
    }
    settled = defaultdict(list)
    for pid, at in conn.execute(
        "SELECT patient_id, paid_at FROM emr_payments WHERE kind = 'settlement'"
    ):
        settled[pid].append(at[:10])

    def balance(pid: int, d: date) -> int:
        return sum(o for at, o in ledger[pid] if at[:10] <= d.isoformat())

    prices = list(ORAL_CARE_PRICES.values())
    sums = sorted({a + b for a in prices for b in [0, *prices]})
    n_oral = round(UNRECORDED_SETTLEMENT_COUNT * UNRECORDED_ORAL_SHARE)
    out, taken, n_done = [], set(), Counter()  # n_done: 분할별 구강용품 합 꼴 수
    test = recon_test_months(today)
    for d in _latest_first(rng, [d for d in days if d not in failure]):
        if len(out) == UNRECORDED_SETTLEMENT_COUNT:
            break
        owing = [
            p
            for p in sorted(ledger)
            if p not in taken
            and p not in erred
            and not any(s > d.isoformat() for s in settled[p])
            and balance(p, d) > 0
            and all(at[:10] != d.isoformat() for at, _ in ledger[p])
        ]
        oral = [p for p in owing if balance(p, d) in sums]
        # 그날 구강용품 합 꼴이 되면 그것부터(n_oral까지), 안 되면 일반 꼴 — 날을 헛되이 쓰지 않게(작은 데이터는 일반 꼴이 늘어남)
        # 후보 환자가 적어(장부 잔액이 그대로 남아 같은 환자가 매일 잡힘) dev·test에 번갈아 — 한쪽에 몰리지 않게
        split = d.strftime("%Y-%m") in test
        if sum(n_done.values()) < n_oral and oral and n_done[split] <= n_done[not split]:
            owing, method = oral, "card"
        elif owing:
            method = None
        else:
            continue
        pid = owing[int(rng.integers(len(owing)))]
        amount = balance(pid, d)
        if method == "card":
            n_done[split] += 1
        else:
            method = ("transfer", "card")[int(rng.integers(2))]
        close = HOURS["saturday_close" if d.weekday() == SATURDAY else "weekday_close"]
        open_ = datetime.combine(d, datetime.strptime(HOURS["open"], "%H:%M").time())
        span = (
            datetime.combine(d, datetime.strptime(close, "%H:%M").time()) - open_
        ).seconds // 60
        u = {
            "patient_id": pid,
            "at": open_ + timedelta(minutes=int(rng.integers(span))),
            "amount": amount,
            "method": method,
        }
        if method == "card":
            issuers = list(CARD_ISSUERS)
            w = [CARD_ISSUERS[i]["weight"] for i in issuers]
            u["issuer"] = issuers[int(rng.choice(len(issuers), p=[x / sum(w) for x in w]))]
            while (no := f"{int(rng.integers(10**7, 10**8))}") in used:
                pass
            used.add(no)
            u["approval_no"] = no
        taken.add(pid)
        out.append(u)
    return out


# ── 8c단계 재무 지출 (§2.7 ⑧-2) ─────────────────────────────

LAB_CODES = {p["code"]: p["name"] for p in PROCEDURES if p["name"] in LAB_FEES}
# 증빙 본문 양식 — 종류마다 2~3개. items는 "- 품목 금액원" 줄(기공은 "- 환자 NN번 보철 금액원")
DOC_TEMPLATES = {
    "statement": (
        "거래명세서\n공급자: {issuer}\n일자: {date}\n{items}\n합계 {total}원",
        "납품 명세서 ({issuer})\n납품일 {date}\n{items}\n금액 합계 {total}원",
        "[{issuer}] 거래명세표\n{date}\n{items}\n총액 {total}원",
    ),
    "tax_invoice": (
        "전자세금계산서\n공급자 {issuer}\n작성일자 {date}\n품목 {summary}\n합계금액 {total}원",
        "세금계산서 발행 안내 — {issuer}\n작성일 {date} / {summary}\n공급대가 {total}원",
    ),
    "order_email": (
        "[{issuer}] 주문이 완료되었습니다\n주문일 {date}\n{items}\n결제금액 {total}원",
        "{issuer} 주문 확인\n{date} 주문\n{items}\n총 결제 {total}원 (카드)",
    ),
    "receipt": (
        "{issuer}\n{date}\n{items}\n합계 {total}원\n카드 승인",
        "영수증 — {issuer}\n일시 {date}\n{items}\n받은 금액 {total}원",
    ),
    "bill": (
        "{issuer} {month}월 요금 안내\n{summary} {total}원\n납부일 {date} 자동이체 예정",
        "[{issuer}] {month}월 청구서\n청구 금액 {total}원 ({summary})\n자동이체일 {date}",
    ),
}


def _won(n: int) -> str:
    return f"{n:,}"


def _split(rng, total: int, k: int) -> list[int]:
    """total을 k개 금액으로(10원 단위, 합 = total)"""
    cuts = sorted(int(rng.integers(1, 9)) for _ in range(k - 1))
    weights = [b - a for a, b in zip([0, *cuts], [*cuts, 10], strict=True)]
    parts = [total * w // sum(weights) // 10 * 10 for w in weights]
    parts[-1] += total - sum(parts)
    return parts


def stage_expenses(run, rng, fake) -> None:
    """⑧-2 지출 — 거래처(VENDORS)별 출금(통장 이체·자동이체·병원 카드)과 증빙(fin_expense_documents, 코드 템플릿),
    정답 truth_expense_matches(평가 달만 split). 생성 창의 달마다(TODAY가 속한 달 전까지):
    - 월말 이체: 납품마다 명세서(재료상은 품목, 기공소는 그달 창 안 기공 진료 1건 = 줄 1개, LAB_FEES) + 재료상은 월 세금계산서,
      그달 마지막 은행 영업일에 명세서 합계 이체
    - 병원 카드: 사용마다 1건(가맹점명 = card_name 표기), 증빙 1장 — 쇼핑몰 주문은 한 계정 과목의 품목만(품목으로 과목이 정해짐)
    - 자동이체: 거래처마다 정한 날(은행 영업일로 미룸), 고지서·세금계산서는 EXPENSE_DOC_LEAD_DAYS일 전
    어긋남: 카드 사용 중 EXPENSE_NO_DOC_RATE는 증빙 없음, 증빙 있는 출금 중 EXPENSE_AMOUNT_DIFF_RATE는 출금액이 조금 많음
    (증빙 한 장에 이유 줄 EXPENSE_DIFF_REASONS)"""
    conn, today, holidays = run.conn, run.today, run.holidays
    names = dict(conn.execute("SELECT id, name FROM emr_patients"))
    lab_treatments = defaultdict(list)  # 'YYYY-MM' → [(진료일, 환자 이름, 치아, 보철)]
    for pid, tooth, code, at in conn.execute(
        "SELECT patient_id, tooth_no, procedure_code, performed_at FROM emr_treatments"
        f" WHERE procedure_code IN ({', '.join('?' * len(LAB_CODES))}) AND date(performed_at) >= ?"
        " ORDER BY performed_at, id",
        (*LAB_CODES, run.range_start.isoformat()),
    ):
        day = datetime.strptime(at, FMT).date()
        lab_treatments[day.strftime("%Y-%m")].append(
            (day, names[pid], None if tooth == FULL_MOUTH else tooth, LAB_CODES[code])
        )
    debit_day = {v["name"]: int(rng.integers(5, 28)) for v in VENDORS if v["pay"] == "auto_debit"}

    def amount(lo_hi) -> int:
        lo, hi = lo_hi
        return lo if lo == hi else int(rng.integers(lo, hi + 1)) // 100 * 100

    def doc(kind, issuer, day: date, total: int, items=None, summary="", month=None) -> dict:
        lines = "\n".join(f"- {name} {_won(a)}원" for name, a in items or [])
        body = DOC_TEMPLATES[kind][int(rng.integers(len(DOC_TEMPLATES[kind])))].format(
            issuer=issuer,
            date=day.isoformat(),
            items=lines,
            total=_won(total),
            summary=summary,
            month=month or day.month,
        )
        return {"kind": kind, "issuer": issuer, "on": day, "amount": total, "body": body}

    outflows = []  # dict: table, at, amount, label, vendor, docs, account
    y, m = run.range_start.year, run.range_start.month
    while date(y, m, 1) < date(today.year, today.month, 1):
        first = date(y, m, 1)
        nxt = date(y + (m == 12), m % 12 + 1, 1)
        days = [first + timedelta(days=i) for i in range((nxt - first).days)]
        bank_days = [d for d in days if bank_day(d, holidays) and d >= run.range_start]
        open_days = [
            d
            for d in days
            if open_slots(d.weekday()) and d not in holidays and d >= run.range_start
        ]
        month_end = bank_days[-1]
        for v in VENDORS:
            lo, hi = v["per_month"]
            n = int(rng.integers(lo, hi + 1))
            if v["pay"] == "month_end_transfer":
                docs = []
                if v["kind"] == "lab":
                    rows = lab_treatments[f"{y}-{m:02d}"]
                    k = min(n, len(rows))
                    cuts = (
                        sorted(
                            int(i)
                            for i in rng.choice(range(1, len(rows)), size=k - 1, replace=False)
                        )
                        if k > 1
                        else []
                    )
                    for group in (
                        rows[a:b] for a, b in zip([0, *cuts], [*cuts, len(rows)], strict=True)
                    ):
                        if not group:
                            continue
                        items = [
                            (
                                f"{name} {tooth}번 {kind}" if tooth else f"{name} {kind}",
                                LAB_FEES[kind],
                            )
                            for _, name, tooth, kind in group
                        ]
                        docs.append(
                            doc(
                                "statement",
                                v["name"],
                                min(
                                    group[-1][0], month_end
                                ),  # 이체일 뒤 토요일 진료도 그달 명세서
                                sum(a for _, a in items),
                                items,
                            )
                        )
                else:
                    pool = EXPENSE_ITEMS[v["kind"]]
                    for d in sorted(
                        bank_days[int(i)] for i in rng.integers(len(bank_days), size=n)
                    ):
                        total = amount(v["amount"])
                        picks = [
                            pool[int(i)][0]
                            for i in rng.choice(
                                len(pool), size=int(rng.integers(1, 4)), replace=False
                            )
                        ]
                        docs.append(
                            doc(
                                "statement",
                                v["name"],
                                d,
                                total,
                                list(zip(picks, _split(rng, total, len(picks)), strict=True)),
                            )
                        )
                if not docs:
                    continue
                total = sum(d["amount"] for d in docs)
                if "tax_invoice" in v["docs"]:
                    docs.append(
                        doc("tax_invoice", v["name"], month_end, total, summary=f"{m}월 납품분")
                    )
                outflows.append(
                    {
                        "table": "bank",
                        "at": datetime.combine(month_end, _hhmm(BANK_TIMES["transfer_out"])),
                        "amount": total,
                        "label": v["name"],
                        "vendor": v,
                        "docs": docs,
                        "account": v["account"],
                    }
                )
            elif v["pay"] == "card":
                pool = EXPENSE_ITEMS[v["kind"]]
                for d in sorted(open_days[int(i)] for i in rng.integers(len(open_days), size=n)):
                    at = datetime.combine(d, _hhmm(HOURS["open"])) + timedelta(
                        minutes=int(rng.integers(240))
                    )
                    total = amount(v["amount"])
                    account = pool[int(rng.integers(len(pool)))][
                        1
                    ]  # 한 주문은 한 계정 과목의 품목만
                    same = [name for name, acct in pool if acct == account]
                    picks = [
                        same[int(i)]
                        for i in rng.choice(
                            len(same), size=min(len(same), int(rng.integers(1, 3))), replace=False
                        )
                    ]
                    docs = [
                        doc(
                            v["docs"][0],
                            v["name"],
                            d,
                            total,
                            list(zip(picks, _split(rng, total, len(picks)), strict=True)),
                        )
                    ]
                    outflows.append(
                        {
                            "table": "card",
                            "at": at,
                            "amount": total,
                            "label": v["card_name"] or v["name"],
                            "vendor": v,
                            "docs": docs,
                            "account": account,
                        }
                    )
            else:  # auto_debit
                d = min(date(y, m, debit_day[v["name"]]), month_end)
                while not bank_day(d, holidays):
                    d += timedelta(days=1)
                total = amount(v["amount"])
                item = EXPENSE_ITEMS[v["kind"]][0][0] if v["kind"] != "utility" else v["name"]
                lo, hi = EXPENSE_DOC_LEAD_DAYS
                issued = d - timedelta(days=int(rng.integers(lo, hi + 1)))
                docs = [doc(v["docs"][0], v["name"], issued, total, summary=item, month=m)]
                outflows.append(
                    {
                        "table": "bank",
                        "at": datetime.combine(d, _hhmm(BANK_TIMES["auto_debit"])),
                        "amount": total,
                        "label": v["name"],
                        "vendor": v,
                        "docs": docs,
                        "account": v["account"],
                    }
                )
        y, m = nxt.year, nxt.month

    cards = [o for o in outflows if o["table"] == "card"]
    for k in rng.choice(
        len(cards), size=round(EXPENSE_NO_DOC_RATE * len(cards)), replace=False
    ).tolist():
        cards[k]["docs"] = []
        cards[k]["account"] = cards[k]["vendor"]["account"]  # 증빙이 없으면 거래처 과목
    with_docs = [o for o in outflows if o["docs"]]
    lo, hi = EXPENSE_AMOUNT_DIFF_RANGE
    for k in rng.choice(
        len(with_docs), size=round(EXPENSE_AMOUNT_DIFF_RATE * len(with_docs)), replace=False
    ).tolist():
        o = with_docs[k]
        diff = max(100, int(o["amount"] * rng.uniform(lo, hi)) // 100 * 100)
        o["amount"] += diff
        o["diff"] = diff

    decoys = _expense_confusers(rng.spawn(1)[0], outflows, doc, today)
    for o in outflows:  # 금액 차이 이유 줄 — 혼합 주문이 증빙을 다시 만든 뒤, 마지막 명세서·영수증(세금계산서만 있으면 그것)
        if o.get("diff"):
            d = ([x for x in o["docs"] if x["kind"] != "tax_invoice"] or o["docs"])[-1]
            d["body"] += f"\n{EXPENSE_DIFF_REASONS[d['kind']]} {_won(o['diff'])}원 별도 청구"
    conn.execute(  # 디코이 수 — verify가 어느 출금에도 안 붙은 증빙 수와 맞춘다(단계 안에서 정해져 meta를 여기서)
        "INSERT INTO truth.truth_gen_meta VALUES ('expense_decoys', ?)", (str(len(decoys)),)
    )

    # 적재 — 통장 출금은 8b 입금 뒤 id, 카드 사용·증빙은 시각순
    start = conn.execute("SELECT coalesce(max(id), 0) FROM fin_bank_transactions").fetchone()[0]
    bank = sorted(
        (o for o in outflows if o["table"] == "bank"), key=lambda o: (o["at"], o["label"])
    )
    for i, o in enumerate(bank, start + 1):
        o["id"] = i
    for i, o in enumerate(sorted(cards, key=lambda o: (o["at"], o["label"])), 1):
        o["id"] = i
    docs = sorted(  # 같은 날·같은 키면 디코이가 앞 — 가장 가까운 증빙을 고르는 규칙이 디코이를 먼저 만난다
        [d for o in outflows for d in o["docs"]] + decoys,
        key=lambda d: (d["on"], d["issuer"], d["kind"], d["amount"], not d.get("decoy")),
    )
    for i, d in enumerate(docs, 1):
        d["id"] = i
    conn.executemany(
        "INSERT INTO fin_bank_transactions (id, occurred_at, direction, amount, memo) VALUES (?, ?, 'out', ?, ?)",
        [(o["id"], o["at"].strftime(FMT), o["amount"], o["label"]) for o in bank],
    )
    conn.executemany(
        "INSERT INTO fin_card_expenses (id, used_at, merchant, amount) VALUES (?, ?, ?, ?)",
        [
            (o["id"], o["at"].strftime(FMT), o["label"], o["amount"])
            for o in sorted(cards, key=lambda o: o["id"])
        ],
    )
    conn.executemany(
        "INSERT INTO fin_expense_documents (id, kind, issuer, issued_on, amount, body) VALUES (?, ?, ?, ?, ?, ?)",
        [
            (d["id"], d["kind"], d["issuer"], d["on"].isoformat(), d["amount"], d["body"])
            for d in docs
        ],
    )
    months, test = recon_months(today), recon_test_months(today)
    conn.executemany(
        "INSERT INTO truth.truth_expense_matches (outflow_table, outflow_id, document_ids, account, split)"
        " VALUES (?, ?, ?, ?, ?)",
        [
            (
                "fin_bank_transactions" if o["table"] == "bank" else "fin_card_expenses",
                o["id"],
                json.dumps(sorted(d["id"] for d in o["docs"])),
                o["account"],
                None
                if o["at"].strftime("%Y-%m") not in months
                else "test"
                if o["at"].strftime("%Y-%m") in test
                else "dev",
            )
            for o in outflows
        ],
    )


def _expense_confusers(rng, outflows: list[dict], doc, today: date) -> list[dict]:
    """⑧-2 헷갈리는 모양(데이터 재설계 설계 §2) — 새 자식 난수로 outflows를 고치고 디코이 증빙(어느 출금에도 안 붙음)을 낸다.
    모양마다 평가 달 마지막 달(화면 기본 달)부터 고른다(RECON_LATEST_MONTH_MIN)"""
    last = recon_months(today)[-1]

    def pick(items: list, rate: float) -> list:
        order = [items[int(i)] for i in rng.permutation(len(items))]
        head = [o for o in order if o["at"].strftime("%Y-%m") == last][:RECON_LATEST_MONTH_MIN]
        return (head + [o for o in order if o not in head])[: round(rate * len(items))]

    # 쇼핑몰 혼합 주문 — 두 과목 품목, 정답은 금액이 큰 과목
    shop = [
        o
        for o in outflows
        if o["table"] == "card" and o["docs"] and o["vendor"]["kind"] == "shopping"
    ]
    for o in pick(shop, EXPENSE_MIXED_ORDER_RATE):
        pool = EXPENSE_ITEMS["shopping"]
        accts = sorted({a for _, a in pool})
        if len(accts) < 2:
            break
        picks = [
            [n for n, a in pool if a == acct][int(rng.integers(sum(a == acct for _, a in pool)))]
            for acct in accts[:2]
        ]
        total = o["docs"][0]["amount"]
        big = max(100, int(total * rng.uniform(0.55, 0.8)) // 100 * 100)
        amounts = [big, total - big] if rng.random() < 0.5 else [total - big, big]
        o["docs"] = [
            doc(
                o["vendor"]["docs"][0],
                o["vendor"]["name"],
                o["docs"][0]["on"],
                total,
                list(zip(picks, amounts, strict=True)),
            )
        ]
        o["account"] = accts[:2][amounts.index(max(amounts))]
        o["mixed"] = True
    # 주문 두 건을 한 번에 결제 — 증빙 둘 = 출금 하나
    cards = [
        o
        for o in outflows
        if o["table"] == "card"
        and not o.get("mixed")  # 혼합 주문을 쪼개면 한 증빙에 한 과목만 남아 정답 과목과 어긋난다
        and len(o["docs"]) == 1
        and o["docs"][0]["amount"] == o["amount"]
    ]
    for o in pick(cards, EXPENSE_SPLIT_DOC_RATE):
        d0 = o["docs"][0]
        a = max(100, int(d0["amount"] * rng.uniform(0.3, 0.7)) // 100 * 100)
        items = [line for line in d0["body"].splitlines() if line.startswith("- ")]
        name = items[0][2:].rsplit(" ", 1)[0] if items else "주문"
        o["docs"] = [
            doc(d0["kind"], d0["issuer"], d0["on"], x, [(name, x)]) for x in (a, d0["amount"] - a)
        ]
    # 디코이 — 같은 거래처·같은 금액의 취소 주문·발행 취소 고지서·견적, 진짜 증빙보다 출금에 가깝거나 같은 날
    one = [
        o
        for o in outflows
        if o["docs"] and o["table"] == "card" or (o["docs"] and o["vendor"]["pay"] == "auto_debit")
    ]
    decoys = []
    for o in pick(one, EXPENSE_DECOY_RATE):
        d0 = o["docs"][0]
        on = o["at"].date() if o["table"] == "card" else o["at"].date() - timedelta(days=1)
        word = (  # 카드 = 취소 주문, 공과금 고지서 = 발행 취소, 세금계산서 = 견적
            "취소"
            if o["table"] == "card"
            else "발행 취소"
            if d0["kind"] == "bill"
            else "견적(확정 전)"
        )
        d = doc(d0["kind"], d0["issuer"], on, o["amount"], [(word, o["amount"])], summary=word)
        d["body"] = f"[{word}] " + d["body"]
        d["decoy"] = True
        decoys.append(d)
    # 표기 변형 — 적요·가맹점명(은행·결제대행사), 월말 명세서 발행처(법인)
    for o in pick([o for o in outflows], EXPENSE_ALIAS_RATE):
        o["label"] = vendor_aliases(o["vendor"])["card" if o["table"] == "card" else "memo"]
    month_end = [o for o in outflows if o["vendor"]["pay"] == "month_end_transfer" and o["docs"]]
    for o in pick(month_end, EXPENSE_ISSUER_ALIAS_RATE):
        alias = vendor_aliases(o["vendor"])["issuer"]
        for d in o["docs"]:
            d["body"] = d["body"].replace(d["issuer"], alias)
            d["issuer"] = alias
    return decoys


def _hhmm(s: str):
    return datetime.strptime(s, "%H:%M").time()
