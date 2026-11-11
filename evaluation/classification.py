"""① 통화 분류 채점 — 평가 설계 §2.2(지표·정확도 셋), §2.4(블라인드), §2.6(판정기), §1 원칙 5(폴백 제외)·§7(few-shot 제외·dev 덤프).
입력은 앱이 쓴 ax_call_classifications·ax_model_judgments·ax_blind_votes·ax_model_blind_votes·ax_llm_calls(칸은 shared/app_schema.sql)와
truth_call_labels. 사람 판정(ax_judgments)은 운영 감사라 읽지 않는다(평가 설계 §2.3)."""

import json
import re
from collections import Counter
from itertools import combinations
from pathlib import Path

import yaml

from backend.prompts import FRONT_MATTER, PROMPTS_DIR
from evaluation import EvalRefused, call_baselines
from evaluation.metrics import inverse_weights, macro_accuracy, mcnemar_exact, weighted_accuracy
from evaluation.splits import silver_split
from generator.master import CALL_FLAGS
from shared.call_labels import differing_items, same_value

ITEMS = ("primary_purpose", "counterpart", "urgency") + CALL_FLAGS  # 9항목
PURPOSE = "primary_purpose"


def version_key(v: str) -> list:
    """버전 자연 정렬 — v2 < v10"""
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", v)]


def _calls(conn, hard: bool = False) -> dict[str, dict]:
    """생성 통화 + 골드(붙여넣기 통화는 골드가 없어 빠진다). 어려운 통화(split hard)는 기본으로 뺀다 — dev·test 채점·가중
    모집단·골드 점검에 섞이지 않게(어려운 통화 설계 §2). hard=True면 어려운 통화만(종류·절반 포함)"""
    rows = conn.execute(
        "SELECT c.call_uid, c.in_eval_pool, c.in_blind_pool, c.transcript, l.split, h.hard_type, h.half, "
        + ", ".join(f"l.{i}" for i in ITEMS)
        + " FROM source.ax_calls c JOIN truth.truth_call_labels l ON l.call_uid = c.call_uid"
        " LEFT JOIN truth.truth_hard_calls h ON h.call_uid = c.call_uid"
        " WHERE c.source = 'generated'"  # source. — app DB에 같은 이름 표가 있으면 SQLite는 main을 먼저 고른다
         + (" AND l.split = 'hard'" if hard else " AND l.split != 'hard'")
    ).fetchall()
    return {
        r["call_uid"]: {
            "uid": r["call_uid"],
            "pool": r["in_eval_pool"],
            "blind": r["in_blind_pool"],
            "split": r["split"],
            "transcript": r["transcript"],
            "gold": {i: r[i] for i in ITEMS},
            "hard_type": r["hard_type"],
            "half": r["half"],
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


def _score(
    ids, pred: dict, ref: dict, calls: dict, population: Counter, drop: set | None = None
) -> dict:
    """ids의 통화를 pred vs ref로 — 항목별 k/n(급함은 urgent 여부만 — shared.call_labels.same_value), 목적 Strict는 역확률 가중·카테고리 평균 병기(카테고리 = 골드 목적).
    drop = 모수에서 뺄 (통화, 항목) — 골드 점검의 골드 오류 추정(평가 설계 §2.3, dev만)"""
    ids = sorted(i for i in ids if i in pred and i in ref)
    drop = drop or set()
    keep = {i: [c for c in ids if (c, i) not in drop] for i in ITEMS}
    items = {
        i: {"k": sum(same_value(i, pred[c][i], ref[c][i]) for c in keep[i]), "n": len(keep[i])}
        for i in ITEMS
    }
    rows = [
        (calls[c]["gold"][PURPOSE], pred[c][PURPOSE] == ref[c][PURPOSE]) for c in keep[PURPOSE]
    ]
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


def _judgments(conn) -> list[dict]:
    """판정기(모델 B) 판정 — 칸은 분류와 같다(앱 설계 §1)"""
    rows = conn.execute(
        "SELECT j.call_uid, j.prompt_version, j.model_alias, j.model_actual, j.status, j.labels,"
        " lc.fallback_used AS fallback FROM ax_model_judgments j"
        " JOIN ax_llm_calls lc ON lc.id = j.llm_call_id"
    ).fetchall()
    return [dict(r, labels=json.loads(r["labels"]) if r["labels"] else None) for r in rows]


def evaluate(
    conn,
    split: str,
    versions: list[str] | None = None,
    fewshot: dict | None = None,
    dump: bool = False,
    gold_drop: set | None = None,
) -> dict:
    """split = dev|test. versions 없으면 분류가 있는 버전 전부. fewshot = {버전: {call_uid}} — dev 집계에서 뺀다.
    사람 판정은 운영 구간 감사라 채점하지 않는다(평가 설계 §2.3)"""
    calls = _calls(conn)
    population = Counter(c["gold"][PURPOSE] for c in calls.values())
    gold_ids = _gold_ids(calls, split)
    pool_ids = {i for i, c in calls.items() if c["pool"] and c["split"] == split}
    gold = {i: c["gold"] for i, c in calls.items()}
    rows = _classifications(conn)
    versions = versions or sorted({r["prompt_version"] for r in rows}, key=version_key)
    fewshot = fewshot or {}
    out = {"versions": {}}
    usable = {}
    for v in versions:
        mine = [r for r in rows if r["prompt_version"] == v and r["call_uid"] in calls]
        _one_model(v, mine)
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
            **(
                {"model_vs_gold_audited": _score(gold_ids, ok, gold, calls, population, gold_drop)}
                if gold_drop is not None
                else {}
            ),
            "excluded": {
                "fallback": sum(r["status"] == "ok" and bool(r["fallback"]) for r in in_gold),
                "failed": sum(r["status"] == "failed" for r in in_gold),
                "fewshot": sum(calls[r["call_uid"]]["uid"] in skip for r in in_gold),
            },
        }
    out["compare"] = [_compare(a, b, usable, gold_ids, gold) for a, b in combinations(versions, 2)]
    # 강한 기준선(규칙·글자 n-gram)과 같은 통화에서 나란히 — 생성 녹취의 천장을 드러낸다(§2.2)
    out["baselines"] = call_baselines.evaluate(calls, split, usable)
    # 어려운 통화(hc-) — 같은 기준선·버전을 hard에서만(설계 §6). test 실행에는 넣지 않는다
    hard = _calls(conn, hard=True)
    if hard and split == "dev":
        hard_ok = {
            v: {
                r["call_uid"]: r["labels"]
                for r in rows
                if r["prompt_version"] == v
                and r["call_uid"] in hard
                and r["status"] == "ok"
                and not r["fallback"]
            }
            for v in versions
        }
        hard_ok = {
            v: ok for v, ok in hard_ok.items() if ok
        }  # hc-를 아직 분류하지 않은 버전은 뺀다
        out["hard"] = call_baselines.evaluate(calls, "hard", hard_ok, hard)
    out["blind"] = _blind(conn, rows, calls, split, versions)
    if dump:  # dev 판정 풀만 — 비풀(재료·검증)은 사람이 건별로 보지 않는다(평가 설계 §2.7)
        out["dump"] = [
            {
                "call_uid": calls[c]["uid"],
                "version": v,
                "item": i,
                "model": pred[c][i],
                "gold": gold[c][i],
                "transcript": calls[c]["transcript"],
            }
            for v, pred in usable.items()
            for c in sorted(pool_ids & pred.keys())
            for i in ITEMS
            if not same_value(i, pred[c][i], gold[c][i])
        ]
    return out


def _gold_ids(calls: dict, split: str) -> set:
    """골드로 재는 통화 — dev = 비풀 + dev 판정 풀, test = test 판정 풀만(평가 설계 §1 원칙 2)"""
    return {
        i
        for i, c in calls.items()
        if (c["split"] in ("silver", "dev") if split == "dev" else c["split"] == "test")
    }


def _usable(rows: list[dict], v: str, calls: dict) -> dict:
    mine = [r for r in rows if r["prompt_version"] == v and r["call_uid"] in calls]
    _one_model(v, mine)
    return {r["call_uid"]: r for r in mine if r["status"] == "ok" and not r["fallback"]}


TRAINED_ON = ("silver_material",)  # 판정기 프롬프트 머리말 trained_on 값


def trained_uids(conn, version: str, prompts_dir: Path = PROMPTS_DIR) -> set[str]:
    """판정기가 학습한 통화 — prompts/judge/<version>.md 머리말 trained_on. silver_material = silver_split 재료 구간 전부
    (학습에서 원본 오류로 빠진 통화도 함께). 파일·머리말·키가 없으면 빈 집합(Qwen 판정기 설계 '학습한 통화 빼기', 평가 설계 §2.5)"""
    path = prompts_dir / "judge" / f"{version}.md"
    m = FRONT_MATTER.match(path.read_text(encoding="utf-8")) if path.exists() else None
    value = (yaml.safe_load(m[1]) or {}).get("trained_on") if m else None
    if value is None:
        return set()
    if value not in TRAINED_ON:
        raise EvalRefused(f"{path}: trained_on {value!r}는 {TRAINED_ON} 중 하나여야 함")
    calls = _calls(conn)
    silver = [
        {"call_uid": u, **{k: c["gold"][k] for k in (PURPOSE, "emergency", "complaint")}}
        for u, c in calls.items()
        if c["split"] == "silver"
    ]
    return {u for u, part in silver_split(silver).items() if part == "material"}


def judge_skip(conn, versions: list[str], prompts_dir: Path = PROMPTS_DIR) -> set[str]:
    """판정기 버전들이 학습한 통화의 합 — 판정기끼리 같은 모수로 비교하려고 모든 판정기 쪽에서 뺀다(비교 규칙)"""
    return set().union(*(trained_uids(conn, v, prompts_dir) for v in versions))


def judge_versions(conn) -> list[str]:
    rows = conn.execute("SELECT DISTINCT prompt_version FROM ax_model_judgments")
    return sorted((r[0] for r in rows), key=version_key)


def evaluate_judge(
    conn,
    split: str,
    versions: list[str] | None = None,
    gold_drop: set | None = None,
    skip: set | None = None,
) -> dict:
    """판정기 vs 골드(평가 설계 §2.6) — 판정기 버전별, 분할은 모델 vs 골드와 같다. skip = 판정기 학습 통화(dev에서만 뺀다)"""
    calls = _calls(conn)
    population = Counter(c["gold"][PURPOSE] for c in calls.values())
    gold = {i: c["gold"] for i, c in calls.items()}
    rows = _judgments(conn)
    ids = _gold_ids(calls, split) - ((skip or set()) if split == "dev" else set())
    out = {}
    for v in versions or sorted({r["prompt_version"] for r in rows}, key=version_key):
        ok = _usable(rows, v, calls)
        pred = {c: r["labels"] for c, r in ok.items()}
        out[v] = {
            "model_actual": sorted({r["model_actual"] for r in rows if r["prompt_version"] == v}),
            "judge_vs_gold": _score(ids, pred, gold, calls, population),
        }
        if gold_drop is not None:
            out[v]["judge_vs_gold_audited"] = _score(ids, pred, gold, calls, population, gold_drop)
    return out


def judge_version(conn) -> str | None:
    """교차검증에 쓸 판정기 버전 — 하나뿐이면 그것, 여럿이면 ax_settings.prompt_versions의 judge(앱 설계 §6)"""
    versions = {
        r[0] for r in conn.execute("SELECT DISTINCT prompt_version FROM ax_model_judgments")
    }
    if len(versions) == 1:
        return versions.pop()
    row = conn.execute("SELECT value FROM ax_settings WHERE key = 'prompt_versions'").fetchone()
    return json.loads(row[0]).get("judge") if row else None


CORE = (PURPOSE, "urgency", "emergency")  # 핵심 엇갈림 — 앱 설계 §1 is_core


def crosscheck(
    conn, split: str, a: str, b: str, fewshot: set | None = None, skip: set | None = None
) -> dict:
    """모델 2개 교차검증(평가 설계 §2.5) — 통화 단위로 9항목 중 하나라도 A ≠ B면 엇갈림(급함은 urgent 여부만 — A 오류도 같은 비교), 양쪽 ok·폴백 아닌 골드 통화만.
    fewshot = A의 예시 call_uid, skip = 판정기 학습 통화(judge_skip) — 둘 다 dev 집계에서 뺀다(§7, §2.5 비교 규칙).
    일치 오류 = 엇갈림 없는 통화 중 골드와 다른 것, 포착 = A가 틀린 통화 중 엇갈림이 붙은 것, 향상 배수 = 포착률 ÷ 엇갈림 비율.
    가중은 골드 목적 카테고리의 역확률(§2.2)"""
    calls = _calls(conn)
    rows_a = _usable(_classifications(conn), a, calls)
    rows_b = _usable(_judgments(conn), b, calls)
    models_a = {r["model_actual"] for r in rows_a.values()}
    if models_a and models_a == {r["model_actual"] for r in rows_b.values()}:
        return {"a": a, "b": b, "skipped": "모델 A·B가 같음"}
    drop = ((fewshot or set()) | (skip or set())) if split == "dev" else set()
    ids = sorted((_gold_ids(calls, split) & rows_a.keys() & rows_b.keys()) - drop)
    cat = {c: calls[c]["gold"][PURPOSE] for c in ids}
    weights = inverse_weights(
        Counter(c["gold"][PURPOSE] for c in calls.values()), Counter(cat.values())
    )
    la = {c: rows_a[c]["labels"] for c in ids}
    lb = {c: rows_b[c]["labels"] for c in ids}
    diff = {c: differing_items(la[c], lb[c]) for c in ids}
    differs = {c for c in ids if diff[c]}
    wrong = {c for c in ids if differing_items(la[c], calls[c]["gold"])}

    def rate(k_ids: set, n_ids) -> dict:
        n_ids = list(n_ids)
        w = sum(weights[cat[c]] for c in n_ids)
        return {
            "k": len(k_ids),
            "n": len(n_ids),
            "weighted": sum(weights[cat[c]] for c in k_ids) / w if w else None,
        }

    agreed = [c for c in ids if c not in differs]
    disagree = rate(differs, ids)
    catch = rate(differs & wrong, wrong)
    lift = (
        (catch["k"] / catch["n"]) / (disagree["k"] / disagree["n"])
        if catch["n"] and disagree["k"]
        else None
    )
    return {
        "a": a,
        "b": b,
        "n": len(ids),
        "disagree": disagree,
        "core": rate({c for c in differs if set(diff[c]) & set(CORE)}, ids),
        "agree_error": rate(wrong & set(agreed), agreed),
        "catch": catch,
        "lift": lift,
    }


def _one_model(v: str, mine: list[dict]) -> None:
    if len({r["model_alias"] for r in mine}) > 1:
        raise EvalRefused(
            f"{v}: 한 버전에 모델 별칭이 둘 이상 — 버전은 (프롬프트 × 모델) 하나다(앱 설계 §1), 판정기는 ax_model_judgments에"
        )


def _compare(a: str, b: str, usable: dict, ref_ids: set, ref: dict) -> dict:
    """버전 비교 — 두 버전 모두 쓸 수 있는 통화 교집합으로만(평가 설계 §2.2), 골드 기준 항목별 맞힌 수와 McNemar p"""
    ids = sorted(ref_ids & usable[a].keys() & usable[b].keys())
    items = {}
    for i in ITEMS:
        ra = [same_value(i, usable[a][c][i], ref[c][i]) for c in ids]
        rb = [same_value(i, usable[b][c][i], ref[c][i]) for c in ids]
        only_a = sum(x and not y for x, y in zip(ra, rb, strict=True))
        only_b = sum(y and not x for x, y in zip(ra, rb, strict=True))
        items[i] = {"a": sum(ra), "b": sum(rb), "p": mcnemar_exact(only_a, only_b)}
    return {"a": a, "b": b, "n": len(ids), "items": items}


def _blind(conn, rows: list[dict], calls: dict, split: str, versions: list[str]) -> dict:
    """블라인드 — LLM 쌍 비교(판정기 버전별)와 사람 투표(최종 비교)를 따로, 좌우 (prompt_version, model_actual) 쌍별로
    (평가 설계 §2.4). 승수는 그 쪽에 돌리고 좌/우 선택 비율 병기. 한쪽이라도 폴백·실패 분류이거나 쌍 비교 호출이
    폴백이면 뺀다(§1 원칙 5). test는 versions가 낀 쌍만(그 버전의 test 묶음). agreement = 쌍 비교 판정기 버전별로,
    같은 두 분류에 사람도 고른 건 중 이긴 쪽(또는 비슷·둘 다 틀림)이 같은 수"""
    by_id = {r["id"]: r for r in rows}
    llm = conn.execute(
        "SELECT v.left_classification_id, v.right_classification_id, v.choice,"
        " v.model_alias || '/' || v.prompt_version AS judge FROM ax_model_blind_votes v"
        " JOIN ax_llm_calls a ON a.id = v.first_llm_call_id"
        " JOIN ax_llm_calls b ON b.id = v.second_llm_call_id"
        " WHERE a.fallback_used = 0 AND b.fallback_used = 0"
    ).fetchall()
    human = conn.execute(
        "SELECT left_classification_id, right_classification_id, choice, NULL AS judge"
        " FROM ax_blind_votes"
    ).fetchall()
    out = {}
    picks = {}
    for name, votes in (("llm", llm), ("human", human)):
        pairs = {}
        for v in votes:
            left, right = by_id[v[0]], by_id[v[1]]
            call = calls.get(left["call_uid"])
            if not call or not call["blind"] or call["split"] != split:
                continue
            if any(x["fallback"] or x["status"] != "ok" for x in (left, right)):
                continue
            if split == "test" and not {left["prompt_version"], right["prompt_version"]} & set(
                versions
            ):
                continue
            sides = {
                "left": (left["prompt_version"], left["model_actual"]),
                "right": (right["prompt_version"], right["model_actual"]),
            }
            key = (v[3],) + tuple(sorted(sides.values(), key=lambda s: (version_key(s[0]), s[1])))
            p = pairs.setdefault(
                key,
                {
                    "sides": [{"version": s[0], "model": s[1], "wins": 0} for s in key[1:]],
                    "tie": 0,
                    "both_wrong": 0,
                    "left": 0,
                    "right": 0,
                }
                | ({"judge": v[3]} if v[3] else {}),
            )
            if v[2] in ("left", "right"):
                p["sides"][key[1:].index(sides[v[2]])]["wins"] += 1
            p[v[2]] += 1
            pick = {"left": v[0], "right": v[1]}.get(v[2], v[2])
            picks.setdefault(frozenset((v[0], v[1])), {})[v[3] or name] = pick
        out[name] = [
            pairs[k]
            for k in sorted(
                pairs, key=lambda k: (k[0] or "", [(version_key(s[0]), s[1]) for s in k[1:]])
            )
        ]
    both = [p for p in picks.values() if "human" in p]
    judges = sorted({j for p in both for j in p if j != "human"}, key=version_key)
    out["agreement"] = [
        {
            "judge": j,
            "k": sum(p[j] == p["human"] for p in both if j in p),
            "n": sum(j in p for p in both),
        }
        for j in judges
    ]
    return out
