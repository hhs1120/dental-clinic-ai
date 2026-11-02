"""튜닝 비교 — dev 검색 문항 + ground_truth/tune_qa.json으로 임베딩·리랭커를 비교한다(AI 실험 기록). 조건마다 반복해
순위가 흔들리는 문항과 질문당 시간도 잰다. test 문항은 읽지 않는다. 결과는 reports/tune_<시각>.md(커밋 안 함).
  uv run python -m evaluation.tune --repeat 3 --condition embedding=voyage-4,reranker=haiku --condition …
"""

import argparse
import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from evaluation import EvalRefused, rag
from evaluation.eval import REPORTS, load_app

ROOT = Path(__file__).resolve().parents[1]
TUNE_QA = ROOT / "ground_truth" / "tune_qa.json"
RETRIEVAL_ONLY = ("retrieval",)


def questions(conn, path: Path = TUNE_QA) -> list[dict]:
    extra = json.loads(path.read_text(encoding="utf-8"))
    wrong = [q["id"] for q in extra if q["split"] != "tune"]
    if wrong:
        raise EvalRefused(f"튜닝 파일에 다른 분할 문항: {', '.join(wrong)}")
    dev = [q for q in rag._questions(conn, "dev") if q["type"] in rag.BUCKET]
    return dev + extra


def measure(conn, app, conditions: list[str], repeat: int, qs: list[dict]) -> dict:
    out = {}
    for condition in conditions:
        runs, ranks, seconds = [], {}, 0.0
        for _ in range(repeat):
            start = time.perf_counter()
            r = rag.evaluate(conn, app, "tune", condition, RETRIEVAL_ONLY, questions=qs)
            seconds += time.perf_counter() - start
            runs.append(r)
            for q in r["questions"]:
                ranks.setdefault(q["id"], set()).add(q["rank"])
        out[condition] = {
            "runs": runs,
            "unstable": sorted(i for i, seen in ranks.items() if len(seen) > 1),
            "seconds_per_question": seconds / (repeat * len(qs)),
        }
    return out


def compare_all(measured: dict, conditions: list[str]) -> dict:
    """첫 조건을 기준으로 각 조건의 첫 실행을 짝 비교"""
    base = measured[conditions[0]]["runs"][0]["questions"]
    return {c: rag.compare(base, measured[c]["runs"][0]["questions"]) for c in conditions[1:]}


def _rate(d: dict) -> str:
    return f"{d['k']}/{d['n']}"


def render(measured: dict, conditions: list[str], n: int) -> str:
    lines = [f"# 튜닝 비교 — 문항 {n}개", ""]
    for c in conditions:
        m = measured[c]
        lines += [f"## {c}", ""]
        lines.append("| 실행 | Hit@5 | MRR | Top-1 | 최신성 | 차트 Hit@5 | 차트 MRR |")
        lines.append("|---|---|---|---|---|---|---|")
        for i, r in enumerate(m["runs"], 1):
            s, ch = r["retrieval"], r["chart"]
            lines.append(
                f"| {i} | {_rate(s['hit5'])} | {s['mrr']:.3f} | {_rate(s['top1'])}"
                f" | {_rate(s['fresh'])} | {_rate(ch['hit5'])} | {ch['mrr']:.3f} |"
            )
        lines += [
            "",
            f"질문당 {m['seconds_per_question']:.2f}초 · 순위가 흔들린 문항 {len(m['unstable'])}개"
            f" {', '.join(m['unstable'])}",
            "",
        ]
    for c, r in compare_all(measured, conditions).items():
        lines += [f"## {conditions[0]} → {c}", ""]
        for name, v in r["metrics"].items():
            lines.append(f"- {name}: {json.dumps(v, ensure_ascii=False, default=str)}")
        lines += [f"- 구제 {r['rescued']}", f"- 손실 {r['lost']}", ""]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--condition", action="append", required=True)
    parser.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args(argv)
    data = ROOT / "data"
    conn = sqlite3.connect(f"file:{data / 'app.sqlite'}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("ATTACH DATABASE ? AS source", (f"file:{data / 'source.sqlite'}?mode=ro",))
    conn.execute(
        "ATTACH DATABASE ? AS truth", (f"file:{ROOT / 'ground_truth' / 'truth.sqlite'}?mode=ro",)
    )
    app = load_app(data / "app.sqlite", data / "source.sqlite")
    qs = questions(conn)
    measured = measure(conn, app, args.condition, args.repeat, qs)
    text = render(measured, args.condition, len(qs))
    path = REPORTS / f"tune_{datetime.now():%Y%m%d-%H%M%S}.md"
    path.write_text(text, encoding="utf-8")
    print(text)
    print(f"리포트: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
