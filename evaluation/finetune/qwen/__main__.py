"""Qwen 파인튜닝 비교 명령 — 분류기 설계 "Qwen 비교".
uv run python -m evaluation.finetune.qwen data
"""

import sys
from pathlib import Path

from evaluation.finetune.qwen import data

ROOT = Path(__file__).resolve().parents[3]


def main(argv: list[str]) -> None:
    cmd = argv[0] if argv else ""
    if cmd == "data":
        for k, v in data.write_all(ROOT).items():
            print(f"{k}: {v}")
    else:
        raise SystemExit("명령: data")


if __name__ == "__main__":
    main(sys.argv[1:])
