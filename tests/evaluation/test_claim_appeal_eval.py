"""⑤-4 재심 사유서 채점(evaluation/claim_appeal.py) — 설계 docs/superpowers/specs/2026-11-10-claim-review-design.md §4.
정답 다툴 만함 건 고르기, 첫 답·최종 답 기록, 이어 돎, 후검증 통과율·첫 답 근거 없는 문장 비율을 본다."""

import json
import sqlite3

import pytest

from backend import claim_appeal as ca
from evaluation import claim_appeal as ev
from generator.master import ADJUST_REASONS
from tests.backend.test_claim_appeal import FakeGw, contested, good, llm, s, src  # noqa: F401
from tests.evaluation.eval_db import TRUTH_SCHEMA

R02 = ADJUST_REASONS["R02"]["note"]
DOC = ADJUST_REASONS["D01"]["note"]


@pytest.fixture
def world(src, tmp_path):  # noqa: F811
    from tests.backend.test_claim_appeal import reviewed_for
    from tests.backend.test_claims import add_claim

    ok = contested(src, 1)  # 요건 사유, 오류 없음 = 다툴 만함
    err = contested(src, 2)  # 요건 사유, 오류 있음 = 정당
    add_claim(src, 3, note=DOC, reviewed=reviewed_for(10))  # 서류 사유 — 대상 아님
    t = src.conn.execute("SELECT treatment_id FROM emr_claims WHERE id = ?", (err,)).fetchone()[0]
    truth = sqlite3.connect(tmp_path / "truth.sqlite")
    truth.executescript(TRUTH_SCHEMA)
    truth.execute(
        "INSERT INTO truth_claim_errors (treatment_id, kind, requirement) VALUES (?, 'requirement', 'R02')",
        (t,),
    )
    truth.commit()
    truth.close()
    conn = src.app()
    conn.execute("ATTACH DATABASE ? AS truth", (str(tmp_path / "truth.sqlite"),))
    return conn, ok


def test_targets_contestable_truth_only(world, monkeypatch):
    conn, ok = world
    monkeypatch.setattr(ev, "claim_split", lambda cid: "dev")
    rows = ev.targets(conn, "dev")
    assert [r["claim_id"] for r in rows] == [ok]
    assert rows[0]["heading"] and "judgment" not in ca.appeal_facts(rows[0])


def test_run_records_first_and_final(world, monkeypatch, tmp_path):
    conn, ok = world
    monkeypatch.setattr(ev, "claim_split", lambda cid: "dev")
    rows = ev.targets(conn, "dev")
    bad = {
        "sentences": [s("근거 없는 문장입니다.", "chart", "차트에 없는 문장입니다")],
        "assumptions": [],
    }
    path = tmp_path / "runs.jsonl"
    out = ev.run(FakeGw(llm(bad), llm(good())), rows, path)
    assert out == {"called": 1, "skipped": 0, "failed": 0}
    (rec,) = [json.loads(x) for x in path.read_text("utf-8").splitlines()]
    assert rec["first"] == bad and rec["final"] == good() and rec["claim_id"] == ok


def test_run_skips_done(world, monkeypatch, tmp_path):
    conn, _ = world
    monkeypatch.setattr(ev, "claim_split", lambda cid: "dev")
    rows = ev.targets(conn, "dev")
    path = tmp_path / "runs.jsonl"
    ev.run(FakeGw(llm(good())), rows, path)
    gw = FakeGw()
    assert ev.run(gw, rows, path)["skipped"] == 1 and gw.calls == []


def test_score(world, monkeypatch, tmp_path):
    conn, _ = world
    monkeypatch.setattr(ev, "claim_split", lambda cid: "dev")
    rows = ev.targets(conn, "dev")
    two = good()
    # 차트 인용 칸에 두 문장 — 둘째 문장에는 인용이 없어 근거 없음(추측 칸이면 둘 다 추측 번호가 근거)
    two["sentences"][2]["text"] += " 그러므로 요건을 갖추었습니다."
    path = tmp_path / "runs.jsonl"
    ev.run(FakeGw(llm(two), llm(good())), rows, path)
    out = ev.score(rows, ev.read_runs(path))
    assert (out["n"], out["run"], out["first_valid"], out["final_valid"]) == (1, 1, 0, 1)
    assert (out["sentences"], out["unbacked"]) == (5, 1) and out["unbacked_rate"] == 0.2


def test_render():
    from evaluation.eval import _render_claim_appeal

    text = "\n".join(
        _render_claim_appeal(
            {
                "prompt_version": "v1",
                "n": 70,
                "run": 70,
                "first_valid": 60,
                "final_valid": 68,
                "sentences": 350,
                "unbacked": 7,
                "unbacked_rate": 0.02,
            }
        )
    )
    assert "⑤-4" in text and "68/70" in text
