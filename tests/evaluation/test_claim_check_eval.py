"""⑤-2 AI 점검 채점(evaluation/claim_check.py) — 설계 docs/superpowers/specs/2026-11-10-claim-review-design.md §2.
청구 줄 해시 분할, dev 고정 표본, 요건 오류 정밀도·재현율과 "서술 있는 줄 전부" 기준선, 조정 건 판단과 두 기준선."""

import json
import sqlite3
from datetime import date

import pytest

from evaluation import claim_check as ev
from generator.master import ADJUST_REASONS
from tests.backend.test_claims import add_claim, reviewed_for
from tests.backend.test_missed import Source
from tests.evaluation.eval_db import TRUTH_SCHEMA

DAY = date(2026, 12, 1)
EXTRACT = "U0012"
REQ_NOTE = ADJUST_REASONS["R05"]["note"]


class World:
    """source(청구·차트) + app(ax_claim_checks) + truth(오류·차트 종류)"""

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.src = Source(tmp_path / "source.sqlite")
        self.truth = []  # (treatment_id, error, narrative)
        self.checks = {}

    def claim(self, pid, *, error, narrative=True, ai=None, **kw):
        cid = add_claim(self.src, pid, code=EXTRACT, **kw)
        t = self.src.conn.execute(
            "SELECT treatment_id FROM emr_claims WHERE id = ?", (cid,)
        ).fetchone()[0]
        self.truth.append((t, error, narrative))
        if ai:
            self.checks[cid] = ai
        return cid

    def open(self):
        truth = sqlite3.connect(self.tmp / "truth.sqlite")
        truth.executescript(TRUTH_SCHEMA)
        for t, error, narrative in self.truth:
            truth.execute(
                "INSERT INTO truth_chart_notes (treatment_id, kind, is_special, requirement,"
                " evidence_present, is_decoy, facts) VALUES (?, ?, 0, 'R05', ?, 0, '{}')",
                (t, "narrative" if narrative else "template", int(not error)),
            )
            if error:
                truth.execute(
                    "INSERT INTO truth_claim_errors (treatment_id, kind, requirement)"
                    " VALUES (?, 'requirement', 'R05')",
                    (t,),
                )
        truth.commit()
        truth.close()
        conn = self.src.app()
        with conn:
            for cid, ai in self.checks.items():
                req, version = ai if isinstance(ai, tuple) else (ai, ev.cc.PROMPT_VERSION)
                result = (
                    None
                    if req == "fail"
                    else json.dumps({"requirement": req, "evidence": [], "omissions": []})
                )
                conn.execute(
                    "INSERT INTO ax_claim_checks VALUES (?, '{}', 'h', ?, ?, ?, 'm', 'now')",
                    (cid, result, int(result is not None), version),
                )
        conn.execute("ATTACH DATABASE ? AS truth", (str(self.tmp / "truth.sqlite"),))
        return conn


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


@pytest.fixture
def all_dev(monkeypatch):
    monkeypatch.setattr(ev, "claim_split", lambda claim_id: "dev")


def test_split_stable_half():
    splits = [ev.claim_split(i) for i in range(1, 2001)]
    assert splits == [ev.claim_split(i) for i in range(1, 2001)]
    assert 0.45 < splits.count("dev") / 2000 < 0.55


def test_sample_fixed_and_dev_only(world):
    rev = reviewed_for(10)
    for pid in range(1, 41):
        world.claim(pid, error=pid % 2 == 0, status="paid")
    for pid in range(41, 61):  # 요건 사유로 깎였지만 오류 없는 줄 — 다툴 만함 정답
        world.claim(pid, error=False, status="adjusted", note=REQ_NOTE, reviewed=rev)
    conn = world.open()
    got = ev.dev_sample(conn, DAY, n=12)
    assert got == ev.dev_sample(conn, DAY, n=12) and len(got) == 12
    assert all(ev.claim_split(c) == "dev" for c in got)
    errors = ev.error_claims(conn)
    assert sum(c in errors for c in got) == 6
    contest = [c for c in got if c > 40 and c not in errors]
    # 판단이 어려운 음성(요건 사유로 깎였지만 오류 없음)을 적어도 이만큼 고정해 넣는다
    assert len(contest) >= round(12 / 2 * ev.SAMPLE_CONTEST_SHARE)


def test_score_requirement(world, all_dev):
    world.claim(1, error=True, ai="missing", status="paid")  # TP
    world.claim(2, error=False, ai="missing", status="paid")  # FP
    world.claim(3, error=True, ai="met", status="pending")  # FN
    world.claim(4, error=False, narrative=False, ai="met", status="pending")  # TN
    world.claim(5, error=False, status="paid")  # 점검 안 함 — 모집단 밖
    world.claim(6, error=True, ai="fail", status="paid")  # 후검증 실패 — 표시 없음 = 음성(FN)
    world.claim(7, error=True, ai="na", status="paid")  # 판단 불가 — 음성(FN)
    s = ev.score(world.open(), DAY, "dev")["requirement"]
    assert (s["n"], s["checked"], s["valid"]) == (7, 6, 5)
    assert s["answers"] == {"met": 2, "missing": 2, "na": 1, "failed": 1}
    assert s["ai"] == {"tp": 1, "fp": 1, "fn": 3, "precision": 0.5, "recall": 0.25}
    assert s["narrative_all"]["recall"] == 1.0  # 기준선 — 같은 6줄에서 서술 줄 전부
    assert s["narrative_all"]["precision"] == pytest.approx(4 / 5)
    confirmed = s["by_period"]["confirmed"]
    assert (confirmed["ai"]["tp"], confirmed["ai"]["fn"]) == (1, 2)
    assert confirmed["narrative_all"]["recall"] == 1.0
    assert s["by_period"]["filed"]["ai"]["fn"] == 1


def test_score_only_current_version_and_ids(world, all_dev):
    """표본 반복 — 다른 프롬프트 버전 결과(앱 범위 실행 등)와 표본 밖 줄은 섞지 않는다"""
    a = world.claim(1, error=True, ai="missing", status="paid")
    world.claim(2, error=True, ai=("met", "v0"), status="paid")  # 옛 버전
    world.claim(3, error=False, ai="missing", status="paid")  # 표본 밖
    s = ev.score(world.open(), DAY, "dev", claim_ids=[a, 2])["requirement"]
    assert s["checked"] == 1
    assert s["ai"]["tp"] == 1 and s["ai"]["fp"] == 0


def test_score_adjust_baselines(world, all_dev):
    rev = reviewed_for(10)
    for pid in (1, 2, 3):
        world.claim(pid, error=True, ai="met", status="adjusted", note=REQ_NOTE, reviewed=rev)
    world.claim(4, error=False, ai="met", status="adjusted", note=REQ_NOTE, reviewed=rev)
    world.claim(5, error=False, ai="na", status="adjusted", note=REQ_NOTE, reviewed=rev)
    world.claim(6, error=False, status="adjusted", note=REQ_NOTE, reviewed=rev)  # 점검 안 함
    s = ev.score(world.open(), DAY, "dev")["adjust"]
    assert s["n"] == 5  # 점검한 조정 건만 — 기준선도 같은 5건에서
    assert s["ai"] == {"correct": 1, "answered": 4, "accuracy": 0.2}
    assert s["all_justified"] == pytest.approx(0.6)
    assert s["all_contestable"] == pytest.approx(0.4)


def test_test_split_runs_once(tmp_path):
    assert not ev.test_done(tmp_path, "v1")
    ev.record_test(tmp_path, "v1")
    assert ev.test_done(tmp_path, "v1") and not ev.test_done(tmp_path, "v2")
    with pytest.raises(ev.AlreadyRun):
        ev.check_test(tmp_path, "v1")
    ev.check_test(tmp_path, "v1", force=True)


def test_keyword_rule_words():
    """요건 사유별 근거 낱말이 없으면 오류로 본다 — 부정 표현은 가리지 않는다(점검 때 규칙 그대로)"""
    assert ev.keyword_rule("기존 충전물 파절로 재시행.", ["R01"], 4) is False
    assert ev.keyword_rule("이력 있음, 재시행.", ["R01"], 4) is True
    assert ev.keyword_rule("파절 소견 없음.", ["R01"], 4) is False
    assert ev.keyword_rule("소와열구 깊음.", ["R04"], 4) is False
    assert ev.keyword_rule("실란트 시행.", ["R04"], 4) is True


def test_keyword_rule_r03_depth():
    assert ev.keyword_rule("치주낭 3mm, 출혈 있음.", ["R03"], 4) is True
    assert ev.keyword_rule("치주낭 5mm, 출혈 있음.", ["R03"], 4) is False
    assert ev.keyword_rule("잇몸 깊게 벌어짐.", ["R03"], 4) is True  # 숫자가 없으면 근거를 못 찾음


def test_keyword_reasons_by_procedure_and_context():
    assert ev.keyword_reasons("U0008", "근관 형성.") == []  # 이력 없으면 재시행 요건 아님
    assert ev.keyword_reasons("U0008", "같은 치아 이력 있음.") == ["R01"]
    assert ev.keyword_reasons("U0018", "치석제거 14일 전, 재평가 후.") == ["R02"]
    assert ev.keyword_reasons("U0018", "치주낭 5mm.") == ["R03"]
    assert ev.keyword_reasons("U0012", "발치.") == ["R05"]


def test_score_has_keyword_column(world, all_dev):
    world.claim(1, error=True, ai="missing", status="paid")
    s = ev.score(world.open(), DAY, "dev")["requirement"]
    assert set(s["keyword"]) >= {"precision", "recall", "tp", "fp", "fn"}


def test_report_has_keyword_columns():
    """리포트 표에 키워드 규칙 기준선 열(정밀도·재현율) — AI·서술 줄 전부와 나란히"""
    from evaluation.eval import _render_claim_check

    pr = {"precision": 0.5, "recall": 0.25}
    part = {"ai": pr, "narrative_all": pr, "keyword": {"precision": 0.65, "recall": 0.67}}
    c = {
        "prompt_version": "v2",
        "requirement": part
        | {
            "n": 1,
            "checked": 1,
            "valid": 1,
            "errors": 1,
            "answers": {"met": 0, "missing": 1, "na": 0, "failed": 0},
            "by_period": {"confirmed": part, "filed": part},
        },
        "adjust": {
            "n": 0,
            "ai": {"accuracy": None, "answered": 0},
            "all_justified": None,
            "all_contestable": None,
        },
    }
    text = "\n".join(_render_claim_check(c))
    assert "키워드 정밀도" in text and "65.0%" in text and "67.0%" in text
