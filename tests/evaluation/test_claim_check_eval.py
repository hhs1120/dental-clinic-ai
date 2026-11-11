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
            for cid, req in self.checks.items():
                result = (
                    None
                    if req == "fail"
                    else json.dumps({"requirement": req, "evidence": [], "omissions": []})
                )
                conn.execute(
                    "INSERT INTO ax_claim_checks VALUES (?, '{}', 'h', ?, ?, 'v1', 'm', 'now')",
                    (cid, result, int(result is not None)),
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
    for pid in range(1, 41):
        world.claim(pid, error=pid % 2 == 0, status="paid")
    conn = world.open()
    got = ev.dev_sample(conn, DAY, n=6)
    assert got == ev.dev_sample(conn, DAY, n=6) and len(got) == 6
    assert all(ev.claim_split(c) == "dev" for c in got)
    errors = ev.error_claims(conn)
    assert sum(c in errors for c in got) == 3


def test_score_requirement(world, all_dev):
    world.claim(1, error=True, ai="missing", status="paid")  # TP
    world.claim(2, error=False, ai="missing", status="paid")  # FP
    world.claim(3, error=True, ai="met", status="pending")  # FN
    world.claim(4, error=False, narrative=False, ai="met", status="pending")  # TN
    world.claim(5, error=False, status="paid")  # 점검 안 함
    world.claim(6, error=True, ai="fail", status="paid")  # 후검증 실패
    s = ev.score(world.open(), DAY, "dev")["requirement"]
    assert (s["n"], s["checked"], s["valid"]) == (6, 5, 4)
    assert s["ai"] == {"tp": 1, "fp": 1, "fn": 1, "precision": 0.5, "recall": 0.5}
    assert s["narrative_all"]["recall"] == 1.0  # 기준선 — 같은 4줄에서 서술 줄 전부
    assert s["narrative_all"]["precision"] == pytest.approx(2 / 3)
    assert s["by_period"]["confirmed"]["ai"]["tp"] == 1
    assert s["by_period"]["filed"]["ai"]["fn"] == 1


def test_score_adjust_baselines(world, all_dev):
    rev = reviewed_for(10)
    for pid in (1, 2, 3):
        world.claim(pid, error=True, ai="met", status="adjusted", note=REQ_NOTE, reviewed=rev)
    world.claim(4, error=False, ai="met", status="adjusted", note=REQ_NOTE, reviewed=rev)
    world.claim(5, error=False, ai="na", status="adjusted", note=REQ_NOTE, reviewed=rev)
    s = ev.score(world.open(), DAY, "dev")["adjust"]
    assert s["n"] == 5
    assert s["ai"] == {"correct": 1, "answered": 4, "accuracy": 0.2}
    assert s["all_justified"] == pytest.approx(0.6)
    assert s["all_contestable"] == pytest.approx(0.4)


def test_test_split_runs_once(tmp_path):
    ev.record_test(tmp_path, "v1")
    with pytest.raises(ev.AlreadyRun):
        ev.record_test(tmp_path, "v1")
    ev.record_test(tmp_path, "v1", force=True)
    ev.record_test(tmp_path, "v2")
