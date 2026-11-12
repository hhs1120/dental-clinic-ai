"""⑤-2b 추가 처치 누락(backend/claim_omission.py) — 설계 docs/superpowers/specs/2026-11-10-claim-review-design.md §2b.
입력에 청구 상태가 없는지, 인용 후검증, 코드의 급여 거름(시술 표·나이·청구된 것)을 본다."""

import json
from datetime import date

import pytest

from backend import claim_omission as co
from generator.clinic import master_values_rows
from generator.master import PROCEDURES
from shared.llm import LLMResult
from tests.backend.test_claims import add_claim
from tests.backend.test_missed import Source

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


# ── 대상·저장·실행 ────────────────────────────────────────────────

MODEL = "claude-sonnet-5-5"
DAY = date(2026, 12, 1)
NOTE = ROW["note"]
HIT = {"omissions": [it("아말감/GI", "12")]}
BAD = {"omissions": [it("아말감/GI", "12", "차트에 없는 문장입니다")]}


def llm(data):
    return LLMResult(True, "", data, MODEL, False, 1, 1, 0.001, 1)


class FakeGw:
    """complete는 대본 순서대로(대본이 비면 빈 답), 받은 인자를 남긴다"""

    config = {"aliases": {"chat": [MODEL]}, "models": {MODEL: {}}}

    def __init__(self, *turns):
        self.turns, self.calls = list(turns), []

    def complete(self, alias, **kw):
        self.calls.append(kw)
        return self.turns.pop(0) if self.turns else llm({"omissions": []})


@pytest.fixture
def src(tmp_path):
    return Source(tmp_path / "source.sqlite")


def claim(src, pid, *, note_text=NOTE, status="pending", **kw):
    cid = add_claim(src, pid, status=status, **kw)
    src.conn.execute(
        "UPDATE emr_chart_notes SET note = ? WHERE treatment_id ="
        " (SELECT treatment_id FROM emr_claims WHERE id = ?)",
        (note_text, cid),
    )
    return cid


def build(conn, gw, scope="all"):
    return co.build(conn, gw, co.targets(conn, DAY, scope=scope), use_batch=False)


def test_targets_scope(src):
    pend = claim(src, 1)
    sub = claim(src, 2, status="submitted")
    claim(src, 3, status="paid")
    conn = src.app()
    assert {t["claim_id"] for t in co.targets(conn, DAY, scope="app")} == {pend}
    rows = co.targets(conn, DAY, scope="all")
    assert {t["claim_id"] for t in rows} == {pend, sub}
    assert rows[0]["procedure_code"] == "U0001" and rows[0]["age"] == 46  # 스케일링도 든다


def test_build_saves_and_reuses(src):
    claim(src, 1)
    conn = src.app()
    out = build(conn, FakeGw(llm(HIT)))
    assert (out["called"], out["skipped"], out["failed"]) == (1, 0, 0)
    out = build(conn, FakeGw())
    assert (out["called"], out["skipped"]) == (0, 1)


def test_build_retries_once_then_null(src):
    cid = claim(src, 1)
    conn = src.app()
    out = build(conn, FakeGw(llm(BAD), llm(BAD)))
    assert out["failed"] == 1
    got = co.results(conn, [cid])[cid]
    assert got["valid"] is False and got["omissions"] is None


def test_empty_note_not_called(src):
    cid = claim(src, 1, note_text="")
    gw = FakeGw()
    conn = src.app()
    out = build(conn, gw)
    assert gw.calls == [] and out["empty"] == 1
    assert co.results(conn, [cid])[cid]["omissions"] == []


def test_results_filtered(src):
    cid = claim(src, 1)
    conn = src.app()
    resin = it("레진", "14", "스케일링 시행하고 이어서")
    build(conn, FakeGw(llm({"omissions": [resin, *HIT["omissions"]]})))
    got = co.results(conn, [cid])[cid]
    assert got["valid"] and [(x["procedure_code"], x["tooth"]) for x in got["omissions"]] == [
        ("U0006", "12")
    ]
    assert got["prompt_version"] == co.PROMPT_VERSION and got["at"]


def test_request_has_no_status(src):
    claim(src, 1, status="submitted")
    gw = FakeGw()
    build(src.app(), gw)
    (call,) = gw.calls
    blob = json.dumps(call["messages"], ensure_ascii=False)
    assert "submitted" not in blob and "status" not in blob and NOTE in blob
    names = call["schema"]["properties"]["omissions"]["items"]["properties"]["procedure"]["enum"]
    assert len(names) == len(PROCEDURES) and "아말감/GI" in names
    assert call["ref"][0] == "claim"


def test_todo_ids_app_only(src):
    pend = claim(src, 1)
    claim(src, 2, status="submitted")
    claim(src, 3, note_text="")
    conn = src.app()
    assert co.todo_ids(conn, DAY, MODEL) == {pend}
    build(conn, FakeGw(), scope="app")
    assert co.todo_ids(conn, DAY, MODEL) == set()


def test_schema_accepted_by_sdk():
    """SDK 구조화 출력 변환이 받는 꼴 — type 목록(["string","null"])은 받지 않아 anyOf로"""
    from anthropic.lib._parse._transform import transform_schema

    transform_schema(co.schema(["아말감/GI"]))


def test_prompt_v2_no_ambiguous_example():
    """v2 — '때움'은 레진과 아말감/GI가 갈리지 않는 말이라 예시로 들지 않는다(설계 §2b)"""
    from backend import prompts

    assert co.PROMPT_VERSION == "v2"
    assert "때움" not in prompts.load(co.TASK, co.PROMPT_VERSION)["system"]
