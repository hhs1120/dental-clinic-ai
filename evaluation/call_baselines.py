"""① 분류 강한 기준선 (평가 설계 §2.2, §1 원칙 8). 생성 녹취는 라벨을 정해진 말로 드러내므로 LLM 분류기의 정확도는
이 기준선과 나란히 봐야 뜻이 있다.
- 학습 없는 규칙: 직원 줄에 회신·연락·전달 약속이 있으면 할 일 있음(`has_todo` — 분류 프롬프트의 정의 그대로)
- 학습 기준선: 글자 n-gram TF-IDF + 로지스틱 회귀(scikit-learn). silver 재료 구간(evaluation/splits.py)으로만 학습
채점은 재료 밖 골드 통화(dev = 재료 밖 silver + dev 판정 풀, test = test 판정 풀)에서, 분류기 버전도 같은 통화로 센다.
급함은 urgent 여부만(shared.call_labels.same_value)."""

import re
from collections import defaultdict

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline

from evaluation.splits import silver_split
from shared.call_labels import ITEMS, same_value

NGRAM = (2, 4)  # 글자 n-gram 길이 범위(양 끝 포함)
STAFF = re.compile(r"^직원:(.*)$", re.M)
# 할 일 약속 — 분류 프롬프트 has_todo 정의(prompts/classify/v2 "회신·전달·서류 준비 등")의 요지 중 말로 드러나는 것만
TODO = re.compile(
    r"(연락|전화)\s*(드릴|드리겠|드리도록|줄게|할게)|회신|전달(해|드리|하겠)|알려\s*드릴"
)


def rule_has_todo(transcript: str) -> int:
    """직원 줄에 회신·연락·전달 약속이 있으면 1"""
    return int(any(TODO.search(line) for line in STAFF.findall(transcript)))


def train(rows: list[tuple[str, object]]):
    """(글, 라벨) → 글자 n-gram TF-IDF + 로지스틱 회귀. 라벨이 하나뿐이면 그 값을 늘 낸다"""
    labels = {label for _, label in rows}
    if len(labels) == 1:
        return labels.pop()
    model = make_pipeline(
        TfidfVectorizer(analyzer="char_wb", ngram_range=NGRAM, min_df=2, sublinear_tf=True),
        LogisticRegression(
            C=10, max_iter=3000
        ),  # 약한 정규화 — 기본 C=1은 목적을 84%로 덜 맞혀 기준선이 약해진다
    )
    model.fit([t for t, _ in rows], [str(label) for _, label in rows])
    return model


def predict(model, text: str):
    if not hasattr(model, "predict"):
        return model
    out = model.predict([text])[0]
    return int(out) if out.isdigit() else out


def evaluate(calls: dict, split: str, usable: dict, hard: dict | None = None) -> dict:
    """calls = classification._calls 모양, usable = {버전: {call_uid: 라벨}}(분류기 채점과 같은 폴백·실패 제외).
    채점 통화는 재료 밖 골드 중 모든 버전이 쓸 수 있는 것 — 표의 열이 모두 같은 통화를 센다.
    split hard면 채점 통화는 hard(classification._calls(hard=True))에서, 재료는 그대로 silver(어려운 통화 설계 §6)이고
    종류별(by_type)·urgency_inverted 절반별(by_half) 같은 모양을 더한다.
    → {trained_on, n, ids, rule: {has_todo}, ngram: {항목: k/n}, versions: {버전: {항목: k/n}}}. 재료가 없으면 n 0"""
    silver = [
        {"call_uid": u, **{k: c["gold"][k] for k in ("primary_purpose", "emergency", "complaint")}}
        for u, c in calls.items()
        if c["split"] == "silver"
    ]
    material = sorted(u for u, part in silver_split(silver).items() if part == "material")
    pool = hard if split == "hard" else calls
    ids = sorted(
        u
        for u, c in pool.items()
        if u not in material
        and (
            split == "hard"
            or (c["split"] in ("silver", "dev") if split == "dev" else c["split"] == "test")
        )
        and all(u in ok for ok in usable.values())
    )
    if not material or not ids:
        return {
            "trained_on": len(material),
            "n": 0,
            "ids": [],
            "rule": {},
            "ngram": {},
            "versions": {},
        }
    models = {
        i: train([(calls[u]["transcript"], calls[u]["gold"][i]) for u in material]) for i in ITEMS
    }
    out = {"trained_on": len(material)} | _score(pool, ids, models, usable)
    if split == "hard":
        for key, name in (("hard_type", "by_type"), ("half", "by_half")):
            groups = defaultdict(list)
            for u in ids:
                if pool[u][key]:
                    groups[pool[u][key]].append(u)
            out[name] = {g: _score(pool, us, models, usable) for g, us in sorted(groups.items())}
    return out


def _score(pool: dict, ids: list[str], models: dict, usable: dict) -> dict:
    def kn(ok) -> dict:
        return {"k": sum(ok(u) for u in ids), "n": len(ids)}

    return {
        "n": len(ids),
        "ids": ids,
        "rule": {
            "has_todo": kn(
                lambda u: rule_has_todo(pool[u]["transcript"]) == pool[u]["gold"]["has_todo"]
            )
        },
        "ngram": {
            i: kn(
                lambda u, i=i: same_value(
                    i, predict(models[i], pool[u]["transcript"]), pool[u]["gold"][i]
                )
            )
            for i in ITEMS
        },
        "versions": {
            v: {
                i: kn(lambda u, i=i, ok=ok: same_value(i, ok[u][i], pool[u]["gold"][i]))
                for i in ITEMS
            }
            for v, ok in usable.items()
        },
    }
