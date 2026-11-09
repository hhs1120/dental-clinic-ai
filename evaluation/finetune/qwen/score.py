"""Qwen 비교 채점과 교체 기준 — 분류기 설계 "Qwen 비교".
실패(파싱 실패·폴백·결과 없음)는 모든 모델에서 오답으로 세고 건수를 적는다. 급함은 3값과 urgent 여부를 함께 낸다(평가 설계 §2.5).
검증 구간은 비가중 k/n(관문과 같다, 평가 설계 §2.7), dev·문체 dev는 역확률 가중(평가 설계 §2.2)을 주 수치로.
"""

from collections import Counter

from evaluation.finetune.qwen.data import SETS, STYLES
from evaluation.metrics import inverse_weights, macro_accuracy, weighted_accuracy
from shared.call_labels import FLAGS, ITEMS

MARGIN_VALIDATION = 0.02  # 교체 기준 1·2 — 지금 분류기 라벨 모델보다 이만큼 넘게 낮으면 탈락
MARGIN_STYLE = 0.05  # 교체 기준 3 — 문체 dev 최저 벌이 원본 dev보다 이만큼 넘게 떨어지면 탈락
PURPOSE = "primary_purpose"


def score_set(pred: dict, rows: list[dict], population: Counter | None) -> dict:
    """rows의 gold vs pred[id] — 항목별 맞은 수, urgent 여부, 플래그 평균, 목적 Strict(population이 있으면 가중·카테고리 평균)"""
    n = len(rows)
    got = [(r["gold"], pred.get(r["id"])) for r in rows]
    items = {i: sum(p is not None and p[i] == g[i] for g, p in got) for i in ITEMS}
    urgent = sum(p is not None and (p["urgency"] == "urgent") == (g["urgency"] == "urgent")
                 for g, p in got)  # fmt: skip
    cats = [(g[PURPOSE], p is not None and p[PURPOSE] == g[PURPOSE]) for g, p in got]
    sample = Counter(c for c, _ in cats)
    purpose = {"k": items[PURPOSE], "strict": items[PURPOSE] / n if n else None}
    if population is not None:
        purpose |= {
            "weighted": weighted_accuracy(cats, inverse_weights(population, sample)),
            "macro": macro_accuracy(cats),
            "by_category": {
                c: {"k": sum(ok for cc, ok in cats if cc == c), "n": k}
                for c, k in sorted(sample.items())
            },
        }
    return {
        "n": n,
        "failed": sum(p is None for _, p in got),
        "items": items,
        "urgent": urgent,
        "flags_mean": sum(items[f] for f in FLAGS) / (len(FLAGS) * n) if n else None,
        "purpose": purpose,
    }


def check_coverage(preds: dict[str, dict], rows: list[dict]) -> None:
    """모델마다 평가 행 id가 예측에 다 있는지(값 None = 실패는 있음으로 본다) — 일부만 낸 파일이 실패로 섞여 채점되지 않게"""
    ids = {r["id"] for r in rows}
    short = {m: len(ids - set(p)) for m, p in preds.items() if ids - set(p)}
    if short:
        raise SystemExit(f"예측이 평가 셋을 다 덮지 않는다(빠진 건수): {short}")


def criteria(qwen: dict[str, dict], ref_validation: dict) -> dict:
    """교체 기준 3개(학습 전 고정) — qwen은 셋 이름 → score_set 결과, ref_validation은 지금 분류기 라벨 모델의 검증 구간"""
    q, r = qwen["validation"], ref_validation
    c1 = q["purpose"]["strict"] >= r["purpose"]["strict"] - MARGIN_VALIDATION
    c2 = q["flags_mean"] >= r["flags_mean"] - MARGIN_VALIDATION
    worst = min(STYLES, key=lambda s: qwen[s]["purpose"]["weighted"])
    dev = qwen["dev"]["purpose"]["weighted"]
    c3 = qwen[worst]["purpose"]["weighted"] >= dev - MARGIN_STYLE
    return {
        "c1": {"pass": c1, "qwen": q["purpose"]["strict"], "ref": r["purpose"]["strict"]},
        "c2": {"pass": c2, "qwen": q["flags_mean"], "ref": r["flags_mean"]},
        "c3": {
            "pass": c3,
            "worst_style": worst,
            "worst": qwen[worst]["purpose"]["weighted"],
            "dev": dev,
        },
        "all": c1 and c2 and c3,
    }


def _pct(x) -> str:
    return "—" if x is None else f"{x:.3f}"


def report(results: dict[str, dict[str, dict]], crit: dict[str, dict]) -> str:
    """모델 × 셋 표와 Qwen 모델별 교체 기준 통과표(마크다운) — 실험 기록에 붙인다"""
    lines = [
        "| 모델 | 셋 | 목적 k/n | 목적 가중 | 급함 urgent 여부 | 급함 3값 | 플래그 평균 | 실패 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for model, sets in results.items():
        for s in SETS:
            if s not in sets:
                continue
            x = sets[s]
            lines.append(
                f"| {model} | {s} | {x['purpose']['k']}/{x['n']} | "
                f"{_pct(x['purpose'].get('weighted'))} | {x['urgent']}/{x['n']} | "
                f"{x['items']['urgency']}/{x['n']} | {_pct(x['flags_mean'])} | {x['failed']} |"
            )
    lines += [
        "",
        "| 모델 | ① 검증 목적 | ② 검증 플래그 | ③ 문체 최저 | 교체 |",
        "|---|---|---|---|---|",
    ]
    for model, c in crit.items():
        mark = {True: "통과", False: "탈락"}
        lines.append(
            f"| {model} | {mark[c['c1']['pass']]} {_pct(c['c1']['qwen'])} vs {_pct(c['c1']['ref'])} | "
            f"{mark[c['c2']['pass']]} {_pct(c['c2']['qwen'])} vs {_pct(c['c2']['ref'])} | "
            f"{mark[c['c3']['pass']]} {c['c3']['worst_style']} {_pct(c['c3']['worst'])} vs dev "
            f"{_pct(c['c3']['dev'])} | {mark[c['all']]} |"
        )
    return "\n".join(lines) + "\n"
