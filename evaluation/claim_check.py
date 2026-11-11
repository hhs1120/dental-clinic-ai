"""⑤-2 AI 점검 채점 (평가 설계 §5 ⑤, 설계 docs/superpowers/specs/2026-11-10-claim-review-design.md §2).
대상은 전 기간 요건 대상 청구 줄(backend/claim_check.py targets "all"), dev/test는 청구 id 해시로 반반.
(a) 임상 요건형 오류(truth_claim_errors kind requirement) — AI가 근거 없음(missing)이라 한 줄을 양성으로 정밀도·재현율,
"서술 있는 줄 전부"(truth_chart_notes kind narrative) 기준선과 같은 줄에서 나란히, 기간별(확정 월 / 제출·대기)로도.
(b) 요건 사유로 깎인 조정·불능 건의 판단 — 정답 정당(오류 줄) / 다툴 만함(오류 없는 줄)(가상 데이터 생성 설계 §2.4),
AI 추천(근거 있음 → 다툴 만함, 없음 → 정당)의 정확도를 "모두 정당"·"모두 다툴 만함" 기준선과 나란히.
프롬프트는 dev 고정 표본(dev_sample)으로만 고치고, test는 사람이 프롬프트 버전마다 한 번(평가 설계 §2.7).
추가 처치 누락(omitted_extra)은 2b."""

import argparse
import hashlib
import json
from datetime import date, datetime
from pathlib import Path

from backend import claim_check as cc
from backend.prep import master_values

ROOT = Path(__file__).resolve().parents[1]
CONFIRMED = (
    "paid",
    "adjusted",
    "denied",
)  # 심사 결과가 난 줄 — 그 밖(submitted·pending)은 제출·대기
AI_JUDGMENT = {"met": "contestable", "missing": "justified"}  # backend/claims.py AI_SUGGEST와 같다


class AlreadyRun(Exception):
    pass


def claim_split(claim_id: int) -> str:
    """청구 줄 단위 해시 반반 — sha256 첫 바이트 짝수 dev, 홀수 test"""
    return "dev" if hashlib.sha256(f"claim:{claim_id}".encode()).digest()[0] % 2 == 0 else "test"


def _order(claim_id: int) -> str:
    return hashlib.sha256(f"sample:{claim_id}".encode()).hexdigest()


def error_claims(conn) -> set[int]:
    """임상 요건형 오류가 있는 청구 id(정답 DB)"""
    return {
        r[0]
        for r in conn.execute(
            "SELECT c.id FROM source.emr_claims c JOIN truth.truth_claim_errors e"
            " ON e.treatment_id = c.treatment_id WHERE e.kind = 'requirement'"
        )
    }


def dev_sample(conn, today: date, n: int = 60) -> list[int]:
    """프롬프트를 고칠 때 보는 dev 고정 표본 — 오류 줄 n/2 + 오류 없는 줄 n/2, 각 층에서 해시 순 앞쪽"""
    errors = error_claims(conn)
    dev = [
        t["claim_id"]
        for t in cc.targets(conn, today, scope="all")
        if claim_split(t["claim_id"]) == "dev"
    ]
    bad = sorted((c for c in dev if c in errors), key=_order)[: n // 2]
    good = sorted((c for c in dev if c not in errors), key=_order)[: n - len(bad)]
    return sorted(bad + good)


def _pr(pairs: list[tuple[bool, bool]]) -> dict:
    """(예측 양성, 정답 양성) 목록 → tp·fp·fn·정밀도·재현율"""
    tp = sum(p and t for p, t in pairs)
    fp = sum(p and not t for p, t in pairs)
    fn = sum(t and not p for p, t in pairs)
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": tp / (tp + fp) if tp + fp else None,
        "recall": tp / (tp + fn) if tp + fn else None,
    }


def _requirement(rows: list[dict]) -> dict:
    """rows: 후검증 통과한 줄 — ai·narrative·error"""
    return {
        "ai": _pr([(r["ai"] == "missing", r["error"]) for r in rows]),
        "narrative_all": _pr([(r["narrative"], r["error"]) for r in rows]),
    }


def score(conn, today: date, split: str) -> dict:
    """(a) 요건 오류, (b) 조정 건 판단 — conn은 앱 DB에 source·truth를 붙인 연결"""
    mv = master_values(conn)
    req_notes = {k for k, t in mv["adjust_note_types"].items() if t == "requirement"}
    errors = error_claims(conn)
    narrative = {
        r[0]
        for r in conn.execute(
            "SELECT c.id FROM source.emr_claims c JOIN truth.truth_chart_notes n"
            " ON n.treatment_id = c.treatment_id WHERE n.kind = 'narrative'"
        )
    }
    rows = [t for t in cc.targets(conn, today, scope="all") if claim_split(t["claim_id"]) == split]
    checks = cc.results(conn, [t["claim_id"] for t in rows])
    lines = []
    for t in rows:
        c = checks.get(t["claim_id"])
        lines.append(
            {
                "claim_id": t["claim_id"],
                "status": t["status"],
                "adjust_note": t["adjust_note"],
                "checked": c is not None,
                "valid": bool(c and c["valid"]),
                "ai": c["requirement"] if c else None,
                "error": t["claim_id"] in errors,
                "narrative": t["claim_id"] in narrative,
            }
        )
    valid = [x for x in lines if x["valid"]]
    adjust = [x for x in lines if x["status"] in cc.CUT and x["adjust_note"] in req_notes]
    truth = {x["claim_id"]: "justified" if x["error"] else "contestable" for x in adjust}
    answered = [x for x in adjust if x["valid"] and x["ai"] in AI_JUDGMENT]
    correct = sum(AI_JUDGMENT[x["ai"]] == truth[x["claim_id"]] for x in answered)
    n_adj = len(adjust)
    return {
        "split": split,
        "prompt_versions": sorted(
            {r[0] for r in conn.execute("SELECT DISTINCT prompt_version FROM ax_claim_checks")}
        ),
        "requirement": {
            "n": len(lines),
            "checked": sum(x["checked"] for x in lines),
            "valid": len(valid),
            "errors": sum(x["error"] for x in lines),
            **_requirement(valid),
            "by_period": {
                "confirmed": _requirement([x for x in valid if x["status"] in CONFIRMED]),
                "filed": _requirement([x for x in valid if x["status"] not in CONFIRMED]),
            },
        },
        "adjust": {
            "n": n_adj,
            "ai": {
                "correct": correct,
                "answered": len(answered),
                "accuracy": correct / n_adj if n_adj else None,
            },
            "all_justified": sum(v == "justified" for v in truth.values()) / n_adj
            if n_adj
            else None,
            "all_contestable": sum(v == "contestable" for v in truth.values()) / n_adj
            if n_adj
            else None,
        },
    }


def record_test(reports_dir: Path, version: str, force: bool = False) -> None:
    """test는 프롬프트 버전마다 한 번(평가 설계 §1 원칙 2) — 다시 돌리려면 force"""
    path = Path(reports_dir) / "claim_check_runs.jsonl"
    seen = (
        [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x]
        if path.exists()
        else []
    )
    if not force and any(h["split"] == "test" and h["prompt_version"] == version for h in seen):
        raise AlreadyRun(f"test claim_check({version})은 이미 돌렸다 — 다시 돌리려면 --force")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {"split": "test", "prompt_version": version, "at": datetime.now().isoformat()},
                ensure_ascii=False,
            )
            + "\n"
        )


def _connect():
    from backend import db

    conn = db.connect(ROOT / "data" / "app.sqlite", ROOT / "data" / "source.sqlite")
    db.init_schema(conn)
    conn.execute(
        "ATTACH DATABASE ? AS truth",
        (f"{(ROOT / 'ground_truth' / 'truth.sqlite').resolve().as_uri()}?mode=ro",),
    )
    return conn


def main(argv=None) -> None:
    from backend.harness.llm import make_gateway
    from shared.clinic import load_clinic

    p = argparse.ArgumentParser(prog="python -m evaluation.claim_check")
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("sample", help="dev 고정 표본만 점검(과금, 일반 호출)")
    s.add_argument("--n", type=int, default=60)
    sc = sub.add_parser("score", help="채점 — test는 사람만, 프롬프트 버전마다 한 번")
    sc.add_argument("--split", choices=("dev", "test"), default="dev")
    sc.add_argument("--force", action="store_true")
    args = p.parse_args(argv)
    conn = _connect()
    today = load_clinic().today
    if args.command == "sample":
        ids = dev_sample(conn, today, args.n)
        rows = cc.targets(conn, today, scope="all", claim_ids=ids)
        print(cc.build(conn, make_gateway(conn), rows, use_batch=False))
        print(json.dumps(score(conn, today, "dev") | {"sample": len(ids)}, ensure_ascii=False))
        return
    if args.split == "test":
        record_test(ROOT / "reports", cc.PROMPT_VERSION, args.force)
    out = score(conn, today, args.split)
    path = ROOT / "reports" / f"claim_check_{args.split}_{datetime.now():%Y%m%d_%H%M%S}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
