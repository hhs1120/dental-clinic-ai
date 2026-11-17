"""① 분류 프롬프트 관문 — 기준·후보 버전을 silver 검증 구간에서 각각 GATE_RUNS번 분류하고 통화·항목마다 다수결로
같은 통화 짝 비교(McNemar)를 해 합격/불합격을 낸다. 같은 프롬프트도 돌릴 때마다 답이 흔들리므로 한 번씩 비교하지 않는다.
원본: 평가 설계 §2.5(급함은 urgent 여부)·§2.7, 설계 docs/superpowers/specs/2026-11-09-prompt-gate-design.md."""

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from backend import classify as cl
from backend import prompts
from backend.autoclose import autoclose_reason
from backend.calls import active_versions
from evaluation.classification import ITEMS
from evaluation.metrics import (
    GATE_ATTEMPTS,
    GATE_AUTOCLOSE_ERROR_CAP,
    GATE_FALSE_ALARM_CAP,
    GATE_P,
    GATE_REGRESSION_CAP,
    GATE_RUNS,
    mcnemar_exact,
    wilson,
)
from evaluation.splits import silver_split
from shared.llm import Gateway, JsonlLog, load_config

SAFETY = ("emergency", "complaint")  # 새로 놓침·오탐 증가를 따로 보는 항목
RUNS_DIR = Path("evaluation/runs/prompt-gate")  # 버전별 건별 실행 — 골드와 엮여 커밋하지 않는다
TRIES = 3  # 통화 하나를 실패(무효 라벨·폴백 답)로 다시 부르는 상한
REPORT = Path("reports/prompt_gate.jsonl")  # 판정 1회 = 1행, 집계만(통화 id 없음) — 커밋
IMPROVE_REPORT = Path(
    "reports/improve_runs.jsonl"
)  # 자동 개선 후보 1개 = 1행(evaluation/improve.py)
GATE_VALUES = {
    "runs": GATE_RUNS,
    "p": GATE_P,
    "false_alarm_cap": GATE_FALSE_ALARM_CAP,
    "regression_cap": GATE_REGRESSION_CAP,
    "attempts": GATE_ATTEMPTS,
    "autoclose_error_cap": GATE_AUTOCLOSE_ERROR_CAP,
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


def majority_labels(runs: list[dict[str, dict]]) -> dict[str, dict]:
    """majority와 같은 다수결이되 값은 원래 라벨(급함 3값 등) — 자동 종결 조건(autoclose_reason)에 넣는다"""
    out = {}
    for uid in runs[0]:
        out[uid] = {}
        for i in ITEMS:
            vals = [value(r[uid], i) for r in runs]
            top, n = Counter(vals).most_common(1)[0]
            pick = top if n >= 2 else vals[0]
            out[uid][i] = next(r[uid][i] for r in runs if value(r[uid], i) == pick)
    return out


def auto_conditions(
    cand: dict[str, dict], gold: dict[str, dict], policy: dict, *,
    base_todo: dict[str, tuple[list, bool]], judge: dict[str, dict], trust: dict | None,
) -> list[str]:  # fmt: skip
    """켜진 자동 처리 조건(평가 설계 §1 원칙 9·§2.7, 자동 개선 설계 §5) — 못 넘은 조건 이름.
    ① 자동 종결: 후보 다수결 라벨 + 활성 판정기로 다시 닫아 보고, 닫힌 통화 중 골드가 할 일·응급·불만·urgent인
    비율의 Wilson 상한이 GATE_AUTOCLOSE_ERROR_CAP 이하. 할 일 목록·요약 폴백은 기준 버전 것을 빌린다(요약
    프롬프트가 같다). 켜진 정책이 있으면 후보의 dev 입력 신뢰 경계 위반 0건(결과가 없으면 미측정 탈락)"""
    if not any(policy.values()):
        return []
    failed = []
    if policy.get("call_autoclose"):
        wrong = closed = 0
        for u, g in gold.items():
            todo, summary_fb = base_todo.get(u, ([], True))
            cls = {"labels": cand[u], "ok": True, "fallback": summary_fb, "todo": todo}
            if autoclose_reason(cls, judge.get(u)):
                closed += 1
                wrong += bool(g["has_todo"] or g["emergency"] or g["complaint"]
                              or g["urgency"] == "urgent")  # fmt: skip
        ci = wilson(wrong, closed)
        if ci and ci[1] > GATE_AUTOCLOSE_ERROR_CAP:
            failed.append("autoclose_error")
    if trust is None:
        failed.append("trust_unmeasured")
    elif trust["violations"]:
        failed.append("trust_violation")
    return failed


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
    # 오답 증가 상한 이하면 유의할 수 없다(c−b ≤ 3이면 p ≥ 0.25) — 상한 하나로 본다
    if any(s["c"] - s["b"] > GATE_REGRESSION_CAP for i, s in stats.items() if i != item):
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


def attempts(
    base: str, report: Path = REPORT, improve_report: Path = IMPROVE_REPORT
) -> set[tuple[str, str | None]]:
    """같은 기준·같은 관문 값으로 판정한 서로 다른 (후보, 프롬프트 해시) — 이름이 같아도 내용을 고치면
    새 시도다. 같은 버전끼리의 점검은 세지 않는다. 자동 개선에서 관문 전 검사로 탈락한 후보(gate 없음)도
    센다(자동 개선 설계 §3)"""
    rows = [
        r for path in (report, improve_report) if path.exists()
        for r in map(json.loads, path.read_text().splitlines())
        if path == report or r.get("gate") is None
    ]  # fmt: skip
    return {
        (r["candidate"], r.get("candidate_sha256"))
        for r in rows
        if r["base"] == base and r["candidate"] != base and r["gate_hash"] == gate_hash()
    }


class _Spent:
    """게이트웨이 호출 비용을 더한다 — 기록 행의 cost_usd"""

    def __init__(self, gw):
        self.gw, self.cost, self.lock = gw, 0.0, threading.Lock()

    def complete(self, alias, **kwargs):
        r = self.gw.complete(alias, **kwargs)
        with self.lock:  # fill_runs가 여러 스레드로 부른다
            self.cost += r.cost_usd
        return r


def run_gate(
    gw, a: str, b: str, item: str, *, gold_rows: list[dict], calls: dict, policy: dict,
    config: dict, runs_root: Path = RUNS_DIR, report: Path = REPORT, workers: int = 8,
    auto: dict | None = None, improve_report: Path = IMPROVE_REPORT,
) -> dict:  # fmt: skip
    """판정 1회 — 멈출 조건은 부르기 전에 본다. 합격해도 아무것도 바꾸지 않는다(전환은 사람이 앱 설계 §7.2로).
    자동 처리가 켜져 있으면 auto(auto_inputs 결과 — 기준 할 일·판정기·후보 신뢰 경계)로 조건을 더한다"""
    if item not in ITEMS:
        raise SystemExit(f"목표 항목은 {', '.join(ITEMS)} 중 하나")
    if any(policy.values()) and auto is None:
        raise SystemExit("auto_inputs_missing")
    if not all(re.fullmatch(r"[\w-]+", v) for v in (a, b)):  # 실행 폴더 밖을 지우지 않게
        raise SystemExit("버전 이름은 글자·숫자·-·_만")
    fp = {v: fingerprint(v, config) for v in (a, b)}
    tried = attempts(a, report, improve_report)
    if b != a and (b, fp[b]["prompt_sha256"]) not in tried and len(tried) >= GATE_ATTEMPTS:
        raise SystemExit("attempt_cap")
    gold = population(gold_rows)
    spent = _Spent(gw)
    picked = {u: calls[u] for u in gold}
    base_runs, cand_runs = (fill_runs(spent, v, picked, fp[v], runs_root, workers) for v in (a, b))
    stats = compare(majority(base_runs), majority(cand_runs), gold)
    failed = decide(stats, item)
    if any(policy.values()):
        failed += auto_conditions(majority_labels(cand_runs), gold, policy, **auto)
    row = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "base": a,
        "candidate": b,
        "candidate_sha256": fp[b]["prompt_sha256"],
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


def auto_inputs(app: str, source: str, truth: str, base: str, cand: str) -> dict:
    """켜진 자동 처리 조건의 입력(무료) — 기준 버전에 저장된 할 일 목록·요약 폴백, 활성 판정기 결과, 후보 버전의
    dev 입력 신뢰 경계 집계(저장된 실행이 없으면 None — 측정은 사람이 make trust-eval)"""
    from evaluation import input_trust
    from evaluation.autoclose import _arg
    from evaluation.classification import _judgments

    conn = _ro(app)
    conn.row_factory = sqlite3.Row
    for name, path in (("source", source), ("truth", truth)):
        conn.execute(f"ATTACH DATABASE ? AS {name}", (f"{Path(path).resolve().as_uri()}?mode=ro",))
    base_todo = {
        u: ((json.loads(t) if t else []), bool(fb))
        for u, t, fb in conn.execute(
            "SELECT k.call_uid, k.todo, COALESCE(s.fallback_used, 0) FROM ax_call_classifications k"
            " LEFT JOIN ax_llm_calls s ON s.id = k.summary_llm_call_id"
            " WHERE k.model_alias = 'classify' AND k.prompt_version = ? AND k.status = 'ok'",
            (base,),
        )
    }
    _, judge_v = active_versions(conn)
    judge = {r["call_uid"]: _arg(r) for r in _judgments(conn) if r["prompt_version"] == judge_v}
    rows = [r for r in input_trust.read_runs(input_trust.PATHS["dev"]["runs"])
            if r["model"] == "classify" and r["version"] == cand]  # fmt: skip
    runs = input_trust.judge_runs(judge_v)
    trust = None
    if rows and runs is not None:
        got = input_trust.score(input_trust.targets(conn, "dev"), rows, cand, judge_v, runs)
        trust = None if got["incomplete"] else got
    return {"base_todo": base_todo, "judge": judge, "trust": trust}


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
    policy = json.loads(got[0]) if got else {}
    auto = (
        auto_inputs(args.app, args.source, args.truth, args.a, args.b)
        if any(policy.values())
        else None
    )
    row = run_gate(
        make_gateway(runs / "llm.jsonl"), args.a, args.b, args.item, gold_rows=gold_rows,
        calls=calls, policy=policy, config=load_config(),
        runs_root=runs, report=Path(args.report), workers=args.workers, auto=auto,
    )  # fmt: skip
    print(f"{args.a} → {args.b} · 목표 {args.item} · 검증 {row['n']}건 · ${row['cost_usd']}")
    print(f"{'항목':16} {'기준 오답':>8} {'후보 오답':>8} {'b':>4} {'c':>4} {'p':>8}")
    for i, s in row["items"].items():
        print(
            f"{i:16} {s['base_wrong']:>8} {s['cand_wrong']:>8} {s['b']:>4} {s['c']:>4} {s['p']:>8.4f}"
        )
    conds = ["target_up", "no_new_miss", "false_alarm_cap", "no_regression"]
    if auto is not None:
        conds += ["autoclose_error", "trust_unmeasured", "trust_violation"]
    for cond in conds:
        print(f"{cond:16} {'불통과' if cond in row['failed'] else '통과'}")
    print("합격" if row["passed"] else "불합격")


if __name__ == "__main__":
    main()
