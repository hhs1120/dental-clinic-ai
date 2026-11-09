"""Qwen 판정기 게이트웨이 확인 (Qwen 판정기 설계 '판정기 v3' 확인) — (가) 어댑터, (나) 게이트웨이 경로."""

import json

from evaluation.finetune.qwen import gateway_check as gc

A = '{"primary_purpose":"book","counterpart":"patient","urgency":"normal","emergency":0,"complaint":0,"price_inquiry":0,"wants_appt":1,"appt_confirmed":0,"has_todo":0}'  # noqa: E501
B = A.replace("book", "cancel")
ROWS = [{"id": "c-1"}, {"id": "c-2"}]
OUT = {"c-1": A, "c-2": B}


def infer(r):
    return OUT[r["id"]]


def gateway(r):
    return OUT[r["id"]], json.loads(OUT[r["id"]])


def test_all_same():
    assert gc.compare(ROWS, OUT, infer, gateway) == []


def test_adapter_differs():
    out = gc.compare(ROWS, {"c-1": A, "c-2": A}, infer, gateway)
    assert [(d["id"], d["kind"]) for d in out] == [("c-2", "adapter")]


def test_gateway_text_or_labels_differ():
    def gw(r):
        return ("다른 글", None) if r["id"] == "c-1" else (B, json.loads(A))

    out = gc.compare(ROWS, OUT, infer, gw)
    assert [(d["id"], d["kind"]) for d in out] == [("c-1", "gateway"), ("c-2", "gateway")]
