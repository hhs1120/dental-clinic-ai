"""③ 기록 검색 AI 요약 (기능 정의 §4 ③ 기록 검색, 설계 docs/superpowers/specs/2026-11-09-search-summary-docs-design.md).
검색 결과가 무엇을 말하는지 몇 문장으로 묶고 문장마다 끝에 출처 번호 [n](= cited_ids[n-1])을 붙인다. 하네스 없이 게이트웨이
호출 한 번이고, 인용·문장 번호·숫자 검사에 어긋나면 한 번 다시 부른다. 두 번째도 어긋나면 고정 문구로 끝낸다. 옛 공지는
입력에서 뺀다. 같은 검색 인자·같은 결과·같은 프롬프트 버전이면 끝난 행을 다시 쓴다. 검색 요약 1회 = ax_search_logs 1행."""

import json
import re
import sqlite3
from datetime import datetime

from backend import prompts
from backend.harness.loop import external
from backend.harness.store import count_block
from backend.rag_search import SearchArgs
from backend.search_cards import chart_plain
from backend.usage import record_voyage
from backend.verify import allowed_numbers, numbers_ok
from shared.llm import BudgetExceeded

TASK, ALIAS = "search_summary", "chat"
PROMPT_VERSION = "v1"
SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "cited_ids": {"type": "array", "items": {"type": "string"}},
        "refused": {"type": "boolean"},
    },
    "required": ["text", "cited_ids", "refused"],
    "additionalProperties": False,
}
REFUSED_TEXT = "검색 결과에서 정리할 내용을 찾지 못했습니다"
FAILED_TEXT = "요약을 만들지 못했습니다"
CITE_OUTSIDE = "search_summary.cite_outside"
CITE_MISSING = "search_summary.cite_missing"
NUMBER = "search_summary.number_unverified"
STATUS = {CITE_OUTSIDE: "cite_failed", CITE_MISSING: "cite_failed", NUMBER: "number_failed"}
REASON = {
    CITE_OUTSIDE: "cited_ids가 이번 검색 결과 밖이거나, 본문 번호가 cited_ids 범위 밖이거나, 쓰이지 않은 출처가 있다.",
    CITE_MISSING: "끝에 번호가 없는 문장이 있다.",
    NUMBER: "검색 결과에 없는 숫자가 있다.",
}
RETRY_HINT = " 이번 검색 결과만 인용하고, 모든 문장 끝에 cited_ids 순번 [n]을 붙이고, 숫자는 결과에 있는 그대로 쓴다."
MARK = re.compile(r"\[(\d+)\]")
# 줄바꿈, 또는 . ? ! ] 뒤 공백에서 나눈다 — 숫자 뒤·마침표 뒤 마침표('2026. 3. 1.', '...')에서는 나누지 않는다
SENTENCES = re.compile(r"\n+|(?<=[.?!\]])(?<![\d.]\.)\s+")
ENDS_MARKED = re.compile(r"(?:\[\d+\])+[.?!]?$")  # 문장 끝 번호 — '…다[1].' 또는 '…다.[1]'
# 마침표 뒤에 띄우고 붙인 번호('…있습니다. [1]')는 앞 문장의 번호다 — 실측 첫 응답 5건 중 4건이 이 모양
LOOSE_MARKS = re.compile(r"([.?!])\s+((?:\[\d+\])+)")


def strip_marks(text: str) -> str:
    return MARK.sub("", text)


def tidy(text: str) -> str:
    """'…다. [1]' → '…다.[1]' — 검사와 화면이 같은 모양을 본다"""
    return LOOSE_MARKS.sub(r"\1\2", text.strip())


def check(text: str, cited: list[str], input_ids: list[str], sources: list[str]) -> str | None:
    """요약 검사 — 어긋난 규칙 id 또는 None(refused가 아닐 때만 부른다)"""
    text = tidy(text)
    marks = {int(n) for n in MARK.findall(text)}
    # 번호는 1..len(cited)를 빠짐없이 — 범위 밖 번호도, 본문에 안 쓰인 출처도 막는다
    if not cited or not set(cited) <= set(input_ids) or marks != set(range(1, len(cited) + 1)):
        return CITE_OUTSIDE
    if any(not ENDS_MARKED.search(s.strip()) for s in SENTENCES.split(text) if s.strip()):
        return CITE_MISSING
    if not numbers_ok(strip_marks(text), allowed_numbers(sources)):
        return NUMBER
    return None


def _doc_id(chunk_id: str) -> int | None:
    kind, *rest = chunk_id.split(":")
    return int(rest[0]) if kind == "doc" else None


def _key(args: SearchArgs) -> str:
    return json.dumps(
        [
            {
                "name": "search",
                "args": args.model_dump(mode="json"),
                "prompt_version": PROMPT_VERSION,
            }
        ],
        ensure_ascii=False,
        sort_keys=True,
    )


def _result(status, text, cited, input_ids, log_id, reused, cost, fallback) -> dict:
    return {
        "status": status,
        "text": text,
        "cited_ids": cited,
        "retrieved_ids": input_ids,
        "log_id": log_id,
        "reused": reused,
        "cost_usd": cost,
        "fallback_used": fallback,
    }


def _shown(status: str, text: str) -> str:
    return text if status == "answered" else REFUSED_TEXT if status == "refused" else FAILED_TEXT


def summarize(
    conn: sqlite3.Connection, gw, searcher, args: SearchArgs, *, is_eval: bool = False
) -> dict:
    """검색 1회 → {status, text, cited_ids, retrieved_ids, log_id, reused, cost_usd, fallback_used}.
    text는 answered면 모델 본문, refused면 REFUSED_TEXT, 그 밖은 FAILED_TEXT"""
    found = searcher.search(**args.model_dump(mode="json"))
    chunks = [
        chart_plain(conn, c)
        for b in found.values()
        for c in b
        if _doc_id(c["id"]) not in searcher.replaced_by
    ]
    input_ids = [c["id"] for c in chunks]
    cost = record_voyage(conn, searcher.drain_usage(), is_eval=is_eval, ref=None)
    conn.commit()
    if not chunks:  # 옛 공지뿐 — 부르지 않고 기록도 남기지 않는다
        return _result("refused", REFUSED_TEXT, [], [], None, False, cost, False)

    key, retrieved = _key(args), json.dumps(input_ids, ensure_ascii=False)
    if not is_eval:
        row = conn.execute(
            "SELECT id, status, answer_text, cited_ids FROM ax_search_logs"
            " WHERE kind = 'search_summary' AND is_eval = 0 AND status IN ('answered', 'refused')"
            " AND tool_calls = ? AND retrieved_ids = ? ORDER BY id DESC LIMIT 1",
            (key, retrieved),
        ).fetchone()
        if row:
            return _result(
                row[1],
                _shown(row[1], row[2] or ""),
                json.loads(row[3]),
                input_ids,
                row[0],
                True,
                cost,
                False,
            )

    sources = [args.query] + [
        str(v) for v in (args.date_from, args.date_to, args.patient_chart_no) if v
    ]
    sources += [c["text"] for c in chunks]
    messages = [
        {
            "role": "user",
            "content": f"검색어: {args.query}\n\n검색 결과:\n"
            + "\n\n".join(external(c["id"], c["text"]) for c in chunks),
        }
    ]
    status, text, cited, fallback = "failed", "", [], False
    try:
        for _ in range(2):  # 처음 + 한 번 다시
            r = gw.complete(
                ALIAS,
                task=TASK,
                system=prompts.load(TASK, PROMPT_VERSION)["system"],
                messages=messages,
                schema=SCHEMA,
                prompt_version=PROMPT_VERSION,
            )
            cost += r.cost_usd
            fallback |= r.fallback_used
            if not r.ok or not r.data:
                status = "failed"
                break
            data = r.data
            if data["refused"]:
                status = "refused"
                break
            rule = check(data["text"], data["cited_ids"], input_ids, sources)
            if rule is None:
                status, text, cited = "answered", tidy(data["text"]), data["cited_ids"]
                break
            with conn:  # 바로 커밋 — 다시 부르는 동안 쓰기 잠금을 쥐지 않는다
                count_block(conn, rule)
            status = STATUS[rule]
            messages = messages + [
                {"role": "assistant", "content": json.dumps(data, ensure_ascii=False)},
                {"role": "user", "content": REASON[rule] + RETRY_HINT},
            ]
    except BudgetExceeded:
        status = "budget"
    except Exception:  # 챗처럼 실패 행을 남기고 다시 던진다
        conn.rollback()
        _log(conn, args.query, key, retrieved, "", [], "failed", fallback, is_eval)
        raise
    log_id = _log(conn, args.query, key, retrieved, text, cited, status, fallback, is_eval)
    return _result(status, _shown(status, text), cited, input_ids, log_id, False, cost, fallback)


def _log(conn, question, key, retrieved, text, cited, status, fallback, is_eval) -> int:
    now = datetime.now().isoformat(sep=" ", timespec="seconds")
    log_id = conn.execute(
        "INSERT INTO ax_search_logs (kind, question, tool_calls, retrieved_ids, answer_text,"
        " cited_ids, status, fallback_used, is_eval, created_at)"
        " VALUES ('search_summary', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            question,
            key,
            retrieved,
            text or None,
            json.dumps(cited, ensure_ascii=False),
            status,
            int(fallback),
            int(is_eval),
            now,
        ),
    ).lastrowid
    conn.commit()
    return log_id
