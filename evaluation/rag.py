"""③ RAG 검색·챗 채점 — 평가 설계 §3.2(채점 규칙)·§3.3(지표). 문항은 truth_rag_qa.
검색 지표는 질문 원문으로 검색기를 직접 한 번 호출한 순위(정답 버킷 목록 안), 챗 지표는 챗 로그로 센다.

앱 연결은 RagApp 두 함수 — eval.py가 앱을 같은 프로세스에서 호출한다(앱 착수 때 backend가 구현):
  search(question, condition) → {"manual_results": [청크 id], "case_results": [청크 id]}  (순위순, 앱 설계 §3)
  chat(question, qa_id, condition) → {"tool_calls": [{name, args}], "retrieved_ids": [...], "answer_text": str, "cited_ids": [...]}
"""
import json
from typing import Protocol

from evaluation.metrics import MAX_TOOL_CALLS, first_hit_rank, mcnemar_exact, number_match, numbers_in, source_id, tool_path

SEARCH_TYPES = ("manual", "notice", "case")  # 문서 검색형 40 — Hit@5·MRR·Top-1·인용
PARTS = ("retrieval", "chat")  # 검색 지표(검색기 직접 호출) / 챗 지표(chat 호출)
BUCKET = {"manual": "manual_results", "notice": "manual_results", "case": "case_results", "chart": "case_results"}


class RagApp(Protocol):
    def search(self, question: str, condition: str | None = None) -> dict: ...

    def chat(self, question: str, qa_id: str, condition: str | None = None) -> dict: ...


def _questions(conn, split: str) -> list[dict]:
    rows = conn.execute("SELECT * FROM truth.truth_rag_qa WHERE split = ? ORDER BY id", (split,)).fetchall()
    return [dict(r, answer_doc_ids=json.loads(r["answer_doc_ids"]), expected_tools=json.loads(r["expected_tools"]),
                 expected_args=json.loads(r["expected_args"])) for r in rows]


def _latest_notices(conn) -> set[str]:
    """옛 공지를 대체한 최신 공지 — 공지형 인용은 이걸 인용해야 통과"""
    return {f"doc:{r[0]}" for r in conn.execute("SELECT d.id FROM ax_documents d JOIN truth.truth_doc_meta m ON m.doc_id = d.id"
                                                " WHERE d.type = 'notice' AND m.supersedes_doc_id IS NOT NULL")}


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
        (value,) = numbers_in(q["answer_text"])  # verify가 숫자 하나를 보장
        return why or (None if number_match(log["answer_text"], value) else "숫자")
    cited, answers = set(log["cited_ids"]), set(q["answer_doc_ids"])
    sources = {source_id(c) for c in cited}
    return ("인용 없음" if not cited else "검색 안 한 청크 인용" if not cited <= set(log["retrieved_ids"])
            else "정답 인용 없음" if not sources & answers
            else "최신 공지 인용 없음" if q["type"] == "notice" and not sources & answers & latest else None)


def _rate(qs: list[dict], field: str) -> dict:
    return {"k": sum(bool(q[field]) for q in qs), "n": len(qs)}


def evaluate(conn, app: RagApp, split: str, condition: str | None = None, parts=PARTS) -> dict:
    """parts = 돌릴 묶음(PARTS 부분집합). 안 돌린 묶음의 지표는 None"""
    latest = _latest_notices(conn)
    out_q = []
    for q in _questions(conn, split):
        r = {"id": q["id"], "type": q["type"], "question": q["question"]}
        if "retrieval" in parts and q["type"] in BUCKET:
            ranked = app.search(q["question"], condition)[BUCKET[q["type"]]]
            rank = first_hit_rank(ranked, set(q["answer_doc_ids"]))
            r.update(rank=rank, hit=rank is not None, rr=1 / rank if rank else 0.0, top1=rank == 1)
            if q["type"] == "notice":
                r["fresh"] = rank == 1  # 1위 청크가 정답 문서(옛 공지가 아님)
        if "chat" in parts and q["type"] != "chart":
            r["chat_fail"] = _chat_fail(q, app.chat(q["question"], q["id"], condition), latest)
            r["chat_ok"] = r["chat_fail"] is None
        out_q.append(r)
    search = [q for q in out_q if q["type"] in SEARCH_TYPES]
    chart = [q for q in out_q if q["type"] == "chart"]
    retrieval = "retrieval" in parts
    out = {
        "condition": condition,
        "retrieval": {"hit5": _rate(search, "hit"), "mrr": _mean(search, "rr"), "top1": _rate(search, "top1"),
                      "fresh": _rate([q for q in search if q["type"] == "notice"], "fresh")} if retrieval else None,
        "chart": {"hit5": _rate(chart, "hit"), "mrr": _mean(chart, "rr")} if retrieval else None,
        "chat": None if "chat" not in parts else {
            "citation": _rate(search, "chat_ok"), "tool": _rate([q for q in out_q if q["type"] == "data"], "chat_ok"),
            "refusal": _rate([q for q in out_q if q["type"] == "refusal"], "chat_ok")},
        "questions": out_q,
    }
    return out


def _mean(qs: list[dict], field: str) -> float | None:
    return sum(q[field] for q in qs) / len(qs) if qs else None


PAIRED_FIELDS = ("hit", "top1", "fresh", "rr", "chat_ok")  # 짝 비교에 쓰는 문항 칸 — test json에는 이것만 남긴다


def paired(result: dict) -> list[dict]:
    """문항별 통과 여부만(질문 원문·순위·실패 이유 없이) — test에서 기본 조건 결과와 짝 비교하려고 남긴다"""
    return [{k: q[k] for k in ("id", "type") + PAIRED_FIELDS if k in q} for q in result["questions"]]


# 짝 비교 지표 → (대상 유형, 문항 칸). 평균 지표(MRR)는 McNemar 없이 평균만
MEANS = {"mrr": (SEARCH_TYPES, "rr"), "chart_mrr": (("chart",), "rr")}
COMPARE = {"hit5": (SEARCH_TYPES, "hit"), "top1": (SEARCH_TYPES, "top1"), "fresh": (("notice",), "fresh"),
           "chart_hit5": (("chart",), "hit"), "citation": (SEARCH_TYPES, "chat_ok"), "tool": (("data",), "chat_ok"),
           "refusal": (("refusal",), "chat_ok")}


def compare(base: list[dict], cond: list[dict]) -> dict:
    """같은 문항에 두 조건(문항 목록 — evaluate의 questions 또는 paired) — 분수 + McNemar p, MRR은 평균 짝,
    구제(기본 실패 → 조건 통과)/손실 문항 (평가 설계 §3.3)"""
    b = {q["id"]: q for q in base}
    c = {q["id"]: q for q in cond}
    out = {"metrics": {}, "rescued": {}, "lost": {}}
    for name, (types, field) in MEANS.items():
        ids = sorted(i for i in b.keys() & c.keys() if b[i]["type"] in types and field in b[i] and field in c[i])
        if ids:
            out["metrics"][name] = {"base": sum(b[i][field] for i in ids) / len(ids),
                                    "cond": sum(c[i][field] for i in ids) / len(ids), "n": len(ids)}
    for name, (types, field) in COMPARE.items():
        ids = sorted(i for i in b.keys() & c.keys() if b[i]["type"] in types and field in b[i] and field in c[i])
        if not ids:
            continue
        rescued = [i for i in ids if c[i][field] and not b[i][field]]
        lost = [i for i in ids if b[i][field] and not c[i][field]]
        out["metrics"][name] = {"base": sum(bool(b[i][field]) for i in ids), "cond": sum(bool(c[i][field]) for i in ids),
                                "n": len(ids), "p": mcnemar_exact(len(lost), len(rescued))}
        out["rescued"][name], out["lost"][name] = rescued, lost
    return out
