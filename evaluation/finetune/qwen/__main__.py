"""Qwen 파인튜닝 비교 명령 — 분류기 설계 "Qwen 비교". MLX가 필요한 명령은 --group finetune으로.
uv run python -m evaluation.finetune.qwen data
uv run --group finetune python -m evaluation.finetune.qwen lengths
uv run --group finetune python -m evaluation.finetune.qwen train <full|half|speed> [continue|resume] [epochs=N] [키=값 …]
  기본 1에폭. continue는 끝난 어댑터에서 N에폭 더, resume은 부모까지 죽어 끝나지 않은 학습을 마지막 체크포인트부터.
  키=값은 lora.yaml 덮기(예: steps_per_eval=100)
uv run python -m evaluation.finetune.qwen status <full|half|speed> [--watch]  (30초마다 갱신)
uv run --group finetune python -m evaluation.finetune.qwen predict <full|half|speed|none> [셋,…] [limit]
  셋 기본 validation,dev,s1,s2,s3 · limit은 셋마다 앞 N건 · none은 학습 전 기본 모델
uv run --env-file .env python -m evaluation.finetune.qwen baselines [--run-style [N]]
  Haiku v2·Solar v2 → out/baseline.jsonl. 검증·dev는 app.sqlite(읽기만), 문체 dev는 --run-style일 때 API 호출(과금, N건만 가능)
uv run python -m evaluation.finetune.qwen score  → out/report.md (있는 Qwen 예측 half·full과 기준선)
uv run --group finetune python -m evaluation.finetune.qwen gateway-check [n]  (Qwen 판정기 확인 — app.sqlite 안 씀, 기본 30건)
"""

import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

from evaluation.finetune.qwen import data

ROOT = Path(__file__).resolve().parents[3]


def _predict(name: str, sets: str = ",".join(data.SETS), limit: str | None = None) -> None:
    from evaluation.finetune.qwen import infer, train

    wanted = sets.split(",")
    if unknown := set(wanted) - set(data.SETS):
        raise SystemExit(f"모르는 셋 {sorted(unknown)} — {','.join(data.SETS)} 중에서")
    rows = [json.loads(line) for line in (data.DATA / "eval.jsonl").read_text().splitlines()]
    picked = []
    for s in wanted:
        mine = [r for r in rows if r["set"] == s]
        picked += mine[: int(limit)] if limit else mine
    adapter = None if name == "none" else train.ADAPTERS / name
    out = train.OUT / f"pred_{name}.jsonl"
    fingerprint = infer.adapter_fingerprint(adapter)
    gen = infer.mlx_generate(train.config()["model"], adapter)
    infer.predict(picked, out, gen, fingerprint)
    ids = {p["id"] for p in picked}
    mine = [r for r in map(json.loads, out.read_text().splitlines()) if r["id"] in ids]
    secs = sorted(r["sec"] for r in mine)
    print(f"{out.name}: {len(secs)}건, 건당 중앙 {secs[len(secs) // 2]}초·최대 {secs[-1]}초,"
          f" 파싱 실패 {sum(r['labels'] is None for r in mine)}, Peak mem {infer.peak_memory_gb():.2f}GB")  # fmt: skip


def _baselines(args: list[str]) -> None:
    from evaluation.finetune.qwen import baselines, train

    rows = [json.loads(line) for line in (data.DATA / "eval.jsonl").read_text().splitlines()]
    style_path = train.OUT / "baseline_style.jsonl"
    style = [r for r in rows if r["set"] in data.STYLES]
    if "--run-style" in args:
        from shared.llm import Gateway, JsonlLog, load_config

        rest = args[args.index("--run-style") + 1 :]
        before = sum(json.loads(x)["cost"] for x in style_path.read_text().splitlines()) if (
            style_path.exists()) else 0.0  # fmt: skip
        gw = Gateway(load_config(), JsonlLog(train.OUT / "llm_log.jsonl"), "runtime")
        baselines.run_style(style[: int(rest[0])] if rest else style, style_path, gw)
        lines = [json.loads(x) for x in style_path.read_text().splitlines()]
        for m in baselines.MODELS:
            mine = [r for r in lines if r["model"] == m]
            print(f"{m}: 호출 {len(mine)}건 누적 ${sum(r['cost'] for r in mine):.4f}")
        print(f"이번 실행 ${sum(r['cost'] for r in lines) - before:.4f}")
    app = baselines.from_app(ROOT / "data" / "app.sqlite",
                             {r["call_uid"] for r in rows if r["set"] not in data.STYLES})  # fmt: skip
    got = baselines.latest(style_path)
    out = []
    for r in rows:
        for m in baselines.MODELS:
            labels = got.get((r["id"], m)) if r["set"] in data.STYLES else app[m][r["call_uid"]]
            out.append({"id": r["id"], "set": r["set"], "model": m, "labels": labels})
    path = train.OUT / "baseline.jsonl"
    path.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in out))
    for m in baselines.MODELS:
        miss = {s: sum(x["labels"] is None for x in out if x["model"] == m and x["set"] == s)
                for s in data.SETS}  # fmt: skip
        print(f"{path.name} {m} 라벨 없음(오답): {miss}")


def _population() -> Counter:
    """역확률 가중의 모집단 — 생성 통화 전체의 골드 목적 건수(evaluation/classification.py와 같다, 평가 설계 §2.2)"""
    conn = sqlite3.connect(f"file:{ROOT}/data/source.sqlite?mode=ro", uri=True)
    conn.execute(f"ATTACH 'file:{ROOT}/ground_truth/truth.sqlite?mode=ro' AS truth")
    rows = conn.execute(
        "SELECT l.primary_purpose, COUNT(*) FROM ax_calls c JOIN truth.truth_call_labels l"
        " USING (call_uid) WHERE c.source = 'generated' AND l.split != 'hard' GROUP BY 1"
    ).fetchall()
    conn.close()
    return Counter(dict(rows))


def _score() -> None:
    from evaluation.finetune.qwen import score, train

    rows = [json.loads(line) for line in (data.DATA / "eval.jsonl").read_text().splitlines()]
    preds = {}
    for name in ("half", "full"):
        path = train.OUT / f"pred_{name}.jsonl"
        if path.exists():
            preds[f"qwen_{name}"] = {
                r["id"]: r["labels"] for r in map(json.loads, path.read_text().splitlines())
            }
    for r in map(json.loads, (train.OUT / "baseline.jsonl").read_text().splitlines()):
        preds.setdefault(r["model"], {})[r["id"]] = r["labels"]
    score.check_coverage(preds, rows)
    population = _population()
    results = {
        m: {
            s: score.score_set(
                p, [r for r in rows if r["set"] == s], None if s == "validation" else population
            )
            for s in data.SETS
        }
        for m, p in preds.items()
    }
    crit = {
        m: score.criteria(results[m], results["haiku_v2"]["validation"])
        for m in results
        if m.startswith("qwen_")
    }
    text = score.report(results, crit)
    (train.OUT / "report.md").write_text(text)
    print(text)


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
        opts = dict(a.split("=", 1) for a in rest if "=" in a)
        epochs = int(opts.pop("epochs", 1))
        overrides = {k: train.parse_override(v) for k, v in opts.items()}
        train.train(name, epochs, "continue" in rest, overrides, resume="resume" in rest)
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
    elif cmd == "baselines":
        _baselines(args)
    elif cmd == "score":
        _score()
    elif cmd == "gateway-check":
        from evaluation.finetune.qwen import gateway_check

        sys.exit(gateway_check.main(*map(int, args)))
    else:
        raise SystemExit(
            "명령: data | lengths | train | status | predict | baselines | score | gateway-check"
        )


if __name__ == "__main__":
    main(sys.argv[1:])
