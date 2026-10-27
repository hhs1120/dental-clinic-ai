"""7단계 통화 전사 작업 (가상 데이터 생성 설계 §4.1 '7단계 구현 결정') — verify 테스트 DB의 통화 4건으로 본다."""
import json
from types import SimpleNamespace

import pytest

from generator import llm_jobs, transcripts
from generator.llm_jobs import LLMContext
from generator.master import CALL_DURATION_SEC, CALL_LABEL_WORDS, TRANSCRIPT_CHARS_PER_SEC
from tests.generator.verify_db import make_db


@pytest.fixture
def conn(tmp_path):
    conn = make_db(tmp_path)
    conn.execute("UPDATE ax_calls SET has_transcript = 0, transcript = NULL")
    return conn


def _paths(tmp_path):
    return {k: tmp_path / k for k in ("llm_cache", "llm_jobs", "llm_results")}


def _run(conn, ctx):
    return SimpleNamespace(conn=conn, state={"llm": ctx})


def _row(conn, uid):
    return {r["call_uid"]: r for r in transcripts.rows(conn)}[uid]


def test_payload_has_labels_and_facts_without_internal_ids(conn):
    p = transcripts.payload(_row(conn, "c-0004"))
    assert p["direction"] == "inbound" and p["call_date"] == "2026-11-14"
    assert p["labels"]["primary_purpose"] == "reschedule" and p["labels"]["noise_type"] == "guardian_confused"
    assert p["labels"]["counterpart"] == "guardian" and "appt_confirmed" in p["labels"]["flags"]
    text = json.dumps(p, ensure_ascii=False)
    assert "appointment_id" not in text and '"id"' not in text
    assert p["facts"]["patient"]["name"] == "김하늘"


def test_full_mouth_tooth_is_blank(conn):
    """치아 '00'(전체 구강 내부 표시)은 입력에서 None — '00번 치아'라고 쓰지 않게"""
    conn.execute("UPDATE truth.truth_call_labels SET used_fact = json_set(used_fact, '$.recent_visits',"
                 " json('[{\"date\": \"2026-10-06\", \"procedure\": \"스케일링\", \"tooth\": \"00\"}]'))"
                 " WHERE call_uid = 'c-0001'")
    assert transcripts.payload(_row(conn, "c-0001"))["facts"]["recent_visits"][0]["tooth"] is None


def test_must_include_dates_and_topic(conn):
    names = llm_jobs.procedure_names(conn)
    assert transcripts.must_include(json.loads(_row(conn, "c-0004")["used_fact"]), names) == [["11월 18일"], ["11월 17일"]]
    assert transcripts.must_include(json.loads(_row(conn, "c-0002")["used_fact"]), names) == [["미백"]]
    assert transcripts.must_include(json.loads(_row(conn, "c-0001")["used_fact"]), names) == []


def test_checks(conn):
    c = transcripts.checks(_row(conn, "c-0003"), llm_jobs.procedure_names(conn))
    lo, hi = CALL_DURATION_SEC
    assert c["field"] == "transcript" and c["speakers"] == ["환자:", "직원:"]
    assert c["length"] == [lo * TRANSCRIPT_CHARS_PER_SEC, hi * TRANSCRIPT_CHARS_PER_SEC]
    assert c["forbidden_words"] == list(CALL_LABEL_WORDS) and c["forbidden_patterns"]  # config forbidden_topics
    assert c["must_include"] == [["10월 6일"]]


def test_simplify_keeps_only_needed_facts(conn):
    p = transcripts.payload(_row(conn, "c-0004"))
    s = transcripts.simplify(p)
    assert s["labels"] == p["labels"] and set(s["facts"]) == {"call_date", "appointment"}
    assert s["facts"]["appointment"]["from_date"] == "2026-11-17"


def test_duration_from_spoken_chars():
    lo, hi = CALL_DURATION_SEC
    assert transcripts.duration_sec("환자: " + "가" * (100 * TRANSCRIPT_CHARS_PER_SEC) + "\n직원: 네") == 100
    assert transcripts.duration_sec("환자: 네\n직원: 네") == lo
    assert transcripts.duration_sec("환자: " + "가" * 10_000) == hi


def test_stage_fake_fills_every_call(conn, tmp_path):
    transcripts.stage(_run(conn, LLMContext(_paths(tmp_path), fake=True)), None, None)
    rows = conn.execute("SELECT has_transcript, transcript, duration_sec FROM ax_calls").fetchall()
    assert all(r[0] == 1 and r[1] for r in rows)
    assert all(CALL_DURATION_SEC[0] <= r[2] <= CALL_DURATION_SEC[1] for r in rows)


def test_stage_exports_misses_then_applies_cache(conn, tmp_path):
    paths = _paths(tmp_path)
    ctx = LLMContext(paths, fake=False)
    transcripts.stage(_run(conn, ctx), None, None)
    assert ctx.finish() == {"transcript": 4}
    jobs = [json.loads(line) for line in (paths["llm_jobs"] / "transcript.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [j["item"] for j in jobs] == ["c-0001", "c-0002", "c-0003", "c-0004"]
    assert jobs[0]["prompt_version"] == transcripts.TASK.prompt and jobs[0]["system"]
    user = json.loads(jobs[2]["user"])
    assert user["must_mention"] == ["10월 6일"] and "응급" in user["avoid_words"] and len(user["length_chars"]) == 2
    good = {"scenario": "확인", "transcript": "환자: " + "그날 갈게요 " * 50 + "10월 6일\n직원: 네 알겠습니다"}
    paths["llm_results"].mkdir()
    (paths["llm_results"] / "r.jsonl").write_text(
        json.dumps({"key": jobs[2]["key"], "output": good, "source": "claude-code"}, ensure_ascii=False) + "\n",
        encoding="utf-8")
    assert llm_jobs.import_results(paths)["transcript"]["ok"] == 1
    transcripts.stage(_run(conn, LLMContext(paths, fake=False)), None, None)
    row = conn.execute("SELECT has_transcript, transcript, duration_sec FROM ax_calls WHERE call_uid = 'c-0003'").fetchone()
    assert row[0] == 1 and row[1] == good["transcript"] and row[2] == transcripts.duration_sec(good["transcript"])
    assert conn.execute("SELECT count(*) FROM ax_calls WHERE has_transcript = 0").fetchone()[0] == 3
    assert "scenario" not in (paths["llm_cache"] / "transcript.jsonl").read_text(encoding="utf-8")



def test_price_and_insurance_calls_get_master_price(conn):
    """가격·보험 문의는 주제 시술의 비용·급여를 입력에 — RAG 문서와 같은 master 값"""
    p = transcripts.payload(_row(conn, "c-0002"))  # 가격 문의, 주제 미백
    assert p["facts"]["topic_price"] == {"procedure": "미백", "total": "300,000원", "insured": False}
    assert transcripts.topic_price("스케일링") == {"procedure": "스케일링", "total": "60,000원", "insured": True,
                                                 "patient_pays": "18,000원", "insured_age": "19세 이상"}
    assert "topic_price" not in transcripts.payload(_row(conn, "c-0001"))["facts"]  # 수납 문의
