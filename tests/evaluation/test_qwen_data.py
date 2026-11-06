"""Qwen 학습·평가 셋 (evaluation/finetune/qwen/data.py, 분류기 설계 "Qwen 비교", AI 실험 기록 §8)."""

import json
import random

import pytest

from evaluation import splits
from evaluation.finetune.qwen import data
from shared.call_labels import FLAGS, ITEMS


def _labels(uid, rng, split="silver"):
    return {
        "call_uid": uid,
        "split": split,
        "direction": "inbound",
        "transcript": f"직원: 네.\n환자: {uid}",
        "primary_purpose": rng.choice(["book", "cancel", "complaint", "emergency", "unknown"]),
        "counterpart": "patient",
        "urgency": rng.choice(["urgent", "normal", "low"]),
        **{f: rng.randint(0, 1) for f in FLAGS},
    }


def _fixture(n=200):
    rng = random.Random(0)
    silver = [_labels(f"c-{i:04d}", rng) for i in range(n)]
    wrong = {r["call_uid"] for r in silver[::17]}
    aug = [
        {
            "aug_id": f"{r['call_uid']}-s{k}",
            "call_uid": r["call_uid"],
            "style": f"s{k}",
            "direction": r["direction"],
            "transcript": f"바꿈 {k} {r['call_uid']}",
            "original_wrong": r["call_uid"] in wrong,
            "train": k == 1 or i % 3 != 0,  # 일부는 train=false
        }
        for i, r in enumerate(silver)
        for k in (1, 2)
    ]
    return silver, aug, wrong


def _uids(rows):
    return {r["call_uid"] for r in rows}


def test_no_validation_calls():
    silver, aug, _ = _fixture()
    split = splits.silver_split(silver)
    sets = data.build_sets(silver, aug)
    for name in ("full", "half", "heldout"):
        assert all(split[u] == "material" for u in _uids(sets[name])), name


def test_original_wrong_excluded():
    silver, aug, wrong = _fixture()
    sets = data.build_sets(silver, aug)
    for rows in sets.values():
        assert not _uids(rows) & wrong


def test_only_train_variants():
    silver, aug, _ = _fixture()
    train_ids = {a["aug_id"] for a in aug if a["train"]}
    originals = {r["call_uid"] for r in silver}
    for rows in data.build_sets(silver, aug).values():
        assert {r["id"] for r in rows} - originals <= train_ids


def test_grouped_by_call():
    silver, aug, _ = _fixture()
    sets = data.build_sets(silver, aug)
    assert not _uids(sets["heldout"]) & (_uids(sets["full"]) | _uids(sets["half"]))
    assert _uids(sets["half"]) <= _uids(sets["full"])
    # 원본이 있으면 그 통화의 train 변형도 같은 셋에
    for rows in sets.values():
        ids = {r["id"] for r in rows}
        for a in aug:
            if a["train"] and a["call_uid"] in _uids(rows):
                assert a["aug_id"] in ids


def test_shares():
    silver, aug, wrong = _fixture(1000)
    split = splits.silver_split(silver)
    material = [u for u, s in split.items() if s == "material" and u not in wrong]
    sets = data.build_sets(silver, aug)
    strata = 16  # 층 수 상한(희귀 묶음 4 × emergency × complaint) — 층마다 반올림 오차 0.5 이하
    held = len(_uids(sets["heldout"]))
    assert abs(held - len(material) * data.HELDOUT_SHARE) <= strata
    rest = len(material) - held
    assert abs(len(_uids(sets["half"])) - rest * data.HALF_SHARE) <= strata
    assert len(_uids(sets["full"])) == rest


def test_rejects_non_silver():
    silver, aug, _ = _fixture()
    silver[0] = dict(silver[0], split="dev")
    with pytest.raises(ValueError):
        data.build_sets(silver, aug)


def test_deterministic():
    silver, aug, _ = _fixture()
    assert data.build_sets(silver, aug) == data.build_sets(silver, aug)


def test_eval_sets():
    rng = random.Random(1)
    silver, _, _ = _fixture()
    dev = [_labels(f"d-{i:04d}", rng, "dev") for i in range(5)]
    style = [
        {"aug_id": f"{d['call_uid']}-s{k}", "call_uid": d["call_uid"], "style": f"s{k}",
         "direction": d["direction"], "transcript": f"문체 {k}", "kept": True}
        for d in dev
        for k in (1, 2, 3)
    ]  # fmt: skip
    sets = data.eval_sets(silver, dev, style)
    split = splits.silver_split(silver)
    assert _uids(sets["validation"]) == {u for u, s in split.items() if s == "validation"}
    assert len(sets["dev"]) == 5 and all(len(sets[s]) == 5 for s in ("s1", "s2", "s3"))
    gold = {d["call_uid"]: {i: d[i] for i in ITEMS} for d in dev}
    for r in sets["s1"]:
        assert r["gold"] == gold[r["call_uid"]] and r["transcript"] == "문체 1"


def _example():
    rng = random.Random(2)
    return {i: v for i, v in _labels("c-0001", rng).items() if i in ITEMS}


def test_target_json_order():
    out = json.loads(data.target_json(_example()))
    assert list(out) == list(ITEMS)
    assert all(type(out[f]) is int and out[f] in (0, 1) for f in FLAGS)


def test_parse_labels_extra_text():
    labels = _example()
    text = "<think>\n</think>\n```json\n" + data.target_json(labels) + "\n```\n끝"
    assert data.parse_labels(text) == labels


def test_parse_labels_bool_flags():
    labels = _example()
    raw = json.dumps({**labels, **{f: bool(labels[f]) for f in FLAGS}})
    out = data.parse_labels(raw)
    assert out == labels and all(type(out[f]) is int for f in FLAGS)


@pytest.mark.parametrize(
    "broken",
    [
        lambda d: {k: v for k, v in d.items() if k != "has_todo"},
        lambda d: {**d, "primary_purpose": "없는값"},
        lambda d: {**d, "emergency": 2},
    ],
)
def test_parse_labels_invalid(broken):
    assert data.parse_labels(json.dumps(broken(_example()))) is None


def test_parse_labels_not_json():
    assert data.parse_labels("모르겠습니다") is None
    assert data.parse_labels("{깨진") is None


def test_mlx_row():
    row = {"direction": "inbound", "transcript": "직원: 네.", "labels": _example()}
    msgs = data.mlx_row(row)["messages"]
    assert [m["role"] for m in msgs] == ["system", "user", "assistant"]
    assert msgs[0]["content"] == data.INSTRUCTION
    assert msgs[1]["content"] == "방향: inbound\n\n직원: 네."
    assert msgs[2]["content"] == data.target_json(row["labels"])
