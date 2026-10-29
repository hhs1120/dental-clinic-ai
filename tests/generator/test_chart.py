"""5b단계 차트 사실 — 실제로 생성해 오류 비율·디코이 층화·사실 구성을 본다 (가상 데이터 생성 설계 §4.3).
필수 검사 전부는 tests/generator/test_records.py가 본다."""

import json
import math
import re
import sqlite3
from collections import Counter

import pytest

from generator import build, chart, llm_jobs, verify
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
    conn = sqlite3.connect(out / "source.sqlite")
    conn.row_factory = sqlite3.Row
    conn.execute("ATTACH DATABASE ? AS truth", (str(out / "truth.sqlite"),))
    return conn


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    out = tmp_path_factory.mktemp("chart")
    RUN["run"] = build.generate(out / "source.sqlite", out / "truth.sqlite", CONFIG, fake_llm=True)
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
    build.generate(tmp_path / "source.sqlite", tmp_path / "truth.sqlite", CONFIG, fake_llm=True)
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
    build.generate(tmp_path / "source.sqlite", tmp_path / "truth.sqlite", CONFIG, fake_llm=True)
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


def test_plan_is_open_items_at_visit_end(db):
    """다음 계획 = 그 내원을 마친 시각에 열린 치료계획 항목(§2.3·§4.3 (5)) — 코스 첫 내원은 남은 항목이 있고,
    치주 계획을 만든 위생 내원은 잇몸치료 항목과 깊은 치주낭"""
    conn, _, _ = db
    plans = RUN["run"].state["plans"]
    perio = {plan.appointment_id for plan in plans}
    got = Counter()
    for code, appt_id, facts in conn.execute(
        "SELECT t.procedure_code, t.appointment_id, n.facts FROM emr_treatments t"
        " JOIN truth.truth_chart_notes n ON n.treatment_id = t.id WHERE n.kind = 'narrative'"
    ):
        f = json.loads(facts)
        if f["plan"] and "items" in f["plan"]:
            got["items"] += 1
        if code in chart.HYGIENE_CODES and appt_id in perio:
            assert "잇몸치료" in [i["procedure"] for i in f["plan"]["items"]], (
                f
            )  # 앞선 계획의 열린 항목도 함께
            assert f["findings"]["pocket_mm"] >= 4, f
            got["perio"] += 1
    assert got["items"] and got["perio"]


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
    """초안 문장: 조사 처리('2차 우식으로'), 주의사항이 있으면 '특이사항 없음' 템플릿을 쓰지 않음"""
    conn, _, _ = db
    notes = [r[0] for r in conn.execute("SELECT note FROM emr_chart_notes")]
    assert not any("우식로" in n for n in notes)
    assert not any("특이사항 없음" in n and (" 주의, " in n or "진료 중" in n) for n in notes)


def test_notes_are_memo_style(db):
    """차트는 짧은 메모체 — '~함·~음'·명사로 끝내고 '~습니다'·높임말('하시며')을 쓰지 않는다(템플릿·초안 모두)"""
    conn, _, _ = db
    notes = [r[0] for r in conn.execute("SELECT note FROM emr_chart_notes")]
    assert notes and not any(re.search(r"니다|하시며|하셔서|했다\.", n) for n in notes)


def test_draft_merges_repeated_plan_items():
    """같은 시술이 계획에 여러 번이면 'N회'로 한 번만 — 같은 말을 여러 번 넣으라고 하면 모델이 문장을 덧붙인다"""
    f = FACTS | {"plan": {"items": [{"procedure": "잇몸치료", "tooth": None}] * 2}}
    draft = chart.draft_note(f, 1)
    assert "잇몸치료 2회" in draft and draft.count("잇몸치료") == 2  # 오늘 시술 + 남은 치료(2회)
    must = chart.narrative_must_include(f)
    assert must[-3:] == [["잇몸치료"], ["2회"], list(chart.PLAN_WORDS)]


def test_drafts_vary_by_visit():
    """초안 꼴(환자 말·소견·시행·주의사항·남은 계획)은 진료 번호 해시로 고른다 — 프롬프트에 여러 꼴을 줘도 모델은 건마다
    따로 불려 한 꼴로 몰린다. 같은 진료는 늘 같은 초안, '호소'는 증상인 환자 말에만"""
    drafts = [chart.draft_note(FACTS, tid) for tid in range(200)]
    assert chart.draft_note(FACTS, 7) == chart.draft_note(FACTS, 7)
    openers = {d.split(".")[0].split('"', 2)[2] for d in drafts if d.startswith('"')}
    assert len(openers) >= 2 and any(not d.startswith('"') for d in drafts)  # 소견 뒤에 환자 말
    assert (
        len({re.search(r"남은|남아|예정|다음", d.split("시행함")[-1]).group(0) for d in drafts})
        >= 3
    )
    calm = FACTS | {"cc": "정기 검진 받으러 왔어요"}
    assert not any("호소" in chart.draft_note(calm, tid) for tid in range(200))
    sore = FACTS | {"cc": "잇몸이 붓고 피가 나요"}
    assert any("호소" in chart.draft_note(sore, tid) for tid in range(200))  # 증상
    assert not any("호소" in d for d in drafts)  # '잇몸이 내려간 것 같아요' — 느낌이라 호소 아님


def test_drafts_quote_or_shorten_the_patient():
    """환자 말은 진료 번호로 반쯤은 따옴표, 반쯤은 차트식으로 줄인 말(CC_SHORT) — 모든 C.C에 줄인 말이 있고, 줄인 말은
    근거 말을 품지 않는다(근거 없는 줄의 C.C도 줄여 쓰므로)"""
    every = {c for ccs in chart.CC.values() for c in ccs} | {
        c for ccs in chart.CC_NO_EVIDENCE.values() for c in ccs
    }
    assert every <= set(chart.CC_SHORT)
    evidence = [w for ws in chart.EVIDENCE_WORDS.values() for w in ws]
    assert not [s for s in chart.CC_SHORT.values() if any(w in s for w in evidence)]
    drafts = [chart.draft_note(FACTS, tid) for tid in range(400)]
    quoted = sum(f'"{FACTS["cc"]}"' in d for d in drafts)
    assert 0.35 < quoted / len(drafts) < 0.65
    short = chart.CC_SHORT[FACTS["cc"]]
    assert all(short in d for d in drafts if f'"{FACTS["cc"]}"' not in d)


@pytest.mark.parametrize(
    "note, hit",
    [
        ("잇몸치료 시행했습니다.", True),
        ('"이가 시려요"라고 하시며 내원.', True),
        ("레진 시행했다.", True),
        ("다음 내원 때 이어서 진행.", True),
        ("전악 스케일링 시행함.", True),  # 치아 없는 진료에 부위 말
        ("부분 매복 상태로 골삭제 필요하여 발치 시행함.", True),  # 시행한 골삭제를 필요로
        ('"이가 시려요"라며 내원. 26번 치아 레진 시행함. 남은 치료로 크라운 예정.', False),
    ],
)
def test_memo_style_is_checked(note, hit):
    """서술 검증이 메모체가 아닌 끝맺음·높임말·초안에 없는 시점을 걸러 다시 쓰게 한다"""
    checks = chart.narrative_checks(FACTS | {"plan": None}, "홍길동")
    assert chart.MEMO_STYLE_FORBIDDEN in checks["forbidden_patterns"]
    assert bool(re.search(chart.MEMO_STYLE_FORBIDDEN, note)) is hit


# ── 9b단계 차트 서술 작업 (§4.3 '9b단계 구현 결정') ────────────

FACTS = {
    "alerts": [{"label": "고혈압", "action": "혈압 측정 후 진행"}],
    "cc": "잇몸이 내려간 것 같아요",
    "discovered": ["마취가 잘 안 됨"],
    "findings": {"bleeding": True, "calculus": "많음", "mobility": 0, "pocket_mm": 6},
    "plan": {"items": [{"procedure": "잇몸치료", "tooth": None}]},
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
        list(chart.DISCOVERED_WORDS),  # 진료 중 새로 알게 됐다는 말
        ["14일"],
        ["재평가"],
        ["잇몸치료"],
        list(chart.PLAN_WORDS),  # 남은 치료가 그날 시술과 같아도 계획 문장이 빠지지 않게
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
    assert chart.narrative_must_include(other)[-3:] == [
        ["5개월"],
        ["무치악"],
        list(chart.OTHER_CLINIC_WORDS),
    ]  # 타 기관 이력은 그 말이 있어야 원내 이력으로 읽히지 않는다
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
        "SELECT n.treatment_id, n.note, t.facts FROM emr_chart_notes n"
        " JOIN truth.truth_chart_notes t USING (treatment_id) WHERE t.kind = 'narrative' LIMIT 1"
    ).fetchone()
    assert row["note"] == chart.draft_note(json.loads(row["facts"]), row["treatment_id"])


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
    note = "16번 치아 까만 점 보여 레진 충전 시행함."
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


def test_narrative_drafts_pass_their_checks(db):
    """서술 줄의 초안(draft_note)은 그 줄의 9b 검증(꼭 넣을 사실·금지 어휘)을 그대로 통과한다 — 초안에서 빠진 사실이 있으면
    LLM이 영영 통과 못 하는 건이 생긴다 (§4.3 '9b단계 구현 결정')"""
    conn, _, _ = db
    names = {
        tid: name
        for tid, name in conn.execute(
            "SELECT t.id, p.name FROM emr_treatments t JOIN emr_patients p ON p.id = t.patient_id"
        )
    }
    narratives = [
        x
        for x in RUN["run"].state["chart"]
        if x["id"]
        in {
            r[0]
            for r in conn.execute(
                "SELECT treatment_id FROM truth.truth_chart_notes WHERE kind = 'narrative'"
            )
        }
    ]
    assert narratives
    failed = {
        x["id"]: problems
        for x in narratives
        if (
            problems := llm_jobs.problems(
                chart.NARRATIVE_SCHEMA,
                chart.narrative_checks(x["facts"], names[x["id"]]),
                {"note": chart.draft_note(x["facts"], x["id"])},
            )
        )
    }
    assert failed == {}
