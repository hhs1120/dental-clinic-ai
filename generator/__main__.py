"""make data — 데이터 생성기 진입점. 단계는 docs/치과 AX - 가상 데이터 생성 설계.md §5.1, 실행은 generator/build.py.

생성기 구현 메모 (코드가 생기면 해당 함수 docstring으로 옮긴다):
- 생성기 LLM 호출도 shared/ 게이트웨이 경유 → llm_calls_gen.jsonl, 상한 DAILY_GEN_USD_LIMIT(런타임과 분리,
  금액은 기능 정의 §7). 전사는 claude-sonnet-5-5 실시간, Semaphore(20) + 백오프
- 출력 경로는 generator/paths.py. 7·9·9b단계는 large만 실제 LLM, small은 fake 게이트웨이
"""
import argparse
from pathlib import Path

from generator.build import generate
from generator.paths import ROOT, SCALES, output_paths


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scale", choices=SCALES, default="large")
    parser.add_argument("--regen-llm", action="store_true")
    parser.add_argument("--db", type=Path, help="경로를 직접 줄 때(테스트용)")
    parser.add_argument("--truth", type=Path)
    args = parser.parse_args()
    if (args.db is None) != (args.truth is None):
        # 하나만 주면 나머지가 기본 경로로 떨어져 실제 DB를 덮어쓴다 (verify는 읽기 전용이라 허용)
        parser.error("--db와 --truth는 같이 준다")
    paths = output_paths(args.scale)
    generate(args.scale, args.db or paths["app"], args.truth or paths["truth"], ROOT / "config" / "generator.yaml")


if __name__ == "__main__":
    main()
