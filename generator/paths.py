"""출력 경로. Makefile·verify·생성기가 같이 쓴다. 앱·평가가 보는 data/, 정답은 ground_truth/ 아래."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def output_paths() -> dict[str, Path]:
    return {
        "app": ROOT / "data" / "app.sqlite",
        "llm_log": ROOT / "data" / "llm_calls_gen.jsonl",
        "truth": ROOT / "ground_truth" / "truth.sqlite",
    }
