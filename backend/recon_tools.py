"""⑧-1 조사 에이전트의 조회 도구 (기능 정의 §5 ⑧-1, 설계 docs/superpowers/specs/2026-11-11-finance-recon-design.md §3).
모두 읽기 전용이다 — 장부(emr_payments)·카드 승인·정산 입금·통장·내원·미수·가족·구강용품 가격표와, 며칠 늦게 적은
정산을 잇는 "금액으로 다른 날 찾기"(AI 실험 기록 §24). 결과마다 조회 번호(lookup_no)와 한 줄 요약(summary)을 붙인다 —
근거는 이 번호로 가리키고(backend/recon_agent.py 근거 훅), 화면의 조사 과정 줄도 같은 번호·요약을 보인다.
에이전트 입력 글(input_text)에는 규칙 추정 원인과 생성 메타를 넣지 않는다(설계 §3 "입력에 넣지 않는 것").
master 값은 source.ax_master_values로만 읽는다."""

import sqlite3
from datetime import date
from typing import ClassVar, Literal

from pydantic import BaseModel, Field, model_validator

from backend.harness.tools import Tool
from backend.prep import master_values
from backend.recon_match import add_bank_days

LOOKUPS = (
    "ledger_day",
    "card_sales",
    "card_deposits",
    "bank_tx",
    "visits_day",
    "patient_balance",
    "family",
    "oral_care_prices",
    "find_amount",
)
CARD_SALES_DAYS = (
    7  # 기간 조회 상한(일) — 결과가 모델 입력 상한(AgentSpec.result_limit)을 넘지 않게
)
BANK_TX_DAYS = 14
FIND_AMOUNT_DAYS = 31
BALANCE_ROWS = 10
ORAL_MAX_ITEMS = 10  # 구강용품 조합 — 한 번 판매의 개수 상한
ORAL_MAX_COMBOS = 5
NOTE_MAX = 120  # purpose·checked 한 줄 길이 상한(자)


def won(n: int) -> str:
    return f"{n:,}원"


def lookup_steps(steps: list[dict], submit: str) -> list[dict]:
    """조회 번호 순 단계 — 끝내는 도구가 아니고 훅을 지나 오류 없이 돈 도구 단계. 번호 = 위치 + 1"""
    return [
        s
        for s in steps
        if s["kind"] == "tool"
        and s["tool"] != submit
        and s["hook_status"] == "pass"
        and not s["is_error"]
    ]


def _rows(conn, sql: str, *args) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


class _Note(BaseModel):
    """조회마다 받는 조사 과정 칸 — 결과 계산에는 쓰지 않고 화면 줄(가설·비교)로만 보인다(설계 §3 "조사 과정 줄 종류")"""

    purpose: str = Field(
        min_length=1, max_length=NOTE_MAX, description="이 조회로 확인하려는 것 한 줄"
    )
    checked: str | None = Field(
        default=None,
        max_length=NOTE_MAX,
        description="직전 조회 결과가 의심한 것과 맞았는지 한 줄(첫 조회는 비움)",
    )


class _Day(_Note):
    day: date


class _Range(_Note):
    date_from: date
    date_to: date
    max_days: ClassVar[int] = 0

    @model_validator(mode="after")
    def _span(self):
        span = (self.date_to - self.date_from).days
        if span < 0 or span > self.max_days:
            raise ValueError(f"date_to − date_from은 0~{self.max_days}일")
        return self


class _CardSales(_Range):
    max_days: ClassVar[int] = CARD_SALES_DAYS
    amount_min: int | None = None
    amount_max: int | None = None


class _Deposits(_Note):
    sales_date: date
    issuer: str | None = None


class _BankTx(_Range):
    max_days: ClassVar[int] = BANK_TX_DAYS
    direction: Literal["in", "out"] = "in"


class _Patient(_Note):
    patient_id: int


class _Balance(_Patient):
    before: date


class _Oral(_Note):
    amount: int | None = Field(default=None, gt=0)


class _FindAmount(_Range):
    max_days: ClassVar[int] = FIND_AMOUNT_DAYS
    amount: int = Field(gt=0)


def _ledger_day(conn, a) -> dict:
    day = a["day"]
    rows = _rows(
        conn,
        "SELECT p.id AS payment_id, substr(p.paid_at, 12, 5) AS time, p.patient_id, pt.name,"
        " p.kind, p.amount_due, p.amount_paid, p.method, p.approval_no FROM source.emr_payments p"
        " JOIN source.emr_patients pt ON pt.id = p.patient_id"
        " WHERE date(p.paid_at) = ? ORDER BY p.paid_at, p.id",
        day,
    )
    prev = conn.execute(
        "SELECT max(date(paid_at)) FROM source.emr_payments WHERE date(paid_at) < ?", (day,)
    ).fetchone()[0]
    nxt = conn.execute(
        "SELECT min(date(paid_at)) FROM source.emr_payments WHERE date(paid_at) > ?", (day,)
    ).fetchone()[0]
    by = {
        m: sum(r["amount_paid"] for r in rows if r["method"] == m)
        for m in ("card", "cash", "transfer")
    }
    return {
        "summary": f"{day} 장부 {len(rows)}건 · 카드 {won(by['card'])} · 현금 {won(by['cash'])}"
        f" · 이체 {won(by['transfer'])}",
        "rows": rows,
        "prev_open_day": prev,
        "next_open_day": nxt,
    }


def _card_sales(conn, a) -> dict:
    sql = (
        "SELECT id, approved_at AS time, issuer, amount, approval_no, cancel_of"
        " FROM source.fin_card_sales WHERE date(approved_at) BETWEEN ? AND ?"
    )
    args = [a["date_from"], a["date_to"]]
    if a["amount_min"] is not None:
        sql += " AND amount >= ?"
        args.append(a["amount_min"])
    if a["amount_max"] is not None:
        sql += " AND amount <= ?"
        args.append(a["amount_max"])
    rows = _rows(conn, sql + " ORDER BY approved_at, id", *args)
    lo, hi = a["amount_min"], a["amount_max"]
    band = (
        ""
        if lo is None and hi is None
        else f" · 금액 {'' if lo is None else f'{lo:,}'}~{'' if hi is None else won(hi)}"
    )
    return {
        "summary": f"{a['date_from']}~{a['date_to']}{band} 카드 승인 {len(rows)}건",
        "rows": rows,
    }


def _card_deposits(conn, a) -> dict:
    """정산 입금 + 코드가 계산한 기대값 — 그 카드사·그날 승인 합계(취소 차감), 수수료 int(합계 × 율), 예상 입금일"""
    mv = master_values(conn)
    holidays = {date.fromisoformat(d) for d in mv["bank_holidays"]}
    sql = "SELECT * FROM source.fin_card_deposits WHERE sales_date = ?"
    args = [a["sales_date"]]
    if a["issuer"]:
        sql += " AND issuer = ?"
        args.append(a["issuer"])
    rows = []
    for d in _rows(conn, sql + " ORDER BY id", *args):
        cfg = mv["card_issuers"][d["issuer"]]
        approved = conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN cancel_of IS NULL THEN amount ELSE -amount END), 0)"
            " FROM source.fin_card_sales WHERE issuer = ? AND date(approved_at) = ?",
            (d["issuer"], d["sales_date"]),
        ).fetchone()[0]
        bank = conn.execute(
            "SELECT 1 FROM source.fin_bank_transactions WHERE direction = 'in' AND memo = ?"
            " AND date(occurred_at) = ? AND amount = ?",
            (d["issuer"], d["deposited_on"], d["amount"]),
        ).fetchone()
        rows.append(
            {
                "id": d["id"],
                "issuer": d["issuer"],
                "sales_date": d["sales_date"],
                "sales_total": d["sales_total"],
                "approved_total": approved,
                "fee": d["fee"],
                "expected_fee": int(approved * cfg["fee_rate"]),
                "amount": d["amount"],
                "deposited_on": d["deposited_on"],
                "expected_on": add_bank_days(
                    date.fromisoformat(d["sales_date"]), cfg["settle_days"], holidays
                ).isoformat(),
                "bank_in": bank is not None,
            }
        )
    who = f" {a['issuer']}" if a["issuer"] else ""
    return {"summary": f"{a['sales_date']}{who} 매출 정산 입금 {len(rows)}건", "rows": rows}


def _bank_tx(conn, a) -> dict:
    rows = _rows(
        conn,
        "SELECT id, occurred_at AS time, amount, memo FROM source.fin_bank_transactions"
        " WHERE direction = ? AND date(occurred_at) BETWEEN ? AND ? ORDER BY occurred_at, id",
        a["direction"],
        a["date_from"],
        a["date_to"],
    )
    kind = "입금" if a["direction"] == "in" else "출금"
    return {"summary": f"{a['date_from']}~{a['date_to']} 통장 {kind} {len(rows)}건", "rows": rows}


def _visits_day(conn, a) -> dict:
    rows = _rows(
        conn,
        "SELECT a.id AS appointment_id, a.patient_id, p.name, substr(a.scheduled_at, 12, 5) AS start,"
        " strftime('%H:%M', a.scheduled_at, '+' || a.duration_min || ' minutes') AS end, a.status,"
        " (SELECT group_concat(pr.name, ', ') FROM source.emr_treatments t"
        "  JOIN source.emr_procedures pr ON pr.code = t.procedure_code"
        "  WHERE t.appointment_id = a.id) AS procedures"
        " FROM source.emr_appointments a JOIN source.emr_patients p ON p.id = a.patient_id"
        " WHERE date(a.scheduled_at) = ? AND a.status != 'cancelled' ORDER BY a.scheduled_at, a.id",
        a["day"],
    )
    return {"summary": f"{a['day']} 내원·예약 {len(rows)}건", "rows": rows}


def _patient_balance(conn, a) -> dict:
    """그날 전 잔액(backend/query_tools.py _balance와 같은 식)과 남은 금액이 있는 수납 행"""
    pid, before = a["patient_id"], a["before"]
    balance = conn.execute(
        "SELECT COALESCE(SUM(amount_due - amount_paid), 0) FROM source.emr_payments"
        " WHERE patient_id = ? AND date(paid_at) < ?",
        (pid, before),
    ).fetchone()[0]
    rows = _rows(
        conn,
        "SELECT id AS payment_id, paid_at, kind, amount_due, amount_paid FROM source.emr_payments"
        " WHERE patient_id = ? AND date(paid_at) < ? AND amount_due > amount_paid"
        " ORDER BY paid_at DESC LIMIT ?",
        pid,
        before,
        BALANCE_ROWS,
    )
    return {
        "summary": f"환자 {pid} {before} 전 미수 {won(balance)}",
        "balance": balance,
        "rows": rows,
    }


def _family(conn, a) -> dict:
    p = conn.execute(
        "SELECT id, name, family_id, guardian_patient_id FROM source.emr_patients WHERE id = ?",
        (a["patient_id"],),
    ).fetchone()
    if p is None:
        raise ValueError(f"없는 환자: {a['patient_id']}")
    g = conn.execute(
        "SELECT id, name FROM source.emr_patients WHERE id = ?", (p["guardian_patient_id"],)
    ).fetchone()
    fam = (
        _rows(
            conn,
            "SELECT id, name FROM source.emr_patients WHERE family_id = ? AND id != ? ORDER BY id",
            p["family_id"],
            p["id"],
        )
        if p["family_id"]
        else []
    )
    return {
        "summary": f"{p['name']} 보호자 {g['name'] if g else '없음'} · 가족 {len(fam)}명",
        "patient": {"id": p["id"], "name": p["name"]},
        "guardian": dict(g) if g else None,
        "family": fam,
    }


def oral_combos(prices: dict[str, int], amount: int) -> list[dict[str, int]]:
    """amount가 구강용품 몇 개(ORAL_MAX_ITEMS개까지, 같은 품목 여러 개 포함)의 합으로 정확히 되는 조합 — 개수 적은 순
    ORAL_MAX_COMBOS개. 숫자 맞추기는 코드가 한다(기능 정의 §3 원칙 2)"""
    items = sorted(prices.items(), key=lambda kv: -kv[1])
    out: list[dict[str, int]] = []

    def walk(i: int, left: int, used: dict[str, int], n: int) -> None:
        if left == 0:
            out.append(dict(used))
            return
        if i == len(items) or n == ORAL_MAX_ITEMS:
            return
        name, price = items[i]
        for k in range(min(left // price, ORAL_MAX_ITEMS - n), -1, -1):
            if k:
                used[name] = k
            walk(i + 1, left - k * price, used, n + k)
            used.pop(name, None)

    walk(0, amount, {}, 0)
    return sorted(out, key=lambda c: (sum(c.values()), sorted(c.items())))[:ORAL_MAX_COMBOS]


def _oral_care_prices(conn, a) -> dict:
    prices = master_values(conn)["oral_care_prices"]
    if a["amount"] is None:
        return {"summary": f"구강용품 {len(prices)}가지", "prices": prices, "combos": None}
    combos = oral_combos(prices, a["amount"])
    shown = " / ".join(" + ".join(f"{n}×{k}" for n, k in c.items()) for c in combos[:2])
    return {
        "summary": f"{won(a['amount'])} 구강용품 " + (f"조합 {shown}" if combos else "조합 없음"),
        "prices": prices,
        "combos": combos,
    }


def _find_amount(conn, a) -> dict:
    """그 금액인 장부 행(받은 돈 또는 낼 돈)·카드 승인·통장 입금 — 날짜가 다른 같은 금액을 잇는다"""
    amt, lo, hi = a["amount"], a["date_from"], a["date_to"]
    rows = [
        {"table": "emr_payments"} | r
        for r in _rows(
            conn,
            "SELECT p.id, p.paid_at AS time, p.patient_id, pt.name, p.kind, p.amount_due,"
            " p.amount_paid, p.method FROM source.emr_payments p"
            " JOIN source.emr_patients pt ON pt.id = p.patient_id"
            " WHERE (p.amount_paid = ? OR p.amount_due = ?) AND date(p.paid_at) BETWEEN ? AND ?"
            " ORDER BY p.paid_at, p.id",
            amt,
            amt,
            lo,
            hi,
        )
    ]
    rows += [
        {"table": "fin_card_sales"} | r
        for r in _rows(
            conn,
            "SELECT id, approved_at AS time, issuer, amount, approval_no, cancel_of"
            " FROM source.fin_card_sales WHERE amount = ? AND date(approved_at) BETWEEN ? AND ?"
            " ORDER BY approved_at, id",
            amt,
            lo,
            hi,
        )
    ]
    rows += [
        {"table": "fin_bank_transactions"} | r
        for r in _rows(
            conn,
            "SELECT id, occurred_at AS time, amount, memo FROM source.fin_bank_transactions"
            " WHERE direction = 'in' AND amount = ? AND date(occurred_at) BETWEEN ? AND ?"
            " ORDER BY occurred_at, id",
            amt,
            lo,
            hi,
        )
    ]
    n = {
        t: sum(r["table"] == t for r in rows)
        for t in ("emr_payments", "fin_card_sales", "fin_bank_transactions")
    }
    return {
        "summary": f"{lo}~{hi} {won(amt)} — 장부 {n['emr_payments']} · 카드 승인 {n['fin_card_sales']}"
        f" · 통장 입금 {n['fin_bank_transactions']}",
        "rows": rows,
    }


_SPECS = {
    "ledger_day": (
        _Day,
        _ledger_day,
        "그날 장부 수납 전부(받은 금액 0원 진료 행 포함)와 이웃 진료일",
    ),
    "card_sales": (
        _CardSales,
        _card_sales,
        f"기간({CARD_SALES_DAYS}일 안)·금액 범위의 카드 승인(취소 포함)",
    ),
    "card_deposits": (
        _Deposits,
        _card_deposits,
        "매출일(·카드사)의 정산 입금과 기대값(승인 합계·수수료·예상 입금일)·통장 입금 여부",
    ),
    "bank_tx": (_BankTx, _bank_tx, f"기간({BANK_TX_DAYS}일 안)의 통장 입금 또는 출금"),
    "visits_day": (_Day, _visits_day, "그날 내원·예약(시작·끝 시각·시술)"),
    "patient_balance": (
        _Balance,
        _patient_balance,
        "환자의 그날 전 미수 잔액과 남은 금액이 있는 수납",
    ),
    "family": (_Patient, _family, "환자의 보호자·같은 가족"),
    "oral_care_prices": (
        _Oral,
        _oral_care_prices,
        "구강용품 가격표 — amount를 주면 그 금액이 구강용품 몇 개의 합으로 정확히 되는 조합(코드가 계산)",
    ),
    "find_amount": (
        _FindAmount,
        _find_amount,
        f"기간({FIND_AMOUNT_DAYS}일 안)에서 그 금액인 장부 행·카드 승인·통장 입금 — 다른 날에 적힌 같은 돈 찾기",
    ),
}


def tools(submit: str) -> list[Tool]:
    """조회 도구 9개. submit = 끝내는 도구 이름(조회 번호를 셀 때 뺀다)"""

    def wrap(fn):
        def run(ctx, args):
            conn: sqlite3.Connection = ctx.store.conn
            out = fn(conn, args)
            no = len(lookup_steps(ctx.store.steps(ctx.run_id), submit)) + 1
            return {"lookup_no": no} | out

        return run

    return [Tool(name, desc, args, wrap(fn)) for name, (args, fn, desc) in _SPECS.items()]


def input_text(item: dict, books: dict, today: date) -> str:
    """에이전트 입력 — 건의 날짜·결제수단, 관련 장부 행·금융 행, 합계와 차이. 규칙 추정 원인은 넣지 않는다"""
    pays = {p["id"]: p for p in books["payments"] + books["zero_visits"]}
    sales = {s["id"]: s for s in books["sales"]}
    deps = {d["id"]: d for d in books["deposits"]}
    bank = {b["id"]: b for b in books["bank_in"]}
    detail = {p["id"]: p for p in item["parts"] if p["table"] == "fin_card_deposits"}
    lines = [
        f"오늘: {today.isoformat()}",
        f"불일치 건 날짜: {', '.join(item['dates'])} · 결제수단: {item['channel']}",
        "",
        "장부 수납 행:",
    ]
    for pid in item["payment_ids"]:
        p = pays[pid]
        lines.append(
            f"- {p['paid_at'][:16]} 환자 {p['patient_id']} {p['name']} {p['kind']} · 낼 돈 {won(p['amount_due'])}"
            f" · 받은 돈 {won(p['amount_paid'])} · {p['method']}"
            + (f" · 승인번호 {p['approval_no']}" if p["approval_no"] else "")
        )
    if not item["payment_ids"]:
        lines.append("- 없음")
    lines += ["", "카드·통장 행:"]
    for r in item["fin"]:
        if r["table"] == "fin_card_sales":
            s = sales[r["id"]]
            lines.append(
                f"- 카드 승인 {s['approved_at'][:16]} {s['issuer']} {won(s['amount'])}"
                f" · 승인번호 {s['approval_no']}"
                + (f" · 취소(원승인 {s['cancel_of']})" if s["cancel_of"] else "")
            )
        elif r["table"] == "fin_card_deposits":
            d, x = deps[r["id"]], detail[r["id"]]
            lines.append(
                f"- 정산 입금 {d['deposited_on']} {d['issuer']} 매출일 {d['sales_date']} · 매출 {won(d['sales_total'])}"
                f" · 수수료 {won(d['fee'])} · 입금 {won(d['amount'])}"
                f" (승인 합계 {won(x['computed_total'])} · 기대 수수료 {won(x['computed_fee'])}"
                f" · 예상 입금일 {x['expected_on']})"
            )
        else:
            b = bank[r["id"]]
            lines.append(
                f"- 통장 입금 {b['occurred_at'][:16]} {won(b['amount'])} · 적요 {b['memo']}"
            )
    if not item["fin"]:
        lines.append("- 없음")
    diff = item["fin_total"] - item["ledger_total"]
    lines += [
        "",
        f"장부 합계 {won(item['ledger_total'])} · 카드·통장 합계 {won(item['fin_total'])}"
        f" · 차이(카드·통장 − 장부) {won(diff)}",
    ]
    return "\n".join(lines)
