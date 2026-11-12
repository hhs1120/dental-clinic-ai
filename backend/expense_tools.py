"""⑧-2 증빙 탐색 에이전트의 조회 도구 (기능 정의 §5 ⑧-2, 설계 docs/superpowers/specs/2026-11-11-finance-recon-design.md §4).
모두 읽기 전용이다 — 증빙 검색·증빙 본문·거래처 표·계정 과목 목록. 증빙은 실행 대상 출금(ctx.target = 출금 키,
backend/expense.py outflow_key)의 확정 후보 범위(expense._candidates — DOC_WINDOW 안 + 출금한 달 발행)만 본다 — 확정이
후보 밖 증빙을 막으므로 AI도 같은 범위에서 고른다. 결과마다 조회 번호(lookup_no)와 한 줄 요약(summary), 조회 인자
purpose·checked는 화면 줄로만(backend/recon_tools.py Note). 에이전트 입력 글(input_text)은 출금만 — 규칙 답(고른
증빙·과목·꼬리표)을 넣지 않는다(설계 §4)."""

import sqlite3
from datetime import date

from pydantic import Field

from backend import expense
from backend.harness.tools import Tool
from backend.recon_tools import Note, lookup_steps, won

LOOKUPS = ("search_docs", "read_doc", "vendors", "accounts")
SEARCH_ROWS = 30  # 검색 결과 상한 — 모델 입력 상한(AgentSpec.result_limit)을 넘지 않게
_SOURCE = {"fin_bank_transactions": "통장 출금", "fin_card_expenses": "병원 카드 사용"}


def outflow_by_key(conn: sqlite3.Connection, key: str) -> dict | None:
    """출금 키 → 출금(없으면 None)"""
    data = expense._load(conn, None)
    return next((o for o in data["outflows"] if expense.outflow_key(o) == key), None)


def _candidates(conn: sqlite3.Connection, key: str) -> list[dict]:
    data = expense._load(conn, None)
    o = next((o for o in data["outflows"] if expense.outflow_key(o) == key), None)
    if o is None:
        raise ValueError(f"출금이 아님: {key}")
    return expense._candidates(o, data)


class _Search(Note):
    issuer: str | None = Field(default=None, description="발행처 글자(부분 일치)")
    amount_min: int | None = None
    amount_max: int | None = None
    text: str | None = Field(default=None, description="본문에 들어 있는 글자")
    date_from: date | None = None
    date_to: date | None = None


class _Read(Note):
    document_id: int


def _search(conn, key: str, a: dict) -> dict:
    rows = [
        d
        for d in _candidates(conn, key)
        if (a["issuer"] is None or a["issuer"] in d["issuer"])
        and (a["amount_min"] is None or d["amount"] >= a["amount_min"])
        and (a["amount_max"] is None or d["amount"] <= a["amount_max"])
        and (a["text"] is None or a["text"] in d["body"])
        and (a["date_from"] is None or d["issued_on"] >= a["date_from"])
        and (a["date_to"] is None or d["issued_on"] <= a["date_to"])
    ]
    shown = [{k: d[k] for k in ("id", "kind", "issuer", "issued_on", "amount")} for d in rows]
    cond = " · ".join(
        f"{k} {v}"
        for k, v in (
            ("발행처", a["issuer"]),
            ("금액", _band(a["amount_min"], a["amount_max"])),
            ("본문", a["text"]),
            ("기간", _band(a["date_from"], a["date_to"])),
        )
        if v
    )
    return {
        "summary": f"증빙 검색{' (' + cond + ')' if cond else ''} {len(rows)}건",
        "rows": shown[:SEARCH_ROWS],
        "more": max(0, len(rows) - SEARCH_ROWS),
    }


def _band(lo, hi) -> str | None:
    if lo is None and hi is None:
        return None

    def fmt(v) -> str:
        return "" if v is None else (f"{v:,}" if isinstance(v, int) else str(v))

    return f"{fmt(lo)}~{fmt(hi)}"


def _read(conn, key: str, a: dict) -> dict:
    doc = next((d for d in _candidates(conn, key) if d["id"] == a["document_id"]), None)
    if doc is None:
        raise ValueError(f"이 출금의 후보 증빙이 아님: {a['document_id']}")
    return {
        "summary": f"증빙 {doc['id']} {doc['issuer']} {doc['issued_on']} {won(doc['amount'])} 본문",
        **expense._doc_view(doc),
    }


def _vendors(conn, key: str, a: dict) -> dict:
    vs = expense.master_values(conn)["expense_vendors"]
    rows = [{k: v.get(k) for k in ("name", "card_name", "pay", "account", "by_item")} for v in vs]
    return {"summary": f"거래처 {len(rows)}곳", "rows": rows}


def _accounts(conn, key: str, a: dict) -> dict:
    accounts = expense.master_values(conn)["expense_accounts"]
    return {"summary": f"계정 과목 {len(accounts)}개", "accounts": accounts}


_SPECS = {
    "search_docs": (
        _Search,
        _search,
        "이 출금의 후보 증빙(출금일 근처·출금한 달 발행) 검색 — 발행처 글자·금액 범위·본문 글자·발행일 기간. 본문은 빼고 보인다",
    ),
    "read_doc": (_Read, _read, "후보 증빙 하나의 본문(품목·금액) — 과목은 품목으로 고른다"),
    "vendors": (
        Note,
        _vendors,
        "거래처 표 — 이름·카드 내역 가맹점명·결제 방식(month_end_transfer = 월말에 그달 명세서 합계 이체)·기본 계정 과목·품목으로 정함",
    ),
    "accounts": (Note, _accounts, "계정 과목 목록 — 과목은 이 가운데 하나"),
}


def tools(submit: str) -> list[Tool]:
    """조회 도구 4개. submit = 끝내는 도구 이름(조회 번호를 셀 때 뺀다)"""

    def wrap(fn):
        def run(ctx, args):
            out = fn(ctx.store.conn, ctx.target, args)
            no = len(lookup_steps(ctx.store.steps(ctx.run_id), submit)) + 1
            return {"lookup_no": no} | out

        return run

    return [Tool(name, desc, args, wrap(fn)) for name, (args, fn, desc) in _SPECS.items()]


def input_text(o: dict) -> str:
    """에이전트 입력 — 출금 하나(출금일·출금처 글자 그대로·금액·통장/카드)"""
    return "\n".join(
        [
            f"출금: {o['at'][:16]} · {_SOURCE[o['table']]}",
            f"출금처: {o['label']}",
            f"금액: {won(o['amount'])}",
        ]
    )
