"""⑤-2b 추가 처치 누락 평가(evaluation/claim_omission.py) — 설계 docs/superpowers/specs/2026-11-10-claim-review-design.md §2b.
합성 사례 명세(코드)·문장 검사·문장 쓰기(가짜 게이트웨이)를 본다."""

import json

from evaluation import claim_omission as ev
from shared.llm import LLMResult

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


def test_split_half():
    dev = sum(ev.split_of(s["id"]) == "dev" for s in ev.case_specs())
    assert 15 <= dev <= 26
    assert ev.split_of("om-01") == ev.split_of("om-01")


# ── 문장 검사 ─────────────────────────────────────────────────────


def note_for(s, body):
    return body.format(t=s["extra"]["tooth"], c=s["claimed"]["procedure"])


def test_check_note_ok():
    s = spec("planned")
    assert (
        ev.check_note(s, note_for(s, "{c} 시행함. {t}번 치아 아말감은 다음 내원 때 예정.")) == []
    )


def test_check_note_markers():
    s = spec("everyday_word")
    assert ev.check_note(s, note_for(s, "{c} 시행하고 {t}번 치아 때움.")) == []
    assert ev.check_note(s, note_for(s, "{c} 시행하고 {t}번 치아 아말감으로 때움."))
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


def test_write_cases_gives_up(tmp_path):
    s = spec("planned")
    bad = note_for(s, "{c} 시행함. {t}번 아말감 진행.")
    out = ev.write_cases(FakeGw(bad, bad, bad), tmp_path / "c.jsonl", specs=[s])
    assert out["failed"] == [s["id"]] and not (tmp_path / "c.jsonl").exists()
