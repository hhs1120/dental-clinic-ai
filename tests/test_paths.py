"""규모 → 출력 경로 대응은 generator/paths.py 하나 (Makefile·verify·생성기가 같이 쓴다)."""
import pytest

from generator.paths import ROOT, output_paths


def test_large_uses_default_paths_app_and_eval_read():
    p = output_paths("large")
    assert p["app"] == ROOT / "data" / "app.sqlite"
    assert p["truth"] == ROOT / "ground_truth" / "truth.sqlite"


def test_small_is_separate_and_truth_stays_under_ground_truth():
    p = output_paths("small")
    assert p["app"] == ROOT / "data" / "small" / "app.sqlite"
    assert p["truth"] == ROOT / "ground_truth" / "small" / "truth.sqlite"
    assert p["llm_log"].parent == ROOT / "data" / "small"
    assert set(p.values()).isdisjoint(output_paths("large").values())


def test_unknown_scale_rejected():
    with pytest.raises(ValueError):
        output_paths("smal")
