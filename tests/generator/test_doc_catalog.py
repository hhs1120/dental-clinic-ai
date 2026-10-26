"""RAG 문서 목록 표 (generator/doc_catalog.py, 가상 데이터 생성 설계 §4.2 '9단계 구현 결정') — 표 자체의 규칙."""
import json
from collections import Counter
from datetime import date

import yaml

from generator.doc_catalog import BY_KEY, CATALOG, CLAIM_DOCS, CLAIM_HEADINGS, KINDS
from generator.master import ADJUST_REASONS, HOURS, PROCEDURES
from generator.paths import ROOT

CFG = yaml.safe_load((ROOT / "config" / "generator.yaml").read_text(encoding="utf-8"))
HOLIDAYS = set(json.loads((ROOT / CFG["holidays"]).read_text(encoding="utf-8")))
RANGE_START = CFG["range"]["start"].isoformat()
TODAY = CFG["today"].isoformat()
NOTICE = [d for d in CATALOG if d.type == "notice" and d.kind == "core"]
OLD = [d for d in CATALOG if d.kind == "old_version"]


def test_counts_match_config_and_types():
    kinds = Counter(d.kind for d in CATALOG)
    assert kinds["core"] == CFG["documents"]["core"]
    assert sum(kinds[k] for k in KINDS if k != "core") == CFG["documents"]["distractor"]
    # 종류별 수는 §4.2 표와 test_doc_sync.py가 비교
    assert len(BY_KEY) == len(CATALOG)


def test_interview_topics_in_manual():
    titles = {d.title for d in CATALOG if d.type == "manual" and d.kind == "core"}
    assert {"틀니 수리 접수 절차", "구형 임플란트 부품 주문", "일계표 마감"} <= titles


def test_slot_lengths_from_master():
    """예약 길이 매뉴얼은 master 시술 소요 시간 — 가상 예약 데이터와 같은 값"""
    words = BY_KEY["manual-slot-length"].facts[0].words
    minutes = {p["name"]: p["duration_min"] for p in PROCEDURES}
    assert words == (f"{minutes['임플란트 1차']}분", f"{minutes['신경치료']}분")


def test_core_facts_have_words_except_education():
    for d in CATALOG:
        if d.kind == "core":
            assert bool(d.facts) == (d.type != "education"), d.key
            assert all(f.words for f in d.facts if d.type != "claim_standard"), d.key
        assert len({f.id for f in d.facts}) == len(d.facts), d.key


def test_fact_words_in_fact_text():
    """초안은 사실 문장을 그대로 옮긴다 — 말이 문장에 없으면 초안이 검증에 걸린다 (청구 기준 소제목은 초안이 따로 씀)"""
    for d in CATALOG:
        for f in d.facts:
            if not f.id.endswith(".heading"):
                assert all(w in f.text for w in f.words), (d.key, f.id)


def test_fact_ids_unique_across_catalog():
    ids = [f.id for d in CATALOG for f in d.facts]
    assert len(ids) == len(set(ids))


def test_distractor_pairs():
    """비슷한 주제·타 병원은 핵심 문서를 짝으로, type은 짝과 같음(타 병원은 external). 옛 버전은 최신 공지가 짝"""
    for d in CATALOG:
        if d.kind == "core":
            assert d.pair is None
            continue
        pair = BY_KEY[d.pair]
        assert pair.kind == "core", d.key
        if d.kind == "similar":
            assert d.type == pair.type and not d.facts, d.key
        elif d.kind == "external":
            assert d.type == "external" and d.facts, d.key
        else:
            assert d.type == pair.type == "notice" and d.title == pair.title, d.key


def test_notice_versions():
    """최신 공지마다 옛 버전 정확히 1개. 옛 < 생성 기간 시작, 옛 < 최신 ≤ TODAY, EMR과 겹치면 최신 ≤ 생성 기간 시작"""
    assert sorted(d.pair for d in OLD) == sorted(d.key for d in NOTICE)
    for old in OLD:
        new = BY_KEY[old.pair]
        assert old.effective < RANGE_START and old.effective < new.effective <= TODAY, old.key
        if new.emr:
            assert new.effective <= RANGE_START, new.key
        assert {w for f in old.facts for w in f.words} != {w for f in new.facts for w in f.words}, old.key
    assert all(d.effective is None for d in CATALOG if d.type != "notice")


def test_holiday_notices_match_config():
    for key in ("notice-chuseok", "notice-seollal"):
        days = [w for f in BY_KEY[key].facts for w in f.words]
        for w in days:
            m, d = (int(x) for x in w.replace("월", "").replace("일", "").split())
            assert date(2026, m, d).isoformat() in HOLIDAYS, (key, w)


def test_claim_standard_covers_every_reason_once():
    assert set(CLAIM_HEADINGS) == set(ADJUST_REASONS)
    reasons = [r for rs in CLAIM_DOCS.values() for r in rs]
    assert sorted(reasons) == sorted(ADJUST_REASONS)
    heads = [f.reason for d in CATALOG if d.type == "claim_standard" for f in d.facts if f.id.endswith(".heading")]
    assert sorted(heads) == sorted(ADJUST_REASONS)
    rules = [f.reason for d in CATALOG if d.type == "claim_standard" for f in d.facts if not f.id.endswith(".heading")]
    assert sorted(rules) == sorted(ADJUST_REASONS)  # 사유마다 기준 문장 하나


def test_emr_values_from_master():
    """EMR과 겹치는 값은 master에서 — 진료시간·미백 가격이 master와 같다"""
    hours = {w for f in BY_KEY["faq-hours"].facts for w in f.words}
    assert {"9시 30분", "6시 30분", "1시 30분"} <= hours and HOURS["weekday_close"] == "18:30"
    whitening = next(p for p in PROCEDURES if p["name"] == "미백")["fee_total"]
    assert f"{whitening:,}원" in BY_KEY["notice-whitening"].facts[0].words
