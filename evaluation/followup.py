"""① 후속 처리 평가 (평가 설계 §5, 설계 docs/superpowers/specs/2026-11-08-followup-autoclose-design.md "PR ②").
평가 실행: 채점 모수 통화(evaluation/call_actions.py eval_calls)에 후속 에이전트를 app.sqlite 사본에서 라운드 배치로
돌려 evaluation/followup_runs.jsonl에 쌓는다 — 판정 풀 통화가 들어가 앱 DB에는 쓰지 않는다. 채점은 저장된 줄만 읽는다."""

import argparse
import json
import sqlite3
from pathlib import Path

from backend import db, followup
from backend.harness import rounds
from backend.harness.loop import Runner
from backend.harness.store import RunStore, SqliteLlmLog
from evaluation.call_actions import eval_calls
from shared.clinic import load_clinic
from shared.llm import Gateway, load_config

ROOT = Path(__file__).resolve().parents[1]
RUNS_DIR = ROOT / "evaluation" / "runs"  # 커밋 안 함(.gitignore)
COPY = RUNS_DIR / "followup.sqlite"
STATE = RUNS_DIR / "followup_batch.json"
OUT = ROOT / "evaluation" / "followup_runs.jsonl"


def load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _copy(app_db: Path, copy: Path) -> None:
    """앱 DB를 한 번만 복사 — 이미 있으면 이어 쓴다(끊긴 실행·배치 상태가 거기 있다)"""
    if copy.exists():
        return
    copy.parent.mkdir(parents=True, exist_ok=True)
    src, dst = sqlite3.connect(app_db), sqlite3.connect(copy)
    src.backup(dst)
    src.close()
    dst.close()


def _run_id(runner: Runner, store: RunStore, uid: str, text: str) -> int:
    """끊긴 실행이 있으면 그것, 없으면 새 실행"""
    row = store.conn.execute(
        "SELECT id FROM ax_agent_runs WHERE agent = ? AND target = ? AND status = 'running'",
        (runner.spec.name, uid),
    ).fetchone()
    return row[0] if row else rounds.create(runner, uid, text)


def run(
    app_db: Path,
    source_db: Path,
    truth_db: Path,
    *,
    out: Path = OUT,
    copy: Path = COPY,
    state: Path = STATE,
    gw_factory=None,
    searcher=None,
    batch: bool = True,
    min_batch: int = rounds.ROUND_BATCH_MIN,
    poll_seconds: int = 60,
) -> dict:
    """줄이 없는 모수 통화만. 후검증 실패는 한 번 더(앱과 같은 규칙) — {"done", "failed", "cost_usd"}"""
    _copy(app_db, copy)
    conn = db.connect(copy, source_db)
    conn.execute("ATTACH DATABASE ? AS truth", (f"{truth_db.resolve().as_uri()}?mode=ro",))
    clinic = load_clinic()
    gw = (gw_factory or (lambda c: Gateway(load_config(), SqliteLlmLog(c), "runtime")))(conn)
    reg, names = followup.registry(clinic, searcher)
    store = RunStore(conn)
    runner = Runner(followup.spec(names), gw, store, reg)
    done = {r["call_uid"] for r in load(out)}
    todo = {}
    for u in (u for us in eval_calls(conn).values() for u in us if u not in done):
        found = followup.active(conn, u, include_pool=True)
        if found:
            todo[u] = found
    rows, counts = [], {"done": 0, "failed": 0, "cost_usd": 0.0}
    for attempt in range(followup.ATTEMPTS):
        if not todo:
            break
        ids = {
            u: _run_id(runner, store, u, followup.input_text(c, k)) for u, (c, k) in todo.items()
        }
        rounds.run_rounds(
            runner,
            gw,
            list(ids.values()),
            state,
            min_batch=min_batch if batch else len(ids) + 1,
            poll_seconds=poll_seconds,
        )
        retry = {}
        for u, rid in ids.items():
            call, cls = todo[u]
            status, args, reason = followup.judge_run(store, rid, call["transcript"])
            if status == "unverified" and attempt + 1 < followup.ATTEMPTS:
                retry[u] = todo[u]
                continue
            ok = status == "done"
            r = store.run(rid)
            rows.append(
                {
                    "call_uid": u,
                    "classification_id": cls["id"],
                    "prompt_version": followup.PROMPT_VERSION,
                    "action": args["action"] if ok else None,
                    "summary": args["summary"] if ok else None,
                    "facts": args["facts"] if ok else [],
                    "status": "done" if ok else "failed",
                    "reason": reason,
                    "tool_calls": r["tool_calls"],
                    "cost_usd": r["cost_usd"],
                }
            )
            counts["done" if ok else "failed"] += 1
        todo = retry
    total_cost = sum(
        r[0] or 0
        for r in conn.execute(
            "SELECT cost_usd FROM ax_agent_runs WHERE agent = ?", (followup.AGENT,)
        )
    )
    with out.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    counts["cost_usd"] = round(total_cost, 4)
    return counts


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="python -m evaluation.followup")
    p.add_argument("command", choices=("run",))
    p.add_argument("--no-batch", action="store_true", help="라운드 배치 없이 일반 호출만")
    args = p.parse_args(argv)
    data = ROOT / "data"
    # 문서 검색은 원본 색인을 읽기만 한다 — 키·색인이 없으면 문서 검색 없이
    searcher = followup._searcher(
        db.connect(data / "app.sqlite", data / "source.sqlite"), load_clinic()
    )
    r = run(
        data / "app.sqlite",
        data / "source.sqlite",
        ROOT / "ground_truth" / "truth.sqlite",
        searcher=searcher,
        batch=not args.no_batch,
    )
    print(f"후속 처리 평가 실행 {r}")


if __name__ == "__main__":
    main()
