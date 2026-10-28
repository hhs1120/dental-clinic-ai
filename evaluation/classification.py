"""① 통화 분류 채점 — 평가 설계 §2.2(지표·정확도 셋), §2.4(블라인드), §1 원칙 5(폴백 제외)·§7(few-shot 제외·dev 덤프).
입력은 앱이 쓴 ax_call_classifications·ax_judgments·ax_blind_votes·ax_llm_calls(칸은 shared/app_schema.sql)와 truth_call_labels."""

import json
import re
from collections import Counter
from itertools import combinations

from evaluation import EvalRefused
from evaluation.metrics import inverse_weights, macro_accuracy, mcnemar_exact, weighted_accuracy
from generator.master import CALL_FLAGS

ITEMS = ("primary_purpose", "counterpart", "urgency") + CALL_FLAGS  # 9항목
PURPOSE = "primary_purpose"


def version_key(v: str) -> list:
    """버전 자연 정렬 — v2 < v10"""
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", v)]


def _calls(conn) -> dict[str, dict]:
    """생성 통화 + 골드(붙여넣기 통화는 골드가 없어 빠진다)"""
    rows = conn.execute(
        "SELECT c.call_uid, c.in_eval_pool, c.in_blind_pool, c.transcript, l.split, "
        + ", ".join(f"l.{i}" for i in ITEMS)
        + " FROM ax_calls c JOIN truth.truth_call_labels l ON l.call_uid = c.call_uid WHERE c.source = 'generated'"
    ).fetchall()
    return {
        r["call_uid"]: {
            "uid": r["call_uid"],
            "pool": r["in_eval_pool"],
            "blind": r["in_blind_pool"],
            "split": r["split"],
            "transcript": r["transcript"],
            "gold": {i: r[i] for i in ITEMS},
        }
        for r in rows
    }


def _classifications(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT cl.id, cl.call_uid, cl.prompt_version, cl.model_alias, cl.model_actual, cl.status, cl.labels,"
        " lc.fallback_used AS fallback FROM ax_call_classifications cl"
        " JOIN ax_llm_calls lc ON lc.id = cl.llm_call_id"
    ).fetchall()
    return [dict(r, labels=json.loads(r["labels"]) if r["labels"] else None) for r in rows]


def _score(ids, pred: dict, ref: dict, calls: dict, population: Counter) -> dict:
    """ids의 통화를 pred vs ref로 — 항목별 k/n, 목적 Strict는 역확률 가중·카테고리 평균 병기(카테고리 = 골드 목적)"""
    ids = sorted(i for i in ids if i in pred and i in ref)
    items = {i: {"k": sum(pred[c][i] == ref[c][i] for c in ids), "n": len(ids)} for i in ITEMS}
    rows = [(calls[c]["gold"][PURPOSE], pred[c][PURPOSE] == ref[c][PURPOSE]) for c in ids]
    sample = Counter(cat for cat, _ in rows)
    weights = inverse_weights(population, sample)
    by_category = {
        cat: {"k": sum(ok for c, ok in rows if c == cat), "n": n}
        for cat, n in sorted(sample.items())
    }
    purpose = dict(
        items[PURPOSE],
        weighted=weighted_accuracy(rows, weights),
        macro=macro_accuracy(rows),
        by_category=by_category,
    )
    return {"n": len(ids), "items": items, "purpose": purpose}


def evaluate(
    conn,
    split: str,
    versions: list[str] | None = None,
    fewshot: dict | None = None,
    dump: bool = False,
) -> dict:
    """split = dev|test. versions 없으면 분류가 있는 버전 전부. fewshot = {버전: {call_uid}} — dev 집계에서 뺀다"""
    calls = _calls(conn)
    population = Counter(c["gold"][PURPOSE] for c in calls.values())
    gold_ids = {
        i
        for i, c in calls.items()
        if (c["split"] in ("silver", "dev") if split == "dev" else c["split"] == "test")
    }
    pool_ids = {i for i, c in calls.items() if c["pool"] and c["split"] == split}
    gold = {i: c["gold"] for i, c in calls.items()}
    human = {
        r["call_uid"]: json.loads(r["labels"])
        for r in conn.execute("SELECT call_uid, labels FROM ax_judgments")
    }
    rows = _classifications(conn)
    versions = versions or sorted({r["prompt_version"] for r in rows}, key=version_key)
    fewshot = fewshot or {}
    out = {
        "versions": {},
        "human_vs_gold": _score(pool_ids & human.keys(), human, gold, calls, population),
    }
    usable = {}
    for v in versions:
        mine = [r for r in rows if r["prompt_version"] == v and r["call_uid"] in calls]
        if len({r["model_alias"] for r in mine}) > 1:
            raise EvalRefused(
                f"{v}: 한 버전에 모델 별칭이 둘 이상 — 모델 2개 교차검증(평가 설계 §2.5) 착수 때 채점 단위를 정한다"
            )
        skip = (
            {calls[r["call_uid"]]["uid"] for r in mine} & fewshot.get(v, set())
            if split == "dev"
            else set()
        )
        ok = {
            r["call_uid"]: r["labels"]
            for r in mine
            if r["status"] == "ok"
            and not r["fallback"]
            and calls[r["call_uid"]]["uid"] not in skip
        }
        usable[v] = ok
        in_gold = [r for r in mine if r["call_uid"] in gold_ids]
        out["versions"][v] = {
            "model_actual": sorted({r["model_actual"] for r in mine}),
            "model_vs_gold": _score(gold_ids, ok, gold, calls, population),
            "model_vs_human": _score(pool_ids & human.keys(), ok, human, calls, population),
            "excluded": {
                "fallback": sum(r["status"] == "ok" and bool(r["fallback"]) for r in in_gold),
                "failed": sum(r["status"] == "failed" for r in in_gold),
                "fewshot": sum(calls[r["call_uid"]]["uid"] in skip for r in in_gold),
            },
        }
    judged = pool_ids & human.keys()
    out["compare"] = [
        _compare(a, b, usable, ids, ref, name)
        for a, b in combinations(versions, 2)
        for name, ids, ref in (("gold", gold_ids, gold), ("human", judged, human))
    ]
    out["blind"] = _blind(conn, rows, calls, split)
    if dump:
        out["dump"] = [
            {
                "call_uid": calls[c]["uid"],
                "version": v,
                "item": i,
                "model": pred[c][i],
                "human": human.get(c, {}).get(i),
                "gold": gold[c][i],
                "transcript": calls[c]["transcript"],
            }
            for v, pred in usable.items()
            for c in sorted(pool_ids & pred.keys())
            for i in ITEMS
            if pred[c][i] != human.get(c, gold[c])[i]
        ]
    return out


def _compare(a: str, b: str, usable: dict, ref_ids: set, ref: dict, ref_name: str) -> dict:
    """버전 비교 — 두 버전 모두 쓸 수 있는 통화 교집합으로만(평가 설계 §2.2), 항목별 맞힌 수와 McNemar p.
    ref = 골드(모델 vs 골드 셋) 또는 사람 판정(그 split 판정 풀 — 결과표의 '항목별 모델 vs 사람 일치율')"""
    ids = sorted(ref_ids & usable[a].keys() & usable[b].keys())
    items = {}
    for i in ITEMS:
        ra = [usable[a][c][i] == ref[c][i] for c in ids]
        rb = [usable[b][c][i] == ref[c][i] for c in ids]
        only_a = sum(x and not y for x, y in zip(ra, rb, strict=True))
        only_b = sum(y and not x for x, y in zip(ra, rb, strict=True))
        items[i] = {"a": sum(ra), "b": sum(rb), "p": mcnemar_exact(only_a, only_b)}
    return {"ref": ref_name, "a": a, "b": b, "n": len(ids), "items": items}


def _blind(conn, rows: list[dict], calls: dict, split: str) -> list[dict]:
    """블라인드 — 좌우 (prompt_version, model_actual) 쌍별로 따로(평가 설계 §2.4), 승수는 그 쪽에 돌리고 좌/우 선택 비율 병기.
    한쪽이라도 폴백·실패 분류면 뺀다(§1 원칙 5)"""
    by_id = {r["id"]: r for r in rows}
    pairs = {}
    for v in conn.execute(
        "SELECT left_classification_id, right_classification_id, choice FROM ax_blind_votes"
    ):
        left, right = by_id[v[0]], by_id[v[1]]
        call = calls.get(left["call_uid"])
        if not call or not call["blind"] or call["split"] != split:
            continue
        if any(x["fallback"] or x["status"] != "ok" for x in (left, right)):
            continue
        sides = {
            "left": (left["prompt_version"], left["model_actual"]),
            "right": (right["prompt_version"], right["model_actual"]),
        }
        key = tuple(sorted(sides.values(), key=lambda s: (version_key(s[0]), s[1])))
        p = pairs.setdefault(
            key,
            {
                "sides": [{"version": s[0], "model": s[1], "wins": 0} for s in key],
                "tie": 0,
                "both_wrong": 0,
                "left": 0,
                "right": 0,
            },
        )
        if v[2] in ("left", "right"):
            p["sides"][key.index(sides[v[2]])]["wins"] += 1
        p[v[2]] += 1
    return [pairs[k] for k in sorted(pairs, key=lambda k: [(version_key(s[0]), s[1]) for s in k])]
