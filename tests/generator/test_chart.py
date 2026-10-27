"""5b단계 차트 사실 — 실제로 생성해 오류 비율·디코이 층화·사실 구성을 본다 (가상 데이터 생성 설계 §4.3).
필수 검사 전부는 tests/generator/test_records.py가 본다."""

import json
import math
import sqlite3
from collections import Counter

import pytest

from generator import build, chart, verify
from generator.master import (
    CHART_DECOY_RATIO,
    CHART_FORBIDDEN_WORDS,
    DIAGNOSIS_ERROR_RATE,
    PROCEDURE_DIAGNOSES,
    REQUIREMENT_CUT_ERROR_SHARE,
    REQUIREMENT_ERROR_RATE,
)
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
    assert _within(
        sum(r["error"] == "requirement" for r in requirement),
        len(requirement),
        REQUIREMENT_CUT_ERROR_SHARE,
    )


def test_open_month_error_rates(db):
    """제출 월·전월: 요건 줄의 REQUIREMENT_ERROR_RATE가 요건 오류, 나머지 급여 줄의 DIAGNOSIS_ERROR_RATE가 상병 착오"""
    _, _, lines = db
    open_lines = [r for r in lines if r["window"] and r["bucket"] in ("submitted", "pending")]
    req = [r for r in open_lines if r["requirement"]]
    assert _within(sum(r["error"] == "requirement" for r in req), len(req), REQUIREMENT_ERROR_RATE)
    rest = [r for r in open_lines if r["error"] != "requirement"]
    assert _within(sum(r["error"] == "diagnosis" for r in rest), len(rest), DIAGNOSIS_ERROR_RATE)
    assert (
        sum(bool(r["error"]) for r in open_lines if r["bucket"] == "pending") >= 10
    )  # ⑤-2 평가 모수


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
    assert req and all(
        facts[r["treatment_id"]]["requirement"]["key"] == r["requirement"]
        and facts[r["treatment_id"]]["requirement"]["evidence"] == bool(r["evidence_present"])
        for r in req
    )
    r03 = [facts[r["treatment_id"]] for r in req if r["requirement"] == "R03"]
    assert r03 and all(
        (f["findings"]["pocket_mm"] >= 4) == f["requirement"]["evidence"] for f in r03
    )
    assert any(f.get("alerts") for f in facts.values()) and any(
        f.get("discovered") for f in facts.values()
    )


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
    for sql in (
        "SELECT * FROM truth.truth_claim_errors ORDER BY treatment_id",
        "SELECT treatment_id, dx_code FROM emr_chart_notes ORDER BY treatment_id",
        "SELECT treatment_id, requirement, evidence_present, facts FROM truth.truth_chart_notes ORDER BY treatment_id",
    ):
        assert [tuple(r) for r in conn.execute(sql)] == [tuple(r) for r in other.execute(sql)], sql
    assert (
        other.execute("SELECT sum(is_decoy) FROM truth.truth_chart_notes").fetchone()[0]
        < conn.execute("SELECT sum(is_decoy) FROM truth.truth_chart_notes").fetchone()[0]
    )


def test_open_month_errors_independent_of_review(db, tmp_path, monkeypatch):
    """심사 결과 가중을 바꿔도 제출 월·전월의 오류(⑤-2 정답)·요건 사실·상병은 그대로 — 확정 월 추첨과 난수를 나누고,
    사실은 진료마다 따로 시드한다 (§5.1)"""
    from generator import records

    conn, _, _ = db
    monkeypatch.setattr(
        records,
        "ADJUST_TYPE_WEIGHTS",
        {
            "clinical": {"requirement": 50, "documentation": 40, "diagnosis": 10},
            "other": {"requirement": 0, "documentation": 50, "diagnosis": 50},
        },
    )
    build.generate(tmp_path / "app.sqlite", tmp_path / "truth.sqlite", CONFIG, fake_llm=True)
    other = _open(tmp_path)
    open_months = "SELECT treatment_id FROM emr_claims WHERE status IN ('submitted', 'pending')"
    for sql in (
        f"SELECT * FROM truth.truth_claim_errors WHERE treatment_id IN ({open_months}) ORDER BY treatment_id",
        f"SELECT treatment_id, requirement, evidence_present, facts FROM truth.truth_chart_notes"
        f" WHERE treatment_id IN ({open_months}) ORDER BY treatment_id",
        f"SELECT treatment_id, dx_code FROM emr_chart_notes WHERE treatment_id IN ({open_months}) ORDER BY treatment_id",
    ):
        assert [tuple(r) for r in conn.execute(sql)] == [tuple(r) for r in other.execute(sql)], sql
    settled = "SELECT treatment_id, adjust_note FROM emr_claims WHERE status IN ('adjusted', 'denied') ORDER BY treatment_id"
    assert [tuple(r) for r in conn.execute(settled)] != [
        tuple(r) for r in other.execute(settled)
    ]  # 심사 결과는 바뀜


def test_plan_remaining_is_course_visits_left(db):
    """다음 계획의 남은 내원 = 그 내원을 마친 시점의 코스 남은 횟수(4단계 치료 계획) — 다음 예약이 잡혔는지·나중에
    바뀌었는지와 무관 (§4.3)"""
    conn, _, _ = db
    left = RUN["run"].state["course_left"]
    got = Counter()
    for tid, appt_id, facts in conn.execute(
        "SELECT t.id, t.appointment_id, n.facts FROM emr_treatments t JOIN truth.truth_chart_notes n ON n.treatment_id = t.id"
    ):
        plan = json.loads(facts)["plan"] or {}
        assert plan.get("remaining", 0) == left.get(appt_id, 0), (tid, plan)
        got[plan.get("remaining", 0)] += 1
    assert got[1] and got[2]  # 코스 첫 회차는 2회 이상 남는다


def test_courses_recorded_in_truth(db):
    """코스(4단계 에피소드)는 정답 DB에 — 노쇼 뒤 전화 재예약도 같은 코스. 앱 DB에는 두지 않는다"""
    conn, _, _ = db
    episode = RUN["run"].state["episode"]
    assert (
        dict(conn.execute("SELECT appointment_id, course_id FROM truth.truth_appointment_courses"))
        == episode
    )


def test_retreatment_is_across_courses(db):
    """R01(같은 부위 재시행)의 장부 이력은 다른 코스의 같은 치아·시술 — 코스 안 반복(신경치료 2회차)은 재시행이 아니다"""
    conn, _, _ = db
    episode = RUN["run"].state["episode"]
    checked = 0
    for aid, pid, tooth, code, at in conn.execute(
        "SELECT t.appointment_id, t.patient_id, t.tooth_no, t.procedure_code, t.performed_at FROM emr_treatments t"
        " JOIN truth.truth_chart_notes n ON n.treatment_id = t.id WHERE n.requirement = 'R01'"
    ):
        prior = [
            r[0]
            for r in conn.execute(
                "SELECT appointment_id FROM emr_treatments WHERE patient_id = ? AND tooth_no = ?"
                " AND procedure_code = ? AND performed_at < ?",
                (pid, tooth, code, at),
            )
        ]
        assert not prior or any(episode.get(p, ("pre", p)) != episode.get(aid) for p in prior), aid
        checked += 1
    assert checked


def test_no_evidence_lines_get_neutral_cc(db):
    """근거 없는 요건 줄의 C.C는 근거로 읽히지 않는 문장(chart.CC_NO_EVIDENCE)"""
    conn, _, _ = db
    seen = set()
    for key, facts in conn.execute(
        "SELECT requirement, facts FROM truth.truth_chart_notes WHERE evidence_present = 0"
    ):
        if key in chart.CC_NO_EVIDENCE:
            assert json.loads(facts)["cc"] in chart.CC_NO_EVIDENCE[key], key
            seen.add(key)
    assert {"R01", "R04"} <= seen


def test_draft_sentences(db):
    """초안 문장: 조사 처리('2차 우식으로'), 주의사항이 있으면 '특이사항은 없습니다' 템플릿을 쓰지 않음"""
    conn, _, _ = db
    notes = [r[0] for r in conn.execute("SELECT note FROM emr_chart_notes")]
    assert not any("우식로" in n for n in notes)
    assert not any(
        "특이사항은 없습니다" in n and ("주의사항" in n or "진료 중" in n) for n in notes
    )


# ── 9b단계 차트 서술 작업 (§4.3 '9b단계 구현 결정') ────────────

FACTS = {
    "alerts": [{"label": "고혈압", "action": "혈압 측정 후 진행"}],
    "cc": "잇몸이 내려간 것 같아요",
    "discovered": ["마취가 잘 안 됨"],
    "findings": {"bleeding": True, "calculus": "많음", "mobility": 0, "pocket_mm": 6},
    "plan": {"remaining": 2},
    "procedure": "잇몸치료",
    "tooth": None,
    "requirement": {"key": "R02", "evidence": True, "scaling_days_ago": 14, "linked": True},
}


def test_narrative_must_include_facts():
    """시술은 표준 이름만 — 진료 기록이라 별칭(검색·전사용)은 인정하지 않는다"""
    got = chart.narrative_must_include(FACTS)
    assert got == [
        ["잇몸치료"],
        ["6mm"],
        ["고혈압"],
        ["마취가 잘 안 됨"],
        ["14일"],
        ["재평가"],
        ["2회"],
    ]  # 근거 말
    tooth = {
        **FACTS,
        "tooth": "26",
        "procedure": "신경치료",
        "findings": None,
        "alerts": [],
        "discovered": [],
        "requirement": {"key": "R01", "evidence": True, "prev_days": 200, "reason": "2차 우식"},
        "plan": None,
    }
    assert chart.narrative_must_include(tooth) == [["26번"], ["신경치료"], ["7개월"], ["2차 우식"]]
    other = {
        **tooth,
        "requirement": {
            "key": "R06",
            "evidence": True,
            "other_clinic": True,
            "edentulous_months": 5,
        },
    }
    assert chart.narrative_must_include(other)[-2:] == [["5개월"], ["무치악"]]
    sealant = {
        **tooth,
        "requirement": {
            "key": "R04",
            "evidence": True,
            "surface": "깊은 소와열구가 있고 우식이 없는 교합면",
        },
    }
    assert chart.narrative_must_include(sealant)[-1] == ["소와열구"]
    impacted = {
        **tooth,
        "requirement": {
            "key": "R05",
            "evidence": True,
            "impaction": "부분 매복",
            "bone_removal": True,
        },
    }
    assert chart.narrative_must_include(impacted)[-2:] == [["부분 매복"], ["골삭제"]]


def test_narrative_checks_forbid_evidence_words_without_evidence():
    """근거 없는 요건 줄은 근거 말이 금지 — 지어 넣으면 ⑤-2·⑤-3 정답(근거 없음)과 어긋난다"""
    no_link = {
        **FACTS,
        "requirement": {"key": "R02", "evidence": False, "scaling_days_ago": 14, "linked": False},
    }
    assert "재평가" in chart.narrative_checks(no_link, "김철수")["forbidden_words"]
    assert ["재평가"] not in chart.narrative_must_include(no_link)
    no_reason = {
        **FACTS,
        "requirement": {"key": "R01", "evidence": False, "prev_days": 200, "reason": None},
    }
    assert {"파절", "2차 우식"} <= set(
        chart.narrative_checks(no_reason, "김철수")["forbidden_words"]
    )
    assert "재평가" not in chart.narrative_checks(FACTS, "김철수")["forbidden_words"]  # 근거 있음


def test_narrative_checks_forbid_answer_words_and_patient_name():
    c = chart.narrative_checks(FACTS, "김철수")
    assert c["field"] == "note" and "김철수" in c["forbidden_words"]
    assert set(CHART_FORBIDDEN_WORDS) <= set(c["forbidden_words"]) and c["forbidden_patterns"]


def test_narrative_stage_fake_keeps_draft(db, tmp_path):
    """가짜 게이트웨이면 초안이 그대로 남는다 — db 픽스처(가짜 모드 생성)의 서술 줄 본문 = 초안"""
    conn, _, _ = db
    row = conn.execute(
        "SELECT n.note, t.facts FROM emr_chart_notes n JOIN truth.truth_chart_notes t USING (treatment_id)"
        " WHERE t.kind = 'narrative' LIMIT 1"
    ).fetchone()
    assert row["note"] == chart.draft_note(json.loads(row["facts"]))


def test_narrative_payload_hides_requirement_key_and_evidence():
    """LLM 입력에 요건 사유 키·충족 여부(정답)가 없다 — 사실 필드만 history로"""
    p = chart.narrative_payload(FACTS, "초안")
    text = json.dumps(p, ensure_ascii=False)
    assert "R02" not in text and "evidence" not in text and "requirement" not in text
    assert (
        p["facts"]["history"] == {"scaling_days_ago": 14, "linked": True} and p["draft"] == "초안"
    )
    r03 = chart.narrative_payload(
        {**FACTS, "requirement": {"key": "R03", "evidence": False}}, "초안"
    )
    assert "history" not in r03["facts"]


def test_narrative_stage_exports_then_applies_cache(tmp_path):
    """실제 모드: 서술 줄 → 작업 → 들여오기 → 본문 덮어쓰기 (verify 테스트 DB의 3번 레진 서술 줄)"""
    from types import SimpleNamespace

    from generator import llm_jobs
    from tests.generator.verify_db import make_db

    conn = make_db(tmp_path)
    paths = {k: tmp_path / k for k in ("llm_cache", "llm_jobs", "llm_results")}
    ctx = llm_jobs.LLMContext(paths, fake=False)
    chart.stage_narratives(SimpleNamespace(conn=conn, state={"llm": ctx}), None, None)
    assert ctx.finish() == {"chart": 1}
    (job,) = [
        json.loads(line)
        for line in (paths["llm_jobs"] / "chart.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert job["item"] == "3" and job["checks"]["must_include"] == [["16번"], ["레진"]]
    assert "김하늘" in job["checks"]["forbidden_words"]  # 3번 진료 환자 이름
    note = "16번 치아에 까만 점이 보여 레진 충전을 시행했습니다."
    paths["llm_results"].mkdir()
    (paths["llm_results"] / "chart.jsonl").write_text(
        json.dumps(
            {"key": job["key"], "output": {"note": note}, "source": "claude-code"},
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    assert llm_jobs.import_results(paths)["chart"]["ok"] == 1
    chart.stage_narratives(
        SimpleNamespace(conn=conn, state={"llm": llm_jobs.LLMContext(paths, fake=False)}),
        None,
        None,
    )
    assert (
        conn.execute("SELECT note FROM emr_chart_notes WHERE treatment_id = 3").fetchone()[0]
        == note
    )
