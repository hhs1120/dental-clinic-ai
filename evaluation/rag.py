"""③ RAG 검색·챗 채점 — 평가 설계 §3.2(채점 규칙)·§3.3(지표). 문항은 truth_rag_qa.
검색 지표는 질문 원문으로 검색기를 직접 한 번 호출한 순위(정답 버킷 목록 안), 챗 지표는 chat()의 반환값으로 센다.

앱 연결은 RagApp 두 함수 — eval.py가 앱을 같은 프로세스에서 호출한다(앱 착수 때 backend가 구현):
  search(question, condition) → {"manual_results": [청크 id], "case_results": [청크 id]}  (순위순, 앱 설계 §3)
  chat(question, qa_id, condition) → {"tool_calls": [{name, args}], "retrieved_ids": [...], "answer_text": str, "cited_ids": [...],
    "fallback_used"?: bool, "cost_usd"?: float}
  baseline(question, qa_id, condition) → {"retrieved_ids", "answer_text", "cited_ids", "refused", "cost_usd"?}  (단순 RAG 기준선, 있으면)
보고 지표(설계 2026-11-09 챗 "새 지표 셋"): 차트형 인용 정확도·공지형 현재 값·답 내용(사례형·매뉴얼형). 폴백이 섞인 문항은
챗 지표 모수에서 뺀다(평가 설계 §1 원칙 5).
"""

import json
import re
from typing import Protocol

from evaluation.metrics import (
    MAX_TOOL_CALLS,
    first_hit_rank,
    mcnemar_exact,
    number_match,
    numbers_in,
    source_id,
    tool_path,
)

SEARCH_TYPES = ("manual", "notice", "case")  # 문서 검색형 40 — Hit@5·MRR·Top-1·인용
PARTS = ("retrieval", "chat")
CONTENT_TYPES = ("manual", "case")  # 답 내용 — 청크 겹침 비교의 판단 지표
BASELINE_TYPES = (
    *SEARCH_TYPES,
    "refusal",
)  # 단순 RAG 기준선 짝 비교 모수(데이터형은 도구가 있어야 풀림)  # 검색 지표(검색기 직접 호출) / 챗 지표(chat 호출)
BUCKET = {
    "manual": "manual_results",
    "notice": "manual_results",
    "case": "case_results",
    "chart": "case_results",
}


class RagApp(Protocol):
    def search(self, question: str, condition: str | None = None) -> dict: ...

    def chat(self, question: str, qa_id: str, condition: str | None = None) -> dict: ...

    def baseline(self, question: str, qa_id: str, condition: str | None = None) -> dict: ...


def _questions(conn, split: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM truth.truth_rag_qa WHERE split = ? ORDER BY id", (split,)
    ).fetchall()
    return [
        dict(
            r,
            answer_doc_ids=json.loads(r["answer_doc_ids"]),
            expected_tools=json.loads(r["expected_tools"]),
            expected_args=json.loads(r["expected_args"]),
        )
        for r in rows
    ]


def _notice_pairs(conn) -> dict[str, str]:
    """최신 공지 → 그 옛 공지 — 최신성 채점(공지 대 그 옛 공지로만, 평가 설계 §3.1)"""
    return {
        f"doc:{r[0]}": f"doc:{r[1]}"
        for r in conn.execute(
            "SELECT d.id, m.supersedes_doc_id FROM source.ax_documents d"
            " JOIN truth.truth_doc_meta m ON m.doc_id = d.id"
            " WHERE d.type = 'notice' AND m.supersedes_doc_id IS NOT NULL"
        )
    }


def _fresh(ranked: list[str], answers: list[str], pairs: dict) -> bool:
    """정답 최신 공지가 그 옛 공지보다 위(옛 공지가 없거나 아래) — FAQ 등 다른 정답 문서의 순위는 보지 않는다"""
    for doc in (d for d in answers if d in pairs):
        new, old = first_hit_rank(ranked, {doc}), first_hit_rank(ranked, {pairs[doc]})
        if new is not None and (old is None or new < old):
            return True
    return False


def _latest_notices(conn) -> set[str]:
    """옛 공지를 대체한 최신 공지 — 공지형 인용은 이걸 인용해야 통과"""
    return {
        f"doc:{r[0]}"
        for r in conn.execute(
            "SELECT d.id FROM source.ax_documents d JOIN truth.truth_doc_meta m ON m.doc_id = d.id"
            " WHERE d.type = 'notice' AND m.supersedes_doc_id IS NOT NULL"
        )
    }


def _fact_words(conn) -> dict[str, list[str]]:
    """심은 사실 id → 본문에 그대로 있는 말(truth_doc_meta.planted_facts)"""
    out = {}
    for (facts,) in conn.execute("SELECT planted_facts FROM truth.truth_doc_meta"):
        for f in json.loads(facts):
            out[f["id"]] = f["words"]
    return out


def _flat(text: str) -> str:
    return re.sub(r"[\s,]", "", text)


def _has_content(q: dict, answer: str, words: dict) -> bool | None:
    """답 내용 — 사실 id 힌트면 그 words 중 하나, 본문 구절이면 그 구절(공백·쉼표 제거)이 답에 있나. 힌트 없으면 None"""
    hint = q.get("answer_chunk_hint")
    if not hint:
        return None
    if hint in words:
        return any(_flat(w) in _flat(answer) for w in words[hint])
    return _flat(hint) in _flat(answer)


def _baseline_log(b: dict) -> dict:
    """기준선 답 → 챗 로그 모양(거절은 마지막 호출 refuse로) — 같은 판정 함수로 채점"""
    return {
        "tool_calls": [{"name": "refuse" if b["refused"] else "search_docs", "args": {}}],
        "retrieved_ids": b["retrieved_ids"],
        "answer_text": b["answer_text"],
        "cited_ids": [] if b["refused"] else b["cited_ids"],
    }


def _chat_fail(q: dict, log: dict, latest: set) -> str | None:
    """챗 1문항 판정 — 통과면 None (평가 설계 §3.2·§3.3)"""
    calls = log["tool_calls"]
    last = calls[-1]["name"] if calls else None
    if len(calls) > MAX_TOOL_CALLS:
        return f"호출 {MAX_TOOL_CALLS}회 초과"
    if q["type"] == "refusal":
        return None if last == "refuse" else "거절 안 함"
    if last == "refuse":
        return "거절"  # 과잉 거절은 그 유형 정답률에 반영
    if q["type"] == "data":
        why = tool_path(calls, q["expected_tools"], q["expected_args"])
        (value,) = numbers_in(
            q["answer_text"]
        )  # verify가 같은 함수로 숫자 하나를 보장(check_qa_answer_sql)
        return why or (None if number_match(log["answer_text"], value) else "숫자")
    cited, answers = set(log["cited_ids"]), set(q["answer_doc_ids"])
    sources = {source_id(c) for c in cited}
    return (
        "인용 없음"
        if not cited
        else "검색 안 한 청크 인용"
        if not cited <= set(log["retrieved_ids"])
        else "정답 인용 없음"
        if not sources & answers
        else "최신 공지 인용 없음"
        if q["type"] == "notice" and not sources & answers & latest
        else None
    )


def _rate(qs: list[dict], field: str) -> dict:
    return {"k": sum(bool(q[field]) for q in qs if field in q), "n": sum(field in q for q in qs)}


def _baseline_summary(base_q: list[dict], out_q: list[dict], cost: float) -> dict:
    """단순 RAG 기준선 vs 챗 — 문서 검색형 인용·거절형 환각 억제, 구제(기준선 실패 → 챗 통과)/손실 (평가 설계 §3.3)"""
    chat_q = [q for q in out_q if any(b["id"] == q["id"] for b in base_q)]
    cmp = compare(base_q, chat_q)
    return {
        "citation": _rate([q for q in base_q if q["type"] in SEARCH_TYPES], "chat_ok"),
        "refusal": _rate([q for q in base_q if q["type"] == "refusal"], "chat_ok"),
        "compare": {"rescued": cmp["rescued"], "lost": cmp["lost"]},
        "cost_usd": round(cost, 4),
        "calls": len(base_q),
    }


def evaluate(
    conn, app: RagApp, split: str, condition: str | None = None, parts=PARTS, questions=None
) -> dict:
    """parts = 돌릴 묶음(PARTS 부분집합). 안 돌린 묶음의 지표는 None. questions를 주면 split 대신 그 문항을 쓴다"""
    latest, pairs = _latest_notices(conn), _notice_pairs(conn)
    words = _fact_words(conn) if "chat" in parts else {}
    has_baseline = "chat" in parts and callable(getattr(app, "baseline", None))
    out_q, base_q, cost = [], [], {"chat": 0.0, "baseline": 0.0, "calls": 0}
    for q in questions if questions is not None else _questions(conn, split):
        r = {"id": q["id"], "type": q["type"], "question": q["question"]}
        if "retrieval" in parts and q["type"] in BUCKET:
            ranked = app.search(q["question"], condition)[BUCKET[q["type"]]]
            rank = first_hit_rank(ranked, set(q["answer_doc_ids"]))
            r.update(rank=rank, hit=rank is not None, rr=1 / rank if rank else 0.0, top1=rank == 1)
            if q["type"] == "notice":
                r["fresh"] = _fresh(ranked, q["answer_doc_ids"], pairs)
        if "chat" in parts:
            log = app.chat(q["question"], q["id"], condition)
            if log.get("fallback_used"):
                r["chat_fail"] = "폴백 — 집계 제외"
                r["excluded"] = True
            else:
                r["chat_fail"] = _chat_fail(q, log, latest)
                r["chat_ok"] = r["chat_fail"] is None
                if q["type"] == "notice":
                    r["notice_value"] = bool(_has_content(q, log["answer_text"], words))
                content = _has_content(q, log["answer_text"], words)
                if q["type"] in CONTENT_TYPES and content is not None:
                    r["answer_content"] = content
                cost["chat"] += log.get("cost_usd") or 0.0
                cost["calls"] += len(log["tool_calls"])
            if has_baseline and q["type"] in BASELINE_TYPES and not r.get("excluded"):
                b = app.baseline(q["question"], q["id"], condition)
                cost["baseline"] += b.get("cost_usd") or 0.0
                ok = _chat_fail(q, _baseline_log(b), latest) is None
                base_q.append({"id": q["id"], "type": q["type"], "chat_ok": ok})
        out_q.append(r)
    search = [q for q in out_q if q["type"] in SEARCH_TYPES]
    chart = [q for q in out_q if q["type"] == "chart"]
    retrieval = "retrieval" in parts
    out = {
        "condition": condition,
        "retrieval": {
            "hit5": _rate(search, "hit"),
            "mrr": _mean(search, "rr"),
            "top1": _rate(search, "top1"),
            "fresh": _rate([q for q in search if q["type"] == "notice"], "fresh"),
        }
        if retrieval
        else None,
        "chart": {"hit5": _rate(chart, "hit"), "mrr": _mean(chart, "rr")} if retrieval else None,
        "chat": None
        if "chat" not in parts
        else {
            "citation": _rate(search, "chat_ok"),
            "tool": _rate([q for q in out_q if q["type"] == "data"], "chat_ok"),
            "refusal": _rate([q for q in out_q if q["type"] == "refusal"], "chat_ok"),
            "chart_citation": _rate([q for q in chart if "chat_ok" in q], "chat_ok"),
            "notice_value": _rate(out_q, "notice_value"),
            "answer_content": _rate(out_q, "answer_content"),
            "excluded_fallback": sum(bool(q.get("excluded")) for q in out_q),
            "cost_usd": round(cost["chat"], 4),
            "tool_calls": cost["calls"],
            "baseline": _baseline_summary(base_q, out_q, cost["baseline"])
            if has_baseline
            else None,
        },
        "questions": out_q,
    }
    return out


def _mean(qs: list[dict], field: str) -> float | None:
    return sum(q[field] for q in qs) / len(qs) if qs else None


PAIRED_FIELDS = (
    "hit",
    "top1",
    "fresh",
    "rr",
    "chat_ok",
    "notice_value",
    "answer_content",
)  # 짝 비교에 쓰는 문항 칸 — test json에는 이것만 남긴다


def paired(result: dict) -> list[dict]:
    """문항별 통과 여부만(질문 원문·순위·실패 이유 없이) — test에서 기본 조건 결과와 짝 비교하려고 남긴다"""
    return [
        {k: q[k] for k in ("id", "type") + PAIRED_FIELDS if k in q} for q in result["questions"]
    ]


# 짝 비교 지표 → (대상 유형, 문항 칸). 평균 지표(MRR)는 McNemar 없이 평균만
MEANS = {"mrr": (SEARCH_TYPES, "rr"), "chart_mrr": (("chart",), "rr")}
COMPARE = {
    "hit5": (SEARCH_TYPES, "hit"),
    "top1": (SEARCH_TYPES, "top1"),
    "fresh": (("notice",), "fresh"),
    "chart_hit5": (("chart",), "hit"),
    "citation": (SEARCH_TYPES, "chat_ok"),
    "tool": (("data",), "chat_ok"),
    "refusal": (("refusal",), "chat_ok"),
    "chart_citation": (("chart",), "chat_ok"),
    "notice_value": (("notice",), "notice_value"),
    "answer_content": (CONTENT_TYPES, "answer_content"),
}


def compare(base: list[dict], cond: list[dict]) -> dict:
    """같은 문항에 두 조건(문항 목록 — evaluate의 questions 또는 paired) — 분수 + McNemar p, MRR은 평균 짝,
    구제(기본 실패 → 조건 통과)/손실 문항 (평가 설계 §3.3)"""
    b = {q["id"]: q for q in base}
    c = {q["id"]: q for q in cond}
    out = {"metrics": {}, "rescued": {}, "lost": {}}
    for name, (types, field) in MEANS.items():
        ids = sorted(
            i
            for i in b.keys() & c.keys()
            if b[i]["type"] in types and field in b[i] and field in c[i]
        )
        if ids:
            out["metrics"][name] = {
                "base": sum(b[i][field] for i in ids) / len(ids),
                "cond": sum(c[i][field] for i in ids) / len(ids),
                "n": len(ids),
            }
    for name, (types, field) in COMPARE.items():
        ids = sorted(
            i
            for i in b.keys() & c.keys()
            if b[i]["type"] in types and field in b[i] and field in c[i]
        )
        if not ids:
            continue
        rescued = [i for i in ids if c[i][field] and not b[i][field]]
        lost = [i for i in ids if b[i][field] and not c[i][field]]
        out["metrics"][name] = {
            "base": sum(bool(b[i][field]) for i in ids),
            "cond": sum(bool(c[i][field]) for i in ids),
            "n": len(ids),
            "p": mcnemar_exact(len(lost), len(rescued)),
        }
        out["rescued"][name], out["lost"][name] = rescued, lost
    return out
