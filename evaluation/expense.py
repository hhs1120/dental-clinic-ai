"""⑧-2 지출 증빙 매칭·계정 과목 규칙 기준선 채점 (평가 설계 §1 원칙 8·§5 ⑧-2, 재무 대조 설계 §2).
기준선 원본은 backend/expense.py(화면과 같은 규칙), 이 파일은 dev 채점 — 사람 확정 덮어쓰기 없는 순수 규칙만 부른다.
4단계 증빙 탐색 에이전트(backend/expense_agent.py — 지금 프롬프트 버전 실행, 앱 DB에 쌓인 것)를 score_agent가 같은
정답으로 나란히 채점한다(설계 §4): 매칭(증빙 집합 = 정답, 증빙 없음 포함)·과목, 짝 비교(규칙 틀림 → AI 맞음 = 구제,
반대 = 손실), 규칙 결과 묶음별(통과·증빙 못 찾음·과목 확인 필요), 확신도별, 자동 확정 오류율(backend/expense_auto.py
조건을 넘은 줄 중 증빙이나 과목이 틀린 비율 — 정책과 무관하게 센다), 실행 없음은 오답. test는 사람만
(python -m evaluation.expense score --split test, 평가 설계 §2.7)."""

import argparse
import json
from datetime import datetime
from pathlib import Path

from backend import expense as app_expense
from backend import expense_agent, expense_auto
from backend.expense import DOC_WINDOW, rule_account, rule_match, vendors_by_label
from generator.master import VENDORS as MASTER_VENDORS

__all__ = ["DOC_WINDOW", "rule_account", "rule_match", "score", "score_agent", "vendors_by_label"]

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "reports" / "expense_runs.jsonl"  # test 채점 이력 — 프롬프트 버전마다 한 번
GROUPS = (
    "passed",
    "none",
    "check",
)  # 규칙 결과 묶음 — 통과(증빙 찾음 + 과목 정해짐)·증빙 못 찾음·과목 확인 필요
CONFIDENCES = ("high", "mid", "low")


def score(conn, split: str, vendors: dict | None = None) -> dict:
    """{n, match: {k,n}, account: {k,n}} — truth_expense_matches의 그 split 출금만(평가 설계 §2.7 — test는 사람만)"""
    vendors = vendors or vendors_by_label(MASTER_VENDORS)
    docs = [
        dict(zip(("id", "kind", "issuer", "issued_on", "amount"), r, strict=True))
        for r in conn.execute(
            "SELECT id, kind, issuer, issued_on, amount FROM source.fin_expense_documents"
        )
    ]
    rows = conn.execute(
        "SELECT outflow_table, outflow_id, document_ids, account FROM truth.truth_expense_matches"
        " WHERE split = ?",
        (split,),
    ).fetchall()
    match = account = 0
    for table, oid, dids, acct in rows:
        q = (
            "SELECT occurred_at, amount, memo FROM source.fin_bank_transactions WHERE id = ?"
            if table == "fin_bank_transactions"
            else "SELECT used_at, amount, merchant FROM source.fin_card_expenses WHERE id = ?"
        )
        at, amount, label = conn.execute(q, (oid,)).fetchone()
        out = {"table": table, "at": at, "amount": amount, "label": label}
        match += rule_match(out, docs, vendors) == sorted(json.loads(dids))
        account += rule_account(out, vendors) == acct
    n = len(rows)
    return {"n": n, "match": {"k": match, "n": n}, "account": {"k": account, "n": n}}


def _kn() -> dict:
    return {"k": 0, "n": 0}


def _pair() -> dict:
    return {"rescued": 0, "lost": 0, "both": 0, "neither": 0}


def _add_pair(p: dict, rule_ok: bool, ai_ok: bool) -> str:
    kind = {(True, True): "both", (False, True): "rescued", (True, False): "lost"}.get(
        (rule_ok, ai_ok), "neither"
    )
    p[kind] += 1
    return kind


def score_agent(conn, split: str) -> dict:
    """{n, prompt_version, rule{match,account}, agent{match,account}, pair_match, pair_account, by_group, by_confidence,
    auto, missing, rows(dev)} — {k, n}은 맞은 수·전체(auto는 틀린 수·조건을 넘은 줄 수). 사람 확정은 보지 않는다"""
    data = app_expense._load(conn, None)
    cand = expense_auto.candidates(conn, data | {"conf": {}})
    by_out = {(o["table"], o["id"]): app_expense.outflow_key(o) for o in data["outflows"]}
    rule, agent = {"match": _kn(), "account": _kn()}, {"match": _kn(), "account": _kn()}
    pair_m, pair_a = _pair(), _pair()
    by_group = {
        g: {f"{w}_{x}": _kn() for w in ("rule", "agent") for x in ("match", "account")}
        for g in GROUPS
    }
    by_conf = {c: {"match": _kn(), "account": _kn()} for c in CONFIDENCES}
    auto, missing, rows = _kn(), 0, []
    truth = conn.execute(
        "SELECT outflow_table, outflow_id, document_ids, account FROM truth.truth_expense_matches"
        " WHERE split = ? ORDER BY outflow_table, outflow_id",
        (split,),
    ).fetchall()
    for table, oid, dids, acct in truth:
        c = cand[by_out[(table, oid)]]
        want = sorted(json.loads(dids))
        a = c["agent"] if c["agent"] and c["agent"]["account"] is not None else None
        r_m, r_a = c["rule_ids"] == want, c["rule_account"] == acct
        a_m = bool(a) and a["document_ids"] == want
        a_a = bool(a) and a["account"] == acct
        missing += a is None
        group = (
            "none"
            if not c["rule_ids"]
            else ("check" if c["rule_account"] is None or c["by_item"] else "passed")
        )
        for kn, ok in (
            (rule["match"], r_m),
            (rule["account"], r_a),
            (agent["match"], a_m),
            (agent["account"], a_a),
            (by_group[group]["rule_match"], r_m),
            (by_group[group]["rule_account"], r_a),
            (by_group[group]["agent_match"], a_m),
            (by_group[group]["agent_account"], a_a),
        ):
            kn["n"] += 1
            kn["k"] += ok
        if a:
            for kn, ok in (
                (by_conf[a["confidence"]]["match"], a_m),
                (by_conf[a["confidence"]]["account"], a_a),
            ):
                kn["n"] += 1
                kn["k"] += ok
        if c["reason"]:
            auto["n"] += 1
            auto["k"] += not (a_m and a_a)
        pm, pa = _add_pair(pair_m, r_m, a_m), _add_pair(pair_a, r_a, a_a)
        if pm != "both" or pa != "both":
            o = c["o"]
            rows.append(
                {
                    "at": o["at"][:10],
                    "label": o["label"],
                    "amount": o["amount"],
                    "group": group,
                    "match": pm,
                    "account": pa,
                    "truth_account": acct,
                    "agent_account": a["account"] if a else None,
                }
            )
    return {
        "n": len(truth),
        "prompt_version": expense_agent.PROMPT_VERSION,
        "rule": rule,
        "agent": agent,
        "pair_match": pair_m,
        "pair_account": pair_a,
        "by_group": by_group,
        "by_confidence": by_conf,
        "auto": auto,
        "missing": missing,
        "rows": rows if split == "dev" else None,
    }


def _test_done(version: str) -> bool:
    if not RUNS.exists():
        return False
    seen = [json.loads(x) for x in RUNS.read_text(encoding="utf-8").splitlines() if x]
    return any(h["prompt_version"] == version for h in seen)


def main(argv=None) -> None:
    from backend import db
    from evaluation.eval import _render_expense

    p = argparse.ArgumentParser(prog="python -m evaluation.expense")
    p.add_argument("command", choices=("score",))
    p.add_argument("--split", choices=("dev", "test"), required=True)
    p.add_argument("--force", action="store_true", help="test를 같은 버전으로 다시")
    args = p.parse_args(argv)
    version = expense_agent.PROMPT_VERSION
    if args.split == "test" and not args.force and _test_done(version):
        raise SystemExit(
            f"test ⑧-2({version})는 이미 채점했다 — 다시 하려면 --force(평가 설계 §1 원칙 2)"
        )
    conn = db.connect(ROOT / "data" / "app.sqlite", ROOT / "data" / "source.sqlite")
    conn.execute(
        "ATTACH DATABASE ? AS truth",
        (f"{(ROOT / 'ground_truth' / 'truth.sqlite').resolve().as_uri()}?mode=ro",),
    )
    r = score_agent(conn, args.split)
    print("\n".join(_render_expense(r)))
    if args.split == "test":
        RUNS.parent.mkdir(parents=True, exist_ok=True)
        with RUNS.open("a", encoding="utf-8") as f:
            line = {"split": "test", "prompt_version": version, "at": datetime.now().isoformat()}
            f.write(json.dumps(line | {"result": r}, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
