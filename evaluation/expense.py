"""⑧-2 지출 증빙 매칭·계정 과목 규칙 기준선 채점 (평가 설계 §1 원칙 8·§5 ⑧-2, 재무 대조 설계 §2).
기준선 원본은 backend/expense.py(화면과 같은 규칙), 이 파일은 dev 채점 — 사람 확정 덮어쓰기 없는 순수 규칙만 부른다"""

import json

from backend.expense import DOC_WINDOW, rule_account, rule_match, vendors_by_label
from generator.master import VENDORS as MASTER_VENDORS

__all__ = ["DOC_WINDOW", "rule_account", "rule_match", "score", "vendors_by_label"]


def score(conn, split: str, vendors: dict | None = None) -> dict:
    """{n, match: {k,n}, account: {k,n}} — truth_expense_matches의 그 split 출금만(평가 설계 §2.7 — test는 사람만)"""
    vendors = vendors or vendors_by_label(MASTER_VENDORS)
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
