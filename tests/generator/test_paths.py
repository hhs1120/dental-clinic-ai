"""출력 경로는 generator/paths.py 하나 (Makefile·verify·생성기가 같이 쓴다)."""
from generator.paths import ROOT, output_paths


def test_default_paths_app_and_eval_read():
    p = output_paths()
    assert p["app"] == ROOT / "data" / "app.sqlite"
    assert p["truth"] == ROOT / "ground_truth" / "truth.sqlite"
    assert p["llm_log"].parent == ROOT / "data"
