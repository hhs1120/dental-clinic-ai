"""⑧-1 재무 대조 1단계 — 일계표·불일치 건의 규칙 원인·사람 확정 (기능 정의 §5 ⑧, 설계
docs/superpowers/specs/2026-11-11-finance-recon-design.md §1). 건은 backend/recon_match.py가 찾는다.
규칙 원인(rule_cause)은 ⑧-1 규칙 기준선이다(평가 설계 §5 ⑧-1) — 건의 모양(금액·날짜·승인번호)만 보고 정한 순서로
처음 맞는 원인 하나, unknown은 내지 않는다. 이 순서는 test 실행 전에 바꾸지 않는다.
사람 확정은 ax_recon_confirmations(건 키 단위, 덮어씀). master 값은 source.ax_master_values로만 읽는다.
오늘 화면(backend/today.py)도 open_count로 센다."""

import json
import sqlite3
from collections import defaultdict
from datetime import date
from itertools import combinations_with_replacement

from backend import recon_match


def _oral_sums(prices: dict) -> set[int]:
    """구강용품 1~2개(같은 품목 두 번 포함) 합 — 진료 외 매출 규칙(가상 데이터 생성 설계 §2.7)"""
    vals = list(prices.values())
    return set(vals) | {a + b for a, b in combinations_with_replacement(vals, 2)}


def rule_cause(item: dict, books: dict) -> str:
    """규칙 1~11(설계 §1) — 위에서부터 처음 맞는 원인"""
    parts = item["parts"]
    led = [p for p in parts if p["side"] == "ledger"]
    fin = [p for p in parts if p["side"] == "fin" and "cancel_pair" not in p]
    sales = [p for p in fin if p["table"] == "fin_card_sales"]
    pairs = [p for p in parts if "cancel_pair" in p]
    cash = [p for p in parts if p["side"] == "cash"]
    deps = [p for p in fin if p["table"] == "fin_card_deposits"]
    settle = [p for p in fin if p["channel"] == "settlement"]
    # 1 분할결제 — 장부 카드 1건 ↔ 같은 시각 승인 둘 이상(하나가 장부 번호), 합 = 장부
    for x in led:
        same = [s for s in sales if s["at"] == x["at"]]
        if (
            x["channel"] == "card"
            and len(same) >= 2
            and sum(s["amount"] for s in same) == x["amount"]
            and any(s["approval_no"] == x["approval_no"] for s in same)
        ):
            return "split_payment"
    # 2 취소 후 재승인 — 장부 번호의 승인이 취소되고 같은 금액 새 승인
    for x in led:
        if any(p["approval_no"] == x["approval_no"] for p in pairs) and any(
            s["amount"] == x["amount"] for s in sales
        ):
            return "cancel_reapprove"
    # 3 전 — 카드사 돈이 일부·전부 안 들어옴(정산 행 없음, 통장 입금 없음, 정산 매출 합계 ≠ 승인 합계)은 입금 지연
    if any(
        p.get("deposit_missing")
        or p.get("bank_missing")
        or p.get("sales_total", 0) != p.get("computed_total", 0)
        for p in settle
    ):
        return "deposit_delay"
    # 3·4 정산
    if any(d["fee"] != d["computed_fee"] for d in deps):
        return "fee_mismatch"
    if any(d["deposited_on"] > d["expected_on"] for d in deps):
        return "deposit_delay"
    # 5 금액 오입력 — 같은 결제수단에서 10배, 현금은 차이(금융 − 장부)가 장부 행 하나의 9배(적게 적음)·0.9배(많이 적음)
    for x in led:
        for f in fin:
            if f["channel"] == x["channel"] and (
                x["amount"] * 10 == f["amount"] or f["amount"] * 10 == x["amount"]
            ):
                return "amount_typo"
    for c in cash:
        for r in c["ledger_rows"]:
            if c["amount"] == 9 * r["amount_paid"] or c["amount"] * 10 == -9 * r["amount_paid"]:
                return "amount_typo"
    # 6 날짜 밀림 — 다른 날 같은 금액, 또는 연속한 두 현금 입금 차이가 +X / −X
    for x in led:
        if any(f["amount"] == x["amount"] and f["date"] != x["date"] for f in fin):
            return "date_shift"
    if any(a["amount"] == -c["amount"] for a in cash for c in cash if a is not c):
        return "date_shift"
    # 7 결제수단 오입력 — 같은 금액이 장부 결제수단 X·금융 결제수단 Y에(현금은 입금 기간 안)
    for x in led:
        if any(f["amount"] == x["amount"] and f["channel"] != x["channel"] for f in fin):
            return "method_wrong"
    for c in cash:
        if any(y["amount"] == abs(c["amount"]) for y in led + fin):
            return "method_wrong"
    net = item["fin_total"] - item["ledger_total"]
    if net > 0:
        days = set(item["dates"]) | {d.isoformat() for c in cash for d in c["period"]}
        # 8 입력 누락 — 짝 없는 금융 금액 = 그날 받은 금액 0원 진료 행의 낼 돈
        if any(z["amount_due"] == net and z["paid_at"][:10] in days for z in books["zero_visits"]):
            return "entry_missing"
        # 9 진료 외 매출 — 짝 없는 카드 승인이 구강용품 1~2개 합
        if item["channel"] == "card" and net in _oral_sums(books["mv"]["oral_care_prices"]):
            return "non_clinical_sale"
        return "entry_missing"  # 10 금융에만 있는 돈
    return "amount_typo"  # 11 장부에만 있는 돈


def _confirmations(conn: sqlite3.Connection) -> dict[str, dict]:
    return {
        r["item_key"]: {
            "cause": r["cause"],
            "proposed_cause": r["proposed_cause"],
            "by": r["judged_by"],
            "at": r["judged_at"],
        }
        for r in conn.execute(
            "SELECT item_key, cause, proposed_cause, judged_by, judged_at FROM ax_recon_confirmations"
        )
    }


def _rows(books: dict) -> tuple[dict, dict]:
    """(장부 행 id → 화면 행, (테이블, id) → 화면 행)"""
    pays = {
        p["id"]: {
            "id": p["id"],
            "paid_at": p["paid_at"],
            "name": p["name"],
            "amount_due": p["amount_due"],
            "amount_paid": p["amount_paid"],
            "method": p["method"],
            "approval_no": p["approval_no"],
        }
        for p in books["payments"] + books["zero_visits"]
    }
    fin = {}
    for s in books["sales"]:
        label = s["issuer"] + (" 취소" if s["cancel_of"] else "")
        fin[("fin_card_sales", s["id"])] = {
            "table": "fin_card_sales",
            "at": s["approved_at"],
            "amount": s["amount"],
            "label": label,
        }
    for d in books["deposits"]:
        fin[("fin_card_deposits", d["id"])] = {
            "table": "fin_card_deposits",
            "at": d["deposited_on"],
            "amount": d["amount"],
            "label": f"{d['issuer']} 정산",
        }
    for b in books["bank_in"]:
        fin[("fin_bank_transactions", b["id"])] = {
            "table": "fin_bank_transactions",
            "at": b["occurred_at"],
            "amount": b["amount"],
            "label": b["memo"],
        }
    return pays, fin


def _view(item: dict, books: dict, pays: dict, fin: dict, conf: dict) -> dict:
    out = {k: v for k, v in item.items() if k != "parts"}
    # 정산 행에는 받은 값과 기대값(코드가 계산한 수수료·예상 입금일)을 함께 — 직원이 "얼마여야 했나"를 본다
    detail = {
        p["id"]: {
            "sales_date": p["date"].isoformat(),
            "sales_total": p["sales_total"],
            "fee": p["fee"],
            "expected_fee": p["computed_fee"],
            "expected_on": p["expected_on"],
        }
        for p in item["parts"]
        if p["table"] == "fin_card_deposits"
    }
    rows = []
    for r in item["fin"]:
        row = fin[(r["table"], r["id"])]
        if r["table"] == "fin_card_deposits":
            row = row | {"detail": detail[r["id"]]}
        rows.append(row)
    return out | {
        "rule_cause": rule_cause(item, books),
        "ledger_rows": [pays[i] for i in item["payment_ids"]],
        "fin_rows": rows,
        "confirmation": conf.get(item["key"]),
    }


def ledger(conn: sqlite3.Connection, today: date, month: str | None = None) -> dict:
    """일계표 화면 — 그 달 날짜당 한 줄(카드·현금·이체 장부 vs 금융)과 그 날의 불일치 건(설계 §1)"""
    books = recon_match.load(conn, today)
    found = recon_match.detect(books)
    months = sorted({d.strftime("%Y-%m") for d in books["open_days"]})
    if month is None:
        month = months[-1] if months else None
    elif month not in months:
        raise ValueError(f"장부가 없는 달: {month}")
    conf = _confirmations(conn)
    pays, fin = _rows(books)
    mv = books["mv"]
    skip = set(mv["card_issuers"]) | {mv["cash_deposit"]["memo"], mv["nhis_memo"]}
    led: dict[tuple[str, str], int] = defaultdict(int)
    for p in books["payments"]:
        led[(p["paid_at"][:10], p["method"])] += p["amount_paid"]
    card: dict[str, int] = defaultdict(int)
    for s in books["sales"]:
        card[s["approved_at"][:10]] += -s["amount"] if s["cancel_of"] else s["amount"]
    transfer: dict[str, int] = defaultdict(int)
    cash_in: dict[str, int] = defaultdict(int)
    for b in books["bank_in"]:
        if b["memo"] == mv["cash_deposit"]["memo"]:
            cash_in[b["occurred_at"][:10]] += b["amount"]
        elif b["memo"] not in skip:
            transfer[b["occurred_at"][:10]] += b["amount"]
    period: dict[date, list[date]] = defaultdict(list)
    for d in books["open_days"]:
        period[
            recon_match.cash_deposit_day(d, mv["cash_deposit"]["weekdays"], books["holidays"])
        ].append(d)
    last = {max(days): (dep, days) for dep, days in period.items()}
    status = {d: recon_match.day_status(books, d) for d in books["open_days"]}
    # 그날 현금이 든 입금 기간에 대조 전 날이 섞이면 그 현금은 대조 전(recon_match._cash와 같은 경계)
    cash_pending = {
        d for days in period.values() if any(status[x] == "pending" for x in days) for d in days
    }
    by_day: dict[str, list[dict]] = defaultdict(list)
    for i in found:
        by_day[i["date"]].append(i)
    rows = []
    days = sorted(
        {d for d in books["open_days"] if d.strftime("%Y-%m") == month}
        | {date.fromisoformat(d) for d in by_day if d.startswith(month or "")}
    )
    for d in days:
        k = d.isoformat()
        cash = {
            "ledger": led[(k, "cash")],
            "fin": None,
            "diff": None,
            "deposit": None,
            "pending": d in cash_pending,
        }
        if d in last and d not in cash_pending:
            dep, span = last[d]
            got = cash_in.get(dep.isoformat())
            if got:
                total = sum(led[(x.isoformat(), "cash")] for x in span)
                cash |= {
                    "fin": got,
                    "diff": total - got,
                    "deposit": {"on": dep.isoformat(), "days": len(span)},
                }
        rows.append(
            {
                "date": k,
                "status": status.get(d) or recon_match.day_status(books, d),
                "card": {
                    "ledger": led[(k, "card")],
                    "fin": card[k],
                    "diff": led[(k, "card")] - card[k],
                },
                "cash": cash,
                "transfer": {
                    "ledger": led[(k, "transfer")],
                    "fin": transfer[k],
                    "diff": led[(k, "transfer")] - transfer[k],
                },
                "items": [_view(i, books, pays, fin, conf) for i in by_day[k]],
            }
        )
    in_month = [i for i in found if i["date"].startswith(month or "")]
    return {
        "months": months,
        "month": month,
        "causes": mv["recon_causes"],
        "chips": {
            "days": len([d for d in books["open_days"] if d.strftime("%Y-%m") == month]),
            "mismatch_days": len({i["date"] for i in in_month}),
            "open_items": sum(i["key"] not in conf for i in in_month),
            "open_all": sum(i["key"] not in conf for i in found),
        },
        "days": rows,
    }


def confirm(conn: sqlite3.Connection, key: str, cause: str, by: str, at: str, today: date) -> None:
    """사람 확정 — 지금 탐지에 있는 건 키·원인 9개 중 하나, 아니면 ValueError. 덮어씀, 커밋은 부르는 쪽"""
    books = recon_match.load(conn, today)
    item = next((i for i in recon_match.detect(books) if i["key"] == key), None)
    if item is None:
        raise ValueError(f"불일치 건이 아님: {key}")
    if cause not in books["mv"]["recon_causes"]:
        raise ValueError(f"모르는 원인: {cause}")
    conn.execute(
        "INSERT INTO ax_recon_confirmations (item_key, occurred_on, fin_ids, payment_ids,"
        " proposed_by, proposed_cause, cause, judged_by, judged_at)"
        " VALUES (?, ?, ?, ?, 'rule', ?, ?, ?, ?) ON CONFLICT(item_key) DO UPDATE SET"
        " cause = excluded.cause, proposed_by = excluded.proposed_by,"
        " proposed_cause = excluded.proposed_cause, judged_by = excluded.judged_by,"
        " judged_at = excluded.judged_at",
        (
            key,
            item["date"],
            json.dumps(item["fin"]),
            json.dumps(item["payment_ids"]),
            rule_cause(item, books),
            cause,
            by,
            at,
        ),
    )


def open_summary(conn: sqlite3.Connection, today: date) -> dict:
    """확정 안 된 불일치 건 수(전 기간)와 그런 건이 있는 가장 이른 달 — 오늘 화면 '일계표 불일치' 줄과 링크.
    수는 /finance 칩 open_all과 같다"""
    conf = _confirmations(conn)
    open_ = [i for i in recon_match.detect(recon_match.load(conn, today)) if i["key"] not in conf]
    return {"count": len(open_), "month": min((i["date"][:7] for i in open_), default=None)}


def open_count(conn: sqlite3.Connection, today: date) -> int:
    return open_summary(conn, today)["count"]
