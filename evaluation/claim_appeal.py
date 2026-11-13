"""⑤-4 재심 사유서 채점 (설계 docs/superpowers/specs/2026-11-10-claim-review-design.md §4, 평가 설계 §5 ⑤-4).
정답 기준 다툴 만함(요건 사유로 깎인 조정·불능 중 임상 요건형 오류가 없는 건 — 가상 데이터 생성 설계 §2.4)의 한 분할에
앱과 같은 입력·요청·후검증으로 초안을 쓰고, 후검증 통과율(첫 답·다시 부른 뒤)과 첫 답 원출력의 근거 없는 문장 비율을 낸다
(최종 답은 후검증을 통과해 0이라 첫 답으로 잰다). 초안이 있는 건 = 정답상 다툴 만함이라 기록은 evaluation/ 파일에만
쓴다(app.sqlite에 쓰지 않는다).
uv run --env-file .env python -m evaluation.claim_appeal run --split dev   (과금, 이어 돎 — make claim-appeal-dev)
uv run --env-file .env python -m evaluation.claim_appeal run --split test  (사람, 버전마다 한 번)"""

import argparse
import json
from datetime import datetime
from pathlib import Path

from backend import claim_appeal as ca
from backend import claim_store
from evaluation.claim_check import _req_notes, claim_split, error_claims
from shared.llm import Gateway, JsonlLog, load_config

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "evaluation" / "claim_appeal_runs.jsonl"
LLM_LOG = ROOT / "evaluation" / "claim_appeal_llm.jsonl"  # ax_llm_calls에 쓰지 않는다


class AlreadyRun(Exception):
    pass


def targets(conn, split: str) -> list[dict]:
    """정답 다툴 만함 건(그 분할) — 행은 backend/claim_appeal.py row(판단·기한은 보지 않음)"""
    notes = sorted(_req_notes(conn))
    errors = error_claims(conn)
    ids = [
        r[0]
        for r in conn.execute(
            "SELECT id FROM source.emr_claims WHERE status IN ('adjusted', 'denied')"
            f" AND adjust_note IN ({', '.join('?' * len(notes))}) ORDER BY id",
            notes,
        )
        if r[0] not in errors and claim_split(r[0]) == split
    ]
    return [ca.row(conn, cid) for cid in ids]


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x]


def run(gw, rows: list[dict], path: Path = RUNS) -> dict:
    """같은 요청·후검증·한 번 더로 초안을 쓰고 {claim_id, facts_hash, prompt_version, model, first, final}을 덧붙인다.
    first = 첫 답 원출력, final = 통과한 답 또는 None. 같은 (청구, 해시, 버전, 모델)이고 final이 있으면 건너뛴다"""
    model = gw.config["aliases"][ca.ALIAS][0]
    done = {
        (r["claim_id"], r["facts_hash"], r["prompt_version"], r["model"])
        for r in _read(path)
        if r["final"] is not None
    }
    out = {"called": 0, "skipped": 0, "failed": 0}
    for row in rows:
        facts = ca.appeal_facts(row)
        h = claim_store.facts_hash(facts)
        if (row["claim_id"], h, ca.PROMPT_VERSION, model) in done:
            out["skipped"] += 1
            continue
        req = claim_store.request(ca.KIND, facts, ca.SCHEMA)
        used = gw.complete(ca.ALIAS, task=ca.TASK, prompt_version=ca.PROMPT_VERSION, **req)
        first = used.data if used is not None and used.ok else None
        final = claim_store.valid(ca.KIND, used, facts)
        if final is None:
            used = gw.complete(ca.ALIAS, task=ca.TASK, prompt_version=ca.PROMPT_VERSION, **req)
            final = claim_store.valid(ca.KIND, used, facts)
        out["called"] += 1
        out["failed"] += final is None
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            rec = {
                "claim_id": row["claim_id"],
                "facts_hash": h,
                "prompt_version": ca.PROMPT_VERSION,
                "model": used.model if used else model,
                "first": first,
                "final": final,
            }
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return out


def score(rows: list[dict], runs: list[dict]) -> dict:
    """지금 버전·입력의 마지막 기록으로 — 첫 답 통과·최종 통과 수, 첫 답의 나눈 문장 수·근거 없는 문장 수"""
    last = {
        (r["claim_id"], r["facts_hash"]): r
        for r in runs
        if r["prompt_version"] == ca.PROMPT_VERSION
    }
    n = ran = first_ok = final_ok = total = bad = 0
    for row in rows:
        n += 1
        facts = ca.appeal_facts(row)
        rec = last.get((row["claim_id"], claim_store.facts_hash(facts)))
        if rec is None:
            continue
        ran += 1
        first = rec["first"]
        if isinstance(first, dict):
            try:
                first_ok += ca.check_result(facts, first)
            except (KeyError, TypeError, AttributeError):
                pass
            t, b = ca.unbacked(facts, first)
            total, bad = total + t, bad + b
        final_ok += rec["final"] is not None
    return {
        "prompt_version": ca.PROMPT_VERSION,
        "n": n,
        "run": ran,
        "first_valid": first_ok,
        "final_valid": final_ok,
        "sentences": total,
        "unbacked": bad,
        "unbacked_rate": bad / total if total else None,
    }


def _test_runs() -> Path:
    return ROOT / "reports" / "claim_appeal_test_runs.jsonl"


def main(argv=None) -> None:
    from evaluation.claim_check import _connect

    p = argparse.ArgumentParser(prog="python -m evaluation.claim_appeal")
    p.add_argument("command", choices=("run",))
    p.add_argument("--split", choices=("dev", "test"), required=True)
    p.add_argument("--force", action="store_true")
    args = p.parse_args(argv)
    if args.split == "test" and not args.force:
        if any(r["prompt_version"] == ca.PROMPT_VERSION for r in _read(_test_runs())):
            raise AlreadyRun(f"test claim_appeal({ca.PROMPT_VERSION})은 이미 돌렸다 — --force")
    conn = _connect()
    rows = targets(conn, args.split)
    gw = Gateway(load_config(), JsonlLog(LLM_LOG), scope="generator")
    path = RUNS if args.split == "dev" else ROOT / "evaluation" / "claim_appeal_test.jsonl"
    print(run(gw, rows, path))
    out = score(rows, _read(path)) | {"split": args.split}
    print(json.dumps(out, ensure_ascii=False, indent=1))
    if args.split == "test":
        _test_runs().parent.mkdir(parents=True, exist_ok=True)
        with _test_runs().open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(out | {"at": datetime.now().isoformat()}, ensure_ascii=False) + "\n"
            )


if __name__ == "__main__":
    main()
