"""데이터 재설계 ⑤-2 차트 서술(설계 docs/superpowers/specs/2026-11-11-data-redesign-design.md §3) — 근거 표현 방식
(그대로·바꿔 말하기·부정·R03 숫자 없는 깊이·R03 부분 소견)과 R03 복합 기준. 청구 오류 정답은 그대로."""

import hashlib
import json
import sqlite3

import pytest

from generator import chart, doc_catalog
from generator.master import PERIO_POCKET_MIN_MM


@pytest.fixture(scope="module")
def conn(generated):
    out, _ = generated("chart_redesign")
    c = sqlite3.connect(out / "source.sqlite")
    c.execute("ATTACH DATABASE ? AS truth", (str(out / "truth.sqlite"),))
    return c


def _h(rows) -> str:
    return hashlib.sha256(repr(rows).encode()).hexdigest()[:16]


def test_existing_claim_errors_unchanged(conn):
    """임상 요건형·상병 착오 정답은 그대로(재설계 전 실데이터 값), 추가 처치 누락은 기존 11건이 그대로 남고 더해지기만"""
    rows = conn.execute(
        "select * from truth.truth_claim_errors where kind != 'omitted_extra' order by 1"
    ).fetchall()
    assert _h(rows) == "1fa2be086ed75f2f"
    extra = conn.execute(
        "select * from truth.truth_claim_errors where kind = 'omitted_extra' order by 1"
    ).fetchall()
    assert len(extra) > 11


def test_old_extra_rows_kept(conn):
    """기존 추가 처치 누락 11건(재설계 전 실데이터 — 진료 id·차트에만 있는 처치·치아)이 새 데이터에 그대로 있다"""
    old = {
        (9529, "U0006", "47"),
        (9559, "U0006", "27"),
        (9690, "U0006", "14"),
        (9697, "U0006", "13"),
        (9780, "U0006", "44"),
        (9854, "U0006", "16"),
        (9880, "U0006", "41"),
        (10085, "U0004", "16"),
        (10096, "U0006", "32"),
        (10149, "U0006", "35"),
        (10156, "U0006", "11"),
    }
    got = set(
        conn.execute(
            "select treatment_id, extra_procedure_code, extra_tooth_no from truth.truth_claim_errors"
            " where kind = 'omitted_extra'"
        ).fetchall()
    )
    assert len(old) == 11 and old <= got


def test_styles_present(conn):
    styles = {
        json.loads(f)["requirement"].get("style")
        for (f,) in conn.execute(
            "select facts from truth.truth_chart_notes where requirement is not null and kind = 'narrative'"
        )
    }
    assert {"explicit", "paraphrase", "negated", "plain", "qualitative", "near_miss"} <= styles


def test_near_miss_is_no_evidence_with_deep_pocket(conn):
    """R03 부분 소견 — 치주낭은 기준 이상인데 출혈이 없어 근거 없음(복합 기준)"""
    for (f,) in conn.execute(
        "select facts from truth.truth_chart_notes where requirement = 'R03' and evidence_present = 0"
    ):
        facts = json.loads(f)
        if facts["requirement"].get("style") == "near_miss":
            assert facts["findings"]["pocket_mm"] >= PERIO_POCKET_MIN_MM
            assert facts["findings"]["bleeding"] is False


def test_negated_evidence_passes_checks():
    """부정 표현 줄은 근거 낱말이 금지어에 없고, 부정 문구를 꼭 넣는다"""
    req = {
        "key": "R01",
        "evidence": False,
        "style": "negated",
        "reason": "2차 우식",
        "prev_days": 200,
    }
    f = {
        "tooth": "16",
        "procedure": "아말감/GI",
        "findings": None,
        "alerts": [],
        "discovered": [],
        "requirement": req,
        "plan": None,
    }
    checks = chart.narrative_checks(f, "환자")
    assert "파절" not in checks["forbidden_words"]
    assert [chart.NEGATION["R01"]] in checks["must_include"]


def test_paraphrase_forbids_explicit_word():
    req = {"key": "R04", "evidence": True, "style": "paraphrase", "surface": "x"}
    f = {
        "tooth": "16",
        "procedure": "실란트",
        "findings": None,
        "alerts": [],
        "discovered": [],
        "requirement": req,
        "plan": None,
    }
    checks = chart.narrative_checks(f, "환자")
    assert "소와열구" in checks["forbidden_words"]
    assert [chart.PARAPHRASE["소와열구"]] in checks["must_include"]


def test_style_not_in_llm_payload():
    req = {"key": "R04", "evidence": True, "style": "paraphrase", "surface": "x"}
    f = {"tooth": "16", "procedure": "실란트", "findings": None, "requirement": req}
    assert "style" not in json.dumps(chart.narrative_payload(f, "초안"), ensure_ascii=False)


def test_perio_threshold_single_source():
    text = str(doc_catalog._CLAIM_RULE["R03"])
    assert f"{PERIO_POCKET_MIN_MM}mm" in text and "출혈" in text
