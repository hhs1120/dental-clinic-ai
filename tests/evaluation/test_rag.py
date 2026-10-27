"""RAG 채점 (평가 설계 §3.2·§3.3) — 검색 지표는 검색기 직접 호출 순위(버킷 목록 안), 챗 지표는 챗 로그."""
import json

import pytest

from evaluation import rag
from generator.build import insert_rag_qa
from tests.evaluation.eval_db import make_db

# 문서: 1·2 매뉴얼 / 3 최신 공지(4를 대체) / 4 옛 공지 / 5 FAQ(3과 같은 사실)
DOCS = [(1, "manual", "core", None, None), (2, "manual", "core", None, None), (3, "notice", "core", None, 4),
        (4, "notice", "old_version", 3, None), (5, "faq", "core", None, None)]


def _q(id, type, split, ids=(), tools=("search_docs",), args=None, answer="답"):
    return {"id": id, "type": type, "question": f"질문 {id}", "answer_text": answer, "answer_doc_ids": list(ids),
            "answer_chunk_hint": None, "expected_tools": list(tools), "expected_args": args or {},
            "expect_refusal": type == "refusal", "split": split, "answer_sql": "SELECT 1" if type in ("data", "chart") else None}


QA = [
    _q("m1", "manual", "dev", ["doc:1"]),
    _q("n1", "notice", "dev", ["doc:3", "doc:5"]),
    _q("c1", "case", "dev", ["call:1"]),
    _q("h1", "chart", "dev", ["chart:7"]),
    _q("d1", "data", "dev", tools=("query_schedule",), args={"query_schedule": {"date_from": "2026-12-01", "date_to": "2026-12-05"}},
       answer="2건"),
    _q("r1", "refusal", "dev", tools=("search_docs", "refuse")),
    _q("m2", "manual", "test", ["doc:2"]),
]

SEARCH = {  # 질문 → 검색기 결과(버킷별)
    "m1": {"manual_results": ["doc:2:0", "doc:1:0"], "case_results": ["call:1:0"]},
    "n1": {"manual_results": ["doc:4:0", "doc:3:1"], "case_results": []},
    "c1": {"manual_results": ["doc:1:0"], "case_results": ["call:1:0"]},
    "h1": {"manual_results": [], "case_results": ["chart:9", "chart:7"]},
    "m2": {"manual_results": ["doc:2:0"], "case_results": []},
}


def _log(tools, retrieved=(), cited=(), answer="답"):
    return {"tool_calls": [{"name": n, "args": a} for n, a in tools], "retrieved_ids": list(retrieved),
            "answer_text": answer, "cited_ids": list(cited)}


CHAT = {
    "m1": _log([("search_docs", {})], ["doc:1:0", "doc:2:0"], ["doc:1:0"]),
    "n1": _log([("search_docs", {})], ["doc:5:0", "doc:3:0"], ["doc:5:0"]),  # 최신 공지를 인용 안 함 → 실패
    "c1": _log([("search_docs", {})], [], ["call:1:0"]),  # 검색 안 한 청크 인용 → 실패
    "h1": _log([("search_docs", {})], ["chart:7"], ["chart:7"]),
    "d1": _log([("query_schedule", {"date_from": "2026-11-30", "date_to": "2026-12-06"})], answer="2건이에요"),
    "r1": _log([("search_docs", {}), ("refuse", {"reason": "restricted"})]),
    "m2": _log([("search_docs", {})], ["doc:2:0"], ["doc:2:0"]),
}


class FakeApp:
    def __init__(self, search=SEARCH, chat=CHAT):
        self.search_map, self.chat_map, self.calls = search, chat, []

    def search(self, question, condition=None):
        self.calls.append(("search", question, condition))
        return self.search_map[question.removeprefix("질문 ")]

    def chat(self, question, qa_id, condition=None):
        self.calls.append(("chat", qa_id, condition))
        return self.chat_map[qa_id]


@pytest.fixture
def conn(tmp_path):
    c = make_db(tmp_path)
    for doc_id, type, kind, pair, sup in DOCS:
        c.execute("INSERT INTO ax_documents VALUES (?, ?, ?, '1', NULL, ?, '본문')", (doc_id, type, f"문서 {doc_id}",
                                                                                  f"docs/{type}/{doc_id:03d}.md"))
        c.execute("INSERT INTO truth.truth_doc_meta VALUES (?, ?, ?, ?, ?, ?, '[]', '[]')",
                  (doc_id, f"key-{doc_id}", kind, int(kind != "core"), pair, sup))
    insert_rag_qa(c, QA, schema="truth")
    return c


def test_retrieval_metrics_use_bucket_rank(conn):
    """문서 검색형 dev(m1·n1·c1): Hit@5 3/3, MRR (1/2 + 1/2 + 1) / 3, Top-1 1/3, 최신성 0/1(1위가 옛 공지)"""
    r = rag.evaluate(conn, FakeApp(), "dev", parts=("retrieval",))
    assert (r["retrieval"]["hit5"]["k"], r["retrieval"]["hit5"]["n"]) == (3, 3)
    assert r["retrieval"]["mrr"] == pytest.approx(2 / 3)
    assert (r["retrieval"]["top1"]["k"], r["retrieval"]["fresh"]["k"], r["retrieval"]["fresh"]["n"]) == (1, 0, 1)
    assert (r["chart"]["hit5"]["k"], r["chart"]["hit5"]["n"], r["chart"]["mrr"]) == (1, 1, 0.5)
    assert r["chat"] is None


def test_freshness_true_and_thread_answer(conn):
    """최신 공지가 1위면 최신성 통과 / 사례형 정답이 문자 스레드여도 case_results에서 찾는다"""
    conn.execute("UPDATE truth.truth_rag_qa SET answer_doc_ids = '[\"thread:C-00001:2026-11\"]' WHERE id = 'c1'")
    search = dict(SEARCH, n1={"manual_results": ["doc:3:0", "doc:4:0"], "case_results": []},
                  c1={"manual_results": [], "case_results": ["call:1:0", "thread:C-00001:2026-11"]})
    r = rag.evaluate(conn, FakeApp(search=search), "dev", parts=("retrieval",))
    q = {x["id"]: x for x in r["questions"]}
    assert q["n1"]["fresh"] and q["c1"]["rank"] == 2


def test_chat_only_calls_no_search(conn):
    app = FakeApp()
    r = rag.evaluate(conn, app, "dev", parts=("chat",))
    assert all(kind == "chat" for kind, *_ in app.calls) and r["retrieval"] is None and r["chat"]["tool"]["n"] == 1


def test_retrieval_only_calls_no_chat(conn):
    app = FakeApp()
    rag.evaluate(conn, app, "dev", parts=("retrieval",))
    assert all(kind == "search" for kind, *_ in app.calls)
    assert {q for _, q, _ in app.calls} == {"질문 m1", "질문 n1", "질문 c1", "질문 h1"}  # 검색 지표 대상만, dev만


def test_chat_metrics(conn):
    """인용 1/3(n1 최신 공지 안 인용, c1 검색 안 한 청크), 도구 1/1(날짜 범위 포함), 거절 1/1"""
    r = rag.evaluate(conn, FakeApp(), "dev")
    c = r["chat"]
    assert (c["citation"]["k"], c["citation"]["n"]) == (1, 3)
    assert (c["tool"]["k"], c["tool"]["n"], c["refusal"]["k"], c["refusal"]["n"]) == (1, 1, 1, 1)
    why = {q["id"]: q.get("chat_fail") for q in r["questions"]}
    assert why["n1"] == "최신 공지 인용 없음" and why["c1"] == "검색 안 한 청크 인용"


def test_refuse_on_answerable_question_fails(conn):
    chat = dict(CHAT, m1=_log([("search_docs", {}), ("refuse", {"reason": "not_found"})], ["doc:1:0"]))
    r = rag.evaluate(conn, FakeApp(chat=chat), "dev")
    assert next(q for q in r["questions"] if q["id"] == "m1")["chat_fail"] == "거절"


def test_wrong_number_or_too_many_calls_fails(conn):
    seven = [("query_schedule", {"date_from": "2026-12-01", "date_to": "2026-12-05"})] * 7
    for log, why in ((_log(seven[:1], answer="3건"), "숫자"), (_log(seven, answer="2건"), "호출 6회 초과")):
        r = rag.evaluate(conn, FakeApp(chat=dict(CHAT, d1=log)), "dev")
        assert next(q for q in r["questions"] if q["id"] == "d1")["chat_fail"] == why


def test_compare_lists_rescued_and_lost(conn):
    """같은 문항에 두 조건 — m1은 조건에서 1위로(Top-1 구제), c1은 조건에서 놓침(Hit@5·Top-1 손실)"""
    cond = dict(SEARCH, m1={"manual_results": ["doc:1:0"], "case_results": []}, c1={"manual_results": [], "case_results": []})
    base = rag.evaluate(conn, FakeApp(), "dev", parts=("retrieval",))
    other = rag.evaluate(conn, FakeApp(search=cond), "dev", condition="reranker=off", parts=("retrieval",))
    cmp = rag.compare(base["questions"], other["questions"])
    assert cmp["metrics"]["top1"] == {"base": 1, "cond": 1, "n": 3, "p": 1.0}
    assert cmp["rescued"]["top1"] == ["m1"] and cmp["lost"]["top1"] == ["c1"] and cmp["lost"]["hit5"] == ["c1"]
    assert "citation" not in cmp["metrics"]  # 검색만 돌린 비교엔 챗 지표 없음
    assert cmp["metrics"]["mrr"] == {"base": pytest.approx(2 / 3), "cond": pytest.approx(0.5), "n": 3}  # (1 + 1/2 + 0) / 3
    assert rag.compare(rag.paired(base), rag.paired(other)) == cmp  # test는 남긴 통과 여부만으로 같은 비교


def test_paired_keeps_only_pass_flags(conn):
    p = rag.paired(rag.evaluate(conn, FakeApp(), "dev"))
    assert all(set(q) <= {"id", "type", "hit", "top1", "fresh", "rr", "chat_ok"} for q in p)
