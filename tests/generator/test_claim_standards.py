"""청구 기준 문서 본문(generator/claim_standards.py) — 실제 고시 발췌 절·병원 내부 점검 기준 절
(설계 docs/superpowers/specs/2026-11-13-real-claim-standards-design.md)."""

from backend.claim_check import found
from generator import documents
from generator.claim_standards import INTRO_NOTE, REAL, VIRTUAL, VIRTUAL_MARK, section_body
from generator.doc_catalog import BY_KEY
from generator.master import (
    ADJUST_REASONS,
    CONDITIONAL_INSURED_MIN_AGE,
    PERIO_POCKET_MIN_MM,
    SCALING_MIN_AGE,
)


def test_every_reason_has_one_body():
    assert set(REAL) | set(VIRTUAL) == set(ADJUST_REASONS) and not set(REAL) & set(VIRTUAL)


def test_real_section_shape():
    body = section_body("R05")
    assert body.startswith("> 영구치나 유치의 난발치 및 매복발치는 X-선 촬영 후")
    assert "\n\n출처: 세부사항 2025 p.939, 고시 제2007-46호('07.6.1. 시행)" in body
    assert body.endswith(
        "병원 내부 보충: 촬영으로 확인한 매복 정도와 골삭제 여부를 차트에 함께 적는다."
    )
    assert "병원 내부 보충" not in section_body("D03")
    # 원문은 환자 단위("부분 무치악 환자"), 차트는 부위 단위("무치악 부위") — 보충 줄이 잇는다
    assert section_body("R06").endswith(
        "병원 내부 보충: 차트에 발치 시점이나 무치악 기간과 함께 시술 부위가 무치악 부위임을"
        " 진료 소견으로 적는다. 환자의 말만으로는 근거가 아니다."
    )


def test_virtual_section_marked():
    assert section_body("R03").startswith(VIRTUAL_MARK + "\n\n- ")
    assert f"{PERIO_POCKET_MIN_MM}mm" in section_body("R03")


def test_quote_ages_match_master():
    assert f"{SCALING_MIN_AGE}세 이상" in REAL["E01"].quote[-1]
    assert f"{CONDITIONAL_INSURED_MIN_AGE}세 이상" in REAL["R06"].quote[0]


def test_words_inside_section():
    for r, x in REAL.items():
        assert all(w in section_body(r) for w in x.words), r


def test_found_quote_inside_real_section():
    """한 문단 = 한 줄이라 문단 안 문장 인용이 후검증을 통과한다(인용 표시 '>'가 끼지 않게)"""
    assert found(
        "X-선 촬영 없이 일률적으로 산정된 난발치의 경우는 해당 발치로", section_body("R05")
    )
    assert found("청구된 상병분류기호는 의무기록 내용과 일치하여야 한다", section_body("C01"))


def test_claim_doc_body_is_code():
    body = documents.draft(BY_KEY["claim-외과임플란트"])
    assert INTRO_NOTE in body.split("\n## ")[0]
    assert "## 발치 난이도 산정 근거\n\n" + section_body("R05") in body


def test_quote_one_line_per_element():
    """원문 한 줄 = 인용 원소 하나 — 원소 안에서 줄을 나누지 않는다. R06 '4.'는 따로 인용 문단이라 마크다운 번호가
    원문대로 4(한 목록으로 묶이면 '2.'로 보인다)"""
    assert not [r for r, x in REAL.items() for q in x.quote if "\n" in q]
    assert "\n>\n> 4. 다만, 아래 중 하나에" in section_body("R06")


def test_found_quote_across_quote_lines():
    """두 인용 줄에 걸친 원문 인용도 후검증을 통과한다(인용 표시 '>'는 내용이 아님)"""
    assert found("1. 급여대상 가. 부분 무치악 환자에 대하여", section_body("R06"))
    assert found("일치하여야 한다. ○ 요양급여비용 명세서에", section_body("C01"))
