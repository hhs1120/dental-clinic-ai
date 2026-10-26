"""출력 경로. Makefile·verify·생성기가 같이 쓴다. 앱·평가가 보는 data/, 정답은 ground_truth/, LLM 단계 작업은 generator/llm/ 아래."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def output_paths() -> dict[str, Path]:
    return {
        "app": ROOT / "data" / "app.sqlite",
        "llm_log": ROOT / "data" / "llm_calls_gen.jsonl",
        # LLM 단계(§5.1) — 입력·출력에 라벨이 있어 앱이 읽는 data/ 밖에 둔다
        "llm_cache": ROOT / "generator" / "llm" / "cache",      # 커밋 — 통과·실패한 시도
        "llm_jobs": ROOT / "generator" / "llm" / "jobs",        # 일회용 — make data가 쓰는 빈 건 작업
        "llm_results": ROOT / "generator" / "llm" / "results",  # 일회용 — 채운 결과, make llm-import가 지움
        "truth": ROOT / "ground_truth" / "truth.sqlite",
    }
