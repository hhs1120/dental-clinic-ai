"""5b단계 차트 사실 — small을 실제로 생성해 오류 비율·디코이 층화·사실 구성을 본다 (가상 데이터 생성 설계 §4.3).
필수 검사 전부는 tests/test_records.py가 같은 small로 본다."""
import json
import math
import sqlite3
from collections import Counter

import pytest

from generator import build, chart, verify
from generator.master import (CHART_DECOY_RATIO, DIAGNOSIS_ERROR_RATE, PROCEDURE_DIAGNOSES, REQUIREMENT_CUT_ERROR_SHARE,
                              REQUIREMENT_ERROR_RATE)
from generator.paths import ROOT

CONFIG = ROOT / "config" / "generator.yaml"


def _open(out):
    conn = sqlite3.connect(out / "app.sqlite")
    conn.row_factory = sqlite3.Row
    conn.execute("ATTACH DATABASE ? AS truth", (str(out / "truth.sqlite"),))
    return conn


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    out = tmp_path_factory.mktemp("chart")
    build.generate("small", out / "app.sqlite", out / "truth.sqlite", CONFIG)
    conn = _open(out)
    ctx = verify.load_ctx(CONFIG, conn)
    return conn, ctx, verify._chart_lines(conn, ctx)


def _within(actual, n, p):
    """이항 4σ (+1)"""
    return abs(actual - n * p) <= 4 * math.sqrt(n * p * (1 - p)) + 1


def test_settled_errors_follow_review(db):
    """확정 월: 상병 사유로 깎인 줄은 전부 상병 착오, 요건 사유로 깎인 줄 중 REQUIREMENT_CUT_ERROR_SHARE가 요건 오류"""
    _, _, lines = db
    cut = [r for r in lines if r["window"] and r["cut"] is not None]
    diagnosis = [r for r in cut if r["cut"]["type"] == "diagnosis"]
    requirement = [r for r in cut if r["cut"]["type"] == "requirement"]
    assert diagnosis and all(r["error"] == "diagnosis" for r in diagnosis)
    assert requirement
    assert _within(sum(r["error"] == "requirement" for r in requirement), len(requirement), REQUIREMENT_CUT_ERROR_SHARE)


def test_open_month_error_rates(db):
    """제출 월·전월: 요건 줄의 REQUIREMENT_ERROR_RATE가 요건 오류, 나머지 급여 줄의 DIAGNOSIS_ERROR_RATE가 상병 착오"""
    _, _, lines = db
    open_lines = [r for r in lines if r["window"] and r["bucket"] in ("submitted", "pending")]
    req = [r for r in open_lines if r["requirement"]]
    assert _within(sum(r["error"] == "requirement" for r in req), len(req), REQUIREMENT_ERROR_RATE)
    rest = [r for r in open_lines if r["error"] != "requirement"]
    assert _within(sum(r["error"] == "diagnosis" for r in rest), len(rest), DIAGNOSIS_ERROR_RATE)
    assert sum(bool(r["error"]) for r in open_lines if r["bucket"] == "pending") >= 10  # ⑤-2 평가 모수


def test_error_diagnosis_is_another_procedures_dx(db):
    """상병 착오의 상병도 정상 EMR 값 — 다른 시술의 허용 상병 중 하나"""
    _, _, lines = db
    normal = set().union(*PROCEDURE_DIAGNOSES.values())
    errors = [r for r in lines if r["error"] == "diagnosis"]
    assert errors and all(r["dx_code"] in normal for r in errors)


def test_decoys_stratified_by_procedure(db):
    """디코이 수 = round(특이 × CHART_DECOY_RATIO), 시술별로 평범한 창 안 줄 수에 비례(최대 나머지 배분 ±1)"""
    _, _, lines = db
    window = [r for r in lines if r["window"]]
    specials = sum(r["is_special"] for r in window)
    decoys = [r for r in window if r["is_decoy"]]
    assert len(decoys) == round(specials * CHART_DECOY_RATIO)
    plain = Counter(r["procedure_code"] for r in window if not r["is_special"])
    got = Counter(r["procedure_code"] for r in decoys)
    for code, n in plain.items():
        assert abs(got[code] - len(decoys) * n / sum(plain.values())) <= 1, code


def test_facts(db):
    """사실: 서술 줄은 요건 사실(요건 줄)·주의사항 처리를 담는다. 요건 근거가 없으면 사실에도 없다"""
    conn, _, _ = db
    rows = [dict(r) for r in conn.execute("SELECT * FROM truth.truth_chart_notes")]
    facts = {r["treatment_id"]: json.loads(r["facts"]) for r in rows}
    assert all("procedure" in f and "cc" in f for f in facts.values())
    req = [r for r in rows if r["requirement"]]
    assert req and all(facts[r["treatment_id"]]["requirement"]["key"] == r["requirement"]
                       and facts[r["treatment_id"]]["requirement"]["evidence"] == bool(r["evidence_present"]) for r in req)
    r03 = [facts[r["treatment_id"]] for r in req if r["requirement"] == "R03"]
    assert r03 and all((f["findings"]["pocket_mm"] >= 4) == f["requirement"]["evidence"] for f in r03)
    assert any(f.get("alerts") for f in facts.values()) and any(f.get("discovered") for f in facts.values())


def test_judgment_report_near_target(db):
    """판정 분포가 ADJUST_JUDGMENT_TARGET 근처 — small 확정 월은 8월 하나라 표본 크기에 맞춰 이항 4σ"""
    from generator.master import ADJUST_JUDGMENT_TARGET
    conn, ctx, _ = db
    report = verify.report_adjust_judgment(conn, ctx)
    assert report["n"] > 0
    for key, target in ADJUST_JUDGMENT_TARGET.items():
        assert _within(round(report[key] * report["n"]), report["n"], target / 100), (key, report)


def test_errors_independent_of_decoy_ratio(db, tmp_path, monkeypatch):
    """디코이 배수를 바꿔도 오류·상병·요건은 그대로 — 사실(chart_facts)과 본문(chart_notes) 난수를 나눈다 (§5.1)"""
    conn, _, _ = db
    monkeypatch.setattr(chart, "CHART_DECOY_RATIO", 1.0)
    build.generate("small", tmp_path / "app.sqlite", tmp_path / "truth.sqlite", CONFIG)
    other = _open(tmp_path)
    for sql in ("SELECT * FROM truth.truth_claim_errors ORDER BY treatment_id",
                "SELECT treatment_id, dx_code FROM emr_chart_notes ORDER BY treatment_id",
                "SELECT treatment_id, requirement, evidence_present, facts FROM truth.truth_chart_notes ORDER BY treatment_id"):
        assert [tuple(r) for r in conn.execute(sql)] == [tuple(r) for r in other.execute(sql)], sql
    assert other.execute("SELECT sum(is_decoy) FROM truth.truth_chart_notes").fetchone()[0] < \
           conn.execute("SELECT sum(is_decoy) FROM truth.truth_chart_notes").fetchone()[0]
