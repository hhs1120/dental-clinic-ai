"""Qwen 학습·평가 셋 — 분류기 설계 "Qwen 비교", 재료 경위는 AI 실험 기록 §8.
학습: silver 재료 구간(evaluation/splits.py silver_split) 원본 중 original_wrong이 아닌 것 + 그 원본의 train 변형(aug.jsonl).
재료 구간에서 HELDOUT_SHARE를 떼어 mlx-lm valid(손실 확인·체크포인트 고르기)로, 나머지 전부(full)와 그 절반(half)으로 두 번 학습한다.
검증 구간·dev·test 녹취록과 문체 dev(dev_eval.jsonl)는 학습에 쓰지 않는다. 평가: 검증 구간 silver, dev, 문체 dev s1~s3.
"""

import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

from backend.classify import user_message, valid_labels
from evaluation.splits import silver_split
from generator.master import EVAL_RARE_PURPOSES as RARE
from shared.call_labels import COUNTERPARTS, FLAGS, ITEMS, PURPOSES, URGENCIES

HELDOUT_SHARE = (
    0.1  # 재료 구간 중 valid로 떼는 몫 — 검증 구간은 교체 기준 채점 셋이라 학습 중 보지 않는다
)
HALF_SHARE = 0.5  # 재료 충분성 비교(분류기 설계 "Qwen 비교")
STYLES = ("s1", "s2", "s3")  # 문체 dev 벌(AI 실험 기록 §8)

HERE = Path(__file__).parent
AUG = HERE.parent / "aug"
DATA = HERE / "data"

INSTRUCTION = "\n".join(
    [
        "치과 통화 녹취록을 읽고 라벨 9항목을 아래 순서의 JSON 한 줄로만 답한다.",
        f"- primary_purpose: {'|'.join(PURPOSES)}",
        f"- counterpart: {'|'.join(COUNTERPARTS)}",
        f"- urgency: {'|'.join(URGENCIES)}",
        f"- {', '.join(FLAGS)}: 0 또는 1",
    ]
)


def target_json(labels: dict) -> str:
    """학습 정답 — ITEMS 순서, 플래그는 0/1"""
    out = {i: int(labels[i]) if i in FLAGS else labels[i] for i in ITEMS}
    return json.dumps(out, ensure_ascii=False, separators=(",", ":"))


def parse_labels(text: str) -> dict | None:
    """모델 출력의 첫 JSON 객체 → 라벨(플래그 0/1). 못 읽거나 9항목·값이 안 맞으면 None(채점에서 오답)"""
    m = re.search(r"\{.*?\}", text, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group())
    except json.JSONDecodeError:
        return None
    if isinstance(d, dict):
        d = {k: int(v) if k in FLAGS and type(v) is bool else v for k, v in d.items()}
    return d if valid_labels(d) else None


def _stratum(r: dict) -> tuple:
    """silver_split과 같은 층 키 (희귀 목적 묶음, emergency, complaint)"""
    group = r["primary_purpose"] if r["primary_purpose"] in RARE else "other"
    return (group, int(r["emergency"]), int(r["complaint"]))


def stratified_take(labels: list[dict], share: float, salt: str) -> set[str]:
    """층 안에서 sha256(salt + call_uid) 순 앞쪽 round(n × share)개의 call_uid"""
    strata = defaultdict(list)
    for r in labels:
        strata[_stratum(r)].append(r["call_uid"])
    out = set()
    for uids in strata.values():
        uids.sort(key=lambda u: hashlib.sha256((salt + u).encode()).hexdigest())
        out.update(uids[: round(len(uids) * share)])
    return out


def _gold(r: dict) -> dict:
    return {i: int(r[i]) if i in FLAGS else r[i] for i in ITEMS}


def build_sets(silver: list[dict], aug: list[dict]) -> dict[str, list[dict]]:
    """→ {full, half, heldout}: 행 {id, call_uid, direction, transcript, labels}. 같은 통화의 원본·변형은 한 셋에"""
    if any(r["split"] != "silver" for r in silver):
        raise ValueError("학습 재료는 silver만")
    split = silver_split(silver)
    wrong = {a["call_uid"] for a in aug if a["original_wrong"]}
    pool = [r for r in silver if split[r["call_uid"]] == "material" and r["call_uid"] not in wrong]
    heldout = stratified_take(pool, HELDOUT_SHARE, "heldout")
    rest = [r for r in pool if r["call_uid"] not in heldout]
    half = stratified_take(rest, HALF_SHARE, "half")
    by_uid = {r["call_uid"]: r for r in pool}

    def rows(uids: set) -> list[dict]:
        out = [
            {"id": u, "call_uid": u, "direction": by_uid[u]["direction"],
             "transcript": by_uid[u]["transcript"], "labels": _gold(by_uid[u])}
            for u in uids
        ]  # fmt: skip
        out += [
            {"id": a["aug_id"], "call_uid": a["call_uid"], "direction": a["direction"],
             "transcript": a["transcript"], "labels": _gold(by_uid[a["call_uid"]])}
            for a in aug
            if a["train"] and a["call_uid"] in uids
        ]  # fmt: skip
        return sorted(out, key=lambda r: r["id"])

    return {
        "full": rows({r["call_uid"] for r in rest}),
        "half": rows(half),
        "heldout": rows(heldout),
    }


def eval_sets(silver: list[dict], dev: list[dict], style: list[dict]) -> dict[str, list[dict]]:
    """→ {validation, dev, s1, s2, s3}: 행 {id, call_uid, direction, transcript, gold}. 문체 행의 gold는 원본 dev 골드"""
    split = silver_split(silver)

    def row(r, id_=None, transcript=None):
        return {"id": id_ or r["call_uid"], "call_uid": r["call_uid"], "direction": r["direction"],
                "transcript": transcript or r["transcript"], "gold": _gold(r)}  # fmt: skip

    by_uid = {r["call_uid"]: r for r in dev}
    out = {
        "validation": sorted(
            (row(r) for r in silver if split[r["call_uid"]] == "validation"),
            key=lambda r: r["id"],
        ),
        "dev": sorted((row(r) for r in dev), key=lambda r: r["id"]),
    }
    for s in STYLES:
        out[s] = sorted(
            (
                row(by_uid[v["call_uid"]], v["aug_id"], v["transcript"])
                for v in style
                if v["style"] == s and v["kept"]
            ),
            key=lambda r: r["id"],
        )
    return out


def mlx_row(row: dict) -> dict:
    """mlx-lm chat 형식 한 줄 — 지시·입력(분류기와 같은 user_message)·정답"""
    return {
        "messages": [
            {"role": "system", "content": INSTRUCTION},
            {"role": "user", "content": user_message(row["transcript"], row["direction"])},
            {"role": "assistant", "content": target_json(row["labels"])},
        ]
    }


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def load_rows(root: Path) -> tuple[list, list, list, list]:
    """→ (silver, dev, aug, 문체 dev). silver·dev만 SQL로 고른다 — test는 읽지 않는다"""
    conn = sqlite3.connect(f"file:{root}/data/source.sqlite?mode=ro", uri=True)
    conn.execute(f"ATTACH 'file:{root}/ground_truth/truth.sqlite?mode=ro' AS truth")
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT c.call_uid, c.direction, c.transcript, l.split, "
        + ", ".join(f"l.{i}" for i in ITEMS)
        + " FROM ax_calls c JOIN truth.truth_call_labels l USING (call_uid)"
        " WHERE c.source = 'generated' AND l.split IN ('silver', 'dev') ORDER BY c.call_uid"
    ).fetchall()
    conn.close()
    rows = [dict(r) for r in rows]
    return (
        [r for r in rows if r["split"] == "silver"],
        [r for r in rows if r["split"] == "dev"],
        _jsonl(AUG / "aug.jsonl"),
        _jsonl(AUG / "dev_eval.jsonl"),
    )


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


def write_all(root: Path) -> dict[str, int]:
    """data/{full,half}/{train,valid}.jsonl(valid = heldout) · data/eval.jsonl(행에 set) → 셋별 건수"""
    silver, dev, aug, style = load_rows(root)
    sets = build_sets(silver, aug)
    for name in ("full", "half"):
        _write(DATA / name / "train.jsonl", [mlx_row(r) for r in sets[name]])
        _write(DATA / name / "valid.jsonl", [mlx_row(r) for r in sets["heldout"]])
    evals = eval_sets(silver, dev, style)
    _write(DATA / "eval.jsonl", [dict(r, set=s) for s, rows in evals.items() for r in rows])
    counts = {k: len(v) for k, v in sets.items()} | {k: len(v) for k, v in evals.items()}
    counts |= {f"{k}_calls": len({r["call_uid"] for r in v}) for k, v in sets.items()}
    return counts
