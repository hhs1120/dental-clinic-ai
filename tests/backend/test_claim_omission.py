"""⑤-2b 추가 처치 누락(backend/claim_omission.py) — 설계 docs/superpowers/specs/2026-11-10-claim-review-design.md §2b.
입력에 청구 상태가 없는지, 인용 후검증, 코드의 급여 거름(시술 표·나이·청구된 것)을 본다."""

import json

from backend import claim_omission as co
from generator.clinic import master_values_rows
from generator.master import PROCEDURES

AGE_RANGE = json.loads(dict(master_values_rows())["age_group_range"])
PROCS = PROCEDURES
IDX = co.procedure_index(PROCS)
ROW = {
    "procedure_code": "U0001",
    "procedure_name": "스케일링",
    "tooth_no": "00",
    "age": 40,
    "note": "치주낭 3mm, 출혈 없음. 스케일링 시행하고 이어서 12번 치아 아말감/GI 함께 진행함.",
}
F = co.omission_facts(ROW)


def it(procedure, tooth, quote="12번 치아 아말감/GI 함께 진행함"):
    return {"procedure": procedure, "tooth": tooth, "quote": quote}


# ── 입력 ──────────────────────────────────────────────────────────


def test_facts_only_line_and_note():
    f = co.omission_facts(ROW | {"status": "submitted", "adjust_note": "x"})
    assert set(f) == {"procedure", "tooth", "age", "note"} and f["tooth"] is None
    assert "submitted" not in json.dumps(f, ensure_ascii=False)


# ── 후검증 ────────────────────────────────────────────────────────


def test_check_quote_in_note():
    assert co.check_result(F, {"omissions": [it("아말감/GI", "12")]})
    assert not co.check_result(F, {"omissions": [it("아말감/GI", "12", "없는 문장입니다")]})
    assert co.check_result(F, {"omissions": []})


# ── 급여 거름 ─────────────────────────────────────────────────────


def test_index_drops_ambiguous_alias():
    assert IDX["아말감"]["code"] == "U0006" and IDX["아말감/GI"]["code"] == "U0006"
    assert "임플란트" not in IDX and IDX["임플란트 1차"]["code"] == "U0013"


def test_insured_items_filters():
    items = [
        it("아말감/GI", "12"),  # 남음
        it("레진", "14"),  # 비급여
        it("실란트", "36"),  # 성인 — 나이 밖
        it("구강검진", None),  # 치아 단위 아님
        it("스케일링", "00"),  # 청구된 것·치아 아님
        it("없는처치", "21"),  # 표에 없음
    ]
    got = co.insured_items(items, "U0001", F, IDX, AGE_RANGE)
    assert [(x["procedure_code"], x["tooth"]) for x in got] == [("U0006", "12")]
    assert got[0]["procedure"] == "아말감/GI" and got[0]["quote"] == items[0]["quote"]


def test_insured_items_same_procedure_other_tooth_kept():
    facts = F | {"tooth": "15"}
    got = co.insured_items(
        [it("아말감/GI", "16"), it("아말감/GI", "15")], "U0006", facts, IDX, AGE_RANGE
    )
    assert [x["tooth"] for x in got] == ["16"]


def test_child_sealant_kept():
    got = co.insured_items([it("홈메우기", "36")], "U0002", F | {"age": 9}, IDX, AGE_RANGE)
    assert got[0]["procedure_code"] == "U0004" and got[0]["procedure"] == "실란트"


def test_bad_tooth_dropped():
    assert (
        co.insured_items(
            [it("아말감/GI", "19"), it("아말감/GI", "12번")], "U0001", F, IDX, AGE_RANGE
        )
        == []
    )
