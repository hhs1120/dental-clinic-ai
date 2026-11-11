"""⑧-1 재무 대조 짝짓기 (기능 정의 §5 ⑧, 설계 docs/superpowers/specs/2026-11-11-finance-recon-design.md §1).
장부(emr_payments)와 금융(fin_card_sales·fin_card_deposits·fin_bank_transactions 입금)을 행 단위로 짝짓고, 짝이
안 맞고 남은 것을 금액 관계로 묶어 "불일치 건"으로 낸다 — 하루 합계만 비교하면 취소 후 재승인처럼 상쇄되는 건을 놓친다.
- 카드: 승인번호 + 같은 날 + 같은 금액(손 입력은 같은 날 같은 금액, 시각이 가장 가까운 것). 취소 행과 그 원승인은 "취소 쌍"
- 정산: (카드사, 매출일) 승인 합계·수수료(생성기와 같은 int(합계 × 율))·예상 입금일(가상 데이터 생성 설계 §2.7)
- 현금: 입금 1건 ↔ 그 입금이 덮는 기간의 장부 현금 합계 — 다르면 입금 + 그 기간 장부 현금 행이 한 건
- 이체: 같은 금액, 시각 차 TRANSFER_WINDOW_MIN분 이하. 입금자명은 보지 않는다(보호자 이름 디코이)
받은 금액 0원 진료 행은 짝짓지 않는다(규칙 원인만 쓴다 — backend/recon.py). 정산·현금 입금이 앱 기준일 이후인 날은
대조 전(pending). master 값은 source.ax_master_values로만 읽고, 표마다 한 번 읽는다(행 수와 무관하게 쿼리 수가 같다)."""

import hashlib
import json
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta

from backend.prep import master_values

TRANSFER_WINDOW_MIN = (
    30  # 이체 짝의 시각 차 상한 — 넓히면 다른 날짜 밀림 두 건이 잘못 짝지어진다(설계 §1)
)
FMT = "%Y-%m-%d %H:%M:%S"


def bank_day(d: date, holidays) -> bool:
    """은행 영업일 — 평일·공휴일 아님(master.bank_day와 같은 결과)"""
    return d.weekday() < 5 and d not in holidays


def add_bank_days(d: date, n: int, holidays) -> date:
    """d 뒤 n번째 은행 영업일 — 카드 정산 예상일(master.add_bank_days와 같은 결과)"""
    while n:
        d += timedelta(days=1)
        n -= bank_day(d, holidays)
    return d


def cash_deposit_day(d: date, weekdays, holidays) -> date:
    """d에 받은 현금이 들어가는 통장 입금일 — 다음 날부터 첫 입금 요일, 은행 영업일이 아니면 다음 영업일"""
    x = d + timedelta(days=1)
    while x.weekday() not in weekdays:
        x += timedelta(days=1)
    while not bank_day(x, holidays):
        x += timedelta(days=1)
    return x


def _at(s: str) -> datetime:
    return datetime.strptime(s, FMT)


def load(conn: sqlite3.Connection, today: date) -> dict:
    """대조에 쓰는 행 전부 — 표마다 쿼리 한 번"""
    mv = master_values(conn)
    pays = [
        dict(r) | {"at": _at(r["paid_at"])}
        for r in conn.execute(
            "SELECT p.id, p.patient_id, pt.name, p.kind, p.amount_due, p.amount_paid, p.paid_at,"
            " p.method, p.approval_no FROM source.emr_payments p"
            " JOIN source.emr_patients pt ON pt.id = p.patient_id ORDER BY p.paid_at, p.id"
        )
    ]
    sales = [
        dict(r) | {"at": _at(r["approved_at"])}
        for r in conn.execute(
            "SELECT id, approved_at, issuer, amount, approval_no, cancel_of"
            " FROM source.fin_card_sales ORDER BY approved_at, id"
        )
    ]
    deposits = [
        dict(r) for r in conn.execute("SELECT * FROM source.fin_card_deposits ORDER BY id")
    ]
    bank_in = [
        dict(r) | {"at": _at(r["occurred_at"])}
        for r in conn.execute(
            "SELECT id, occurred_at, amount, memo FROM source.fin_bank_transactions"
            " WHERE direction = 'in' ORDER BY occurred_at, id"
        )
    ]
    return {
        "today": today,
        "mv": mv,
        "holidays": {date.fromisoformat(d) for d in mv["bank_holidays"]},
        "payments": [p for p in pays if p["amount_paid"] > 0],
        "zero_visits": [
            p
            for p in pays
            if p["kind"] == "visit" and p["amount_paid"] == 0 and p["amount_due"] > 0
        ],
        "open_days": sorted({p["at"].date() for p in pays}),
        "sales": sales,
        "deposits": deposits,
        "bank_in": bank_in,
    }


def _cash_day(books: dict, d: date) -> date:
    return cash_deposit_day(d, books["mv"]["cash_deposit"]["weekdays"], books["holidays"])


def day_status(books: dict, d: date) -> str:
    """ok = 대조 대상, pending = 그날 카드 정산이나 현금 입금이 기준일 이후(설계 §1 TODAY 경계)"""
    settle = max(i["settle_days"] for i in books["mv"]["card_issuers"].values())
    if (
        add_bank_days(d, settle, books["holidays"]) >= books["today"]
        or _cash_day(books, d) >= books["today"]
    ):
        return "pending"
    return "ok"


def _pair_closest(left: list[dict], right: list[dict], ok) -> tuple[list[dict], list[dict]]:
    """시각 차가 작은 쌍부터 짝짓고 (남은 left, 남은 right) — ok(l, r)가 짝 후보 조건"""
    cand = sorted(
        (abs((l_["at"] - r["at"]).total_seconds()), i, j)
        for i, l_ in enumerate(left)
        for j, r in enumerate(right)
        if ok(l_, r)
    )
    used_l, used_r = set(), set()
    for _, i, j in cand:
        if i not in used_l and j not in used_r:
            used_l.add(i)
            used_r.add(j)
    return (
        [x for i, x in enumerate(left) if i not in used_l],
        [x for j, x in enumerate(right) if j not in used_r],
    )


def _node(side, channel, table, row, amount, d, **extra) -> dict:
    return {
        "side": side,  # ledger | fin | cash(입금 1건 + 그 기간 장부 현금)
        "channel": channel,
        "table": table,
        "id": row["id"],
        "amount": amount,
        "date": d,
        "at": row.get("at"),
        "approval_no": row.get("approval_no"),
    } | extra


def _card(books, ok_day) -> list[dict]:
    """카드 — 짝 없는 장부 카드·승인 노드와 취소 쌍"""
    sales = [s for s in books["sales"] if ok_day(s["at"].date())]
    alive = {s["id"]: s for s in sales if not s["cancel_of"]}
    pairs, orphans = [], []  # orphans = 같은 날 원승인이 없는 취소(다음 날 취소 등)
    for c in (s for s in sales if s["cancel_of"]):
        orig = next(
            (
                s
                for s in alive.values()
                if s["approval_no"] == c["cancel_of"] and s["at"].date() == c["at"].date()
            ),
            None,
        )
        if orig:
            del alive[orig["id"]]
            pairs.append((orig, c))
        else:
            orphans.append(c)
    ledger = [p for p in books["payments"] if p["method"] == "card" and ok_day(p["at"].date())]
    by_day: dict[date, list[dict]] = defaultdict(list)
    for s in alive.values():
        by_day[s["at"].date()].append(s)
    left_l, left_s = [], []
    for d in sorted({p["at"].date() for p in ledger} | set(by_day)):
        day_l = [p for p in ledger if p["at"].date() == d]
        numbered = [p for p in day_l if p["approval_no"]]
        rest_l, rest_s = _pair_closest(
            numbered,
            by_day[d],
            lambda p, s: p["approval_no"] == s["approval_no"] and p["amount_paid"] == s["amount"],
        )
        hand, rest_s = _pair_closest(
            [p for p in day_l if not p["approval_no"]],
            rest_s,
            lambda p, s: p["amount_paid"] == s["amount"],
        )
        left_l += rest_l + hand
        left_s += rest_s
    nodes = [
        _node("ledger", "card", "emr_payments", p, p["amount_paid"], p["at"].date())
        for p in left_l
    ] + [_node("fin", "card", "fin_card_sales", s, s["amount"], s["at"].date()) for s in left_s]
    nodes += [
        _node("fin", "card", "fin_card_sales", c, c["amount"], c["at"].date(), cancel=True)
        for c in orphans
    ]
    for orig, c in pairs:
        nodes.append(
            _node(
                "fin",
                "card",
                "fin_card_sales",
                orig,
                0,
                orig["at"].date(),
                cancel_pair=[orig["id"], c["id"]],
            )
        )
    return nodes


def _settlement(books, ok_day) -> list[list[dict]]:
    """정산 — (카드사, 매출일)마다 합계·수수료·예상 입금일이 어긋나거나 통장 입금이 없으면 정산 입금 + 그 통장
    입금이 한 건. 매출이 있는데 정산 행이 아예 없으면 그 승인들이 한 건(카드사 돈이 안 들어옴)"""
    totals: dict[tuple[str, str], int] = defaultdict(int)
    sales: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for s in books["sales"]:
        k = (s["issuer"], s["approved_at"][:10])
        totals[k] += -s["amount"] if s["cancel_of"] else s["amount"]
        sales[k].append(s)
    issuers = books["mv"]["card_issuers"]
    bank: dict[tuple[str, str, int], list[dict]] = defaultdict(list)
    for b in books["bank_in"]:
        if b["memo"] in issuers:
            bank[(b["memo"], b["occurred_at"][:10], b["amount"])].append(b)
    out = []
    seen = set()
    for dep in books["deposits"]:
        d = date.fromisoformat(dep["sales_date"])
        seen.add((dep["issuer"], dep["sales_date"]))
        if not ok_day(d):
            continue
        cfg = issuers[dep["issuer"]]
        total = totals.get((dep["issuer"], dep["sales_date"]), 0)
        expected = add_bank_days(d, cfg["settle_days"], books["holidays"]).isoformat()
        rows = bank.get((dep["issuer"], dep["deposited_on"], dep["amount"]))
        b = rows.pop(0) if rows else None  # 같은 키 통장 행이 여럿이면 하나씩 차지한다
        if (
            dep["sales_total"] == total
            and dep["fee"] == int(total * cfg["fee_rate"])
            and dep["deposited_on"] <= expected
            and b
        ):
            continue
        nodes = [
            _node(
                "fin",
                "settlement",
                "fin_card_deposits",
                dep,
                dep["amount"],
                d,
                expected_on=expected,
                deposited_on=dep["deposited_on"],
                fee=dep["fee"],
                sales_total=dep["sales_total"],
                computed_total=total,
                computed_fee=int(total * cfg["fee_rate"]),
                bank_missing=b is None,
            )
        ]
        if b:
            nodes.append(_node("fin", "settlement", "fin_bank_transactions", b, b["amount"], d))
        out.append(nodes)
    for (issuer, day), total in sorted(totals.items()):
        d = date.fromisoformat(day)
        if (issuer, day) in seen or total <= 0 or not ok_day(d):
            continue
        out.append(
            [
                _node(
                    "fin",
                    "settlement",
                    "fin_card_sales",
                    s,
                    0,  # 승인은 카드 짝짓기가 이미 센다 — 이 건의 금액은 받지 못한 정산
                    d,
                    deposit_missing=True,
                    computed_total=total,
                )
                for s in sales[(issuer, day)]
            ]
        )
    return out


def _cash(books, ok_day) -> list[dict]:
    """현금 — 입금일별 장부 현금 합계 ↔ 그날 현금 입금. 다르면 노드 하나(차이 = 금융 − 장부)"""
    memo = books["mv"]["cash_deposit"]["memo"]
    period: dict[date, list[date]] = defaultdict(list)
    for d in books["open_days"]:
        period[_cash_day(books, d)].append(d)
    # 기간에 대조 전 날이 하나라도 섞인 입금은 통째로 대조 전 — 그날 현금도 그 입금에 들어 있다
    period = {dep: days for dep, days in period.items() if all(ok_day(d) for d in days)}
    ledger: dict[date, list[dict]] = defaultdict(list)
    for p in books["payments"]:
        if p["method"] == "cash" and _cash_day(books, p["at"].date()) in period:
            ledger[_cash_day(books, p["at"].date())].append(p)
    bank: dict[date, list[dict]] = defaultdict(list)  # 같은 날 현금 입금이 여러 줄이면 합쳐 본다
    for b in books["bank_in"]:
        if b["memo"] == memo and b["at"].date() < books["today"]:
            bank[b["at"].date()].append(b)
    order = sorted(period)
    out = []
    for dep in sorted(set(ledger) | {d for d in bank if d in period}):
        rows, bs = ledger.get(dep, []), bank.get(dep, [])
        led = sum(p["amount_paid"] for p in rows)
        fin = sum(b["amount"] for b in bs)
        if led == fin:
            continue
        days = period[dep]
        out.append(
            {
                "side": "cash",
                "channel": "cash",
                "table": "fin_bank_transactions",
                "id": bs[0]["id"] if bs else None,
                "ids": [b["id"] for b in bs],
                "seq": order.index(dep),  # 입금 순번 — 날짜 밀림은 바로 이웃한 두 입금만
                "amount": fin - led,
                "date": max(days),
                "deposit_on": dep,
                "period": days,
                "ledger_rows": rows,
                "ledger_total": led,
                "fin_total": fin,
            }
        )
    return out


def _transfer(books, ok_day) -> list[dict]:
    """이체 — 같은 금액, 시각 차 TRANSFER_WINDOW_MIN분 이하. 카드 정산·현금·공단 입금은 후보가 아니다"""
    mv = books["mv"]
    skip = set(mv["card_issuers"]) | {mv["cash_deposit"]["memo"], mv["nhis_memo"]}
    ledger = [p for p in books["payments"] if p["method"] == "transfer" and ok_day(p["at"].date())]
    bank = [b for b in books["bank_in"] if b["memo"] not in skip and ok_day(b["at"].date())]
    window = TRANSFER_WINDOW_MIN * 60
    left_l, left_b = _pair_closest(
        ledger,
        bank,
        lambda p, b: (
            p["amount_paid"] == b["amount"] and abs((p["at"] - b["at"]).total_seconds()) <= window
        ),
    )
    return [
        _node("ledger", "transfer", "emr_payments", p, p["amount_paid"], p["at"].date())
        for p in left_l
    ] + [
        _node("fin", "transfer", "fin_bank_transactions", b, b["amount"], b["at"].date())
        for b in left_b
    ]


class _Groups:
    """노드 묶기(union-find)"""

    def __init__(self, n: int):
        self.parent = list(range(n))
        self.size = [1] * n

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, i: int, j: int) -> None:
        a, b = self.find(i), self.find(j)
        if a != b:
            self.parent[a] = b
            self.size[b] += self.size[a]

    def alone(self, i: int) -> bool:
        return self.size[self.find(i)] == 1


def _group(books, nodes: list[dict]) -> list[list[dict]]:
    """남은 노드를 금액 관계로 묶는다(설계 §1 묶기)"""
    g = _Groups(len(nodes))
    idx = list(range(len(nodes)))
    led = [i for i in idx if nodes[i]["side"] == "ledger"]
    fin = [i for i in idx if nodes[i]["side"] == "fin" and "cancel_pair" not in nodes[i]]
    pairs = [i for i in idx if "cancel_pair" in nodes[i]]
    cash = [i for i in idx if nodes[i]["side"] == "cash"]
    # 취소 쌍 — 원승인 번호가 같은 날 남은 장부 행에 붙인다(없으면 같은 날 환불이라 버린다)
    for i in pairs:
        for j in led:
            if (
                nodes[j]["approval_no"] == nodes[i]["approval_no"]
                and nodes[j]["date"] == nodes[i]["date"]
            ):
                g.union(i, j)
                break
    # 같은 날 — 승인번호 같음, 같은 금액(카드끼리 또는 결제수단이 다름), 둘의 합, 10배(같은 결제수단)
    for j in led:
        lj = nodes[j]
        same_day = [i for i in fin if nodes[i]["date"] == lj["date"]]
        for i in same_day:
            fi = nodes[i]
            if lj["approval_no"] and lj["approval_no"] == fi["approval_no"]:
                g.union(i, j)
            elif fi["amount"] == lj["amount"] and (
                fi["channel"] != lj["channel"] or lj["channel"] == "card"
            ):
                g.union(i, j)
            elif fi["channel"] == lj["channel"] and (
                lj["amount"] * 10 == fi["amount"] or fi["amount"] * 10 == lj["amount"]
            ):
                g.union(i, j)
        for a in same_day:
            for c in same_day:
                if a < c and nodes[a]["amount"] + nodes[c]["amount"] == lj["amount"]:
                    g.union(a, j)
                    g.union(c, j)
    # 이웃 진료일 — 아직 혼자인 장부 행과 이전(없으면 다음) 진료일의 혼자인 같은 금액 금융 행
    days = books["open_days"]
    pos = {d: k for k, d in enumerate(days)}
    for j in sorted(led, key=lambda k: (nodes[k]["date"], nodes[k]["at"])):
        if not g.alone(j) or nodes[j]["date"] not in pos:
            continue
        k = pos[nodes[j]["date"]]
        for nd in [days[k - 1] if k else None, days[k + 1] if k + 1 < len(days) else None]:
            hit = next(
                (
                    i
                    for i in fin
                    if g.alone(i)
                    and nodes[i]["date"] == nd
                    and nodes[i]["amount"] == nodes[j]["amount"]
                ),
                None,
            )
            if hit is not None:
                g.union(hit, j)
                break
    # 현금 — 그 기간의 남은 행과 차이 금액이 같으면, 연속한 두 입금 차이가 +X / −X면 합친다
    for c in cash:
        diff, period = nodes[c]["amount"], set(nodes[c]["period"])
        for i in led + fin:
            if nodes[i]["date"] in period and nodes[i]["amount"] == abs(diff):
                if (diff < 0) == (nodes[i]["side"] == "fin"):
                    g.union(c, i)
    deps = sorted(cash, key=lambda k: nodes[k]["deposit_on"])
    for a, c in zip(deps, deps[1:], strict=False):
        if nodes[c]["seq"] == nodes[a]["seq"] + 1 and nodes[a]["amount"] == -nodes[c]["amount"]:
            g.union(a, c)
    groups: dict[int, list[dict]] = defaultdict(list)
    for i in idx:
        groups[g.find(i)].append(nodes[i])
    # 원장 행이 없는 취소 쌍(같은 날 환불)만 남은 묶음은 불일치가 아니다
    return [ns for ns in groups.values() if not all("cancel_pair" in n for n in ns)]


def _item(nodes: list[dict]) -> dict:
    payment_ids, fin = set(), set()
    ledger_total = fin_total = 0
    for n in nodes:
        if n["side"] == "cash":
            payment_ids |= {p["id"] for p in n["ledger_rows"]}
            fin |= {(n["table"], i) for i in n["ids"]}
            ledger_total += n["ledger_total"]
            fin_total += n["fin_total"]
        elif n["side"] == "ledger":
            payment_ids.add(n["id"])
            ledger_total += n["amount"]
        elif "cancel_pair" in n:
            fin |= {("fin_card_sales", x) for x in n["cancel_pair"]}
        else:
            fin.add((n["table"], n["id"]))
            if n["table"] != "fin_bank_transactions" or n["channel"] != "settlement":
                fin_total += -n["amount"] if n.get("cancel") else n["amount"]
    channels = {n["channel"] for n in nodes}
    channel = next(c for c in ("settlement", "cash", "transfer", "card") if c in channels)
    if channels == {"transfer", "card"}:
        channel = "card"
    dates = sorted({n["date"].isoformat() for n in nodes})
    refs = [{"table": t, "id": i} for t, i in sorted(fin)]
    pids = sorted(payment_ids)
    key = hashlib.sha1(json.dumps([refs, pids]).encode()).hexdigest()[:12]
    return {
        "key": key,
        "date": dates[0],
        "dates": dates,
        "channel": channel,
        "payment_ids": pids,
        "fin": refs,
        "ledger_total": ledger_total,
        "fin_total": fin_total,
        "parts": nodes,
    }


def detect(books: dict) -> list[dict]:
    """불일치 건 — 날짜·키 순. parts = 묶인 노드(규칙 원인이 읽는다, backend/recon.py)"""
    status: dict[date, str] = {}

    def ok_day(d: date) -> bool:
        if d not in status:
            status[d] = day_status(books, d)
        return status[d] == "ok"

    items = [_item(ns) for ns in _settlement(books, ok_day)]
    nodes = _card(books, ok_day) + _cash(books, ok_day) + _transfer(books, ok_day)
    items += [_item(ns) for ns in _group(books, nodes)]
    return sorted(items, key=lambda i: (i["date"], i["key"]))
