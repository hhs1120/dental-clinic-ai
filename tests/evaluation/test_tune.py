"""튜닝 비교 (AI 실험 기록) — dev + 튜닝 질문만 쓰고 test를 섞지 않는다, 반복 실행의 흔들림을 잰다."""

import json

import pytest

from evaluation import EvalRefused, tune
from tests.evaluation.test_rag import SEARCH, FakeApp, make_rag_db


@pytest.fixture
def conn(tmp_path):
    return make_rag_db(tmp_path)


def tune_file(tmp_path, **over):
    q = {
        "id": "tune-manual-01",
        "type": "manual",
        "split": "tune",
        "question": "질문 m2",
        "answer_text": "답",
        "answer_doc_ids": ["doc:2"],
        "answer_chunk_hint": None,
        "expected_tools": ["search_docs"],
        "expected_args": {},
        "expect_refusal": False,
        "answer_sql": None,
    }
    path = tmp_path / "tune_qa.json"
    path.write_text(json.dumps([{**q, **over}], ensure_ascii=False), encoding="utf-8")
    return path


def test_questions_are_dev_search_types_plus_tune(conn, tmp_path):
    qs = tune.questions(conn, tune_file(tmp_path))
    assert "tune-manual-01" in {q["id"] for q in qs}
    assert all(q["split"] in ("dev", "tune") for q in qs)
    assert {q["type"] for q in qs} <= {"manual", "notice", "case", "chart"}


def test_test_split_in_file_is_refused(conn, tmp_path):
    with pytest.raises(EvalRefused):
        tune.questions(conn, tune_file(tmp_path, split="test"))


def test_measure_repeats_each_condition(conn, tmp_path):
    app = FakeApp()
    qs = tune.questions(conn, tune_file(tmp_path))
    out = tune.measure(conn, app, ["reranker=a", "reranker=b"], 2, qs)
    assert len(out["reranker=a"]["runs"]) == 2
    assert out["reranker=a"]["unstable"] == []
    assert out["reranker=a"]["seconds_per_question"] >= 0
    searches = [c for c in app.calls if c[2] == "reranker=b"]
    assert len(searches) == 2 * len(qs)


def test_measure_finds_questions_whose_rank_changed(conn, tmp_path):
    class Flaky(FakeApp):
        n = 0

        def search(self, question, condition=None):
            r = super().search(question, condition)
            if question == "질문 m2":
                Flaky.n += 1
                return {"manual_results": [] if Flaky.n % 2 else ["doc:2:0"], "case_results": []}
            return r

    qs = tune.questions(conn, tune_file(tmp_path))
    out = tune.measure(conn, Flaky(SEARCH), ["reranker=a"], 2, qs)
    assert "tune-manual-01" in out["reranker=a"]["unstable"]


def test_compare_is_first_condition_against_others(conn, tmp_path):
    qs = tune.questions(conn, tune_file(tmp_path))
    out = tune.measure(conn, FakeApp(), ["reranker=a", "reranker=b"], 1, qs)
    pairs = tune.compare_all(out, ["reranker=a", "reranker=b"])
    assert list(pairs) == ["reranker=b"] and "hit5" in pairs["reranker=b"]["metrics"]
