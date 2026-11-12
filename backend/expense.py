"""⑧-2 지출 증빙 규칙 기준선 (재무 대조 설계 §2, 평가 설계 §5 ⑧-2) — 화면의 규칙 = 채점한 기준선.
dev 채점(평가 설계 §5 ⑧-2)이 이 규칙을 부른다. 동작을 바꾸면 dev 점수가 바뀌므로 test 전에 고정한 그대로 둔다.
- 매칭: 월말 이체(거래처 pay month_end_transfer)는 적요 = 거래처, 그달 그 거래처 명세서 합계 = 출금액이면 명세서 전부 + 그달
  세금계산서. 그 밖은 금액이 같고 출금 DOC_WINDOW 안의 증빙 중 날짜가 가장 가까운 하나. 없으면 [](증빙 없음)
- 과목: 적요·가맹점명(card_name)으로 찾은 거래처의 기본 과목
거래처 표는 앱이 source.ax_master_values expense_vendors, 평가가 master.VENDORS로 넘긴다"""

from datetime import date

DOC_WINDOW = (-3, 14)  # 증빙일이 출금 3일 뒤 ~ 14일 전 (출금일 − 증빙일, 일)


def vendors_by_label(vendors: list[dict]) -> dict:
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
