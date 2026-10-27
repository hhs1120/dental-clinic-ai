"""make data — 데이터 생성기 진입점. 단계는 docs/치과 AX - 가상 데이터 생성 설계.md §5.1, 실행은 generator/build.py.
출력 경로는 generator/paths.py. LLM 단계(7·9·9b)는 캐시만 읽고 빈 건은 작업으로 내보낸 뒤 exit 1 — 채우기·들여오기는
generator/llm_jobs.py. --fake-llm은 가짜 게이트웨이(테스트, 과금 없음)
"""
import argparse
import sys
from pathlib import Path

from generator.build import LLMMissing, generate
from generator.paths import ROOT, output_paths


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fake-llm", action="store_true", help="LLM 단계를 가짜 게이트웨이로 (테스트)")
    parser.add_argument("--db", type=Path, help="경로를 직접 줄 때(테스트용)")
    parser.add_argument("--truth", type=Path)
    args = parser.parse_args()
    if (args.db is None) != (args.truth is None):
        # 하나만 주면 나머지가 기본 경로로 떨어져 실제 DB를 덮어쓴다 (verify는 읽기 전용이라 허용)
        parser.error("--db와 --truth는 같이 준다")
    paths = output_paths()
    try:
        generate(args.db or paths["app"], args.truth or paths["truth"], ROOT / "config" / "generator.yaml",
                 fake_llm=args.fake_llm, rag_qa_path=None if args.fake_llm else paths["rag_qa"])
    except LLMMissing as e:
        print(f"LLM 캐시에 없는 건: {e} — DB는 그대로입니다.")
        if e.missing:
            print(f"작업을 {paths['llm_jobs']}에 썼습니다. 채우기: make llm-fill-api TASK=<작업> 또는 Claude Code"
                  " /fill-llm-jobs → make llm-import → make data")
        for task, items in e.exhausted.items():
            print(f"{task}: 시도를 다 쓴 건 {', '.join(items[:10])}{' …' if len(items) > 10 else ''} — 프롬프트를 새 버전으로"
                  " 고치거나 그 건의 캐시 줄을 모두 지운 뒤 make data")
        sys.exit(1)


if __name__ == "__main__":
    main()
