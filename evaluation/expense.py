"""⑧-2 지출 증빙 매칭·계정 과목 규칙 기준선 (평가 설계 §1 원칙 8·§5 ⑧-2, 재무 대조 설계 §2). 데이터 재설계보다 먼저 고정해
재설계 뒤 dev 목표(매칭 75~85%·과목 80~85% — 데이터 재설계 설계)를 이 규칙으로 잰다. ⑧ 2단계가 같은 규칙을 앱으로 옮긴다.
- 매칭: 월말 이체(거래처 pay month_end_transfer)는 적요 = 거래처, 그달 그 거래처 명세서 합계 = 출금액이면 명세서 전부 + 그달
  세금계산서. 그 밖은 금액이 같고 출금 DOC_WINDOW 안의 증빙 중 날짜가 가장 가까운 하나. 없으면 [](증빙 없음)
- 과목: 적요·가맹점명(card_name)으로 찾은 거래처의 기본 과목"""

import json
from datetime import date

from generator.master import VENDORS as MASTER_VENDORS

DOC_WINDOW = (-3, 14)  # 증빙일이 출금 3일 뒤 ~ 14일 전 (출금일 − 증빙일, 일)


def vendors_by_label(vendors: list[dict] = MASTER_VENDORS) -> dict:
    """적요·가맹점명 → 거래처"""
    out = {}
    for v in vendors:
        out[v["name"]] = v
        if v.get("card_name"):
            out[v["card_name"]] = v
    return out


def rule_match(outflow: dict, docs: list[dict], vendors: dict) -> list[int]:
    """outflow = {table, at, amount, label}, docs = 증빙 행, vendors = vendors_by_label() 꼴"""
    v = vendors.get(outflow["label"])
    ym = outflow["at"][:7]
    if v and v["pay"] == "month_end_transfer":
        mine = [d for d in docs if d["issuer"] == v["name"] and d["issued_on"][:7] == ym]
        st = [d for d in mine if d["kind"] == "statement"]
        if st and sum(d["amount"] for d in st) == outflow["amount"]:
            return sorted(d["id"] for d in st + [d for d in mine if d["kind"] == "tax_invoice"])
        return []
    d0 = date.fromisoformat(outflow["at"][:10])
    lo, hi = DOC_WINDOW

    def gap(d) -> int:
        return (d0 - date.fromisoformat(d["issued_on"])).days

    cand = [d for d in docs if d["amount"] == outflow["amount"] and lo <= gap(d) <= hi]
    cand.sort(key=lambda d: (abs(gap(d)), d["id"]))
    return [cand[0]["id"]] if cand else []


def rule_account(outflow: dict, vendors: dict) -> str | None:
    v = vendors.get(outflow["label"])
    return v["account"] if v else None


def score(conn, split: str, vendors: dict | None = None) -> dict:
    """{n, match: {k,n}, account: {k,n}} — truth_expense_matches의 그 split 출금만(평가 설계 §2.7 — test는 사람만)"""
    vendors = vendors or vendors_by_label()
    docs = [
        dict(zip(("id", "kind", "issuer", "issued_on", "amount"), r, strict=True))
        for r in conn.execute(
            "SELECT id, kind, issuer, issued_on, amount FROM source.fin_expense_documents"
        )
    ]
    rows = conn.execute(
        "SELECT outflow_table, outflow_id, document_ids, account FROM truth.truth_expense_matches"
        " WHERE split = ?",
        (split,),
    ).fetchall()
    match = account = 0
    for table, oid, dids, acct in rows:
        q = (
            "SELECT occurred_at, amount, memo FROM source.fin_bank_transactions WHERE id = ?"
            if table == "fin_bank_transactions"
            else "SELECT used_at, amount, merchant FROM source.fin_card_expenses WHERE id = ?"
        )
        at, amount, label = conn.execute(q, (oid,)).fetchone()
        out = {"table": table, "at": at, "amount": amount, "label": label}
        match += rule_match(out, docs, vendors) == sorted(json.loads(dids))
        account += rule_account(out, vendors) == acct
    n = len(rows)
    return {"n": n, "match": {"k": match, "n": n}, "account": {"k": account, "n": n}}
