"""Qwen 파인튜닝 비교 명령 — 분류기 설계 "Qwen 비교". MLX가 필요한 명령은 --group finetune으로.
uv run python -m evaluation.finetune.qwen data
uv run --group finetune python -m evaluation.finetune.qwen lengths
uv run --group finetune python -m evaluation.finetune.qwen train <full|half|speed> [continue] [epochs=N] [키=값 …]
  기본 1에폭. continue는 끝난 어댑터에서 N에폭 더. 키=값은 lora.yaml 덮기(예: steps_per_eval=100)
uv run python -m evaluation.finetune.qwen status <full|half|speed> [--watch]  (30초마다 갱신)
uv run --group finetune python -m evaluation.finetune.qwen predict <full|half|speed|none> [셋,…] [limit]
  셋 기본 validation,dev,s1,s2,s3 · limit은 셋마다 앞 N건 · none은 학습 전 기본 모델
"""

import json
import sys
from pathlib import Path

from evaluation.finetune.qwen import data

ROOT = Path(__file__).resolve().parents[3]


def _predict(name: str, sets: str = ",".join(data.SETS), limit: str | None = None) -> None:
    from evaluation.finetune.qwen import infer, train

    rows = [json.loads(line) for line in (data.DATA / "eval.jsonl").read_text().splitlines()]
    picked = []
    for s in sets.split(","):
        mine = [r for r in rows if r["set"] == s]
        picked += mine[: int(limit)] if limit else mine
    adapter = None if name == "none" else train.ADAPTERS / name
    gen = infer.mlx_generate(train.config()["model"], adapter)
    out = train.OUT / f"pred_{name}.jsonl"
    infer.predict(picked, out, gen)
    mine = [json.loads(line) for line in out.read_text().splitlines()]
    secs = sorted(r["sec"] for r in mine if r["id"] in {p["id"] for p in picked})
    print(f"{out.name}: {len(secs)}건, 건당 중앙 {secs[len(secs) // 2]}초·최대 {secs[-1]}초,"
          f" 파싱 실패 {sum(r['labels'] is None for r in mine)}, Peak mem {infer.peak_memory_gb():.2f}GB")  # fmt: skip


def main(argv: list[str]) -> None:
    cmd, *args = argv or [""]
    if cmd == "data":
        for k, v in data.write_all(ROOT).items():
            print(f"{k}: {v}")
    elif cmd == "lengths":
        from evaluation.finetune.qwen import train

        paths = [
            data.DATA / n / f"{s}.jsonl" for n in ("full", "half") for s in ("train", "valid")
        ]
        print(train.token_lengths(paths), "max_seq_length", train.config()["max_seq_length"])
    elif cmd == "train":
        from evaluation.finetune.qwen import train

        name, *rest = args
        cont = "continue" in rest
        opts = dict(a.split("=", 1) for a in rest if "=" in a)
        epochs = int(opts.pop("epochs", 1))
        train.train(name, epochs, cont, {k: train.parse_override(v) for k, v in opts.items()})
    elif cmd == "status":
        import time

        from evaluation.finetune.qwen import train

        while True:
            text = train.status(args[0])
            print(("\033[2J\033[H" if "--watch" in args else "") + text, flush=True)
            if "--watch" not in args or "끝남" in text:
                break
            time.sleep(30)
    elif cmd == "_part":  # train이 part마다 띄우는 자식 프로세스
        from evaluation.finetune.qwen import train

        sys.exit(train.part_main(args[0]))
    elif cmd == "predict":
        _predict(*args)
    else:
        raise SystemExit("명령: data | lengths | train | status | predict")


if __name__ == "__main__":
    main(sys.argv[1:])
