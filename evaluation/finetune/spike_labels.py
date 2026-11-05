"""Solar 맛보기 — 분류기 라벨 자리 모델을 실험 기록 7의 v1 라벨 프롬프트 원문으로 dev·silver에 돌려 Haiku v1과
비교한다(분류기 설계 "모델 배치"). test는 SQL에서 뺀다. 결과·호출 기록은 evaluation/finetune/spike/.
uv run --env-file .env python evaluation/finetune/spike_labels.py run <model> <dev|silver> [limit]
uv run python evaluation/finetune/spike_labels.py score <model> <dev|silver>
"""

import json
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from evaluation.finetune.aug.augment import ITEMS, SCHEMA  # noqa: E402
from generator.master import CALL_FLAGS  # noqa: E402
from shared.llm import Gateway, JsonlLog, load_config  # noqa: E402

HERE = Path(__file__).parent
OUT = HERE / "spike"
PROMPT = HERE / "aug" / "classify_v1.md"


def load_calls(split: str) -> list[dict]:
    if split not in ("dev", "silver"):
        raise SystemExit("split은 dev 또는 silver")
    conn = sqlite3.connect(f"file:{ROOT}/data/source.sqlite?mode=ro", uri=True)
    conn.execute(f"ATTACH 'file:{ROOT}/ground_truth/truth.sqlite?mode=ro' AS truth")
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT c.call_uid, c.direction, c.transcript, l.* FROM ax_calls c"
        " JOIN truth.truth_call_labels l USING (call_uid)"
        " WHERE c.source = 'generated' AND l.split = ? ORDER BY c.call_uid",
        (split,),
    ).fetchall()
    return [dict(r) for r in rows]


def run(model: str, split: str, limit=None) -> None:
    config = load_config()
    config["aliases"]["spike"] = [model]
    OUT.mkdir(exist_ok=True)
    gw = Gateway(config, JsonlLog(OUT / "llm_log.jsonl"), "runtime")
    system = PROMPT.read_text()
    out_path = OUT / f"{model}_{split}.jsonl"
    done = set()
    if out_path.exists():
        done = {r["call_uid"] for r in map(json.loads, out_path.read_text().splitlines())}
    calls = [c for c in load_calls(split) if c["call_uid"] not in done]
    calls = calls[: int(limit) if limit else None]
    print(f"남은 {len(calls)}건", flush=True)

    def one(c):
        r = gw.complete(
            "spike",
            task="spike-labels",
            system=system,
            schema=SCHEMA,
            max_tokens=1000,
            messages=[{"role": "user", "content": f"방향: {c['direction']}\n\n{c['transcript']}"}],
        )
        return {
            "call_uid": c["call_uid"],
            "ok": r.ok,
            "labels": r.data if r.ok else None,
            "model": r.model,
            "cost": r.cost_usd,
            "in": r.input_tokens,
            "out": r.output_tokens,
            "error": r.error,
        }

    with ThreadPoolExecutor(4) as ex, out_path.open("a") as f:
        for i, row in enumerate(ex.map(one, calls)):
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            if i % 200 == 0:
                print(i, flush=True)
    score(model, split)


def score(model: str, split: str) -> dict:
    """항목별 k/n, 목적 Strict, 플래그 6개 평균 — 실패 건은 오답으로 센다"""
    gold = {c["call_uid"]: c for c in load_calls(split)}
    path = OUT / f"{model}_{split}.jsonl"
    if not path.exists():
        path = HERE / "aug" / f"v1_{model}_{split}.jsonl"  # 실험 기록 7의 Haiku v1 결과
    rows = {r["call_uid"]: r for r in map(json.loads, path.read_text().splitlines())}
    n = len(gold)
    hits = {
        i: sum(
            1
            for uid, g in gold.items()
            if (rows.get(uid) or {}).get("ok") and rows[uid]["labels"][i] == g[i]
        )
        for i in ITEMS
    }
    flags = sum(hits[f] for f in CALL_FLAGS) / (len(CALL_FLAGS) * n)
    failed = sum(1 for uid in gold if not (rows.get(uid) or {}).get("ok"))
    print(f"{model} {split}: {n}건, 실패 {failed}")
    for i in ITEMS:
        print(f"  {i}: {hits[i]}/{n}")
    print(f"  목적 Strict {hits['primary_purpose'] / n:.4f}, 플래그 평균 {flags:.4f}")
    cost = sum(r.get("cost") or 0 for r in rows.values() if r["call_uid"] in gold)
    print(f"  비용 ${cost:.3f}")
    return {"purpose": hits["primary_purpose"] / n, "flags": flags, "hits": hits, "n": n}


if __name__ == "__main__":
    cmd, *args = sys.argv[1:]
    {"run": run, "score": score}[cmd](*args)
