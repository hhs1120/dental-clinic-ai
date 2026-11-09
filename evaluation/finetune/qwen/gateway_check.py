"""Qwen 판정기 게이트웨이 확인 (Qwen 판정기 설계 '판정기 v3' 확인). 같은 통화에서
(가) 지금 어댑터로 낸 실험 경로(infer.py) 출력이 out/pred_full.jsonl과 같은지 — 어댑터 확인,
(나) 게이트웨이 v3 판정(backend.classify.judge)의 출력·라벨이 (가)와 같은지 — 앱 경로 확인.
app.sqlite는 열지 않는다(호출 기록은 임시 JsonlLog). MLX는 함수 안에서만 import한다.
"""

import json
import tempfile
from collections.abc import Callable
from pathlib import Path

from evaluation.finetune.qwen import data

N_DEFAULT = 30  # validation 앞 절반 + dev 앞 절반


def compare(
    rows: list[dict],
    ref: dict[str, str],
    infer_gen: Callable[[dict], str],
    gateway_judge: Callable[[dict], tuple[str, dict | None]],
) -> list[dict]:
    """다른 건 목록 — adapter: 실험 경로 출력이 pred_full과 다름, gateway: 게이트웨이 출력·라벨이 실험 경로와 다름"""
    out = []
    for row in rows:
        text = infer_gen(row)
        if text != ref[row["id"]]:
            out.append({"id": row["id"], "kind": "adapter", "ref": ref[row["id"]], "infer": text})
            continue
        g_text, g_labels = gateway_judge(row)
        if g_text != text or g_labels != data.parse_labels(text):
            out.append({"id": row["id"], "kind": "gateway", "infer": text, "gateway": g_text})
    return out


def main(n: int = N_DEFAULT) -> int:
    from backend import classify as cl
    from evaluation.finetune.qwen import infer, train
    from shared.llm import Gateway, JsonlLog, load_config

    path = data.DATA / "eval.jsonl"
    if not path.exists():
        raise SystemExit(f"{path} 없음 — uv run python -m evaluation.finetune.qwen data 먼저")
    rows = [json.loads(x) for x in path.read_text().splitlines()]
    rows = [r for r in rows if r["set"] == "validation"][: n // 2] + [
        r for r in rows if r["set"] == "dev"
    ][: n - n // 2]
    ref = {
        r["id"]: r["text"]
        for r in map(json.loads, (train.OUT / "pred_full.jsonl").read_text().splitlines())
    }
    gen = infer.mlx_generate(train.config()["model"], train.ADAPTERS / "full")
    log = JsonlLog(Path(tempfile.mkdtemp()) / "llm.jsonl")
    gw = Gateway(load_config(), log, "runtime")

    def judge(row):
        j = cl.judge(gw, row["transcript"], row["direction"], "v3")
        return j.call.text, j.labels

    diffs = compare(rows, ref, gen, judge)
    print(f"{len(rows)}건 중 같음 {len(rows) - len(diffs)}, 다름 {len(diffs)}")
    for d in diffs:
        print(json.dumps(d, ensure_ascii=False))
    return 1 if diffs else 0
