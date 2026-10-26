"""verify RAG 문서 검사(9단계 — 가상 데이터 생성 설계 §4.2·§5.3)가 위반을 잡는지 — 정상 DB를 한 곳씩 깨뜨린다.
fixture 문서: 1 매뉴얼 / 2 비슷한 주제(1의 짝) / 3 최신 공지(4 대체) / 4 옛 공지 / 5 청구 기준 / 6 타 병원"""
import json

import pytest

from generator import verify
from tests.generator.verify_db import CTX, make_db

DOC_CHECKS = [verify.check_document_counts, verify.check_doc_meta_match, verify.check_doc_planted_facts,
              verify.check_doc_similar_clean, verify.check_notice_versions, verify.check_claim_sections,
              verify.check_doc_patient_names, verify.check_forbidden_topics]


def test_valid_db_passes_document_checks(tmp_path):
    conn = make_db(tmp_path)
    assert {c.__name__: c(conn, CTX) for c in DOC_CHECKS if c(conn, CTX)} == {}
    assert all(c in verify.REQUIRED for c in DOC_CHECKS)


def _facts(conn, doc_id, facts):
    conn.execute("UPDATE truth.truth_doc_meta SET planted_facts = ? WHERE doc_id = ?",
                 (json.dumps(facts, ensure_ascii=False), doc_id))


D = "UPDATE ax_documents SET "
M = "UPDATE truth.truth_doc_meta SET "
BREAKS = {
    "doc_missing": ("DELETE FROM ax_documents WHERE id = 6", ["check_document_counts", "check_doc_meta_match"]),
    "distractor_count": (M + "kind = 'core', is_distractor = 0, pair_doc_id = NULL WHERE doc_id = 6",
                         ["check_document_counts"]),
    "fact_missing": (D + "body = '예약 부도가 쌓이면 제한합니다.' WHERE id = 1", ["check_doc_planted_facts"]),
    "fact_digit_prefix": (D + "body = '부도 13회면 13개월' WHERE id = 1", ["check_doc_planted_facts"]),
    "similar_has_pair_fact": (D + "body = '부도 3회면 3개월간 막습니다.' WHERE id = 2", ["check_doc_similar_clean"]),
    "old_after_range_start": (D + "effective_date = '2026-10-02' WHERE id = 4", ["check_notice_versions"]),
    "old_not_before_new": (D + "effective_date = '2026-09-01' WHERE id = 4", ["check_notice_versions"]),
    "new_after_today": (D + "effective_date = '2026-12-02' WHERE id = 3", ["check_notice_versions"]),
    "supersedes_missing": (M + "supersedes_doc_id = NULL WHERE doc_id = 3", ["check_notice_versions"]),
    "old_not_notice": (D + "type = 'manual' WHERE id = 4", ["check_notice_versions"]),
    "heading_missing": (D + "body = replace(body, '## 내역설명 기재', '내역설명') WHERE id = 5", ["check_doc_planted_facts"]),
    "patient_name": (D + "body = body || ' 김철수 님 사례' WHERE id = 1", ["check_doc_patient_names"]),
    "forbidden_doc": (D + "body = body || ' 턱관절 운동' WHERE id = 1", ["check_forbidden_topics"]),
    "forbidden_call": ("UPDATE ax_calls SET transcript = transcript || ' 젖니' WHERE id = 1", ["check_forbidden_topics"]),
    "forbidden_chart": ("UPDATE emr_chart_notes SET note = note || ' TMJ' WHERE treatment_id = 1", ["check_forbidden_topics"]),
}


@pytest.mark.parametrize("case", sorted(BREAKS))
def test_break_is_caught(tmp_path, case):
    sql, expected = BREAKS[case]
    conn = make_db(tmp_path)
    conn.execute(sql)
    failed = sorted(c.__name__ for c in DOC_CHECKS if c(conn, CTX))
    assert failed == sorted(expected)


def test_claim_reason_twice_or_missing(tmp_path):
    conn = make_db(tmp_path)
    facts = json.loads(conn.execute("SELECT planted_facts FROM truth.truth_doc_meta WHERE doc_id = 5").fetchone()[0])
    _facts(conn, 5, facts[1:])  # 첫 사유의 절 없음
    assert verify.check_claim_sections(conn, CTX)
    _facts(conn, 5, facts + facts[:1])  # 첫 사유가 두 절
    assert verify.check_claim_sections(conn, CTX)
