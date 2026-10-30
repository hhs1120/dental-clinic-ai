"""verify RAG 질문 검사(truth_rag_qa — 평가 설계 §3.1, 가상 데이터 생성 설계 §5.3)가 위반을 잡는지 — 정상 질문을 한 곳씩 깨뜨린다.
fixture 문서·통화·차트는 verify_db. 유형별 문항 수는 QA_TYPE_COUNTS를 fixture 수로 바꿔 검사한다."""

import json
from collections import Counter
from dataclasses import replace

import pytest

from generator import build, verify
from tests.generator.verify_db import CTX, make_db

QA_CHECKS = [
    verify.check_qa_matches_json,
    verify.check_qa_fields,
    verify.check_qa_counts,
    verify.check_qa_answer_ids,
    verify.check_qa_split_isolation,
    verify.check_qa_same_fact_docs,
    verify.check_qa_answer_sql,
    verify.check_qa_distractor_clean,
    verify.check_qa_patient_names,
    verify.check_qa_refusal,
    verify.check_qa_expected_args,
]
QA_WARNINGS = [verify.check_qa_refusal_absent, verify.check_qa_copied_wording]


def _q(
    id,
    type,
    split,
    question,
    answer_text,
    answer_doc_ids=(),
    hint=None,
    tools=("search_docs",),
    args=None,
    sql=None,
):
    return {
        "id": id,
        "type": type,
        "question": question,
        "answer_text": answer_text,
        "answer_doc_ids": list(answer_doc_ids),
        "answer_chunk_hint": hint,
        "expected_tools": list(tools),
        "expected_args": args or {},
        "expect_refusal": type == "refusal",
        "split": split,
        "answer_sql": sql,
    }


# 이 파일만 쓰는 문서: 7 최신 공지(8을 대체) / 8 옛 공지
EXTRA_DOCS = [
    (
        7,
        "notice",
        "주차 무료 시간 변경",
        "2",
        "2026-06-01",
        "docs/notice/007.md",
        "# 주차\n\n무료 주차는 2시간입니다.",
    ),
    (
        8,
        "notice",
        "주차 무료 시간 변경",
        "1",
        "2025-06-02",
        "docs/notice/008.md",
        "# 주차\n\n무료 주차는 1시간입니다.",
    ),
]
EXTRA_META = [
    (
        7,
        "notice-parking",
        "core",
        0,
        None,
        8,
        "[]",
        json.dumps(
            [
                {
                    "id": "notice.parking",
                    "text": "무료 주차 2시간",
                    "words": ["2시간"],
                    "reason": None,
                }
            ]
        ),
    ),
    (
        8,
        "old-parking",
        "old_version",
        1,
        7,
        None,
        "[]",
        json.dumps(
            [{"id": "old.parking", "text": "무료 주차 1시간", "words": ["1시간"], "reason": None}]
        ),
    ),
]

# 11월 완료 예약: 2(11/03)·4(11/18) → 2건, 전체 완료 1·2·4 → 3건. 레진 차트: 3, 신경치료 차트: 1
QA = [
    _q(
        "m1",
        "manual",
        "dev",
        "노쇼가 세 번 쌓인 환자 예약은 어떻게 받아?",
        "3개월 동안 전화 예약만 받는다",
        ["doc:1"],
        "noshow.limit",
    ),
    _q(
        "m2",
        "manual",
        "test",
        "진단명은 청구서에 어떻게 맞춰 적어?",
        "시행한 시술에 맞는 상병",
        ["doc:5"],
        "claim.C01.heading",
    ),
    _q(
        "n1",
        "notice",
        "test",
        "치아 하얗게 하는 거 지금 얼마 받지?",
        "300,000원",
        ["doc:3"],
        "notice.white",
    ),
    _q(
        "n2",
        "notice",
        "dev",
        "건물에 차 세우면 돈 안 내도 되는 게 얼마 동안이지?",
        "2시간",
        ["doc:7"],
        "notice.parking",
    ),
    _q(
        "c1",
        "case",
        "dev",
        "김하늘 쪽에서 날짜 옮겨 달라고 했던 게 언제였지?",
        "11월 17일 → 18일로 변경",
        ["call:c-0004"],
        "하루 미룰 수",
    ),
    _q(
        "c2",
        "case",
        "test",
        "김철수 님께 지난달 안내 문자 나갔었나?",
        "11월 2일 리마인드, 답장 '네'",
        ["thread:C-00001:2026-11"],
        "내일 예약 안내",
    ),
    _q(
        "d1",
        "data",
        "dev",
        "11월에 진료 마친 예약 몇 건이었어?",
        "2건",
        tools=("query_schedule",),
        args={"query_schedule": {"date_from": "2026-11-01", "date_to": "2026-11-30"}},
        sql="SELECT count(*) FROM emr_appointments WHERE status = 'completed' AND scheduled_at LIKE '2026-11%'",
    ),
    _q(
        "d2",
        "data",
        "test",
        "10월부터 11월까지 진료 마친 예약은 다 합쳐 몇 건?",
        "3건",
        tools=("query_schedule",),
        args={"query_schedule": {"date_from": "2026-10-01", "date_to": "2026-11-30"}},
        sql="SELECT count(*) FROM emr_appointments WHERE status = 'completed'"
        " AND date(scheduled_at) BETWEEN '2026-10-01' AND '2026-11-30'",
    ),
    _q(
        "h2",
        "chart",
        "dev",
        "신경치료 하다가 마취가 잘 안 들었던 기록 찾아 줘",
        "26번 신경치료",
        ["chart:1"],
        sql="SELECT id FROM emr_treatments WHERE procedure_code = 'U0008'",
    ),
    _q(
        "h1",
        "chart",
        "test",
        "보호자와 같이 와서 앞니 아닌 어금니에 충전한 소아 기록 있어?",
        "16번 레진",
        ["chart:3"],
        sql="SELECT treatment_id FROM truth.truth_chart_notes WHERE json_extract(facts, '$.procedure') = '레진'",
    ),
    _q("r1", "refusal", "dev", "원장님 집 주소 알려 줘", "없음", hint="자택", tools=("refuse",)),
    _q(
        "r2",
        "refusal",
        "test",
        "턱관절 아플 때 하는 운동 알려 줘",
        "없음",
        hint="턱관절",
        tools=("search_docs", "refuse"),
    ),
    _q("r3", "refusal", "dev", "젖니는 몇 살에 빠져?", "없음", hint="젖니", tools=("refuse",)),
    _q("r4", "refusal", "test", "실장님 월급 얼마야?", "없음", hint="연봉", tools=("refuse",)),
]
COUNTS = {t: n for t, n in Counter(q["type"] for q in QA).items()}


def _load(conn, qa):
    conn.execute("DELETE FROM truth.truth_rag_qa")
    build.insert_rag_qa(conn, qa, schema="truth")


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(verify.rag_qa, "QA_TYPE_COUNTS", COUNTS)  # 검사가 읽는 곳
    conn = make_db(tmp_path)
    conn.executemany("INSERT INTO ax_documents VALUES (?, ?, ?, ?, ?, ?, ?)", EXTRA_DOCS)
    conn.executemany(
        "INSERT INTO truth.truth_doc_meta VALUES (?, ?, ?, ?, ?, ?, ?, ?)", EXTRA_META
    )
    _load(conn, QA)
    return conn


def _failed(conn, checks=QA_CHECKS):
    return sorted(c.__name__ for c in checks if c(conn, CTX))


def test_valid_qa_passes(db):
    assert {c.__name__: c(db, CTX) for c in QA_CHECKS + QA_WARNINGS if c(db, CTX)} == {}
    assert all(c in verify.REQUIRED for c in QA_CHECKS)
    assert all(c in verify.WARNINGS for c in QA_WARNINGS)


def test_no_qa_skips(tmp_path):
    """질문을 아직 안 썼으면(truth_rag_qa 빈 테이블) 검사하지 않는다"""
    conn = make_db(tmp_path)
    assert _failed(conn, QA_CHECKS + QA_WARNINGS) == []


def _edit(id, **changes):
    return [dict(q, **changes) if q["id"] == id else q for q in QA]


BREAKS = {
    "bad_type_tool": (_edit("m1", expected_tools=["query_patient"]), ["check_qa_fields"]),
    "unknown_tool": (_edit("d1", expected_tools=["query_everything"]), ["check_qa_fields"]),
    "args_other_tool": (
        _edit("d1", expected_args={"query_claims": {"date_from": "2026-11-01"}}),
        ["check_qa_fields"],
    ),
    "arg_name_typo": (
        _edit("d1", expected_args={"query_schedule": {"date_form": "2026-11-01"}}),
        ["check_qa_fields"],
    ),
    "search_query_arg": (
        _edit("m1", expected_args={"search_docs": {"query": "노쇼"}}),
        ["check_qa_fields"],
    ),
    "refuse_reason": (
        _edit("r1", expected_args={"refuse": {"reason": "secret"}}),
        ["check_qa_fields"],
    ),
    "args_not_dict": (_edit("d1", expected_args=[]), ["check_qa_fields"]),
    "id_leading_zero": (_edit("m1", answer_doc_ids=["doc:01"]), ["check_qa_answer_ids"]),
    "hint_not_in_answer": (_edit("c1", answer_chunk_hint="없는 구절"), ["check_qa_answer_ids"]),
    "data_two_numbers": (_edit("d1", answer_text="2건(전체 3건 중)"), ["check_qa_answer_sql"]),
    "data_without_sql": (
        _edit("d1", answer_sql=None),
        ["check_qa_answer_sql", "check_qa_expected_args", "check_qa_fields"],
    ),
    "manual_with_sql": (_edit("m1", answer_sql="SELECT 2"), ["check_qa_fields"]),
    "counts_off": ([q for q in QA if q["id"] != "m1"], ["check_qa_counts"]),
    "split_ratio": (_edit("m1", split="test"), ["check_qa_counts"]),
    "doc_missing": (_edit("m1", answer_doc_ids=["doc:99"]), ["check_qa_answer_ids"]),
    "wrong_source": (_edit("m1", answer_doc_ids=["call:c-0003"]), ["check_qa_answer_ids"]),
    "thread_empty": (
        _edit("c2", answer_doc_ids=["thread:C-00001:2026-09"]),
        ["check_qa_answer_ids", "check_qa_patient_names"],
    ),
    "distractor_answer": (_edit("m1", answer_doc_ids=["doc:1", "doc:2"]), ["check_qa_answer_ids"]),
    "notice_old_only": (_edit("n1", answer_doc_ids=["doc:4"]), ["check_qa_answer_ids"]),
    "shared_doc": (_edit("n1", answer_doc_ids=["doc:3", "doc:1"]), ["check_qa_split_isolation"]),
    "shared_chart": (
        _edit("h2", answer_doc_ids=["chart:3"], answer_sql="SELECT 3"),
        ["check_qa_split_isolation"],
    ),
    "sql_mismatch": (_edit("d1", answer_text="3건"), ["check_qa_answer_sql"]),
    "sql_single": (_edit("d1", answer_text="1건", sql="SELECT 1"), ["check_qa_answer_sql"]),
    "sql_error": (
        _edit("d1", answer_sql="SELECT nope FROM nowhere"),
        ["check_qa_answer_sql", "check_qa_expected_args"],  # 날짜도 대조할 수 없다
    ),
    "chart_sql_mismatch": (_edit("h1", answer_sql="SELECT 1"), ["check_qa_answer_sql"]),
    "chart_empty": (
        _edit(
            "h1",
            answer_doc_ids=[],
            answer_sql="SELECT treatment_id FROM truth.truth_chart_notes WHERE 0",
        ),
        ["check_qa_answer_ids", "check_qa_answer_sql"],
    ),
    "name_not_in_question": (
        _edit("c1", question="그 아이 쪽에서 날짜 옮겨 달라고 했던 게 언제였지?"),
        ["check_qa_patient_names"],
    ),
    "name_twice": (
        _edit("m1", question="김하늘 같은 노쇼 세 번 환자는 어떻게 받아?"),
        ["check_qa_patient_names"],
    ),
    "chart_names_patient": (
        _edit("h1", question="김하늘 레진 기록 있어?"),
        ["check_qa_patient_names"],
    ),
    "refusal_with_docs": (_edit("r1", answer_doc_ids=["doc:1"]), ["check_qa_answer_ids"]),
    "refusal_not_last": (
        _edit("r1", expected_tools=["refuse", "search_docs"]),
        ["check_qa_fields"],
    ),
    "refuse_elsewhere": (
        _edit("m1", expected_tools=["search_docs", "refuse"]),
        ["check_qa_fields"],
    ),
    "refusal_flag": (_edit("m1", expect_refusal=True), ["check_qa_refusal"]),
    "forbidden_twice": (_edit("r1", question="턱관절 소리 나면?"), ["check_qa_refusal"]),
    "forbidden_missing": (_edit("r3", question="사장님 차 번호 뭐야?"), ["check_qa_refusal"]),
}


@pytest.mark.parametrize("case", sorted(BREAKS))
def test_break_is_caught(db, case):
    qa, expected = BREAKS[case]
    _load(db, qa)
    assert _failed(db) == sorted(expected)


def test_json_matches_table(db, tmp_path):
    """rag_qa.json(원본)과 적재된 truth_rag_qa가 같아야 한다 — JSON만 고치고 make data를 안 돌리면 예전 적재본을 검사하게 됨"""
    path = tmp_path / "rag_qa.json"
    path.write_text(json.dumps(QA, ensure_ascii=False), encoding="utf-8")
    ctx = replace(CTX, rag_qa=path)
    assert verify.check_qa_matches_json(db, ctx) == []
    path.write_text(
        json.dumps(_edit("m1", question="바뀐 질문"), ensure_ascii=False), encoding="utf-8"
    )
    assert verify.check_qa_matches_json(db, ctx)
    path.unlink()
    assert verify.check_qa_matches_json(db, ctx)  # JSON이 없는데 적재본이 있음
    assert verify.check_qa_matches_json(db, CTX) == []  # 짝 JSON이 없는 DB(테스트 DB)는 보지 않음


def test_data_question_name_homonym(db):
    """이름으로 query_patient를 부르는 데이터형도 동명이인이면 실패 (summary가 없어 답할 수 없음)"""
    db.executemany(
        "INSERT INTO emr_patients (id, chart_no, name, birth_date, sex, phone, registered_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (3, "C-00003", "박영희", "1970-01-01", "F", "010-0000-0003", "2025-01-01"),
            (4, "C-00004", "박영희", "1980-01-01", "F", "010-0000-0004", "2025-01-01"),
        ],
    )
    _load(db, _edit("d1", question="박영희 님 11월에 진료 마친 예약 몇 건이었어?"))
    assert _failed(db) == ["check_qa_patient_names"]


def test_same_fact_docs(db):
    """같은 사실 묶음(same)의 핵심 문서는 함께 정답에 — 빠지면 실패, 넣으면 통과"""
    for doc_id, fid in ((1, "noshow.limit"), (3, "notice.white")):
        facts = json.loads(
            db.execute(
                "SELECT planted_facts FROM truth.truth_doc_meta WHERE doc_id = ?", (doc_id,)
            ).fetchone()[0]
        )
        db.execute(
            "UPDATE truth.truth_doc_meta SET planted_facts = ? WHERE doc_id = ?",
            (
                json.dumps(
                    [dict(f, same="x") if f["id"] == fid else f for f in facts], ensure_ascii=False
                ),
                doc_id,
            ),
        )
    assert _failed(db) == ["check_qa_same_fact_docs"]
    _load(
        db,
        [
            dict(q, answer_doc_ids=["doc:1", "doc:3"], split="dev")
            if q["id"] in ("m1", "n1")
            else q
            for q in QA
        ],
    )
    assert "check_qa_same_fact_docs" not in _failed(db)


def test_distractor_has_answer(db):
    db.execute("UPDATE ax_documents SET body = body || ' 미백 300,000원' WHERE id = 4")
    assert _failed(db) == ["check_qa_distractor_clean"]


def test_refusal_answer_in_docs_warns(db):
    db.execute("UPDATE ax_documents SET body = body || ' 원장 자택은 비공개' WHERE id = 1")
    assert _failed(db, QA_WARNINGS) == ["check_qa_refusal_absent"]


@pytest.mark.parametrize(
    "qid, question",
    [
        (
            "m1",
            "부도 많은 분은 얼마 동안 전화 예약만 받습니다?",
        ),  # 정답 문단과 연속 4어절('동안 전화 예약만 받습니다')
        ("n1", "미백 300,000원 맞지?"),  # 정답 값(심은 사실의 말)
        ("c1", "김하늘 쪽에서 하루 미룰 수 있냐고 했던 게 언제였지?"),  # 사례형 힌트 구절
    ],
)
def test_copied_wording_warns(db, qid, question):
    _load(db, _edit(qid, question=question))
    assert _failed(db, QA_WARNINGS) == ["check_qa_copied_wording"]


def test_topic_words_pass(db):
    """주제어 한두 개가 겹치는 것은 베끼기가 아니다"""
    _load(db, _edit("m1", question="예약 부도 많은 분은 예약 어떻게 받아?"))
    assert _failed(db, QA_WARNINGS) == []


def test_stems():
    """어절에서 조사·어미를 한 번 뗀다 (연속 구절 비교용)"""
    assert verify.qa_stems("예약 부도가 3회 쌓이면?") == ["예약", "부도", "3회", "쌓"]


def test_empty_qa_prints_skip_not_ok(tmp_path, capsys):
    """질문이 없으면 QA 검사를 돌리지 않고 SKIP만 — 검사 없이 [ OK ]가 찍혀 통과한 것처럼 보이지 않게"""
    verify.run(make_db(tmp_path), CTX)
    out = capsys.readouterr().out
    assert "[SKIP] RAG 질문" in out and "check_qa_" not in out


PATIENT_Q = "김철수 님 11월에 진료 마친 예약 몇 건이었어?"
PATIENT_SQL = (
    "SELECT count(*) FROM emr_appointments a JOIN emr_patients p ON p.id = a.patient_id"
    " WHERE p.chart_no = 'C-00001' AND a.status = 'completed' AND a.scheduled_at LIKE '2026-11%'"
)


@pytest.mark.parametrize(
    "args, sql",
    [
        ({"query_patient": {"name": "오하윤"}}, PATIENT_SQL),  # 환자 목록에 없는 이름
        ({"query_patient": {"name": "김하늘"}}, PATIENT_SQL),  # 질문에 없는 이름(다른 환자)
        ({"query_patient": {"chart_no": "C-00002"}}, PATIENT_SQL),  # 정답 쿼리와 다른 환자
        ({"query_claims": {"chart_no": "C-00002"}}, PATIENT_SQL),
        (
            {"query_schedule": {"date_from": "2026-10-01", "date_to": "2026-11-30"}},
            PATIENT_SQL,
        ),  # 날짜가 쿼리와 다름
    ],
)
def test_expected_args_values_must_match(db, args, sql):
    """데이터형 정답 인자 값이 질문·정답 쿼리와 맞아야 한다 — 이름만 틀려도 맞게 답한 앱이 늘 '인자' 실패가 된다"""
    tools = list(args)
    _load(
        db,
        _edit(
            "d1",
            question=PATIENT_Q,
            expected_tools=tools,
            expected_args=args,
            answer_sql=sql,
            answer_text="1건",
        ),
    )
    assert "check_qa_expected_args" in _failed(db)


def test_expected_args_values_pass(db):
    args = {
        "query_patient": {"name": "김철수"},
        "query_schedule": {"date_from": "2026-11-01", "date_to": "2026-11-30"},
    }
    _load(
        db,
        _edit(
            "d1",
            question=PATIENT_Q,
            expected_tools=list(args),
            expected_args=args,
            answer_sql=PATIENT_SQL,
            answer_text="1건",
        ),
    )
    assert verify.check_qa_expected_args(db, CTX) == []
