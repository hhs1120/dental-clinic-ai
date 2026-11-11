"""⑧-1 규칙 기준선 채점(평가 설계 §5 ⑧-1) — evaluation/recon.py score. 정답 건 ↔ 탐지 건 대응(설계 §1):
관련 행이 가장 많이 겹치는 탐지 건, 한 탐지 건이 정답 둘과 겹치면 둘 다 그 원인, 미탐지는 오답."""

import json
import sqlite3

import pytest

from evaluation import eval as ev
from evaluation import recon as recon_eval
from generator.master import RECON_CAUSE_GROUPS
from generator.paths import ROOT
from tests.backend.test_recon_match import DAY, Books

TRUTH_SCHEMA = (ROOT / "ground_truth" / "schema.sql").read_text(encoding="utf-8")


def _truth(tmp_path, conn, rows):
    """rows: (날짜, 원인, fin 참조, 수납 id, split)"""
    path = tmp_path / "truth.sqlite"
    t = sqlite3.connect(path)
    t.executescript(TRUTH_SCHEMA)
    t.executemany(
        "INSERT INTO truth_recon_mismatches (occurred_on, cause, fin_ids, payment_ids, split)"
        " VALUES (?, ?, ?, ?, ?)",
        [(d, c, json.dumps(f), json.dumps(p), s) for d, c, f, p, s in rows],
    )
    t.commit()
    t.close()
    conn.execute("ATTACH DATABASE ? AS truth", (str(path),))


@pytest.fixture
def world(tmp_path):
    b = Books(tmp_path / "source.sqlite")
    # 11-03: 금액 오입력(규칙이 맞힘), 11-04: 진료 외 매출 13,000(규칙이 맞힘)
    typo = b.pay("2026-11-03 10:00:00", 3_500, "card")
    s1 = b.sale("2026-11-03 10:00:00", 35_000, "R1")
    s2 = b.sale("2026-11-04 15:00:00", 13_000, "R2")
    b.settle("2026-11-03", "2026-11-04")
    return tmp_path, b.app(), typo, s1, s2


def card(i):
    return [{"table": "fin_card_sales", "id": i}]


def test_score_counts(world):
    tmp_path, conn, typo, s1, s2 = world
    _truth(
        tmp_path,
        conn,
        [
            ("2026-11-03", "amount_typo", card(s1), [typo], "dev"),
            ("2026-11-04", "entry_missing", card(s2), [], "dev"),  # 규칙은 진료 외 매출 → 오답
            ("2026-11-05", "fee_mismatch", [{"table": "fin_card_deposits", "id": 999}], [], "dev"),
        ],
    )
    r = recon_eval.score(conn, "dev", DAY)
    assert r["n"] == 3
    assert r["detected"] == {"k": 2, "n": 3}
    assert r["rule"] == {"k": 1, "n": 3}  # 미탐지는 오답
    assert r["by_group"] == {"rule": {"k": 0, "n": 1}, "ambiguous": {"k": 1, "n": 2}}
    assert r["by_cause"]["amount_typo"] == {"k": 1, "n": 1}
    assert {
        "date": "2026-11-04",
        "cause": "entry_missing",
        "rule_cause": "non_clinical_sale",
    } in r["wrong"]


def test_one_item_two_truths(world):
    tmp_path, conn, typo, s1, _ = world
    _truth(
        tmp_path,
        conn,
        [
            ("2026-11-03", "amount_typo", card(s1), [], "dev"),
            ("2026-11-03", "entry_missing", [], [typo], "dev"),
        ],
    )
    r = recon_eval.score(conn, "dev", DAY)
    assert r["detected"] == {"k": 2, "n": 2}
    assert r["rule"] == {"k": 1, "n": 2}  # 같은 탐지 건 하나의 원인(금액 오입력)으로 둘 다 채점


def test_groups_from_master(world):
    tmp_path, conn, *_ = world
    _truth(tmp_path, conn, [])
    r = recon_eval.score(conn, "dev", DAY)
    assert set(r["by_group"]) == set(RECON_CAUSE_GROUPS.values())


def test_test_split_hides_lists(world):
    tmp_path, conn, typo, s1, _ = world
    _truth(tmp_path, conn, [("2026-11-03", "amount_typo", card(s1), [typo], "test")])
    r = recon_eval.score(conn, "test", DAY)
    assert r["rule"] == {"k": 1, "n": 1}
    assert r["by_cause"] is None and r["wrong"] is None
    assert recon_eval.score(conn, "dev", DAY)["n"] == 0  # 분할로 거른다


def test_eval_dev_includes_recon(world):
    tmp_path, conn, typo, s1, _ = world
    _truth(tmp_path, conn, [("2026-11-03", "amount_typo", card(s1), [typo], "dev")])
    assert ev._has_recon(conn)  # eval.run dev가 이 절을 넣는 조건 — 정답 행이 있음
    text = "\n".join(ev._render_recon(recon_eval.score(conn, "dev", DAY)))
    assert "## ⑧-1 규칙 기준선" in text
    assert "1/1" in text


def test_render_has_no_lists_on_test(world):
    tmp_path, conn, typo, s1, _ = world
    _truth(tmp_path, conn, [("2026-11-03", "amount_typo", card(s1), [typo], "test")])
    text = "\n".join(ev._render_recon(recon_eval.score(conn, "test", DAY)))
    assert "틀린 건" not in text


def test_no_recon_truth_skips(world):
    tmp_path, conn, *_ = world
    _truth(tmp_path, conn, [])
    assert not ev._has_recon(conn)
