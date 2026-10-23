"""규모 → 출력 경로. Makefile·verify·생성기가 같이 쓴다 (규모는 docs/치과 AX - 가상 데이터 생성 설계.md §0).
large는 앱·평가가 보는 기본 경로, small은 따로 둬서 서로 덮어쓰지 않는다. 정답은 규모와 무관하게 ground_truth/ 아래."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCALES = ("small", "large")


def output_paths(scale: str) -> dict[str, Path]:
    if scale not in SCALES:
        raise ValueError(f"알 수 없는 규모: {scale} (가능: {', '.join(SCALES)})")
    data = ROOT / "data" if scale == "large" else ROOT / "data" / scale
    truth = ROOT / "ground_truth" if scale == "large" else ROOT / "ground_truth" / scale
    return {
        "app": data / "app.sqlite",
        "llm_log": data / "llm_calls_gen.jsonl",
        "truth": truth / "truth.sqlite",
    }
