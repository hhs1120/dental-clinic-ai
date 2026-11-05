"""silver 재료·검증 분할 (evaluation/splits.py, 평가 설계 §2.7, 분류기 설계 "Qwen 비교")."""

import random
from collections import Counter

from evaluation import metrics, splits


def _labels(n=200):
    rng = random.Random(0)
    purposes = ["book", "cancel", "complaint", "emergency", "unknown", "price_q"]
    return [
        {
            "call_uid": f"c-{i:04d}",
            "primary_purpose": rng.choice(purposes),
            "emergency": rng.randint(0, 1),
            "complaint": rng.randint(0, 1),
        }
        for i in range(n)
    ]


def _stratum(r):
    group = r["primary_purpose"] if r["primary_purpose"] in splits.RARE else "other"
    return (group, r["emergency"], r["complaint"])


def test_split_deterministic():
    assert splits.silver_split(_labels()) == splits.silver_split(_labels())
    assert set(splits.silver_split(_labels()).values()) == {"material", "validation"}


def test_split_ratio_per_stratum():
    labels = _labels()
    out = splits.silver_split(labels)
    sizes = Counter(_stratum(r) for r in labels)
    material = Counter(_stratum(r) for r in labels if out[r["call_uid"]] == "material")
    for key, n in sizes.items():
        assert material[key] == round(n * metrics.SILVER_MATERIAL_SHARE)


def test_split_independent_of_input_order():
    labels = _labels()
    shuffled = labels[:]
    random.Random(1).shuffle(shuffled)
    assert splits.silver_split(shuffled) == splits.silver_split(labels)
