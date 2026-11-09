"""① 분류 프롬프트 관문 — 기준·후보 버전을 silver 검증 구간에서 각각 GATE_RUNS번 분류하고 통화·항목마다 다수결로
같은 통화 짝 비교(McNemar)를 해 합격/불합격을 낸다. 같은 프롬프트도 돌릴 때마다 답이 흔들리므로 한 번씩 비교하지 않는다.
원본: 평가 설계 §2.5(급함은 urgent 여부)·§2.7, 설계 docs/superpowers/specs/2026-11-09-prompt-gate-design.md."""

import argparse
import hashlib
import json
import shutil
import sqlite3
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from backend import classify as cl
from backend import prompts
from evaluation.classification import ITEMS
from evaluation.metrics import (
    GATE_ATTEMPTS,
    GATE_FALSE_ALARM_CAP,
    GATE_P,
    GATE_REGRESSION_CAP,
    GATE_RUNS,
    mcnemar_exact,
)
from evaluation.splits import silver_split
from shared.llm import Gateway, JsonlLog, load_config

SAFETY = ("emergency", "complaint")  # 새로 놓침·오탐 증가를 따로 보는 항목
RUNS_DIR = Path("evaluation/runs/prompt-gate")  # 버전별 건별 실행 — 골드와 엮여 커밋하지 않는다
TRIES = 3  # 통화 하나를 실패(무효 라벨·폴백 답)로 다시 부르는 상한
REPORT = Path("reports/prompt_gate.jsonl")  # 판정 1회 = 1행, 집계만(통화 id 없음) — 커밋
GATE_VALUES = {
    "runs": GATE_RUNS,
    "p": GATE_P,
    "false_alarm_cap": GATE_FALSE_ALARM_CAP,
    "regression_cap": GATE_REGRESSION_CAP,
    "attempts": GATE_ATTEMPTS,
}


def value(labels: dict, item: str):
    """비교할 값 — 급함은 urgent 여부(평가 설계 §2.5), 플래그는 0/1, 나머지는 그대로. 모델 라벨·골드 행 공통"""
    v = labels[item]
    if item == "urgency":
        return int(v == "urgent")
    return int(v) if isinstance(v, int) else v  # bool은 int의 하위형


def majority(runs: list[dict[str, dict]]) -> dict[str, dict]:
    """통화 → 항목 → 다수결 값. 2벌 이상 같으면 그 값, 모두 다르면 첫 실행 값(결정적이게)"""
    out = {}
    for uid in runs[0]:
        out[uid] = {}
        for i in ITEMS:
            vals = [value(r[uid], i) for r in runs]
            top, n = Counter(vals).most_common(1)[0]
            out[uid][i] = top if n >= 2 else vals[0]
    return out


def compare(base: dict, cand: dict, gold: dict) -> dict[str, dict]:
    """항목 → 오답 수·b(기준 틀림·후보 맞음)·c(기준 맞음·후보 틀림)·p. 응급·불만은 새로 놓침·오탐 증가도"""
    out = {}
    for i in ITEMS:
        ok_b = {u: base[u][i] == value(g, i) for u, g in gold.items()}
        ok_c = {u: cand[u][i] == value(g, i) for u, g in gold.items()}
        b = sum(ok_c[u] and not ok_b[u] for u in gold)
        c = sum(ok_b[u] and not ok_c[u] for u in gold)
        s = {"base_wrong": sum(not v for v in ok_b.values()),
             "cand_wrong": sum(not v for v in ok_c.values()), "b": b, "c": c, "p": mcnemar_exact(b, c)}  # fmt: skip
        if i in SAFETY:
            pos = [u for u, g in gold.items() if value(g, i)]
            neg = [u for u, g in gold.items() if not value(g, i)]
            s["new_miss"] = sum(base[u][i] and not cand[u][i] for u in pos)
            s["false_alarm_delta"] = sum(cand[u][i] for u in neg) - sum(base[u][i] for u in neg)
        out[i] = s
    return out


def decide(stats: dict, item: str) -> list[str]:
    """못 넘은 조건 이름(빈 목록 = 합격) — 설계 "합격 조건" """
    failed = []
    t = stats[item]
    if not (t["b"] > t["c"] and t["p"] < GATE_P):
        failed.append("target_up")
    if any(stats[i]["new_miss"] for i in SAFETY):
        failed.append("no_new_miss")
    if any(stats[i]["false_alarm_delta"] > GATE_FALSE_ALARM_CAP for i in SAFETY):
        failed.append("false_alarm_cap")
    if any(
        (s["c"] > s["b"] and s["p"] < GATE_P) or s["c"] - s["b"] > GATE_REGRESSION_CAP
        for i, s in stats.items()
        if i != item
    ):
        failed.append("no_regression")
    return failed


def population(gold_rows: list[dict]) -> dict[str, dict]:
    """silver 검증 구간 통화 → 골드 행. dev·test 행이 섞여 들어오면 멈춘다(평가 설계 §2.7 금지)"""
    if any(r["split"] != "silver" for r in gold_rows):
        raise SystemExit("holdout_in_population")
    part = silver_split(gold_rows)
    return {r["call_uid"]: r for r in gold_rows if part[r["call_uid"]] == "validation"}


def fingerprint(version: str, config: dict, root: Path = prompts.PROMPTS_DIR) -> dict:
    """저장된 실행이 유효한지 가르는 키 — 라벨 프롬프트 해시와 그 버전을 부를 첫 모델"""
    text = (root / "classify" / version / "labels.md").read_bytes()
    model = config["aliases"]["classify"][0]  # classify 버전 파일에는 머리말 model이 없다
    return {"prompt_sha256": hashlib.sha256(text).hexdigest(), "model": model}


def _read(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    return {r["call_uid"]: r["labels"] for r in map(json.loads, path.read_text().splitlines())}


def load_runs(version: str, fp: dict, root: Path = RUNS_DIR) -> list[dict[str, dict]]:
    """저장된 GATE_RUNS벌 — 키가 지금과 다르면 폴더를 비우고 빈 벌을 돌려준다"""
    d = root / version
    meta = d / "meta.json"
    if meta.exists() and json.loads(meta.read_text()) != fp:
        shutil.rmtree(d)
    return [_read(d / f"run{k}.jsonl") for k in range(1, GATE_RUNS + 1)]


def _label(gw, version: str, fp: dict, transcript: str, direction: str) -> dict | None:
    """유효한 첫 모델 답이 나올 때까지 TRIES번 — 폴백 답은 집계에서 빼므로 실패로 본다(평가 설계 §1 원칙 5)"""
    for _ in range(TRIES):
        r = gw.complete(
            "classify",
            task="classify",
            max_tokens=cl.MAX_TOKENS,
            prompt_version=version,
            **cl.label_request(transcript, direction, version),
        )
        if r.ok and cl.valid_labels(r.data) and not r.fallback_used and r.model == fp["model"]:
            return r.data
    return None


def fill_runs(
    gw, version: str, calls: dict[str, tuple[str, str]], fp: dict, root: Path = RUNS_DIR,
    workers: int = 8,
) -> list[dict[str, dict]]:  # fmt: skip
    """빠진 통화만 불러 줄마다 덧붙인다(도중에 멈춰도 끝난 통화는 남는다). 다 채우지 못하면 판정하지 않고 멈춘다"""
    runs = load_runs(version, fp, root)
    d = root / version
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(json.dumps(fp))
    empty = 0
    for k, run in enumerate(runs, 1):
        todo = sorted(u for u in calls if u not in run)
        with ThreadPoolExecutor(workers) as ex, (d / f"run{k}.jsonl").open("a") as f:
            for uid, labels in zip(
                todo, ex.map(lambda u: _label(gw, version, fp, *calls[u]), todo), strict=True
            ):
                if labels is None:
                    empty += 1
                    continue
                run[uid] = labels
                f.write(json.dumps({"call_uid": uid, "labels": labels}, ensure_ascii=False) + "\n")
        if empty:  # 다음 벌로 넘어가 비용을 더 쓰지 않는다
            raise SystemExit(f"빈 통화 {empty}건 — 판정하지 않음")
    return [{u: r[u] for u in calls} for r in runs]


def gate_hash() -> str:
    """관문 값의 지문 — 사람이 값을 고치면 바뀌어 시도 수가 다시 0부터(평가 설계 §2.7)"""
    return hashlib.sha256(json.dumps(GATE_VALUES, sort_keys=True).encode()).hexdigest()[:12]


def attempts(base: str, report: Path = REPORT) -> set[str]:
    """같은 기준·같은 관문 값으로 판정한 서로 다른 후보 — 같은 버전끼리의 점검은 세지 않는다"""
    if not report.exists():
        return set()
    rows = map(json.loads, report.read_text().splitlines())
    return {
        r["candidate"]
        for r in rows
        if r["base"] == base and r["candidate"] != base and r["gate_hash"] == gate_hash()
    }


class _Spent:
    """게이트웨이 호출 비용을 더한다 — 기록 행의 cost_usd"""

    def __init__(self, gw):
        self.gw, self.cost = gw, 0.0

    def complete(self, alias, **kwargs):
        r = self.gw.complete(alias, **kwargs)
        self.cost += r.cost_usd
        return r


def run_gate(
    gw, a: str, b: str, item: str, *, gold_rows: list[dict], calls: dict, policy: dict,
    config: dict, runs_root: Path = RUNS_DIR, report: Path = REPORT, workers: int = 8,
) -> dict:  # fmt: skip
    """판정 1회 — 멈출 조건은 부르기 전에 본다. 합격해도 아무것도 바꾸지 않는다(전환은 사람이 앱 설계 §7.2로)"""
    if item not in ITEMS:
        raise SystemExit(f"목표 항목은 {', '.join(ITEMS)} 중 하나")
    if any(
        policy.values()
    ):  # 원칙 9 자동 처리 조건은 자동 개선 때 — 그 전에는 관문을 돌리지 않는다
        raise SystemExit("auto_policy_on")
    tried = attempts(a, report)
    if b != a and b not in tried and len(tried) >= GATE_ATTEMPTS:
        raise SystemExit("attempt_cap")
    gold = population(gold_rows)
    spent = _Spent(gw)
    fp = {v: fingerprint(v, config) for v in (a, b)}
    picked = {u: calls[u] for u in gold}
    base, cand = (majority(fill_runs(spent, v, picked, fp[v], runs_root, workers)) for v in (a, b))
    stats = compare(base, cand, gold)
    failed = decide(stats, item)
    row = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "base": a,
        "candidate": b,
        "item": item,
        "gate": GATE_VALUES,
        "gate_hash": gate_hash(),
        "model": fp[b]["model"],
        "n": len(gold),
        "items": stats,
        "failed": failed,
        "passed": not failed,
        "cost_usd": round(spent.cost, 4),
    }
    report.parent.mkdir(parents=True, exist_ok=True)
    with report.open("a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def make_gateway(log: Path):
    return Gateway(load_config(), JsonlLog(log), "runtime")


def _ro(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="python -m evaluation.prompt_gate")
    p.add_argument("--a", required=True, help="기준 버전")
    p.add_argument("--b", required=True, help="후보 버전")
    p.add_argument("--item", required=True, help="목표 항목")
    p.add_argument("--truth", default="ground_truth/truth.sqlite")
    p.add_argument("--source", default="data/source.sqlite")
    p.add_argument("--app", default="data/app.sqlite")
    p.add_argument("--report", default=str(REPORT))
    p.add_argument("--runs", default=str(RUNS_DIR))
    p.add_argument("--workers", type=int, default=8)
    args = p.parse_args(argv)
    gold_rows = [dict(r) for r in _ro(args.truth).execute(
        "SELECT * FROM truth_call_labels WHERE split = 'silver'")]  # fmt: skip
    calls = {r["call_uid"]: (r["transcript"], r["direction"]) for r in _ro(args.source).execute(
        "SELECT call_uid, transcript, direction FROM ax_calls WHERE transcript IS NOT NULL")}  # fmt: skip
    got = (
        _ro(args.app).execute("SELECT value FROM ax_settings WHERE key = 'auto_policy'").fetchone()
    )
    runs = Path(args.runs)
    row = run_gate(
        make_gateway(runs / "llm.jsonl"), args.a, args.b, args.item, gold_rows=gold_rows,
        calls=calls, policy=json.loads(got[0]) if got else {}, config=load_config(),
        runs_root=runs, report=Path(args.report), workers=args.workers,
    )  # fmt: skip
    print(f"{args.a} → {args.b} · 목표 {args.item} · 검증 {row['n']}건 · ${row['cost_usd']}")
    print(f"{'항목':16} {'기준 오답':>8} {'후보 오답':>8} {'b':>4} {'c':>4} {'p':>8}")
    for i, s in row["items"].items():
        print(
            f"{i:16} {s['base_wrong']:>8} {s['cand_wrong']:>8} {s['b']:>4} {s['c']:>4} {s['p']:>8.4f}"
        )
    for cond in ("target_up", "no_new_miss", "false_alarm_cap", "no_regression"):
        print(f"{cond:16} {'불통과' if cond in row['failed'] else '통과'}")
    print("합격" if row["passed"] else "불합격")


if __name__ == "__main__":
    main()
