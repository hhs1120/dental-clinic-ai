"""make data — 데이터 생성기 진입점. 단계는 docs/치과 AX - 가상 데이터 생성 설계.md §5.1.

생성기 구현 메모 (코드가 생기면 해당 함수 docstring으로 옮긴다):
- 단계별 시드: np.random.default_rng([seed, zlib.crc32(b"patients")]) 처럼 단계 이름으로 파생.
  내장 hash()는 문자열에 실행마다 다른 솔트가 붙어 쓰지 않는다. Faker는 전역 Faker.seed가 아니라
  단계별 인스턴스에 seed_instance. 문자열 set·dict 순회에 기대지 말고 정렬(make data는 PYTHONHASHSEED=0으로도 막음)
- 생성기 LLM 호출도 shared/ 게이트웨이 경유 → llm_calls_gen.jsonl, 상한 DAILY_GEN_USD_LIMIT(런타임과 분리,
  금액은 기능 정의 §7). 전사는 프론티어 실시간, Semaphore(20) + 백오프
- 출력 경로는 generator/paths.py. 7·9단계는 large만 실제 LLM, small은 fake 게이트웨이
"""
import argparse

from generator.paths import SCALES


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scale", choices=SCALES, default="large")
    parser.add_argument("--regen-llm", action="store_true")
    args = parser.parse_args()
    raise SystemExit(f"TODO: 생성기 미구현 (scale={args.scale})")


if __name__ == "__main__":
    main()
