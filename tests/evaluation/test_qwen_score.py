"""Qwen 채점·교체 기준 (evaluation/finetune/qwen/score.py, 분류기 설계 "Qwen 비교")."""

from collections import Counter

import pytest

from evaluation import classification
from evaluation.finetune.qwen import score
from shared.call_labels import FLAGS

BASE = {
    "primary_purpose": "book",
    "counterpart": "patient",
    "urgency": "normal",
    **{f: 0 for f in FLAGS},
}


def _rows(golds):
    return [{"id": f"c-{i}", "gold": g} for i, g in enumerate(golds)]


def test_failure_counts_wrong():
    rows = _rows([BASE] * 4)
    pred = {"c-0": BASE, "c-1": BASE, "c-2": BASE, "c-3": None}
    out = score.score_set(pred, rows, None)
    assert out["n"] == 4 and out["failed"] == 1
    assert out["purpose"]["k"] == 3 and out["purpose"]["strict"] == 0.75
    assert "weighted" not in out["purpose"]


def test_missing_counts_failed():
    out = score.score_set({}, _rows([BASE]), None)
    assert out["failed"] == 1 and out["items"]["primary_purpose"] == 0


def test_urgent_only():
    out = score.score_set({"c-0": dict(BASE, urgency="low")}, _rows([BASE]), None)
    assert out["items"]["urgency"] == 0 and out["urgent"] == 1


def test_flags_mean():
    out = score.score_set({"c-0": dict(BASE, has_todo=1)}, _rows([BASE]), None)
    assert out["flags_mean"] == pytest.approx(5 / 6)


def test_weighted_matches_eval():
    golds = [
        BASE,
        BASE,
        dict(BASE, primary_purpose="cancel"),
        dict(BASE, primary_purpose="cancel"),
    ]
    rows = _rows(golds)
    pred = {"c-0": BASE, "c-1": dict(BASE, primary_purpose="cancel"), "c-2": golds[2], "c-3": BASE}
    population = Counter({"book": 90, "cancel": 10})
    mine = score.score_set(pred, rows, population)["purpose"]
    calls = {r["id"]: {"gold": r["gold"]} for r in rows}
    ref = {r["id"]: r["gold"] for r in rows}
    theirs = classification._score(set(ref), pred, ref, calls, population)["purpose"]
    assert mine["weighted"] == pytest.approx(theirs["weighted"])
    assert mine["macro"] == pytest.approx(theirs["macro"])
    assert mine["by_category"] == theirs["by_category"]


def _scored(strict=None, flags=None, weighted=None):
    return {"purpose": {"strict": strict, "weighted": weighted}, "flags_mean": flags}


def _qwen(val_strict, val_flags, dev, styles):
    out = {"validation": _scored(val_strict, val_flags), "dev": _scored(weighted=dev)}
    out |= {s: _scored(weighted=w) for s, w in zip(("s1", "s2", "s3"), styles, strict=True)}
    return out


def test_criteria_boundary():
    ref = _scored(0.98, 0.99)
    ok = score.criteria(_qwen(0.96, 0.97, 0.95, (0.95,) * 3), ref)
    assert ok["c1"]["pass"] and ok["c2"]["pass"] and ok["all"]
    bad = score.criteria(_qwen(0.9599, 0.9699, 0.95, (0.95,) * 3), ref)
    assert not bad["c1"]["pass"] and not bad["c2"]["pass"] and not bad["all"]


def test_criteria_style():
    ref = _scored(0.98, 0.99)
    ok = score.criteria(_qwen(0.98, 0.99, 0.95, (0.93, 0.90, 0.94)), ref)
    assert ok["c3"]["pass"] and ok["c3"]["worst_style"] == "s2" and ok["c3"]["worst"] == 0.90
    bad = score.criteria(_qwen(0.98, 0.99, 0.95, (0.93, 0.8999, 0.94)), ref)
    assert not bad["c3"]["pass"] and not bad["all"]


def test_report_has_all_sets():
    one = score.score_set({"c-0": BASE}, _rows([BASE]), Counter({"book": 1}))
    sets = {s: one for s in ("validation", "dev", "s1", "s2", "s3")}
    results = {"qwen_full": sets, "haiku_v2": sets}
    crit = {"qwen_full": score.criteria(sets, one)}
    text = score.report(results, crit)
    for name in ("validation", "dev", "s1", "s2", "s3", "qwen_full", "haiku_v2"):
        assert name in text
