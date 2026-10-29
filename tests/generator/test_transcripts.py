"""7단계 통화 전사 작업 (가상 데이터 생성 설계 §4.1 '7단계 구현 결정') — verify 테스트 DB의 통화 4건으로 본다."""

import json
import re
from types import SimpleNamespace

import pytest

from generator import llm_jobs, transcripts
from generator.llm_jobs import LLMContext
from generator.master import (
    CALL_DURATION_SEC,
    CALL_LABEL_WORDS,
    CLINIC_NAME,
    COMPLAINT_TOPICS,
    EMERGENCY_SCENES,
    PROCEDURES,
    RECALL_CYCLE_MONTHS,
    SCALING_MIN_AGE,
    THIRD_PARTY_RULE,
    TRANSCRIPT_CHARS_PER_SEC,
)
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


def test_payload_names_the_caller(conn):
    """건(발신이면 받은) 사람 이름과, 환자 기록(이름·번호)이 있으면 성별 — 보호자 통화의 환자는 아이라, 말하는 사람이
    따로 없으면 모델이 이름·'엄마/아빠'를 지어낸다"""
    assert transcripts.payload(_row(conn, "c-0004"))["facts"]["caller"] == {
        "name": "김철수",
        "sex": "M",
    }  # 보호자
    assert transcripts.payload(_row(conn, "c-0002"))["facts"]["caller"] == {
        "name": "박민수"
    }  # 처음 연락


def test_payload_has_labels_and_facts_without_internal_ids(conn):
    p = transcripts.payload(_row(conn, "c-0004"))
    assert p["direction"] == "inbound" and p["call_date"] == "2026-11-14"
    assert (
        p["labels"]["primary_purpose"] == "reschedule"
        and p["labels"]["noise_type"] == "guardian_confused"
    )
    assert p["labels"]["counterpart"] == "guardian" and "appt_confirmed" in p["labels"]["flags"]
    text = json.dumps(p, ensure_ascii=False)
    assert "appointment_id" not in text and '"id"' not in text
    assert p["facts"]["patient"]["name"] == "김하늘"
    assert p["clinic"] == CLINIC_NAME  # 전화 받을 때 병원 이름 — 문자·문서와 같은 이름


def test_full_mouth_tooth_is_blank(conn):
    """치아 '00'(전체 구강 내부 표시)은 입력에서 None — '00번 치아'라고 쓰지 않게"""
    conn.execute(
        "UPDATE truth.truth_call_labels SET used_fact = json_set(used_fact, '$.recent_visits',"
        ' json(\'[{"date": "2026-10-06", "procedure": "스케일링", "tooth": "00"}]\'))'
        " WHERE call_uid = 'c-0001'"
    )
    assert transcripts.payload(_row(conn, "c-0001"))["facts"]["recent_visits"][0]["tooth"] is None


def test_must_include_dates_and_topic(conn):
    names = llm_jobs.procedure_names(conn)
    moved = json.loads(_row(conn, "c-0004")["used_fact"])
    assert transcripts.must_include(moved, names, "reschedule") == [["11월 18일"], ["11월 17일"]]
    # 변경으로 생긴 예약의 확인·취소 통화는 원래 날짜를 꼭 넣지 않는다 — 넣으면 변경 통화처럼 읽힌다
    assert transcripts.must_include(moved, names, "confirm") == [["11월 18일"]]
    assert transcripts.must_include(
        json.loads(_row(conn, "c-0002")["used_fact"]), names, "price_q"
    ) == [["미백", "화이트닝"]]  # 이름 또는 별칭
    assert (
        transcripts.must_include(json.loads(_row(conn, "c-0001")["used_fact"]), names, "billing_q")
        == []
    )


def test_checks(conn):
    c = transcripts.checks(_row(conn, "c-0003"), llm_jobs.procedure_names(conn))
    lo, hi = CALL_DURATION_SEC
    assert c["field"] == "transcript" and c["speakers"] == ["환자:", "직원:"]
    assert c["length"] == [
        round(lo * TRANSCRIPT_CHARS_PER_SEC),
        round(hi * TRANSCRIPT_CHARS_PER_SEC),
    ]
    assert all(isinstance(n, int) for n in c["length"])  # 초당 글자 수가 소수여도 정수 글자 수
    assert (
        c["forbidden_words"] == list(CALL_LABEL_WORDS) and c["forbidden_patterns"]
    )  # config forbidden_topics
    assert c["must_include"] == [["10월 6일"]]


def test_simplify_keeps_only_needed_facts(conn):
    p = transcripts.payload(_row(conn, "c-0004"))
    s = transcripts.simplify(p)
    assert s["labels"] == p["labels"] and set(s["facts"]) == {"call_date", "appointment", "caller"}
    assert s["facts"]["appointment"]["from_date"] == "2026-11-17"


def test_duration_from_spoken_chars():
    lo, hi = CALL_DURATION_SEC
    assert (
        transcripts.duration_sec(
            "환자: " + "가" * round(100 * TRANSCRIPT_CHARS_PER_SEC) + "\n직원: 네"
        )
        == 100
    )
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
    jobs = [
        json.loads(line)
        for line in (paths["llm_jobs"] / "transcript.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [j["item"] for j in jobs] == ["c-0001", "c-0002", "c-0003", "c-0004"]
    assert (
        jobs[0]["prompt_version"].startswith(transcripts.TASK.prompt + "@") and jobs[0]["system"]
    )
    user = json.loads(jobs[2]["user"])
    assert (
        user["must_mention"] == ["10월 6일"]
        and "응급" in user["avoid_words"]
        and len(user["length_chars"]) == 2
    )
    good = {
        "scenario": "확인",
        "transcript": "환자: " + "그날 갈게요 " * 50 + "10월 6일\n직원: 네 알겠습니다",
    }
    paths["llm_results"].mkdir()
    (paths["llm_results"] / "r.jsonl").write_text(
        json.dumps(
            {"key": jobs[2]["key"], "output": good, "source": "claude-code"}, ensure_ascii=False
        )
        + "\n",
        encoding="utf-8",
    )
    assert llm_jobs.import_results(paths)["transcript"]["ok"] == 1
    transcripts.stage(_run(conn, LLMContext(paths, fake=False)), None, None)
    row = conn.execute(
        "SELECT has_transcript, transcript, duration_sec FROM ax_calls WHERE call_uid = 'c-0003'"
    ).fetchone()
    assert (
        row[0] == 1
        and row[1] == good["transcript"]
        and row[2] == transcripts.duration_sec(good["transcript"])
    )
    assert (
        conn.execute("SELECT count(*) FROM ax_calls WHERE has_transcript = 0").fetchone()[0] == 3
    )
    assert "scenario" not in (paths["llm_cache"] / "transcript.jsonl").read_text(encoding="utf-8")


def test_price_and_insurance_calls_get_master_price(conn):
    """가격·보험 문의는 주제 시술의 비용·급여를 입력에 — RAG 문서와 같은 master 값"""
    p = transcripts.payload(_row(conn, "c-0002"))  # 가격 문의, 주제 미백
    fee = {pr["name"]: pr for pr in PROCEDURES}
    assert p["facts"]["topic_price"] == {
        "procedure": "미백",
        "total": f"{fee['미백']['fee_total']:,}원",
        "insured": False,
    }
    scaling = fee["스케일링"]
    assert transcripts.topic_price("스케일링") == {
        "procedure": "스케일링",
        "total": f"{scaling['fee_total']:,}원",
        "insured": True,
        "patient_pays": f"{round(scaling['fee_total'] * scaling['patient_share_rate']):,}원",
        "insured_age": f"{SCALING_MIN_AGE}세 이상",
    }
    assert "topic_price" not in transcripts.payload(_row(conn, "c-0001"))["facts"]  # 수납 문의
    sub = dict(_row(conn, "c-0002")) | {
        "primary_purpose": "treatment_q",
        "accepted_purposes": '["treatment_q", "price_q"]',
    }
    assert (
        transcripts.payload(sub)["facts"]["topic_price"]["procedure"] == "미백"
    )  # 부 주제 가격 문의도


def test_book_call_marks_the_slot_as_new(conn):
    """새 예약 통화(c-0003)의 예약은 이 통화에서 정하는 날짜 — '예약(appointment)'으로 넘기면 모델이 이미 잡힌 예약으로
    읽어 '그날 잡혀 있으세요'라고 쓴다. 통화 전 예약이 없으면 그런 말을 금지 패턴으로도 막는다"""
    row = _row(conn, "c-0003")
    facts = transcripts.payload(row)["facts"]
    assert "appointment" not in facts and facts["new_booking"]["date"] == "2026-10-06"
    assert facts["new_booking"]["before_call"]
    assert (
        transcripts.ALREADY_BOOKED
        in transcripts.checks(row, llm_jobs.procedure_names(conn))["forbidden_patterns"]
    )
    assert "new_booking" in transcripts.simplify(transcripts.payload(row))["facts"]
    moved = _row(conn, "c-0004")  # 예약 변경 — 원래 예약이 있는 통화는 그대로
    assert "appointment" in transcripts.payload(moved)["facts"]
    assert (
        transcripts.ALREADY_BOOKED
        not in transcripts.checks(moved, llm_jobs.procedure_names(conn))["forbidden_patterns"]
    )


def test_book_call_with_another_appointment_may_mention_it(conn):
    """통화 전에 다른 예약이 있으면 '그날 잡혀 있으세요'는 그 예약 이야기일 수 있어 금지하지 않는다"""
    row = dict(_row(conn, "c-0003"))
    fact = json.loads(row["used_fact"]) | {
        "next_appointment": {"date": "2026-10-20", "time": "10:00", "procedure": "스케일링"}
    }
    row["used_fact"] = json.dumps(fact, ensure_ascii=False)
    assert (
        transcripts.ALREADY_BOOKED
        not in transcripts.checks(row, llm_jobs.procedure_names(conn))["forbidden_patterns"]
    )


@pytest.mark.parametrize(
    "line, hit",
    [
        ("직원: 마침 9월 22일 10시 30분에 스케일링이 잡혀 있으세요.", True),
        ("직원: 1월 12일 오후 4시에 레진 치료 예약이 있으시네요.", True),
        ("직원: 1월 31일 10:30에 레진 진료 예약이 이미 잡혀 있으세요.", True),
        ("직원: 그럼 10월 18일 오전 10시 30분으로 예약해 드릴게요.", False),
        ("직원: 9월 23일 오후 5시 30분으로 잡아 둘게요.", False),
    ],
)
def test_already_booked_pattern(line, hit):
    assert bool(re.search(transcripts.ALREADY_BOOKED, line)) is hit


def test_scaling_price_after_this_years_use():
    """그해 급여 스케일링을 이미 받았으면 스케일링은 비급여 가격 — 연 1회(§2.4)"""
    assert transcripts.topic_price("스케일링", 40)["insured"]
    used = transcripts.topic_price("스케일링", 40, scaling_used=True)
    assert not used["insured"] and "patient_pays" not in used and used["insured_limit"]


def test_recall_calls_get_recall_cycle(conn):
    """리콜 문의 통화는 병원의 정기 검진 주기를 입력에 — 없으면 모델이 '6개월' 같은 상식을 지어낸다"""
    recall = dict(_row(conn, "c-0001")) | {
        "primary_purpose": "recall_q",
        "accepted_purposes": '["recall_q"]',
    }
    assert transcripts.payload(recall)["facts"]["recall_months"] == RECALL_CYCLE_MONTHS
    assert "recall_months" not in transcripts.payload(_row(conn, "c-0001"))["facts"]


def _as(conn, uid, **over):
    """통화 행을 목적·상대·플래그만 바꿔 본다 — 테스트 DB의 통화 4건이 모든 경우를 갖추지 않아서"""
    return dict(_row(conn, uid)) | over


def test_cancel_without_new_booking_forbids_rebooking_offer(conn):
    """새 예약을 원하지 않는 취소 통화에서 직원이 '새 날짜 잡게 연락드릴게요·편한 요일 있으세요'라고 하면 wants_appt로
    읽힌다 — 금지 패턴으로 막는다. 새 예약을 원하는 취소 통화는 그대로"""
    names = llm_jobs.procedure_names(conn)
    no = _as(
        conn, "c-0003", primary_purpose="cancel", accepted_purposes='["cancel"]', wants_appt=0
    )
    yes = no | {"wants_appt": 1}
    assert transcripts.REBOOK_OFFER in transcripts.checks(no, names)["forbidden_patterns"]
    assert transcripts.REBOOK_OFFER not in transcripts.checks(yes, names)["forbidden_patterns"]


@pytest.mark.parametrize(
    "line, hit",
    [
        ("직원: 원장님 일정 보고 새 날짜 잡게 다시 연락드릴게요.", True),
        ("직원: 편하신 요일이나 시간대 있으세요?", True),
        ("직원: 나중에 일정 여쭤보려고 저희가 따로 연락드려도 될까요?", True),
        ("직원: 오늘 12시 건은 없던 걸로 해 드릴게요.", False),
        ("환자: 다시 갈 수 있는 날이 정해지면 제가 전화드릴게요.", False),
        ("환자: 다시 잡을 생각은 아직 없어요.", False),  # 환자 말은 괜찮다
        ("직원: 새 일정은 따로 잡지 않고 오늘 건만 정리해 둘게요.", False),
    ],
)
def test_rebook_offer_pattern(line, hit):
    assert bool(re.search(transcripts.REBOOK_OFFER, line)) is hit


def test_emergency_and_complaint_calls_get_a_situation(conn):
    """응급·불만 통화는 상황을 입력에 — 없으면 거의 모두 '어젯밤부터 오른쪽 아래 어금니'·'한 시간 기다렸다'가 된다.
    통화 번호로 정해져(난수 없음) 같은 통화는 늘 같은 상황"""
    base = _as(conn, "c-0001", primary_purpose="emergency", accepted_purposes='["emergency"]')
    facts = transcripts.payload(base)["facts"]
    assert facts["caller_situation"] == transcripts.payload(base)["facts"]["caller_situation"]
    seen = {
        transcripts.payload(base | {"call_uid": f"c-{n:04d}"})["facts"]["caller_situation"]
        for n in range(60)
    }
    assert len(seen) > 10
    assert all(any(s in x for s in EMERGENCY_SCENES) for x in seen)
    sub = _as(
        conn, "c-0001", primary_purpose="cancel", accepted_purposes='["cancel", "complaint"]'
    )
    assert transcripts.payload(sub)["facts"]["caller_situation"] in COMPLAINT_TOPICS  # 부 주제도
    assert "caller_situation" not in transcripts.payload(_row(conn, "c-0003"))["facts"]  # 새 예약


def test_insurer_call_hides_patient_records(conn):
    """보험사 등 제3자에게는 진료 내용을 알려 주지 않는다 — 환자 이름만 넘기고 병원 규칙을 함께"""
    row = _as(conn, "c-0001", counterpart="insurer")
    facts = transcripts.payload(row)["facts"]
    assert facts["patient"] == {"name": "김철수"}
    assert facts["third_party_rule"] == THIRD_PARTY_RULE
    assert not {
        "recent_visits",
        "balance",
        "alerts",
        "open_plan_items",
        "next_appointment",
        "family",
    } & set(facts)
    assert "third_party_rule" in transcripts.simplify(transcripts.payload(row))["facts"]
    assert "third_party_rule" not in transcripts.payload(_row(conn, "c-0001"))["facts"]
