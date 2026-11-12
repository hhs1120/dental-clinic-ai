"""⑧-2 지출 증빙 규칙 기준선 (재무 대조 설계 §2, 평가 설계 §5 ⑧-2) — 화면의 규칙 = 채점한 기준선.
dev 채점(평가 설계 §5 ⑧-2)이 이 규칙을 부른다. 동작을 바꾸면 dev 점수가 바뀌므로 test 전에 고정한 그대로 둔다.
- 매칭: 월말 이체(거래처 pay month_end_transfer)는 적요 = 거래처, 그달 그 거래처 명세서 합계 = 출금액이면 명세서 전부 + 그달
  세금계산서. 그 밖은 금액이 같고 출금 DOC_WINDOW 안의 증빙 중 날짜가 가장 가까운 하나. 없으면 [](증빙 없음)
- 과목: 적요·가맹점명(card_name)으로 찾은 거래처의 기본 과목
거래처 표는 앱이 source.ax_master_values expense_vendors, 평가가 master.VENDORS로 넘긴다.
화면(지출 증빙 탭)·사람 확정·오늘 화면 줄도 여기 — 과목 기본값은 이 출금의 확정 > 같은 적요·가맹점명의 가장 최근 사람
확정(품목으로 정하는 거래처는 건너뜀) > 거래처 기본 과목. 채점은 사람 확정 덮어쓰기 없는 rule_*만 부른다.
확정 제한(설계 §4): 같은 증빙은 살아 있는 확정(사람 확정 + 되돌리지 않은 자동 확정) 하나에만, 기준일 이후 출금은 막는다"""

import hashlib
import json
import sqlite3
from datetime import date, timedelta

from backend.prep import master_values

TABLES = {  # 출금 표 → (시각, 적요·가맹점명) 칸 — 통장은 출금만(설계 §2 대상)
    "fin_bank_transactions": (
        "SELECT id, occurred_at AS at, memo AS label, amount FROM source.fin_bank_transactions"
        " WHERE direction = 'out'"
    ),
    "fin_card_expenses": (
        "SELECT id, used_at AS at, merchant AS label, amount FROM source.fin_card_expenses"
    ),
}
DOC_COLS = "id, kind, issuer, issued_on, amount, body"

DOC_WINDOW = (-3, 14)  # 증빙일이 출금 3일 뒤 ~ 14일 전 (출금일 − 증빙일, 일)
# 자동 확정 종류(backend/expense_auto.py) — 확정 제한이 함께 읽는다. 사람 확정이 그 자동 확정을 되돌릴 때 남기는 이
AUTO_KIND, HUMAN_REVERT = "expense_autoconfirm", "사람이 바꿈"


def outflow_key(o: dict) -> str:
    """출금 키 — 표·시각·금액·출금처 글자 해시(앱 설계 §7.3 "출금일·금액·적요"). 실행 대상·자동 확정 키"""
    raw = f"{o['table']}|{o['at']}|{o['amount']}|{o['label']}"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


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


def _gap(at: str, doc: dict) -> int:
    return (date.fromisoformat(at[:10]) - date.fromisoformat(doc["issued_on"])).days


def _load(conn: sqlite3.Connection, today: date | None) -> dict:
    """출금(today가 있으면 기준일 전만)·증빙·확정·거래처 — 표마다 한 번 읽는다(행 수와 무관하게 쿼리 수 일정)"""
    mv = master_values(conn)
    outflows = [
        {"table": t} | dict(r)
        for t, sql in TABLES.items()
        for r in conn.execute(sql)
        if today is None or r["at"] < today.isoformat()
    ]
    outflows.sort(key=lambda o: (o["at"], o["table"], o["id"]))
    conf, memory = {}, {}
    for r in conn.execute(
        "SELECT outflow_table, outflow_id, label, document_ids, account, judged_by, judged_at"
        " FROM ax_expense_confirmations ORDER BY judged_at DESC, rowid DESC"
    ):
        conf[(r["outflow_table"], r["outflow_id"])] = r
        memory.setdefault(r["label"], r["account"])
    return {
        "mv": mv,
        "vendors": vendors_by_label(mv["expense_vendors"]),
        # 품목으로 정하는 거래처의 이름·가맹점명 — 결제대행사가 덧붙은 표기(KCP*…)도 글자 안에 있으면 그 거래처로 본다
        "by_item_names": [
            n
            for v in mv["expense_vendors"]
            if v["by_item"]
            for n in (v["name"], v["card_name"])
            if n
        ],
        "outflows": outflows,
        "docs": [
            dict(r) for r in conn.execute(f"SELECT {DOC_COLS} FROM source.fin_expense_documents")
        ],
        "conf": conf,
        "memory": memory,
    }


def _doc_view(doc: dict) -> dict:
    return {k: doc[k] for k in ("id", "kind", "issuer", "issued_on", "amount", "body")}


def _agent_view(a: dict | None, by_id: dict) -> dict | None:
    if a is None:
        return None
    ids = a["document_ids"] or []
    return {
        k: a[k]
        for k in ("document_ids", "account", "confidence", "reasons", "see", "status", "lines")
    } | {"docs": [_doc_view(by_id[i]) for i in ids if i in by_id]}


def _row(o: dict, data: dict, by_id: dict, agent=None, auto=None) -> dict:
    """화면 한 줄 — 증빙은 확정 > 자동 확정 > 규칙. 금액 차이 = 출금액 − 증빙 합(명세서가 있으면 명세서 합
    — 같은 거래의 세금계산서는 더하지 않는다), 증빙 없으면 None. agent = AI 결과(backend/expense_agent.py),
    agree = 규칙과 같음(결과 없으면 None, backend/expense_auto.py agree), auto = 되돌리지 않은 자동 확정(설계 §4)"""
    from backend.expense_auto import agree  # expense_auto가 이 모듈을 읽으므로 순환을 피해 여기서

    v = data["vendors"].get(o["label"])
    c = data["conf"].get((o["table"], o["id"]))
    rule_ids = rule_match(o, data["docs"], data["vendors"])
    if c:
        ids = json.loads(c["document_ids"])
    elif auto:
        ids = auto["document_ids"]
    else:
        ids = rule_ids
    docs = [by_id[i] for i in ids if i in by_id]
    st = [d for d in docs if d["kind"] == "statement"]
    # 품목으로 정하는 거래처 — 기억으로 과목을 채우지 않고 늘 확인 필요. 기본 과목(rule_account)은 표의 글자만 본다
    by_item = bool(v and v["by_item"]) or any(n in o["label"] for n in data["by_item_names"])
    if c:
        account, source = c["account"], "confirmed"
    elif auto:
        account, source = auto["account"], "auto"
    elif not by_item and o["label"] in data["memory"]:
        account, source = data["memory"][o["label"]], "memory"
    elif v:
        account, source = v["account"], "vendor"
    else:
        account, source = None, None
    done = agent is not None and agent["account"] is not None
    return {
        "key": outflow_key(o),
        "table": o["table"],
        "id": o["id"],
        "at": o["at"],
        "label": o["label"],
        "amount": o["amount"],
        "vendor": v["name"] if v else None,
        "docs": [_doc_view(d) for d in docs],
        "diff": o["amount"] - sum(d["amount"] for d in st or docs) if docs else None,
        "account": account,
        "account_from": source,
        "by_item": by_item,
        # 과목 확인 필요 = 남은 일(확정·자동 확정 전) — 과목이 비었거나 품목으로 정하는 거래처
        "check": c is None and auto is None and (account is None or by_item),
        "confirmed": {"by": c["judged_by"], "at": c["judged_at"]} if c else None,
        "agent": _agent_view(agent, by_id),
        "agree": agree(rule_ids, rule_account(o, data["vendors"]), agent) if done else None,
        "auto": {"id": auto["id"], "at": auto["at"]} if auto else None,
    }


def _rows(conn: sqlite3.Connection, data: dict, outflows: list[dict]) -> list[dict]:
    """여러 줄 — AI 결과·자동 확정은 한 번에 읽는다(쿼리 수가 줄 수와 무관)"""
    from backend import expense_agent, expense_auto  # 둘 다 이 모듈을 읽으므로 순환을 피해 여기서

    by_id = {d["id"]: d for d in data["docs"]}
    keys = {outflow_key(o) for o in outflows}
    agents, autos = expense_agent.results(conn, keys), expense_auto.active(conn, keys)
    return [
        _row(o, data, by_id, agents.get(outflow_key(o)), autos.get(outflow_key(o)))
        for o in outflows
    ]


def _open(r: dict) -> bool:
    """사람이 볼 줄 — 확정·자동 확정 안 됐고, 증빙 없음·과목 확인 필요·규칙과 다름(설계 §4)"""
    return (
        r["confirmed"] is None
        and r["auto"] is None
        and (not r["docs"] or r["check"] or r["agree"] is False)
    )


def sheet(conn: sqlite3.Connection, today: date, month: str | None = None) -> dict:
    """지출 증빙 탭 — 그 달 출금마다 한 줄(설계 §2). 달은 기준일 전 출금이 있는 달, 기본은 가장 최근 달"""
    data = _load(conn, today)
    months = sorted({o["at"][:7] for o in data["outflows"]})
    if month is None:
        month = months[-1] if months else None
    elif month not in months:
        raise ValueError(f"출금이 없는 달: {month}")
    rows = _rows(conn, data, [o for o in data["outflows"] if o["at"][:7] == month])
    return {
        "months": months,
        "month": month,
        "accounts": data["mv"]["expense_accounts"],
        "chips": {
            "outflows": len(rows),
            "amount": sum(r["amount"] for r in rows),
            "found": sum(bool(r["docs"]) for r in rows),
            "none": sum(not r["docs"] and r["confirmed"] is None for r in rows),
            "check": sum(r["check"] for r in rows),
            "differ": sum(
                r["agree"] is False and r["confirmed"] is None and r["auto"] is None for r in rows
            ),
            "open": sum(r["confirmed"] is None and r["auto"] is None for r in rows),
        },
        "rows": rows,
    }


def _outflow(data: dict, table: str, outflow_id: int) -> dict:
    o = next((o for o in data["outflows"] if (o["table"], o["id"]) == (table, outflow_id)), None)
    if o is None:
        raise ValueError(f"출금이 아님: {table} {outflow_id}")
    return o


def _candidates(o: dict, data: dict) -> list[dict]:
    """창 안 증빙 + 출금한 달에 발행된 증빙 전부 — 월말 이체의 명세서는 발행처가 별칭((주)…)이거나 적요가 잘려
    거래처로 가릴 수 없어도 그달 안에 있다(앱 코드라 기준선 rule_match와 무관)"""
    lo, hi = DOC_WINDOW
    got = [
        d
        for d in data["docs"]
        if lo <= _gap(o["at"], d) <= hi or d["issued_on"][:7] == o["at"][:7]
    ]
    got.sort(key=lambda d: (abs(_gap(o["at"], d)), d["id"]))
    return got


def candidates(conn: sqlite3.Connection, table: str, outflow_id: int) -> list[dict]:
    """증빙 바꾸기 후보 — 출금일 기준 DOC_WINDOW 안 증빙과 출금한 달 발행 증빙 전부(금액 무관).
    날짜 가까운 순. 없는 출금은 ValueError"""
    data = _load(conn, None)
    return [_doc_view(d) for d in _candidates(_outflow(data, table, outflow_id), data)]


def live_doc_owners(conn: sqlite3.Connection, data: dict) -> dict[int, tuple[str, int]]:
    """증빙 id → 그 증빙을 가진 살아 있는 확정의 출금(표, id) — 사람 확정과 되돌리지 않은 자동 확정"""
    owners = {}
    for (table, oid), r in data["conf"].items():
        for d in json.loads(r["document_ids"]):
            owners[d] = (table, oid)
    by_key = {outflow_key(o): (o["table"], o["id"]) for o in data["outflows"]}
    for r in conn.execute(
        "SELECT target, result FROM ax_auto_actions WHERE kind = ? AND reverted_at IS NULL",
        (AUTO_KIND,),
    ):
        if r["target"] in by_key:
            for d in json.loads(r["result"])["document_ids"]:
                owners.setdefault(d, by_key[r["target"]])
    return owners


def confirm(
    conn: sqlite3.Connection,
    table: str,
    outflow_id: int,
    document_ids: list[int],
    account: str,
    by: str,
    at: str,
    today: date,
) -> None:
    """사람 확정 — 없는 출금·모르는 과목·후보 밖 증빙 id·기준일 이후 출금·다른 출금에 이미 확정된 증빙은
    ValueError(설계 §4 확정 제한). 덮어씀, 그 출금의 살아 있는 자동 확정은 되돌린다. 커밋은 부르는 쪽"""
    data = _load(conn, None)
    o = _outflow(data, table, outflow_id)
    if o["at"] >= today.isoformat():
        raise ValueError(f"기준일 이후 출금: {o['at'][:10]}")
    if account not in data["mv"]["expense_accounts"]:
        raise ValueError(f"모르는 계정 과목: {account}")
    if not set(document_ids) <= {d["id"] for d in _candidates(o, data)}:
        raise ValueError(f"후보 밖 증빙: {document_ids}")
    owners = live_doc_owners(conn, data)
    by_id = {(x["table"], x["id"]): x for x in data["outflows"]}
    for d in document_ids:
        if (other := owners.get(d)) and other != (table, outflow_id):
            x = by_id[other]
            raise ValueError(f"이미 다른 출금에 확정된 증빙: {d} ({x['at'][:10]} {x['label']})")
    conn.execute(
        "INSERT INTO ax_expense_confirmations (outflow_table, outflow_id, label, document_ids,"
        " account, judged_by, judged_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(outflow_table, outflow_id) DO UPDATE SET label = excluded.label,"
        " document_ids = excluded.document_ids, account = excluded.account,"
        " judged_by = excluded.judged_by, judged_at = excluded.judged_at",
        (table, outflow_id, o["label"], json.dumps(sorted(set(document_ids))), account, by, at),
    )
    conn.execute(
        "UPDATE ax_auto_actions SET reverted_by = ?, reverted_at = ?"
        " WHERE kind = ? AND target = ? AND reverted_at IS NULL",
        (HUMAN_REVERT, at, AUTO_KIND, outflow_key(o)),
    )


def open_summary(conn: sqlite3.Connection, today: date) -> dict:
    """오늘 화면 '지출 증빙 확인' 줄 — 기준일의 지난달 출금 중 사람이 볼 줄(_open — 증빙 없음·과목 확인 필요·
    규칙과 다름, 확정·자동 확정 안 됨) 수(월말 업무라 지난달만, 설계 §2·§4). month는 링크할 달, 0건이면 None"""
    month = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    data = _load(conn, today)
    n = sum(
        _open(r) for r in _rows(conn, data, [o for o in data["outflows"] if o["at"][:7] == month])
    )
    return {"count": n, "month": month if n else None}
