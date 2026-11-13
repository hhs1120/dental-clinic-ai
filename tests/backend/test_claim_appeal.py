"""⑤-4 재심 사유서 초안(backend/claim_appeal.py) — 설계 docs/superpowers/specs/2026-11-10-claim-review-design.md §4.
입력에 금액·판단이 없는지, 문장 나누기, 후검증(한 칸 한 문장·인용이 원문과 문장에 있음·추측 번호), 근거 없는 문장 세기를 본다."""

import json

from backend import claim_appeal as ca

ROW = {
    "claim_id": 1,
    "adjust_note": "치석제거 후 치주치료 연결 근거 부족",
    "procedure_name": "잇몸치료",
    "tooth_no": "00",
    "heading": "치석제거 뒤 치주치료 연결",
    "body": "- 치석제거를 시행한 날부터 90일 이내에 이어서 치주치료를 하는 경우에는 진료기록부에 재평가 소견을 적는다.",
    "note": "14일 전 치석제거 후 재평가하여 이어서 치주치료 시행함. 치주낭 5mm, 출혈 있음.",
    "status": "adjusted",
    "claim_amount": 35000,
    "judgment": "contestable",
}
F = ca.appeal_facts(ROW)


def s(text, basis, quote=None, assumption=None):
    return {"text": text, "basis": basis, "quote": quote, "assumption": assumption}


GOOD = {
    "sentences": [
        s(
            "조정 사유는 '치석제거 후 치주치료 연결 근거 부족'입니다.",
            "adjustment",
            "치석제거 후 치주치료 연결 근거 부족",
        ),
        s(
            "청구 기준은 치석제거를 시행한 날부터 90일 이내에 이어서 치주치료를 하는 경우 재평가 소견을 적도록 합니다.",
            "standard",
            "치석제거를 시행한 날부터 90일 이내에 이어서 치주치료를",
        ),
        s(
            "차트에는 14일 전 치석제거 후 재평가하여 이어서 치주치료 시행함이라고 적혀 있습니다.",
            "chart",
            "14일 전 치석제거 후 재평가하여 이어서 치주치료 시행함",
        ),
        s("위 기록에 따라 재심사를 요청합니다.", "assumption", assumption=1),
    ],
    "assumptions": ["재심사를 요청한다는 결론은 AI가 쓴 문장입니다"],
}


def test_facts_hide_amount_and_judgment():
    assert set(F) == {"adjust_note", "procedure", "tooth", "standard", "note"}
    assert F["tooth"] is None and F["standard"]["heading"] == "치석제거 뒤 치주치료 연결"
    blob = json.dumps(F, ensure_ascii=False)
    assert "35000" not in blob and "contestable" not in blob and "adjusted" not in blob


def test_split_sentences():
    assert ca.split_sentences("가입니다. 나인가? 다예요!\n라") == [
        "가입니다.",
        "나인가?",
        "다예요!",
        "라",
    ]
    assert ca.split_sentences("치주낭 5.5mm 측정함.") == ["치주낭 5.5mm 측정함."]
    assert ca.split_sentences("  ") == []


def test_check_ok():
    assert ca.check_result(F, GOOD)


def test_check_quote_must_be_in_sentence():
    """원문에는 있지만 문장에는 없는 인용 — 인용한 사실이 문장에 그대로 보여야 한다"""
    bad = json.loads(json.dumps(GOOD))
    bad["sentences"][2]["text"] = "차트에는 재평가 뒤 이어서 치료했다고 적혀 있습니다."
    assert not ca.check_result(F, bad)


def test_check_quote_not_in_source():
    bad = json.loads(json.dumps(GOOD))
    bad["sentences"][2] = s(
        "차트에는 치주낭 7mm 깊은 염증 소견이라고 적혀 있습니다.",
        "chart",
        "치주낭 7mm 깊은 염증 소견",
    )
    assert not ca.check_result(F, bad)


def test_check_two_sentences_in_one_slot():
    bad = json.loads(json.dumps(GOOD))
    bad["sentences"][3]["text"] = "위 기록에 따라 재심사를 요청합니다. 검토 부탁드립니다."
    assert not ca.check_result(F, bad)


def test_check_assumption_index():
    for idx in (0, 2, None):
        bad = json.loads(json.dumps(GOOD))
        bad["sentences"][3]["assumption"] = idx
        assert not ca.check_result(F, bad), idx


def test_check_needs_sentences():
    assert not ca.check_result(F, {"sentences": [], "assumptions": []})


def test_unbacked_counts_split_pieces():
    """한 칸에 두 문장 — 인용이 첫 문장에만 있으면 둘째는 근거 없음"""
    r = {
        "sentences": [
            s(
                "차트에는 14일 전 치석제거 후 재평가하여 이어서 치주치료 시행함이라고 적혀 있습니다. 그러므로 정당합니다.",
                "chart",
                "14일 전 치석제거 후 재평가하여 이어서 치주치료 시행함",
            ),
            s("근거 없는 말입니다.", "assumption", assumption=3),
        ],
        "assumptions": ["하나"],
    }
    assert ca.unbacked(F, r) == (3, 2)
    assert ca.unbacked(F, GOOD) == (4, 0)
