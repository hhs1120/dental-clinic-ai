"""② 자동 개선 — 개선 에이전트가 silver 재료의 오류로 ① 분류 프롬프트 후보를 쓰고, 프롬프트 관문
(evaluation/prompt_gate.py)이 거른다. 넘으면 대기 버전(ax_settings.pending_prompt_version), 전환은 사람이 재분류로.
원본: 평가 설계 §2.7, 설계 docs/superpowers/specs/2026-11-17-auto-improve-design.md.
골드를 보므로 evaluation/에서만 돌고, 에이전트 실행 기록·탈락 후보는 RUNS(커밋 안 함), 앱으로 넘기는 것은
REPORT(라벨 없는 집계)와 대기 버전 설정뿐이다. test·dev·hard 행은 읽지 않는다."""

import hashlib
import json
import re
import shutil
from datetime import datetime
from pathlib import Path

from backend import db, prompts
from evaluation import prompt_gate
from evaluation.claim_qa import COPY_WORDS, _grams
from evaluation.improve_agent import MAX_USD, open_store, propose
from evaluation.metrics import GATE_ATTEMPTS
from evaluation.prompt_gate import IMPROVE_REPORT
from evaluation.splits import silver_split

ITEM = "has_todo"  # 첫 목표 항목(기능 정의 §9) — 고치는 곳은 labels.md의 이 규칙 줄 하나
RUNS = Path(
    "evaluation/runs/improve"
)  # 에이전트 실행 기록·LLM 로그·탈락 후보 — 골드와 엮여 커밋하지 않는다
REPORT = IMPROVE_REPORT
RULE_PREFIX = f"- {ITEM}:"
QUOTES = re.compile(
    r"[\"“”'‘’「」]"
)  # 예시 문장 삽입 — 규칙에는 따옴표 글을 넣지 않는다(평가 설계 §2.7 재료)
CALL_ID = re.compile(r"\b(?:c|hc)-\d+")


def material_errors(conn, gold_rows: list[dict], base: str) -> list[dict]:
    """silver 재료 구간 중 기준 버전 ok 분류의 has_todo가 골드와 다른 통화(call_uid 순). conn은 앱 DB에 source를
    붙인 연결, gold_rows는 truth_call_labels silver 행 전체(층화를 위해 검증 구간도 함께) — 다른 분할이 섞이면 멈춘다"""
    if any(r["split"] != "silver" for r in gold_rows):
        raise SystemExit("holdout_in_material")
    part = silver_split(gold_rows)
    gold = {r["call_uid"]: int(r[ITEM]) for r in gold_rows if part[r["call_uid"]] == "material"}
    out = []
    for u, labels, transcript, direction in conn.execute(
        "SELECT k.call_uid, k.labels, c.transcript, c.direction FROM ax_call_classifications k"
        " JOIN source.ax_calls c ON c.call_uid = k.call_uid"
        " WHERE k.model_alias = 'classify' AND k.prompt_version = ? AND k.status = 'ok'"
        " ORDER BY k.call_uid",
        (base,),
    ):
        if u not in gold:
            continue
        model = int(json.loads(labels)[ITEM])
        if model != gold[u]:
            out.append({"call_uid": u, "transcript": transcript, "direction": direction,
                        "model": model, "gold": gold[u]})  # fmt: skip
    return out


def rule_line(labels_md: str) -> str:
    """labels.md의 목표 항목 규칙 줄 — 없거나 둘 이상이면 ValueError"""
    lines = [x for x in labels_md.splitlines() if x.startswith(RULE_PREFIX)]
    if len(lines) != 1:
        raise ValueError(f"{RULE_PREFIX} 줄이 {len(lines)}개")
    return lines[0]


def candidate_name(base: str, root: Path = prompts.PROMPTS_DIR, runs: Path = RUNS) -> str:
    """<기준>-imp<n> — 앱 프롬프트 폴더와 탈락 후보 보관 폴더 어디에도 없는 가장 작은 n"""
    n = 1
    while (root / "classify" / f"{base}-imp{n}").exists() or (
        runs / "candidates" / f"{base}-imp{n}"
    ).exists():
        n += 1
    return f"{base}-imp{n}"


def write_candidate(base: str, name: str, new_rule: str, root: Path = prompts.PROMPTS_DIR) -> Path:
    """기준 버전 폴더를 복사하고 labels.md의 규칙 줄만 바꾼다(summary.md·모델은 그대로). 있는 폴더는 덮지 않는다"""
    src, dst = root / "classify" / base, root / "classify" / name
    if dst.exists():
        raise FileExistsError(dst)
    shutil.copytree(src, dst)
    md = (dst / "labels.md").read_text(encoding="utf-8")
    (dst / "labels.md").write_text(md.replace(rule_line(md), new_rule), encoding="utf-8")
    return dst


def _copied(text: str, material: list[dict]) -> bool:
    mine = _grams(text, COPY_WORDS)
    return any(mine & _grams(m["transcript"], COPY_WORDS) for m in material)


def check_candidate(base_md: str, cand_md: str, material: list[dict]) -> list[str]:
    """관문 전 코드 검사 — 못 넘은 이름: scope(규칙 줄 밖 변경), example(따옴표 예시 글), copy(재료 녹취와
    연속 COPY_WORDS어절 겹침, 평가 설계 §3.1 베끼기 기준)"""
    old, new = base_md.splitlines(), cand_md.splitlines()
    rule = rule_line(base_md)
    try:
        new_rule = rule_line(cand_md)
    except ValueError:
        return ["scope"]
    if [x for x in old if x != rule] != [x for x in new if x != new_rule]:
        return ["scope"]
    failed = []
    if QUOTES.search(new_rule):
        failed.append("example")
    if _copied(new_rule, material):
        failed.append("copy")
    return failed


def leaks(text: str, material: list[dict]) -> bool:
    """앱으로 넘기는 자유 글 칸 검사(평가 설계 §2.7 누출 차단) — 재료 녹취 베끼기나 통화 id"""
    return bool(CALL_ID.search(text)) or _copied(text, material)


# ── 루프(자동 개선 설계 §3) ─────────────────────────────────────────────


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rows(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def _archive(name: str, root: Path, runs: Path) -> None:
    """관문을 넘지 못한 후보는 앱 프롬프트 폴더에서 치운다 — 넘은 것만 prompts/에 남는다"""
    src = root / "classify" / name
    if src.exists():
        (runs / "candidates").mkdir(parents=True, exist_ok=True)
        shutil.move(src, runs / "candidates" / name)


def improve(
    gw, conn, gold_rows: list[dict], calls: dict, *, base: str, config: dict, item: str = ITEM,
    gate_runner=prompt_gate.run_gate, propose_fn=propose, store=None, auto_for=None,
    prompts_root: Path = prompts.PROMPTS_DIR, runs: Path = RUNS, report: Path = REPORT,
    gate_report: Path = prompt_gate.REPORT,
) -> list[dict]:  # fmt: skip
    """멈춤(대기 버전·시도 상한) → 재료 → (후보 → 검사 → 관문) × 남은 시도. 합격하면 대기 버전을 쓰고 멈춘다.
    conn은 앱 DB(source 붙임 — 쓰는 것은 대기 버전 설정 하나), gold_rows는 silver 행, calls는 관문에 넘길
    통화 → (녹취, 방향). 후보 1개 = report 1행(통화 id·녹취 없음)"""
    if item != ITEM:
        raise SystemExit(f"목표 항목은 지금 {ITEM}만")
    if db.get_setting(conn, "pending_prompt_version"):
        raise SystemExit("pending_exists")
    left = GATE_ATTEMPTS - len(prompt_gate.attempts(base, gate_report, report))
    if left <= 0:
        raise SystemExit("attempt_cap")
    policy = db.get_setting(conn, "auto_policy", {})
    material = material_errors(conn, gold_rows, base)
    base_md = (prompts_root / "classify" / base / "labels.md").read_text(encoding="utf-8")
    failures = [
        {"rule": r["changed_rule"], "failed": r["failed"]}
        for r in _rows(report)
        if r["base"] == base and r["gate_hash"] == prompt_gate.gate_hash() and r["changed_rule"]
    ]
    out = []
    for attempt in range(1, left + 1):
        name = candidate_name(base, prompts_root, runs)
        got = propose_fn(gw, store, rule_line(base_md), material, failures, target=name)
        row = {"at": datetime.now().isoformat(timespec="seconds"), "task": "classify",
               "item": item, "base": base, "candidate": name, "candidate_sha256": None,
               "gate_hash": prompt_gate.gate_hash(), "attempt": attempt,
               "error_types": got["types"], "changed_rule": got["rule"], "gate": None,
               "failed": [], "adopted": False, "cost_usd": got["cost_usd"]}  # fmt: skip
        if got["rule"] is None:
            row["failed"] = ["no_rule"]
        else:
            path = write_candidate(base, name, got["rule"], prompts_root) / "labels.md"
            row["candidate_sha256"] = _sha(path)
            row["failed"] = check_candidate(base_md, path.read_text(encoding="utf-8"), material)
            try:
                if not row["failed"]:
                    on = any(policy.values())
                    g = gate_runner(gw, base, name, item, gold_rows=gold_rows, calls=calls,
                                    policy=policy, config=config, report=gate_report,
                                    improve_report=report,
                                    auto=auto_for(name) if on and auto_for else None)  # fmt: skip
                    row["gate"] = {k: g[k] for k in ("passed", "failed", "items")}
                    row["failed"] = list(g["failed"])
                    row["cost_usd"] = round(row["cost_usd"] + g["cost_usd"], 4)
                    row["adopted"] = g["passed"]
            finally:
                if not row["adopted"]:
                    _archive(name, prompts_root, runs)
        if any(leaks(t["name"], material) for t in row["error_types"] or []):
            row["error_types"] = None
            row["failed"].append("leak")
        if row["changed_rule"] and leaks(row["changed_rule"], material):
            row["changed_rule"] = None
            row["failed"].append("leak")
        if row["adopted"]:
            with conn:
                db.set_setting(conn, "pending_prompt_version", name)
        report.parent.mkdir(parents=True, exist_ok=True)
        with report.open("a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        out.append(row)
        if row["adopted"]:
            break
        if got["rule"]:
            failures.append({"rule": got["rule"], "failed": row["failed"]})
    return out


def _plan(conn, gold_rows: list[dict], base: str, gate_report: Path, report: Path) -> dict:
    """LLM 없이 — 재료 오류 수, 남은 시도, 최근 관문 1회 비용"""
    costs = [r["cost_usd"] for r in _rows(gate_report) if r["base"] != r["candidate"]]
    return {
        "base": base,
        "material_errors": len(material_errors(conn, gold_rows, base)),
        "attempts_left": GATE_ATTEMPTS - len(prompt_gate.attempts(base, gate_report, report)),
        "pending": db.get_setting(conn, "pending_prompt_version"),
        "last_gate_usd": costs[-1] if costs else None,
        "agent_max_usd": MAX_USD,
    }


def main(argv=None) -> None:
    import argparse

    from shared.llm import load_config

    p = argparse.ArgumentParser(prog="python -m evaluation.improve")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--task", default="classify", choices=["classify"])
    r.add_argument("--item", default=ITEM)
    r.add_argument("--plan", action="store_true")
    r.add_argument("--truth", default="ground_truth/truth.sqlite")
    r.add_argument("--source", default="data/source.sqlite")
    r.add_argument("--app", default="data/app.sqlite")
    args = p.parse_args(argv)
    gold_rows = [dict(x) for x in prompt_gate._ro(args.truth).execute(
        "SELECT * FROM truth_call_labels WHERE split = 'silver'")]  # fmt: skip
    conn = db.connect(Path(args.app), Path(args.source))
    base = db.get_setting(conn, "active_prompt_version")
    if args.plan:
        for k, v in _plan(conn, gold_rows, base, prompt_gate.REPORT, REPORT).items():
            print(f"{k}: {v}")
        return
    part = silver_split(gold_rows)
    calls = {u: (t, d) for u, t, d in conn.execute(
        "SELECT call_uid, transcript, direction FROM source.ax_calls WHERE transcript IS NOT NULL")
        if part.get(u) == "validation"}  # fmt: skip
    rows = improve(
        prompt_gate.make_gateway(RUNS / "llm.jsonl"), conn, gold_rows, calls, base=base,
        config=load_config(), item=args.item, store=open_store(),
        auto_for=lambda v: prompt_gate.auto_inputs(args.app, args.source, args.truth, base, v),
    )  # fmt: skip
    for x in rows:
        result = "합격 → 대기 버전" if x["adopted"] else f"탈락 {', '.join(x['failed'])}"
        print(f"{x['candidate']} · {result} · ${x['cost_usd']}")
        print(f"  규칙: {x['changed_rule']}")


if __name__ == "__main__":
    main()
