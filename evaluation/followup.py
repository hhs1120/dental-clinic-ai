"""① 후속 처리 평가 (평가 설계 §5, 설계 docs/superpowers/specs/2026-11-08-followup-autoclose-design.md "PR ②").
평가 실행: 채점 모수 통화(evaluation/call_actions.py eval_calls)에 후속 에이전트를 app.sqlite 사본에서 라운드 배치로
돌려 evaluation/followup_runs.jsonl에 쌓는다 — 판정 풀 통화가 들어가 앱 DB에는 쓰지 않는다. 채점은 저장된 줄만 읽는다."""

import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path

from backend import db, followup
from backend.calls import active_versions
from backend.harness import rounds
from backend.harness.loop import Runner
from backend.harness.store import RunStore, SqliteLlmLog
from evaluation.call_actions import classifier_todo, eval_calls, gold_todo_calls, split_population
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


def _active_version(path: Path) -> str | None:
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = c.execute(
            "SELECT value FROM ax_settings WHERE key = 'active_prompt_version'"
        ).fetchone()
        return json.loads(row[0]) if row else None
    finally:
        c.close()


def _copy(app_db: Path, copy: Path, state: Path) -> None:
    """앱 DB를 복사 — 이미 있으면 이어 쓴다(끊긴 실행·배치 상태가 거기 있다). 활성 분류 버전이 원본과 다르면
    옛 분류로 모수를 세게 되므로 새로 복사한다"""
    if copy.exists() and _active_version(copy) != _active_version(app_db):
        print("활성 분류 버전이 바뀌어 사본을 새로 만든다")
        copy.unlink()
        state.unlink(missing_ok=True)
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
    _copy(app_db, copy, state)
    conn = db.connect(copy, source_db)
    conn.execute("ATTACH DATABASE ? AS truth", (f"{truth_db.resolve().as_uri()}?mode=ro",))
    clinic = load_clinic()
    gw = (gw_factory or (lambda c: Gateway(load_config(), SqliteLlmLog(c), "runtime")))(conn)
    reg, names = followup.registry(clinic, searcher)
    store = RunStore(conn)
    runner = Runner(followup.spec(names), gw, store, reg)
    pv = followup.PROMPT_VERSION
    done = {
        (r["call_uid"], r.get("classification_id"), r.get("prompt_version")) for r in load(out)
    }
    todo = {}
    for u in (u for us in eval_calls(conn).values() for u in us):
        found = followup.active(conn, u, include_pool=True)
        if found and (u, found[1]["id"], pv) not in done:  # 키 = (통화, 분류 행, 프롬프트 버전)
            todo[u] = found
    counts, spent = {"done": 0, "failed": 0, "cost_usd": 0.0}, {}
    for attempt in range(followup.ATTEMPTS):
        if not todo:
            break
        ids = {
            u: _run_id(runner, store, u, followup.input_text(c, k, clinic.today))
            for u, (c, k) in todo.items()
        }
        rounds.run_rounds(
            runner,
            gw,
            list(ids.values()),
            state,
            min_batch=min_batch if batch else len(ids) + 1,
            poll_seconds=poll_seconds,
        )
        retry, rows = {}, []
        for u, rid in ids.items():
            call, cls = todo[u]
            spent[u] = spent.get(u, 0.0) + (store.run(rid)["cost_usd"] or 0)  # 재시도 비용까지
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
                    "prompt_version": pv,
                    "action": args["action"] if ok else None,
                    "summary": args["summary"] if ok else None,
                    "facts": args["facts"] if ok else [],
                    "status": "done" if ok else "failed",
                    "reason": reason,
                    "tool_calls": r["tool_calls"],
                    "cost_usd": round(spent[u], 6),
                }
            )
            counts["done" if ok else "failed"] += 1
        with out.open(
            "a", encoding="utf-8"
        ) as f:  # 시도마다 써서 끊겨도 끝난 통화는 다시 돌지 않게
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        todo = retry
    total_cost = sum(
        r[0] or 0
        for r in conn.execute(
            "SELECT cost_usd FROM ax_agent_runs WHERE agent = ?", (followup.AGENT,)
        )
    )
    counts["cost_usd"] = round(total_cost, 4)
    return counts


# ── 규칙 기준선·채점 ─────────────────────────────────────────────────

# 위에서부터 먼저 맞는 규칙 — 분류기 할 일 문장의 낱말만 본다(정답을 만든 녹취록 라벨과 다른 입력·규칙)
BASELINE_RULES = (
    ("scaling_timing", ("스케일링",)),
    ("document", ("영수증", "진단서", "소견서", "서류", "사본")),
    ("billing", ("미납", "수납", "결제", "남은 금액", "잔액")),
    ("insurance_cost", ("보험", "실손", "비용", "금액", "가격")),
    ("appointment", ("예약", "일정", "날짜", "시간", "변경", "취소")),
    ("doctor_reply", ("원장",)),
)


REPLY_WORDS = ("회신", "안내", "전화", "문자", "연락")


def baseline(todo: list) -> str:
    """규칙 기준선 — 할 일 문장 낱말 규칙(평가 설계 §5, 기능 정의 §8 "에이전트 vs 규칙 기준선")"""
    text = " ".join(t["text"] if isinstance(t, dict) else str(t) for t in todo)
    if "전달" in text and not any(w in text for w in REPLY_WORDS):
        return "doctor_note"  # 원장님께 전달만 하고 환자에게 회신은 없음
    for action, words in BASELINE_RULES:
        if any(w in text for w in words):
            return action
    return "other"


def _kn(hits: list[tuple[str, bool]]) -> dict:
    by = {}
    for action, ok in hits:
        b = by.setdefault(action, {"k": 0, "n": 0})
        b["n"] += 1
        b["k"] += ok
    return {"k": sum(ok for _, ok in hits), "n": len(hits), "by_action": by}


def followup_version() -> str:
    return followup.PROMPT_VERSION


def score(
    conn,
    runs: list[dict],
    truth: dict[str, str],
    split_calls: dict,
    prompt_version: str | None = None,
) -> dict:
    """구간마다 에이전트 vs 기준선 정확도(정답 종류별), 실패 수(이유별), 비용, 분류기가 놓친·더한 할 일 통화 수.
    실행 줄은 그 프롬프트 버전(기본은 지금 버전)·지금 활성 분류 행의 것만. 실패는 오답"""
    pv = prompt_version or followup.PROMPT_VERSION
    a = active_versions(conn)[0]
    todo, cls_id = {}, {}
    for u, i, t in conn.execute(
        "SELECT call_uid, id, todo FROM ax_call_classifications"
        " WHERE model_alias = 'classify' AND prompt_version = ?",
        (a,),
    ):
        todo[u], cls_id[u] = (json.loads(t) if t else []), i
    by_run = {
        r["call_uid"]: r
        for r in runs
        if r.get("prompt_version", pv) == pv
        and r.get("classification_id", cls_id.get(r["call_uid"])) == cls_id.get(r["call_uid"])
    }
    gold = gold_todo_calls(conn)
    pop = {k: [r["call_uid"] for r in v] for k, v in split_population(conn).items()}
    cls = classifier_todo(conn)
    out = {}
    for sp, uids in split_calls.items():
        ids = [u for u in uids if u in truth]
        agent = [(truth[u], (by_run.get(u) or {}).get("action") == truth[u]) for u in ids]
        base = [(truth[u], baseline(todo.get(u, [])) == truth[u]) for u in ids]
        mine = [by_run[u] for u in ids if u in by_run]
        gold_sp = set(gold.get(sp, []))
        out[sp] = {
            "n": len(ids),
            "agent": _kn(agent),
            "baseline": _kn(base),
            "failed": sum(r["status"] != "done" for r in mine),
            "failed_by_reason": dict(
                Counter(
                    (r.get("reason") or "이유 없음").split(" — ")[0]
                    for r in mine
                    if r["status"] != "done"
                )
            ),
            "not_run": sum(u not in by_run for u in ids),
            "cost_usd": round(sum(r.get("cost_usd") or 0 for r in mine), 4),
            "missed_by_classifier": sum(u not in cls for u in gold_sp),
            "extra_by_classifier": sum(u in cls and u not in gold_sp for u in pop.get(sp, [])),
        }
    return out


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="python -m evaluation.followup")
    p.add_argument("command", choices=("run",))
    p.add_argument("--no-batch", action="store_true", help="라운드 배치 없이 일반 호출만")
    args = p.parse_args(argv)
    data = ROOT / "data"
    # 문서 검색은 원본 색인을 읽기만 한다 — 키·색인이 없으면 문서 검색 없이
    src = db.connect(data / "app.sqlite", data / "source.sqlite")
    src.execute("PRAGMA query_only = ON")  # 원본은 읽기만
    searcher = followup._searcher(src, load_clinic())
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
