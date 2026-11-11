"""⑧-1 규칙 기준선 채점 (평가 설계 §5 ⑧-1, 설계 docs/superpowers/specs/2026-11-11-finance-recon-design.md §1).
앱의 탐지(backend/recon_match.py)와 규칙 원인(backend/recon.py rule_cause)을 정답 truth_recon_mismatches와 맞춘다.
정답 건 ↔ 탐지 건 = 관련 행(fin 참조·수납 id)이 가장 많이 겹치는 탐지 건(같으면 키 순 첫 건) — 한 탐지 건이 정답
둘과 겹치면 둘 다 그 원인으로, 겹치는 탐지 건이 없으면 오답. 원인별 표·틀린 건 목록은 dev만(평가 설계 §7).
3단계 에이전트 원인 정확도가 이 숫자와 짝 비교로 나란히 선다."""

import json
from collections import defaultdict
from datetime import date

from backend import recon, recon_match


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


def score(conn, split: str, today: date) -> dict:
    """{n, detected, rule, by_group, by_cause(dev), wrong(dev)} — {k, n}은 맞은 수·전체"""
    books = recon_match.load(conn, today)
    items = recon_match.detect(books)
    groups = books["mv"]["recon_causes"]
    truth = [
        dict(r)
        for r in conn.execute(
            "SELECT occurred_on, cause, fin_ids, payment_ids FROM truth.truth_recon_mismatches"
            " WHERE split = ? ORDER BY occurred_on, id",
            (split,),
        )
    ]
    detected, rule = _kn(), _kn()
    by_group = {g: _kn() for g in sorted(set(groups.values()))}
    by_cause: dict[str, dict] = defaultdict(_kn)
    wrong = []
    for t in truth:
        item = match(t, items)
        got = recon.rule_cause(item, books) if item else None
        ok = got == t["cause"]
        for kn in (rule, by_group[groups[t["cause"]]], by_cause[t["cause"]]):
            kn["n"] += 1
            kn["k"] += ok
        detected["n"] += 1
        detected["k"] += item is not None
        if not ok:
            wrong.append({"date": t["occurred_on"], "cause": t["cause"], "rule_cause": got})
    dev = split == "dev"
    return {
        "n": len(truth),
        "detected": detected,
        "rule": rule,
        "by_group": by_group,
        "by_cause": dict(sorted(by_cause.items())) if dev else None,
        "wrong": wrong if dev else None,
    }
