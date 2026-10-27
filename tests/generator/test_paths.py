"""출력 경로는 generator/paths.py 하나 (Makefile·verify·생성기가 같이 쓴다)."""

from generator.paths import ROOT, output_paths


def test_default_paths_app_and_eval_read():
    p = output_paths()
    assert p["app"] == ROOT / "data" / "app.sqlite"
    assert p["truth"] == ROOT / "ground_truth" / "truth.sqlite"
    assert p["llm_log"].parent == ROOT / "data"


def test_llm_folders_outside_app_data():
    """LLM 작업·캐시에는 라벨이 들어 있어 앱이 읽는 data/ 밖에 둔다 (가상 데이터 생성 설계 §5.1)"""
    p = output_paths()
    assert all(
        not p[k].is_relative_to(ROOT / "data") for k in ("llm_cache", "llm_jobs", "llm_results")
    )
