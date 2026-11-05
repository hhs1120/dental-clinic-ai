"""silver(비풀) 재료·검증 분할 — 평가 설계 §2.7, 분류기 설계 "Qwen 비교". 자동 개선과 Qwen 학습이 같이 쓴다.
층 키 (희귀 목적 묶음, emergency, complaint) — 희귀는 generator/master.py EVAL_RARE_PURPOSES, 나머지 목적은 other.
층 안에서 sha256(call_uid) 순으로 놓고 앞쪽 round(n × SILVER_MATERIAL_SHARE)가 재료
"""

import hashlib
from collections import defaultdict

from evaluation.metrics import SILVER_MATERIAL_SHARE
from generator.master import EVAL_RARE_PURPOSES as RARE


def silver_split(labels: list[dict]) -> dict[str, str]:
    """labels: truth_call_labels silver 행(call_uid·primary_purpose·emergency·complaint) → {call_uid: material | validation}"""
    strata = defaultdict(list)
    for r in labels:
        group = r["primary_purpose"] if r["primary_purpose"] in RARE else "other"
        strata[(group, int(r["emergency"]), int(r["complaint"]))].append(r["call_uid"])
    out = {}
    for uids in strata.values():
        uids.sort(key=lambda u: hashlib.sha256(u.encode()).hexdigest())
        cut = round(len(uids) * SILVER_MATERIAL_SHARE)
        out.update({u: "material" if i < cut else "validation" for i, u in enumerate(uids)})
    return out
