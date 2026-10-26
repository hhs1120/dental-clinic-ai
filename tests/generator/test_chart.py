"""5b단계 차트 사실 — 실제로 생성해 오류 비율·디코이 층화·사실 구성을 본다 (가상 데이터 생성 설계 §4.3).
필수 검사 전부는 tests/generator/test_records.py가 본다."""
import json
import math
import sqlite3
from collections import Counter, defaultdict

import pytest

from generator import build, chart, verify
from generator.master import (CHART_DECOY_RATIO, CHART_FORBIDDEN_WORDS, DIAGNOSIS_ERROR_RATE, PROCEDURE_DIAGNOSES, REQUIREMENT_CUT_ERROR_SHARE,
                              REQUIREMENT_ERROR_RATE)
from generator.paths import ROOT

CONFIG = ROOT / "config" / "generator.yaml"
RUN = {}  # db 픽스처의 생성 실행(run.state — 4단계 에피소드)


def _open(out):
    conn = sqlite3.connect(out / "app.sqlite")
    conn.row_factory = sqlite3.Row
    conn.execute("ATTACH DATABASE ? AS truth", (str(out / "truth.sqlite"),))
    return conn


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    out = tmp_path_factory.mktemp("chart")
    RUN["run"] = build.generate(out / "app.sqlite", out / "truth.sqlite", CONFIG, fake_llm=True)
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
    """판정 분포가 ADJUST_JUDGMENT_TARGET 근처 — 표본 크기에 맞춰 이항 4σ"""
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
    build.generate(tmp_path / "app.sqlite", tmp_path / "truth.sqlite", CONFIG, fake_llm=True)
    other = _open(tmp_path)
    for sql in ("SELECT * FROM truth.truth_claim_errors ORDER BY treatment_id",
                "SELECT treatment_id, dx_code FROM emr_chart_notes ORDER BY treatment_id",
                "SELECT treatment_id, requirement, evidence_present, facts FROM truth.truth_chart_notes ORDER BY treatment_id"):
        assert [tuple(r) for r in conn.execute(sql)] == [tuple(r) for r in other.execute(sql)], sql
    assert other.execute("SELECT sum(is_decoy) FROM truth.truth_chart_notes").fetchone()[0] < \
           conn.execute("SELECT sum(is_decoy) FROM truth.truth_chart_notes").fetchone()[0]


def test_open_month_errors_independent_of_review(db, tmp_path, monkeypatch):
    """심사 결과 가중을 바꿔도 제출 월·전월의 오류(⑤-2 정답)·요건 사실·상병은 그대로 — 확정 월 추첨과 난수를 나누고,
    사실은 진료마다 따로 시드한다 (§5.1)"""
    from generator import records
    conn, _, _ = db
    monkeypatch.setattr(records, "ADJUST_TYPE_WEIGHTS", {"clinical": {"requirement": 50, "documentation": 40, "diagnosis": 10},
                                                         "other": {"requirement": 0, "documentation": 50, "diagnosis": 50}})
    build.generate(tmp_path / "app.sqlite", tmp_path / "truth.sqlite", CONFIG, fake_llm=True)
    other = _open(tmp_path)
    open_months = "SELECT treatment_id FROM emr_claims WHERE status IN ('submitted', 'pending')"
    for sql in (f"SELECT * FROM truth.truth_claim_errors WHERE treatment_id IN ({open_months}) ORDER BY treatment_id",
                f"SELECT treatment_id, requirement, evidence_present, facts FROM truth.truth_chart_notes"
                f" WHERE treatment_id IN ({open_months}) ORDER BY treatment_id",
                f"SELECT treatment_id, dx_code FROM emr_chart_notes WHERE treatment_id IN ({open_months}) ORDER BY treatment_id"):
        assert [tuple(r) for r in conn.execute(sql)] == [tuple(r) for r in other.execute(sql)], sql
    settled = "SELECT treatment_id, adjust_note FROM emr_claims WHERE status IN ('adjusted', 'denied') ORDER BY treatment_id"
    assert [tuple(r) for r in conn.execute(settled)] != [tuple(r) for r in other.execute(settled)]  # 심사 결과는 바뀜


def test_plan_counts_only_booked_by_that_day(db):
    """다음 계획의 남은 내원 = 같은 에피소드에서 그날까지 잡혀 있던(created ≤ 그날) 이후 비취소 예약 — 미래 사실 없음 (§4.3)"""
    conn, _, _ = db
    episode = RUN["run"].state["episode"]
    later = defaultdict(list)
    for a in conn.execute("SELECT id, date(scheduled_at), date(created_at) FROM emr_appointments WHERE status != 'cancelled'"):
        if a[0] in episode:
            later[episode[a[0]]].append((a[1], a[2]))
    checked = 0
    for tid, appt_id, day, facts in conn.execute(
            "SELECT t.id, t.appointment_id, date(t.performed_at), n.facts FROM emr_treatments t"
            " JOIN truth.truth_chart_notes n ON n.treatment_id = t.id"):
        expected = sum(1 for sched, created in later.get(episode.get(appt_id), ()) if sched > day and created <= day)
        plan = json.loads(facts)["plan"] or {}
        assert plan.get("remaining", 0) == expected, (tid, plan, expected)
        checked += bool(expected)
    assert checked


def test_draft_sentences(db):
    """초안 문장: 조사 처리('2차 우식으로'), 주의사항이 있으면 '특이사항은 없습니다' 템플릿을 쓰지 않음"""
    conn, _, _ = db
    notes = [r[0] for r in conn.execute("SELECT note FROM emr_chart_notes")]
    assert not any("우식로" in n for n in notes)
    assert not any("특이사항은 없습니다" in n and ("주의사항" in n or "진료 중" in n) for n in notes)


# ── 9b단계 차트 서술 작업 (§4.3 '9b단계 구현 결정') ────────────

FACTS = {"alerts": [{"label": "고혈압", "action": "혈압 측정 후 진행"}], "cc": "잇몸이 내려간 것 같아요",
         "discovered": ["마취가 잘 안 됨"], "findings": {"bleeding": True, "calculus": "많음", "mobility": 0, "pocket_mm": 6},
         "plan": {"remaining": 2}, "procedure": "잇몸치료", "tooth": None,
         "requirement": {"key": "R02", "evidence": True, "scaling_days_ago": 14, "linked": True}}


def test_narrative_must_include_facts():
    names = {"잇몸치료": ["잇몸치료", "치주치료"], "신경치료": ["신경치료"]}
    got = chart.narrative_must_include(FACTS, names)
    assert got == [["잇몸치료", "치주치료"], ["6mm"], ["고혈압"], ["마취가 잘 안 됨"], ["14일"], ["2회"]]
    tooth = {**FACTS, "tooth": "26", "procedure": "신경치료", "findings": None, "alerts": [], "discovered": [],
             "requirement": {"key": "R01", "evidence": True, "prev_days": 200, "reason": "2차 우식"}, "plan": None}
    assert chart.narrative_must_include(tooth, names) == [["26번"], ["신경치료"], ["7개월"]]
    other = {**tooth, "requirement": {"key": "R06", "evidence": True, "other_clinic": True, "edentulous_months": 5}}
    assert chart.narrative_must_include(other, names)[-1] == ["5개월"]
    sealant = {**tooth, "requirement": {"key": "R04", "evidence": True, "surface": "깊은 소와열구가 있고 우식이 없는 교합면"}}
    assert chart.narrative_must_include(sealant, names)[-1] == ["소와열구"]
    impacted = {**tooth, "requirement": {"key": "R05", "evidence": True, "impaction": "부분 매복", "bone_removal": True}}
    assert chart.narrative_must_include(impacted, names)[-1] == ["부분 매복"]


def test_narrative_checks_forbid_answer_words_and_patient_name():
    c = chart.narrative_checks(FACTS, {"잇몸치료": ["잇몸치료"]}, "김철수")
    assert c["field"] == "note" and "김철수" in c["forbidden_words"]
    assert set(CHART_FORBIDDEN_WORDS) <= set(c["forbidden_words"]) and c["forbidden_patterns"]


def test_narrative_stage_fake_keeps_draft(db, tmp_path):
    """가짜 게이트웨이면 초안이 그대로 남는다 — db 픽스처(가짜 모드 생성)의 서술 줄 본문 = 초안"""
    conn, _, _ = db
    row = conn.execute("SELECT n.note, t.facts FROM emr_chart_notes n JOIN truth.truth_chart_notes t USING (treatment_id)"
                       " WHERE t.kind = 'narrative' LIMIT 1").fetchone()
    assert row["note"] == chart.draft_note(json.loads(row["facts"]))
