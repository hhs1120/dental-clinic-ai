"""make data — 데이터 생성기 진입점. 단계는 docs/치과 AX - 가상 데이터 생성 설계.md §5.1."""
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
