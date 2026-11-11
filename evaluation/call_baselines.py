"""① 분류 강한 기준선 (평가 설계 §2.2, §1 원칙 8). 생성 녹취는 라벨을 정해진 말로 드러내므로 LLM 분류기의 정확도는
이 기준선과 나란히 봐야 뜻이 있다.
- 학습 없는 규칙: 직원 줄에 회신·연락·전달 약속이 있으면 할 일 있음(`has_todo` — 분류 프롬프트의 정의 그대로)
- 학습 기준선: 글자 n-gram TF-IDF + 로지스틱 회귀(scikit-learn). silver 재료 구간(evaluation/splits.py)으로만 학습
채점은 재료 밖 골드 통화(dev = 재료 밖 silver + dev 판정 풀, test = test 판정 풀)에서, 분류기 버전도 같은 통화로 센다.
급함은 urgent 여부만(shared.call_labels.same_value)."""

import re

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline

from evaluation.splits import silver_split
from shared.call_labels import ITEMS, same_value

NGRAM = (2, 4)  # 글자 n-gram 길이 범위(양 끝 포함)
STAFF = re.compile(r"^직원:(.*)$", re.M)
# 할 일 약속 — 분류 프롬프트 has_todo 정의("회신·확인 후 연락을 약속")의 말
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


def evaluate(calls: dict, split: str, usable: dict) -> dict:
    """calls = classification._calls 모양, usable = {버전: {call_uid: 라벨}}(분류기 채점과 같은 폴백·실패 제외).
    → {trained_on, n, rule: {has_todo}, ngram: {항목: k/n}, versions: {버전: {항목: k/n}}}"""
    silver = [
        {"call_uid": u, **{k: c["gold"][k] for k in ("primary_purpose", "emergency", "complaint")}}
        for u, c in calls.items()
        if c["split"] == "silver"
    ]
    material = {u for u, part in silver_split(silver).items() if part == "material"}
    ids = sorted(
        u
        for u, c in calls.items()
        if u not in material
        and (c["split"] in ("silver", "dev") if split == "dev" else c["split"] == "test")
    )
    models = {
        i: train([(calls[u]["transcript"], calls[u]["gold"][i]) for u in material]) for i in ITEMS
    }

    def kn(pred) -> dict:
        return {"k": sum(pred(u) for u in ids), "n": len(ids)}

    return {
        "trained_on": len(material),
        "n": len(ids),
        "rule": {
            "has_todo": kn(
                lambda u: rule_has_todo(calls[u]["transcript"]) == calls[u]["gold"]["has_todo"]
            )
        },
        "ngram": {
            i: kn(
                lambda u, i=i: same_value(
                    i, predict(models[i], calls[u]["transcript"]), calls[u]["gold"][i]
                )
            )
            for i in ITEMS
        },
        "versions": {
            v: {
                i: {
                    "k": sum(same_value(i, ok[u][i], calls[u]["gold"][i]) for u in ids if u in ok),
                    "n": sum(u in ok for u in ids),
                }
                for i in ITEMS
            }
            for v, ok in usable.items()
        },
    }
