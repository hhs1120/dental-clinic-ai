"""출력 경로. Makefile·verify·생성기·평가가 같이 쓴다. data/의 source.sqlite는 생성기 출력(앱은 읽기 전용으로 붙임),
app.sqlite는 앱이 쓰는 DB(가상 데이터 생성 설계 §0). 정답은 ground_truth/, LLM 단계 작업은 generator/llm/ 아래."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def output_paths() -> dict[str, Path]:
    return {
        "source": ROOT / "data" / "source.sqlite",
        "app": ROOT / "data" / "app.sqlite",
        "llm_log": ROOT / "data" / "llm_calls_gen.jsonl",
        # LLM 단계(§5.1) — 입력·출력에 라벨이 있어 앱이 읽는 data/ 밖에 둔다
        "llm_cache": ROOT / "generator" / "llm" / "cache",  # 커밋 — 통과·실패한 시도
        "llm_jobs": ROOT / "generator" / "llm" / "jobs",  # 일회용 — make data가 쓰는 빈 건 작업
        "llm_results": ROOT
        / "generator"
        / "llm"
        / "results",  # 일회용 — 채운 결과, make llm-import가 지움
        "truth": ROOT / "ground_truth" / "truth.sqlite",
        "rag_qa": ROOT
        / "ground_truth"
        / "rag_qa.json",  # 사람이 쓴 RAG 질문(평가 설계 §3.1) — make data가 truth_rag_qa로 적재
    }
