"""⑤-2b 추가 처치 누락 평가(evaluation/claim_omission.py) — 설계 docs/superpowers/specs/2026-11-10-claim-review-design.md §2b.
합성 사례 명세(코드)·문장 검사·문장 쓰기(가짜 게이트웨이)를 본다."""

import json
import sqlite3

import pytest

from backend import claim_omission as co
from evaluation import claim_omission as ev
from generator.clinic import master_values_rows
from generator.master import PROCEDURES
from shared.llm import LLMResult
from tests.backend.test_claims import add_claim
from tests.backend.test_missed import Source
from tests.evaluation.eval_db import TRUTH_SCHEMA

MODEL = "claude-sonnet-5-5"


def llm(data):
    return LLMResult(True, "", data, MODEL, False, 1, 1, 0.001, 1)


def spec(shape):
    return next(s for s in ev.case_specs() if s["shape"] == shape)


# ── 명세 ──────────────────────────────────────────────────────────


def test_specs_fixed_and_counts():
    a, b = ev.case_specs(), ev.case_specs()
    assert a == b and len(a) == 41 and len({s["id"] for s in a}) == 41
    hits = [s for s in a if s["truth"]]
    assert len(hits) == 20 and {s["shape"] for s in hits} == set(ev.SHAPES_HIT)
    assert all(s["truth"] == [] for s in a if s["shape"] in ev.SHAPES_DECOY)
    others = [s for s in a if s["shape"] != "claimed_again"]
    assert all(s["extra"]["tooth"] != s["claimed"]["tooth"] for s in others)
    again = spec("claimed_again")
    assert (again["extra"]["procedure_code"], again["extra"]["tooth"]) == (
        again["claimed"]["procedure_code"],
        again["claimed"]["tooth"],
    )


def test_specs_age_rules():
    for s in ev.case_specs():
        if s["shape"] == "child_sealant":
            assert 6 <= s["claimed"]["age"] <= 12 and s["extra"]["procedure"] == "실란트"
        if s["shape"] == "adult_sealant":
            assert s["claimed"]["age"] >= 19 and s["extra"]["procedure"] == "실란트"
        if s["shape"] == "uninsured":
            assert s["extra"]["procedure"] == "레진"


def test_split_by_shape():
    """모양마다 반반 — 어느 모양도 dev나 test 한쪽에만 몰리지 않는다(프롬프트를 고칠 때 모든 모양을 본다)"""
    specs = ev.case_specs()
    for shape in ev.SHAPES_HIT + ev.SHAPES_DECOY:
        sides = [ev.split_of(s["id"]) for s in specs if s["shape"] == shape]
        assert "dev" in sides and "test" in sides, shape
        assert abs(sides.count("dev") - sides.count("test")) <= 1


# ── 문장 검사 ─────────────────────────────────────────────────────


def note_for(s, body):
    return body.format(t=s["extra"]["tooth"], c=s["claimed"]["procedure"])


def test_check_note_ok():
    s = spec("planned")
    assert (
        ev.check_note(s, note_for(s, "{c} 시행함. {t}번 치아 아말감은 다음 내원 때 예정.")) == []
    )


def test_check_note_markers():
    """다른 표기 — 아말감/GI로만 읽히는 말(GI·글라스아이오노머), '때움'처럼 레진과 갈리지 않는 말은 정답을 못 정해 안 쓴다"""
    s = spec("other_name")
    assert ev.check_note(s, note_for(s, "{c} 시행하고 {t}번 치아 GI 충전함.")) == []
    assert ev.check_note(s, note_for(s, "{c} 시행하고 {t}번 글라스아이오노머 충전함.")) == []
    assert ev.check_note(s, note_for(s, "{c} 시행하고 {t}번 치아 아말감으로 충전."))
    assert ev.check_note(s, note_for(s, "{c} 시행하고 {t}번 치아 때움."))
    p = spec("planned")
    assert ev.check_note(p, note_for(p, "{c} 시행함. {t}번 치아 아말감 진행."))
    assert ev.check_note(p, note_for(p, "{c} 시행함. {t}번 아말감 다음 예정. 삭감 주의"))
    assert ev.check_note(p, note_for(p, "{c} 시행함. 아말감 다음 예정."))  # 치아 번호 없음


def test_check_note_shape_layout():
    s = spec("tooth_first")
    assert ev.check_note(s, note_for(s, "{c} 시행함. {t}번은 아말감/GI도 함께 진행함.")) == []
    assert ev.check_note(s, note_for(s, "{c} 시행함. 이어서 {t}번 아말감/GI 진행함."))
    sep = spec("separated")
    assert (
        ev.check_note(sep, note_for(sep, "{c} 시행함. 출혈 적음. {t}번 아말감/GI 진행함.")) == []
    )
    assert ev.check_note(sep, note_for(sep, "{c} 시행하고 {t}번 아말감/GI 진행함."))


def test_check_note_no_direct_negation():
    """그날 하지 않은 디코이는 맥락(예정·거절·다른 치과·지난)으로만 — '하지 않음' 같은 직접 부정은 정답을 써 주는 셈"""
    for shape, body in (
        ("planned", "{c} 시행함. {t}번 치아 아말감/GI는 이번에 하지 않았고 다음 내원 때 예정."),
        ("declined", "{c} 시행함. {t}번 충전 권했으나 환자 거절, 시행하지 않음."),
        (
            "elsewhere",
            "{c} 시행함. {t}번은 다른 치과에서 아말감/GI 받았다고 함, 오늘은 처치 없음.",
        ),
        ("past_visit", "{c} 시행함. {t}번 아말감/GI는 지난 내원 때 함, 금일 안 함."),
    ):
        s = spec(shape)
        assert ev.check_note(s, note_for(s, body)), shape
    s = spec("planned")
    assert ev.check_note(s, note_for(s, "{c} 시행함. {t}번 아말감/GI 다음 내원 시 예정.")) == []


def test_negation_rule_only_for_not_done_decoys():
    s = spec("adult_sealant")
    assert ev.check_note(s, note_for(s, "{c} 시행함. 오늘 {t}번 실란트 함께 시행함.")) == []


def test_hit_note_must_be_done_that_day():
    """누락 있는 모양의 추가 처치 문장에 계획·거절·다른 치과·지난 내원 표지가 있으면 라벨(그날 함)과 어긋난다"""
    s = spec("separated")
    assert ev.check_note(
        s, note_for(s, "{c} 시행함. 출혈 적음. {t}번 아말감/GI 다음 내원 시 예정.")
    )
    assert ev.check_note(
        s, note_for(s, "{c} 시행함. 출혈 적음. {t}번 아말감/GI는 다른 치과에서 함.")
    )
    assert (
        ev.check_note(s, note_for(s, "{c} 시행함. 다음 내원 예정. {t}번 아말감/GI 시행함.")) == []
    )


# ── 문장 쓰기 ─────────────────────────────────────────────────────


class FakeGw:
    def __init__(self, *notes):
        self.notes, self.calls = list(notes), []

    def complete(self, alias, **kw):
        self.calls.append((alias, kw))
        return llm({"note": self.notes.pop(0)})


def test_write_cases_retries_and_resumes(tmp_path):
    s = spec("planned")
    path = tmp_path / "cases.jsonl"
    bad = note_for(s, "{c} 시행함. {t}번 아말감 진행.")
    good = note_for(s, "{c} 시행함. {t}번 치아 아말감은 다음 내원 때 예정.")
    gw = FakeGw(bad, good)
    out = ev.write_cases(gw, path, specs=[s])
    assert out == {"written": 1, "skipped": 0, "failed": []}
    assert gw.calls[0][0] == "gen" and "문제" in json.dumps(gw.calls[1][1], ensure_ascii=False)
    (case,) = ev.load_cases(path, specs=[s])
    assert case["note"] == good and case["id"] == s["id"]
    gw2 = FakeGw()
    assert ev.write_cases(gw2, path, specs=[s])["skipped"] == 1 and gw2.calls == []


def test_write_cases_rewrites_failing_saved(tmp_path):
    """저장된 문장이 지금 검사에 걸리면(검사 규칙을 더한 뒤) 그 사례만 다시 쓴다 — 걸린 줄은 읽지 않는다"""
    s = spec("planned")
    path = tmp_path / "cases.jsonl"
    old = note_for(s, "{c} 시행함. {t}번 아말감/GI는 이번에 하지 않았고 다음 내원 때 예정.")
    path.write_text(
        json.dumps({"id": s["id"], "spec_hash": ev._spec_hash(s), "note": old}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    assert ev.load_cases(path, specs=[s]) == []
    good = note_for(s, "{c} 시행함. {t}번 아말감/GI 다음 내원 시 예정.")
    assert ev.write_cases(FakeGw(good), path, specs=[s])["written"] == 1
    (case,) = ev.load_cases(path, specs=[s])
    assert case["note"] == good


def test_write_cases_gives_up(tmp_path):
    s = spec("planned")
    bad = note_for(s, "{c} 시행함. {t}번 아말감 진행.")
    out = ev.write_cases(FakeGw(bad, bad, bad), tmp_path / "c.jsonl", specs=[s])
    assert out["failed"] == [s["id"]] and not (tmp_path / "c.jsonl").exists()


# ── 채점 ──────────────────────────────────────────────────────────

IDX = co.procedure_index(PROCEDURES)
AGE_RANGE = json.loads(dict(master_values_rows())["age_group_range"])


def test_match_items():
    assert ev.match([("U0006", "12"), ("U0006", "14")], [("U0006", "12")]) == {
        "tp": 1,
        "fp": 1,
        "fn": 0,
    }
    assert ev.match([("U0006", "13")], [("U0006", "12")]) == {"tp": 0, "fp": 1, "fn": 1}
    assert ev.match([], []) == {"tp": 0, "fp": 0, "fn": 0}


def test_keyword_catches_plan_sentence():
    """기준선은 계획·부정을 가리지 않는다 — 남은 치료 문장도 잡고, 비급여(레진)는 같은 거름으로 버린다"""
    note = "스케일링 시행함. 남은 치료는 46번 치아 아말감/GI. 24번 레진 함께 시행함."
    got = ev.keyword_items(note, "U0001", None, 40, IDX, AGE_RANGE)
    assert got == [("U0006", "46")]
    assert ev.keyword_items("47번 신경치료 시행함.", "U0008", "47", 40, IDX, AGE_RANGE) == []


def case(cid, shape, truth, note):
    return {
        "id": cid,
        "shape": shape,
        "claimed": {"procedure_code": "U0001", "procedure": "스케일링", "tooth": "00", "age": 40},
        "extra": {},
        "truth": truth,
        "note": note,
    }


def run(c, omissions):
    return {
        "id": c["id"],
        "note_hash": ev._note_hash(c["note"]),
        "prompt_version": co.PROMPT_VERSION,
        "model": MODEL,
        "result": None if omissions is None else {"omissions": omissions},
    }


def test_score_synth(monkeypatch):
    monkeypatch.setattr(ev, "split_of", lambda cid: "dev")
    cases = [
        case("a", "plain", [("U0006", "12")], "스케일링 시행하고 12번 아말감/GI 함께 시행함."),
        case("b", "planned", [], "스케일링 시행함. 16번 아말감/GI 다음 예정."),
    ]
    hit = {"procedure": "아말감/GI", "tooth": "12", "quote": "12번 아말감/GI 함께 시행함"}
    runs = [run(cases[0], [hit]), run(cases[1], None)]
    out = ev.score_synth(cases, runs, "dev")
    assert out["n"] == 2 and out["failed"] == 1 and out["missing"] == 0
    assert (out["ai"]["tp"], out["ai"]["fp"], out["ai"]["fn"]) == (1, 0, 0)
    assert (out["keyword"]["tp"], out["keyword"]["fp"]) == (1, 1)
    assert out["by_shape"]["plain"] == {"n": 1, "ai_ok": 1, "keyword_ok": 1}
    assert out["by_shape"]["planned"] == {"n": 1, "ai_ok": 1, "keyword_ok": 0}


class Real:
    """source(청구·차트) + app(ax_claim_omissions) + truth(누락 정답)"""

    def __init__(self, tmp_path):
        self.tmp, self.src = tmp_path, Source(tmp_path / "source.sqlite")
        self.truth, self.saved = [], {}

    def claim(self, pid, note, *, truth=None, ai=None, status="submitted"):
        cid = add_claim(self.src, pid, status=status)
        self.src.conn.execute(
            "UPDATE emr_chart_notes SET note = ? WHERE treatment_id ="
            " (SELECT treatment_id FROM emr_claims WHERE id = ?)",
            (note, cid),
        )
        t = self.src.conn.execute(
            "SELECT treatment_id FROM emr_claims WHERE id = ?", (cid,)
        ).fetchone()[0]
        if truth:
            self.truth.append((t, *truth))
        if ai is not None:
            self.saved[cid] = ai
        return cid

    def open(self):
        truth = sqlite3.connect(self.tmp / "truth.sqlite")
        truth.executescript(TRUTH_SCHEMA)
        truth.executemany(
            "INSERT INTO truth_claim_errors (treatment_id, kind, extra_procedure_code,"
            " extra_tooth_no) VALUES (?, 'omitted_extra', ?, ?)",
            self.truth,
        )
        truth.commit()
        truth.close()
        conn = self.src.app()
        with conn:
            for cid, items in self.saved.items():
                conn.execute(
                    "INSERT INTO ax_claim_omissions VALUES (?, ?, 'h', ?, 1, ?, 'm', 'now')",
                    (
                        cid,
                        json.dumps({"tooth": None, "age": 46}),
                        json.dumps({"omissions": items}),
                        co.PROMPT_VERSION,
                    ),
                )
        conn.execute("ATTACH DATABASE ? AS truth", (str(self.tmp / "truth.sqlite"),))
        return conn


HIT_NOTE = "스케일링 시행하고 이어서 12번 치아 아말감/GI 함께 진행함."
HIT_ITEM = {"procedure": "아말감/GI", "tooth": "12", "quote": "12번 치아 아말감/GI 함께 진행함"}
PLAN_NOTE = "스케일링 시행함. 남은 치료는 46번 치아 아말감/GI."


def test_score_real_dev_hides_positives(tmp_path, monkeypatch):
    """누락 정답 줄이 dev 분할이어도 dev 결과에 들지 않는다(실데이터 23건은 test 전용)"""
    monkeypatch.setattr(ev, "claim_split", lambda cid: "dev")
    w = Real(tmp_path)
    w.claim(1, HIT_NOTE, truth=("U0006", "12"), ai=[HIT_ITEM])
    w.claim(2, PLAN_NOTE, ai=[])
    out = ev.score_real(w.open(), "dev")
    assert "hits" not in out and out["lines"] == 1
    assert (out["ai_fp_lines"], out["keyword_fp_lines"]) == (0, 1)


def test_score_real_test_hits(tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "claim_split", lambda cid: "test")
    w = Real(tmp_path)
    w.claim(1, HIT_NOTE, truth=("U0006", "12"), ai=[HIT_ITEM])
    w.claim(2, PLAN_NOTE, ai=[])
    out = ev.score_real(w.open(), "test")
    assert out["hits"] == {"found": 1, "n": 1} and out["lines"] == 1
    assert out["ai"]["tp"] == 1 and out["keyword"]["tp"] == 1


def test_test_runs_once(tmp_path):
    ev.check_test(tmp_path, "real", "v1")
    ev.record_test(tmp_path, "real", "v1")
    with pytest.raises(ev.AlreadyRun):
        ev.check_test(tmp_path, "real", "v1")
    ev.check_test(tmp_path, "synth", "v1")  # 종류가 다르면 따로
    ev.check_test(tmp_path, "real", "v1", force=True)


def test_run_synth_retries_failed(tmp_path, monkeypatch):
    """두 번 다 후검증에 실패한 사례는 다음 실행에 다시 부른다(이어 돎 — backend build와 같음)"""
    monkeypatch.setattr(ev, "split_of", lambda cid: "dev")
    cases = [case("a", "plain", [("U0006", "12")], HIT_NOTE)]
    bad = {"omissions": [HIT_ITEM | {"quote": "차트에 없는 문장입니다"}]}
    path = tmp_path / "runs.jsonl"
    assert ev.run_synth(Gw(llm(bad), llm(bad)), "dev", cases, path)["failed"] == 1
    gw = Gw(llm({"omissions": [HIT_ITEM]}))
    assert ev.run_synth(gw, "dev", cases, path) == {"called": 1, "skipped": 0, "failed": 0}


def test_run_synth_reuses(tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "split_of", lambda cid: "dev")
    cases = [case("a", "plain", [("U0006", "12")], HIT_NOTE)]
    gw = Gw(llm({"omissions": [HIT_ITEM]}))
    path = tmp_path / "runs.jsonl"
    assert ev.run_synth(gw, "dev", cases, path) == {"called": 1, "skipped": 0, "failed": 0}
    assert gw.calls[0][0] == co.ALIAS
    assert ev.run_synth(Gw(), "dev", cases, path)["skipped"] == 1


class Gw:
    config = {"aliases": {"chat": [MODEL]}, "models": {MODEL: {}}}

    def __init__(self, *turns):
        self.turns, self.calls = list(turns), []

    def complete(self, alias, **kw):
        self.calls.append((alias, kw))
        return self.turns.pop(0) if self.turns else llm({"omissions": []})


def test_render():
    from evaluation.eval import _render_claim_omission

    synth = {
        "n": 2,
        "failed": 0,
        "missing": 0,
        "ai": {"tp": 1, "fp": 0, "fn": 0, "precision": 1.0, "recall": 1.0},
        "keyword": {"tp": 1, "fp": 1, "fn": 0, "precision": 0.5, "recall": 1.0},
        "by_shape": {"plain": {"n": 1, "ai_ok": 1, "keyword_ok": 1}},
    }
    real = {"lines": 3, "missing": 0, "failed": 0, "ai_fp_lines": 0, "keyword_fp_lines": 1}
    text = "\n".join(
        _render_claim_omission({"synth": synth, "real": real, "prompt_version": "v1"})
    )
    assert "⑤-2b" in text and "키워드" in text and "plain" in text
