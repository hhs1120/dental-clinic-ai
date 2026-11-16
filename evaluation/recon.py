"""⑧-1 규칙 기준선 채점 (평가 설계 §5 ⑧-1, 설계 docs/superpowers/specs/2026-11-11-finance-recon-design.md §1).
앱의 탐지(backend/recon_match.py)와 규칙 원인(backend/recon.py rule_cause)을 정답 truth_recon_mismatches와 맞춘다.
정답 건 ↔ 탐지 건 = 관련 행(fin 참조·수납 id)이 가장 많이 겹치는 탐지 건(같으면 키 순 첫 건) — 한 탐지 건이 정답
둘과 겹치면 둘 다 그 원인으로, 겹치는 탐지 건이 없으면 오답. 원인별 표·틀린 건 목록은 dev만(평가 설계 §7).
3단계 조사 에이전트(backend/recon_agent.py — 지금 프롬프트 버전 실행, 앱 DB에 쌓인 것)의 원인 정확도를 같은 대응으로
나란히 채점한다: 실행 없음·unknown은 오답(기권률 따로), 짝 비교(규칙 틀림 → 에이전트 맞음 = 구제, 반대 = 손실),
조사 단계 수, 자동 마감 오류율(backend/recon_auto.py autoclose_reason을 넘는 건 중 정답과 다른 비율 — 정책과 무관하게
센다, 정답 원인 묶음별). test 채점은 사람만(python -m evaluation.recon score --split test, 평가 설계 §2.7)."""

import argparse
import json
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

from backend import recon, recon_agent, recon_auto, recon_match

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "reports" / "recon_runs.jsonl"  # test 채점 이력 — 프롬프트 버전마다 한 번


def _refs(fin: list[dict], payment_ids: list[int]) -> set:
    return {(f["table"], f["id"]) for f in fin} | {("emr_payments", p) for p in payment_ids}


def match(truth_row: dict, items: list[dict]) -> dict | None:
    """정답 건에 대응하는 탐지 건 — 없으면 None"""
    t = _refs(json.loads(truth_row["fin_ids"]), json.loads(truth_row["payment_ids"]))
    hits = [(len(_refs(i["fin"], i["payment_ids"]) & t), i["key"], i) for i in items]
    hits = [h for h in hits if h[0]]
    return min(hits, key=lambda h: (-h[0], h[1]))[2] if hits else None


def _kn() -> dict:
    return {"k": 0, "n": 0}


def score(conn, split: str, today: date, agents: dict | None = None) -> dict:
    """agents = 건 키 → 에이전트 결과 요약(recon_agent.results 모양) — 주면 앱 DB 실행 대신 그것으로 채점(랭그래프 비교
    evaluation/langgraph_recon.py). {n, detected, rule, by_group, by_cause(dev), wrong(dev), agent, agent_by_group, abstain, missing, pair,
    pair_rows(dev), steps_mean, steps_hist(dev), auto, prompt_version} — {k, n}은 맞은 수·전체(auto는 틀린 수·조건을 넘은 수)"""
    books = recon_match.load(conn, today)
    items = recon_match.detect(books)
    groups = books["mv"]["recon_causes"]
    if agents is None:
        agents = recon_agent.results(conn, {i["key"] for i in items})
    truth = [
        dict(r)
        for r in conn.execute(
            "SELECT occurred_on, cause, fin_ids, payment_ids FROM truth.truth_recon_mismatches"
            " WHERE split = ? ORDER BY occurred_on, id",
            (split,),
        )
    ]
    names = sorted(set(groups.values()))
    detected, rule, agent, abstain = _kn(), _kn(), _kn(), _kn()
    by_group = {g: _kn() for g in names}
    agent_by_group = {g: _kn() for g in names}
    auto = {g: _kn() for g in names}
    by_cause: dict[str, dict] = defaultdict(_kn)
    pair = {"rescued": 0, "lost": 0, "both": 0, "neither": 0}
    wrong, pair_rows, missing, ran = [], [], 0, {}
    for t in truth:
        item = match(t, items)
        got = recon.rule_cause(item, books) if item else None
        a = agents.get(item["key"]) if item else None
        a_cause = a["cause"] if a else None
        ok, a_ok = got == t["cause"], a_cause == t["cause"]
        g = groups[t["cause"]]
        for kn in (rule, by_group[g], by_cause[t["cause"]]):
            kn["n"] += 1
            kn["k"] += ok
        for kn in (agent, agent_by_group[g]):
            kn["n"] += 1
            kn["k"] += a_ok
        abstain["n"] += 1
        abstain["k"] += a_cause == "unknown"
        missing += a is None or a_cause is None
        if a and a["status"] != "running":
            ran[a["run_id"]] = a["tool_calls"]
        if item and recon_auto.autoclose_reason(got, a_cause, groups):
            auto[g]["n"] += 1
            auto[g]["k"] += not a_ok
        kind = {(True, True): "both", (False, True): "rescued", (True, False): "lost"}.get(
            (ok, a_ok), "neither"
        )
        pair[kind] += 1
        detected["n"] += 1
        detected["k"] += item is not None
        if not ok:
            wrong.append({"date": t["occurred_on"], "cause": t["cause"], "rule_cause": got})
        if kind in ("rescued", "lost", "neither"):
            pair_rows.append(
                {
                    "date": t["occurred_on"],
                    "cause": t["cause"],
                    "rule_cause": got,
                    "agent_cause": a_cause,
                    "pair": kind,
                }
            )
    dev = split == "dev"
    return {
        "n": len(truth),
        "detected": detected,
        "rule": rule,
        "by_group": by_group,
        "by_cause": dict(sorted(by_cause.items())) if dev else None,
        "wrong": wrong if dev else None,
        "prompt_version": recon_agent.PROMPT_VERSION,
        "agent": agent,
        "agent_by_group": agent_by_group,
        "abstain": abstain,
        "missing": missing,
        "pair": pair,
        "pair_rows": pair_rows if dev else None,
        "steps_mean": round(sum(ran.values()) / len(ran), 2) if ran else None,
        "steps_hist": dict(sorted(Counter(ran.values()).items())) if dev else None,
        "auto": auto,
    }


def _test_done(version: str) -> bool:
    if not RUNS.exists():
        return False
    seen = [json.loads(x) for x in RUNS.read_text(encoding="utf-8").splitlines() if x]
    return any(h["prompt_version"] == version for h in seen)


def main(argv=None) -> None:
    from backend import db
    from evaluation.eval import _render_recon
    from shared.clinic import load_clinic

    p = argparse.ArgumentParser(prog="python -m evaluation.recon")
    p.add_argument("command", choices=("score",))
    p.add_argument("--split", choices=("dev", "test"), required=True)
    p.add_argument("--force", action="store_true", help="test를 같은 버전으로 다시")
    args = p.parse_args(argv)
    version = recon_agent.PROMPT_VERSION
    if args.split == "test" and not args.force and _test_done(version):
        raise SystemExit(
            f"test ⑧-1({version})은 이미 채점했다 — 다시 하려면 --force(평가 설계 §1 원칙 2)"
        )
    conn = db.connect(ROOT / "data" / "app.sqlite", ROOT / "data" / "source.sqlite")
    conn.execute(
        "ATTACH DATABASE ? AS truth",
        (f"{(ROOT / 'ground_truth' / 'truth.sqlite').resolve().as_uri()}?mode=ro",),
    )
    r = score(conn, args.split, load_clinic().today)
    print("\n".join(_render_recon(r)))
    if args.split == "test":
        RUNS.parent.mkdir(parents=True, exist_ok=True)
        with RUNS.open("a", encoding="utf-8") as f:
            line = {"split": "test", "prompt_version": version, "at": datetime.now().isoformat()}
            f.write(json.dumps(line | {"result": r}, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
