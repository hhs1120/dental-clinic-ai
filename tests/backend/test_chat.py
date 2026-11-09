"""질문하기 에이전트 (기능 정의 §4 ③, 설계 2026-11-09 챗 "에이전트") — 스크립트 게이트웨이로 도구 호출 순서를 정해 둔다."""

import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from backend import chat, db
from backend.embeddings import FakeEmbedder
from backend.rag_search import Searcher
from backend.vector_store import VectorStore
from shared.clinic import Clinic
from shared.llm import BudgetExceeded
from tests.backend.test_followup import Script

SOURCE_SCHEMA = Path(__file__).parents[2] / "shared" / "source_schema.sql"
CLINIC = Clinic(today=date(2026, 12, 1), range_start=date(2026, 1, 1), eval_pool_size=150)
CHART_TEXT = "[차트 C-00001 | 2026-11-05 | 치주치료] 스케일링 3일 뒤 재평가 (잇몸치료 · 사흘)"
Q = "주차 몇 시간 무료야?"


@pytest.fixture
def conn(tmp_path):
    source = tmp_path / "s.sqlite"
    s = sqlite3.connect(source)
    s.executescript(SOURCE_SCHEMA.read_text(encoding="utf-8"))
    s.execute("PRAGMA foreign_keys = OFF")
    s.executescript(
        """
        INSERT INTO emr_staff (id, name, role) VALUES (1, '원장', 'doctor');
        INSERT INTO emr_procedures (code, name, category, is_insured, fee_total, patient_share_rate,
            duration_min, is_tooth_level, aliases) VALUES
            ('U0001', '스케일링', 'preventive', 'yes', 50000, 0.3, 30, 0, '[]'),
            ('U0010', '치주치료', 'perio', 'yes', 80000, 0.3, 30, 0, '["잇몸치료"]');
        INSERT INTO emr_patients (id, chart_no, name, birth_date, sex, phone, family_id,
            registered_at, recall_cycle_months) VALUES
            (1, 'C-00001', '김철수', '1980-01-01', 'M', '010-1', NULL, '2020-01-01', 12);
        INSERT INTO emr_treatments (id, appointment_id, patient_id, procedure_code, tooth_no,
            performed_at, fee_total, insured_amount, patient_amount) VALUES
            (101, 1, 1, 'U0001', '00', '2026-11-02 10:00:00', 50000, 35000, 15000),
            (102, 1, 1, 'U0010', '00', '2026-11-05 10:00:00', 80000, 56000, 24000);
        INSERT INTO emr_chart_notes (id, treatment_id, dx_code, note) VALUES
            (1, 101, 'K03', '전악 치석제거'), (2, 102, 'K05', '스케일링 3일 뒤 재평가');
        """
    )
    for i in range(9):
        s.execute(
            "INSERT INTO emr_appointments (id, patient_id, staff_id, scheduled_at, duration_min,"
            " type, planned_procedure_code, status, booked_via, created_at)"
            " VALUES (?, 1, 1, ?, 30, 'hygiene', 'U0001', 'scheduled', 'phone', 'x')",
            (10 + i, f"2026-12-01 {9 + i:02d}:00:00"),
        )
    s.commit()
    s.close()
    c = db.connect(tmp_path / "a.sqlite", source)
    db.init_schema(c)
    chunks = [
        ("doc:1:0", "document", "주차 안내\n진료 당일 2시간 무료", None, None),
        ("chart:102", "chart_note", CHART_TEXT, "C-00001", "2026-11-05"),
    ]
    for cid, kind, text, chart_no, day in chunks:
        c.execute(
            "INSERT INTO ax_chunks (id, source_type, text, chart_no, event_date) VALUES (?, ?, ?, ?, ?)",
            (cid, kind, text, chart_no, day),
        )
        c.execute("INSERT INTO ax_chunks_fts VALUES (?, ?)", (cid, text))
    emb = FakeEmbedder()
    VectorStore(c, emb.model).upsert(
        [x[0] for x in chunks], emb.embed([x[2] for x in chunks], "document")
    )
    c.commit()
    yield c
    c.close()


def ask(conn, *runs, question=Q, gw=None):
    gw = gw or Script(conn, *runs)
    return chat.chat(conn, gw, Searcher(conn, CLINIC, FakeEmbedder()), CLINIC, question)


SEARCH = ("tool", "search_docs", {"query": "주차"})


def ANSWER(text="진료 당일 2시간 무료", cited=("doc:1:0",)):
    return ("tool", "answer", {"text": text, "cited_ids": list(cited)})


def _rule(conn, rule_id):
    row = conn.execute(
        "SELECT blocked FROM ax_rule_counts WHERE rule_id = ?", (rule_id,)
    ).fetchone()
    return row[0] if row else 0


def test_answer_with_search_citation(conn):
    out = ask(conn, [SEARCH, ANSWER()])
    assert out["status"] == "answered" and out["answer_text"] == "진료 당일 2시간 무료"
    assert [c["name"] for c in out["tool_calls"]] == ["search_docs"]
    assert out["cited_ids"] == ["doc:1:0"] and "doc:1:0" in out["retrieved_ids"]


def test_cite_outside_retries_once_then_answers(conn):
    out = ask(conn, [SEARCH, ANSWER(cited=["doc:9:0"]), ANSWER()])
    assert out["status"] == "answered" and _rule(conn, "chat.cite_outside") == 1


def test_cite_outside_twice_is_cite_failed(conn):
    out = ask(conn, [SEARCH, ANSWER(cited=["doc:9:0"]), ANSWER(cited=["doc:9:0"])])
    assert out["status"] == "cite_failed" and out["cited_ids"] == []
    assert out["answer_text"] == chat.FAILED_TEXT and _rule(conn, "chat.cite_outside") == 2


def test_unverified_number_twice_is_number_failed(conn):
    bad = ANSWER(text="진료 당일 7시간 무료")
    out = ask(conn, [SEARCH, bad, bad])
    assert out["status"] == "number_failed" and _rule(conn, "chat.number_unverified") == 2


def test_row_count_number_passes(conn):
    sched = ("tool", "query_schedule", {"date_from": "2026-12-01", "date_to": "2026-12-01"})
    out = ask(conn, [sched, ANSWER(text="오늘 예약 9건", cited=[])], question="오늘 예약 몇 건?")
    assert out["status"] == "answered"


def test_data_answer_without_citation_ok(conn):
    sched = ("tool", "query_schedule", {"date_from": "2026-12-01", "date_to": "2026-12-01"})
    out = ask(conn, [sched, ANSWER(text="9명", cited=[])], question="오늘 몇 명?")
    assert out["status"] == "answered" and out["cited_ids"] == []


def test_answer_without_citation_after_search_is_cite_failed(conn):
    out = ask(
        conn, [SEARCH, ANSWER(text="무료입니다", cited=[]), ANSWER(text="무료입니다", cited=[])]
    )
    assert out["status"] == "cite_failed"


def test_refuse_is_refused_with_reason(conn):
    out = ask(conn, [SEARCH, ("tool", "refuse", {"reason": "not_found"})])
    assert out["status"] == "refused" and out["refuse_reason"] == "not_found"
    assert out["tool_calls"][-1]["name"] == "refuse" and out["answer_text"] == chat.REFUSED_TEXT


def test_seventh_tool_call_is_call_limit(conn):
    out = ask(conn, [SEARCH] * 7 + [ANSWER()])
    assert out["status"] == "call_limit" and len(out["tool_calls"]) > chat.CHAT_MAX_TOOL_CALLS
    assert out["answer_text"] == chat.FAILED_TEXT


def test_chart_text_has_no_search_brackets(conn):
    gw = Script(
        conn,
        [("tool", "search_docs", {"query": "재평가"}), ANSWER(text="재평가", cited=["chart:102"])],
    )
    ask(conn, gw=gw)
    steps = conn.execute("SELECT result FROM ax_agent_steps WHERE tool = 'search_docs'").fetchone()
    text = json.dumps(json.loads(steps[0]), ensure_ascii=False)
    assert "스케일링 3일 뒤 재평가" in text and "잇몸치료 · 사흘" not in text


def test_query_charts_ids_are_citable(conn):
    charts = (
        "tool",
        "query_charts",
        {"procedure": "치주치료", "prev_procedure": "스케일링", "max_gap_days": 4},
    )
    out = ask(conn, [charts, ANSWER(text="3일 뒤 재평가로 적음", cited=["chart:102"])])
    assert out["status"] == "answered" and "chart:102" in out["retrieved_ids"]


def test_two_chats_do_not_block_each_other(conn):
    a = ask(conn, [SEARCH, ANSWER()])
    b = ask(conn, [SEARCH, ANSWER()])
    assert a["log_id"] != b["log_id"]
    assert (
        conn.execute("SELECT COUNT(*) FROM ax_agent_runs WHERE agent = 'chat'").fetchone()[0] == 2
    )


class Broke(Script):
    def complete(self, alias, **kw):
        raise BudgetExceeded("runtime 일일 상한")


def test_budget_exceeded_is_budget_status(conn):
    out = ask(conn, gw=Broke(conn, []))
    assert out["status"] == "budget" and out["answer_text"] == chat.FAILED_TEXT


def test_log_row_matches_return(conn):
    out = ask(conn, [SEARCH, ANSWER()])
    row = conn.execute("SELECT * FROM ax_search_logs WHERE id = ?", (out["log_id"],)).fetchone()
    assert row["status"] == out["status"] and row["kind"] == "chat" and row["question"] == Q
    assert json.loads(row["tool_calls"]) == out["tool_calls"]
    assert json.loads(row["retrieved_ids"]) == out["retrieved_ids"]
    assert json.loads(row["cited_ids"]) == out["cited_ids"] and row["run_id"]


def test_events_reported_before_tools(conn):
    seen = []
    gw = Script(conn, [SEARCH, ANSWER()])
    chat.chat(conn, gw, Searcher(conn, CLINIC, FakeEmbedder()), CLINIC, Q, on_event=seen.append)
    assert seen == [{"type": "step", "tool": "search_docs"}]


def test_max_tool_calls_matches_eval():
    from evaluation.metrics import MAX_TOOL_CALLS

    assert chat.CHAT_MAX_TOOL_CALLS == MAX_TOOL_CALLS


def test_chat_reports_cost(conn):
    assert ask(conn, [SEARCH, ANSWER()])["cost_usd"] >= 0


def test_baseline_one_search_one_answer(conn):
    """단순 RAG 기준선(평가 설계 §3.3) — 질문 원문으로 한 번 검색, 도구 없이 그 결과만으로 답"""
    gw = Script(conn, [])
    out = chat.baseline(conn, gw, Searcher(conn, CLINIC, FakeEmbedder()), "재평가 차트")
    assert set(out) >= {"retrieved_ids", "answer_text", "cited_ids", "refused", "cost_usd"}
    assert set(out["retrieved_ids"]) == {"doc:1:0", "chart:102"}
    sent = conn.execute(
        "SELECT COUNT(*) FROM ax_llm_calls WHERE task = ? AND model_alias = 'chat'",
        (chat.BASELINE_TASK,),
    ).fetchone()[0]
    assert sent == 1 and conn.execute("SELECT COUNT(*) FROM ax_agent_runs").fetchone()[0] == 0


def test_text_ending_is_sent_back_once_then_answers(conn):
    """모델이 answer 없이 글로 끝내면 한 번 되돌려 도구로 내게 한다(dev data-03·05)"""
    out = ask(conn, [SEARCH, ("text", "2시간 무료입니다"), ANSWER()])
    assert out["status"] == "answered"
    nudges = conn.execute(
        "SELECT COUNT(*) FROM ax_agent_steps WHERE kind = 'input' AND result LIKE '%answer%'"
    ).fetchone()[0]
    assert nudges == 1


def test_text_ending_twice_is_failed(conn):
    out = ask(conn, [SEARCH, ("text", "2시간"), ("text", "2시간")])
    assert out["status"] == "failed" and out["answer_text"] == chat.FAILED_TEXT


# ── 리뷰 반영 ────────────────────────────────────────────────────────────


def test_number_in_search_query_is_not_evidence(conn):
    """검색어에 모델이 쓴 숫자는 근거가 아니다(리뷰 1)"""
    search = ("tool", "search_docs", {"query": "주차 7시간"})
    bad = ANSWER(text="주차 7시간 무료")
    assert ask(conn, [search, bad, bad])["status"] == "number_failed"


def test_search_setting_numbers_are_not_evidence(conn):
    """top_k 같은 검색 설정의 숫자도 근거가 아니다(리뷰 1)"""
    search = ("tool", "search_docs", {"query": "주차", "top_k": 8})
    bad = ANSWER(text="주차 8시간 무료")
    assert ask(conn, [search, bad, bad])["status"] == "number_failed"


def test_date_arguments_are_evidence(conn):
    sched = ("tool", "query_schedule", {"date_from": "2026-12-01", "date_to": "2026-12-01"})
    out = ask(conn, [sched, ANSWER(text="12월 1일 9건", cited=[])], question="오늘 예약?")
    assert out["status"] == "answered"


def test_call_limit_ends_run_without_another_model_call(conn):
    """7번째 호출이 막히면 바로 끝낸다 — 답을 청하고 버리지 않는다(리뷰 3)"""
    gw = Script(conn, [SEARCH] * 7 + [ANSWER()])
    out = ask(conn, gw=gw)
    assert out["status"] == "call_limit"
    run = conn.execute("SELECT model_calls FROM ax_agent_runs WHERE agent = 'chat'").fetchone()
    assert run[0] == 7


def test_exception_marks_log_and_run_failed(conn):
    """실행 중 예외 — 기록이 running으로 남지 않는다(리뷰 4)"""

    class Boom(Script):
        def complete(self, alias, **kw):
            raise RuntimeError("장애")

    with pytest.raises(RuntimeError):
        ask(conn, gw=Boom(conn, []))
    assert conn.execute("SELECT status FROM ax_search_logs").fetchone()[0] == "failed"
    assert conn.execute("SELECT status FROM ax_agent_runs").fetchone()[0] == "failed"


def test_bad_args_answer_does_not_use_the_retry(conn):
    """answer가 인자 오류로 막힌 것은 다시 답할 기회를 쓰지 않는다(리뷰 5)"""
    empty = ("tool", "answer", {"text": "", "cited_ids": []})
    out = ask(conn, [SEARCH, empty, ANSWER(cited=["doc:9:0"]), ANSWER()])
    assert out["status"] == "answered"
