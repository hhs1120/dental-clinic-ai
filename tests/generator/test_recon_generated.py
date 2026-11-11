"""⑧-1 탐지 재현율·오탐·규칙 묶음을 생성 데이터 정답으로 확인(평가 설계 §5 ⑧-1 "탐지 재현율은 테스트", 설계
docs/superpowers/specs/2026-11-11-finance-recon-design.md §1). 생성 결과가 이 폴더 픽스처에 있어 backend를 부르는
테스트지만 여기에 둔다. 정답 건 ↔ 탐지 건 대응 = 관련 행이 가장 많이 겹치는 건(같으면 키 순 첫 건)."""

import json
from datetime import date

import pytest

from backend import db, recon, recon_match
from generator.master import RECON_CAUSE_GROUPS, recon_months


@pytest.fixture(scope="module")
def world(generated):
    out, _ = generated("recon")
    conn = db.connect(out / "app.sqlite", out / "source.sqlite")
    db.init_schema(conn)
    conn.execute("ATTACH DATABASE ? AS truth", (str(out / "truth.sqlite"),))
    today = date.fromisoformat(
        conn.execute("SELECT value FROM truth.truth_gen_meta WHERE key = 'today'").fetchone()[0]
    )
    books = recon_match.load(conn, today)
    truth = [dict(r) for r in conn.execute("SELECT * FROM truth.truth_recon_mismatches")]
    return today, books, recon_match.detect(books), truth


def _refs(fin, payment_ids) -> set:
    return {(f["table"], f["id"]) for f in fin} | {("emr_payments", p) for p in payment_ids}


def _match(truth_row, items):
    t = _refs(json.loads(truth_row["fin_ids"]), json.loads(truth_row["payment_ids"]))
    hits = [(len(_refs(i["fin"], i["payment_ids"]) & t), i["key"], i) for i in items]
    hits = [h for h in hits if h[0]]
    return min(hits, key=lambda h: (-h[0], h[1]))[2] if hits else None


def test_every_truth_mismatch_detected(world):
    _, _, items, truth = world
    missed = [(t["occurred_on"], t["cause"]) for t in truth if _match(t, items) is None]
    assert truth and missed == []


def test_no_false_positives(world):
    today, _, items, truth = world
    matched = {_match(t, items)["key"] for t in truth}
    extra = [(i["date"], i["channel"]) for i in items if i["key"] not in matched]
    assert extra == []
    months = recon_months(today)
    assert all(i["date"][:7] in months for i in items)


def test_rule_group_exact(world):
    _, books, items, truth = world
    wrong = [
        (t["occurred_on"], t["cause"], recon.rule_cause(_match(t, items), books))
        for t in truth
        if t["split"]
        == "dev"  # 원인 정확도는 기준선 채점이라 test 분할은 보지 않는다(평가 설계 §2.7)
        and RECON_CAUSE_GROUPS[t["cause"]] == "rule"
        and recon.rule_cause(_match(t, items), books) != t["cause"]
    ]
    assert wrong == []
