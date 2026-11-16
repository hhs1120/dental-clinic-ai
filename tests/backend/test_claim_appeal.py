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


# 병원 내부 점검 기준(고시에 같은 문장 없음) 사유 — 기준 본문을 입력에 넣지 않고 기준 인용을 막는다(실험 기록 §30)
INTERNAL_ROW = ROW | {
    "body": "병원 내부 점검 기준 — 고시에 같은 문장 없음\n\n" + ROW["body"],
}
F_INTERNAL = ca.appeal_facts(INTERNAL_ROW)
NO_STANDARD = {
    "sentences": [x for x in GOOD["sentences"] if x["basis"] != "standard"],
    "assumptions": GOOD["assumptions"],
}


def test_internal_mark_matches_generator():
    """백엔드는 generator를 import하지 않는다 — 기준 문서의 표시 문구가 생성기와 같은지 여기서 본다"""
    from backend.claim_check import INTERNAL_MARK
    from generator.claim_standards import VIRTUAL_MARK

    assert INTERNAL_MARK == VIRTUAL_MARK


def test_is_internal():
    from backend.claim_check import is_internal

    assert is_internal(INTERNAL_ROW["body"]) and not is_internal(ROW["body"])


def test_facts_internal_standard_has_no_body():
    assert F_INTERNAL["standard"] == {
        "heading": "치석제거 뒤 치주치료 연결",
        "body": None,
        "internal": True,
    }
    assert F["standard"]["internal"] is False and F["standard"]["body"] == ROW["body"]


def test_check_internal_rejects_standard_quote():
    assert not ca.check_result(F_INTERNAL, GOOD)
    assert ca.check_result(F_INTERNAL, NO_STANDARD)
    assert ca.unbacked(F_INTERNAL, GOOD) == (4, 1)


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


# ── 저장·초안·검토·제출 ───────────────────────────────────────────

from datetime import date, timedelta  # noqa: E402

import pytest  # noqa: E402

from backend import claims  # noqa: E402
from generator.master import ADJUST_REASONS, APPEAL_DAYS  # noqa: E402
from shared.llm import LLMResult  # noqa: E402
from tests.backend.test_claims import add_claim  # noqa: E402
from tests.backend.test_missed import Source  # noqa: E402

DAY = date(2026, 12, 1)
MODEL = "claude-sonnet-5-5"
R02 = ADJUST_REASONS["R02"]["note"]
STD = "# 치주 분야 청구 기준\n\n머리말.\n\n## 치석제거 뒤 치주치료 연결\n\n" + ROW["body"] + "\n"
AT = "2026-11-13 10:00:00"


def llm(data):
    return LLMResult(True, "", data, MODEL, False, 1, 1, 0.001, 1)


class FakeGw:
    config = {"aliases": {"chat": [MODEL]}, "models": {MODEL: {}}}

    def __init__(self, *turns):
        self.turns, self.calls = list(turns), []

    def complete(self, alias, **kw):
        self.calls.append(kw)
        return self.turns.pop(0) if self.turns else llm(good())


def good():
    g = json.loads(json.dumps(GOOD))
    g["sentences"][0] = s(f"조정 사유는 '{R02}'입니다.", "adjustment", R02)
    return g


@pytest.fixture
def src(tmp_path):
    s_ = Source(tmp_path / "source.sqlite")
    s_.conn.execute(
        "INSERT INTO ax_documents (id, type, title, version, effective_date, path, body)"
        " VALUES (1, 'claim_standard', '치주 분야 청구 기준', '1', '2024-01-01', 'x.md', ?)",
        (STD,),
    )
    return s_


def reviewed_for(days_left: int) -> str:
    return (DAY + timedelta(days=days_left) - timedelta(days=APPEAL_DAYS)).isoformat()


def contested(src, pid=1, note=ROW["note"], judge="contestable"):
    cid = add_claim(src, pid, code="U0018", note=R02, reviewed=reviewed_for(10))
    src.conn.execute(
        "UPDATE emr_chart_notes SET note = ? WHERE treatment_id ="
        " (SELECT treatment_id FROM emr_claims WHERE id = ?)",
        (note, cid),
    )
    return cid


def judged(conn, cid, judgment="contestable"):
    with conn:
        claims.record_judgment(conn, cid, judgment, "데스크", AT, DAY)


def test_draft_only_contestable(src):
    cid = contested(src)
    conn = src.app()
    with pytest.raises(ValueError, match="재심 신청"):
        ca.draft(conn, FakeGw(), cid, DAY)
    judged(conn, cid, "justified")
    with pytest.raises(ValueError):
        ca.draft(conn, FakeGw(), cid, DAY)


def test_draft_saves_and_redraft_resets_review(src):
    cid = contested(src)
    conn = src.app()
    judged(conn, cid)
    got = ca.draft(conn, FakeGw(), cid, DAY)
    assert got["valid"] and len(got["sentences"]) == 4 and got["reviewed"] is None
    ca.review(conn, cid, "데스크", AT, True, DAY)
    assert ca.appeal(conn, cid)["reviewed"] == {"by": "데스크", "at": AT}
    again = ca.draft(conn, FakeGw(), cid, DAY)  # 다시 쓰기 — 검토 확인이 풀린다(Review Focus 2)
    assert again["valid"] and again["reviewed"] is None


def test_draft_failed_twice_none(src):
    cid = contested(src)
    conn = src.app()
    judged(conn, cid)
    bad = llm(
        {
            "sentences": [s("근거 없는 문장입니다.", "chart", "차트에 없는 문장입니다")],
            "assumptions": [],
        }
    )
    got = ca.draft(conn, FakeGw(bad, bad), cid, DAY)
    assert got["valid"] is False and got["sentences"] is None
    with pytest.raises(ValueError):
        ca.review(conn, cid, "데스크", AT, True, DAY)


def test_review_requires_valid_draft(src):
    cid = contested(src)
    conn = src.app()
    judged(conn, cid)
    with pytest.raises(ValueError):
        ca.review(conn, cid, "데스크", AT, True, DAY)  # 초안 없음
    ca.draft(conn, FakeGw(), cid, DAY)
    ca.review(conn, cid, "데스크", AT, True, DAY)
    ca.review(conn, cid, "데스크", AT, False, DAY)
    assert ca.appeal(conn, cid)["reviewed"] is None


def test_submit_requires_review(src):
    cid = contested(src)
    conn = src.app()
    judged(conn, cid)
    ca.draft(conn, FakeGw(), cid, DAY)
    with pytest.raises(ValueError, match="검토"):
        ca.submit(conn, cid, "데스크", AT, DAY)
    ca.review(conn, cid, "데스크", AT, True, DAY)
    ca.submit(conn, cid, "원장", AT, DAY)
    assert ca.appeal(conn, cid)["submitted"] == {"by": "원장", "at": AT}


def test_submitted_locks_redraft_and_judgment(src):
    cid = contested(src)
    conn = src.app()
    judged(conn, cid)
    ca.draft(conn, FakeGw(), cid, DAY)
    ca.review(conn, cid, "데스크", AT, True, DAY)
    ca.submit(conn, cid, "데스크", AT, DAY)
    with pytest.raises(ValueError, match="제출"):
        ca.draft(conn, FakeGw(), cid, DAY)
    with pytest.raises(ValueError, match="제출"):
        judged(conn, cid, "justified")
    with pytest.raises(ValueError):
        ca.review(conn, cid, "데스크", AT, False, DAY)


def test_empty_chart_still_drafts(src):
    """차트 서술이 빈 건 — 초안은 쓴다(조정 사유가 늘 있어 부른다), 차트 인용은 후검증이 막는다"""
    cid = contested(src, note="")
    conn = src.app()
    judged(conn, cid)
    no_chart = good()
    del no_chart["sentences"][2]
    gw = FakeGw(llm(no_chart))
    got = ca.draft(conn, gw, cid, DAY)
    assert len(gw.calls) == 1 and got["valid"]
    assert '"note": ""' in gw.calls[0]["messages"][0]["content"]


def test_judgment_change_keeps_draft(src):
    """초안 뒤 판단을 바꿔도 초안은 남는다(화면이 재심 신청일 때만 보인다 — Review Focus 1)"""
    cid = contested(src)
    conn = src.app()
    judged(conn, cid)
    ca.draft(conn, FakeGw(), cid, DAY)
    judged(conn, cid, "justified")
    assert ca.appeal(conn, cid)["valid"]


# ── API·조정 목록 행 ──────────────────────────────────────────────


@pytest.fixture
def api_client(src):
    from fastapi.testclient import TestClient

    from backend import api
    from shared.clinic import Clinic

    cid = contested(src)
    src.app().close()
    gw = FakeGw()
    api.app.dependency_overrides[api.get_db_paths] = lambda: (
        src.path.parent / "app.sqlite",
        src.path,
    )
    api.app.dependency_overrides[api.get_clinic] = lambda: Clinic(DAY, date(2026, 1, 1), 4)
    api.app.dependency_overrides[api.get_gateway_factory] = lambda: lambda conn: gw
    yield TestClient(api.app), cid
    api.app.dependency_overrides.clear()


def test_api_draft_review_submit_flow(api_client):
    client, cid = api_client
    assert (
        client.post(
            f"/claims/{cid}/judgment", json={"judgment": "contestable", "part": "데스크"}
        ).status_code
        == 200
    )
    r = client.post(f"/claims/{cid}/appeal")
    assert r.status_code == 200 and r.json()["valid"] and len(r.json()["sentences"]) == 4
    assert (
        client.post(
            f"/claims/{cid}/appeal/review", json={"part": "데스크", "checked": True}
        ).status_code
        == 200
    )
    assert client.post(f"/claims/{cid}/appeal/submit", json={"part": "원장"}).status_code == 200
    row = next(x for x in client.get("/claims").json()["rows"] if x["claim_id"] == cid)
    assert (
        row["appeal"]["submitted"]["by"] == "원장" and row["appeal"]["reviewed"]["by"] == "데스크"
    )


def test_api_400s(api_client):
    client, cid = api_client
    assert client.post(f"/claims/{cid}/appeal").status_code == 400  # 판단 없음
    client.post(f"/claims/{cid}/judgment", json={"judgment": "contestable", "part": "데스크"})
    client.post(f"/claims/{cid}/appeal")
    assert (
        client.post(f"/claims/{cid}/appeal/submit", json={"part": "원장"}).status_code == 400
    )  # 검토 없음
    client.post(f"/claims/{cid}/appeal/review", json={"part": "데스크", "checked": True})
    client.post(f"/claims/{cid}/appeal/submit", json={"part": "원장"})
    assert client.post(f"/claims/{cid}/appeal").status_code == 400  # 제출 뒤 다시 쓰기
    r = client.post(f"/claims/{cid}/judgment", json={"judgment": "justified", "part": "데스크"})
    assert r.status_code == 400 and "제출" in r.json()["detail"]


def test_adjusted_row_has_appeal(src):
    cid = contested(src)
    other = add_claim(src, 2, code="U0018", note=R02, reviewed=reviewed_for(10))
    conn = src.app()
    judged(conn, cid)
    ca.draft(conn, FakeGw(), cid, DAY)
    rows = {r["claim_id"]: r for r in claims.adjusted(conn, DAY)["rows"]}
    assert rows[cid]["appeal"]["valid"] and rows[other]["appeal"] is None


# ── 리뷰 반영 ─────────────────────────────────────────────────────


class BoomGw(FakeGw):
    """하루 상한 같은 예외를 던지는 게이트웨이"""

    def complete(self, alias, **kw):
        from shared.llm import BudgetExceeded

        raise BudgetExceeded("하루 상한")


def test_redraft_failure_keeps_reviewed_draft(src):
    """다시 쓰기 도중 예외가 나도 검토까지 끝낸 초안은 남는다(옛 초안을 먼저 지우지 않는다)"""
    cid = contested(src)
    conn = src.app()
    judged(conn, cid)
    ca.draft(conn, FakeGw(), cid, DAY)
    ca.review(conn, cid, "데스크", AT, True, DAY)
    from shared.llm import BudgetExceeded

    with pytest.raises(BudgetExceeded):
        ca.draft(conn, BoomGw(), cid, DAY)
    got = ca.appeal(conn, cid)
    assert got["valid"] and got["reviewed"] == {"by": "데스크", "at": AT}


def test_redraft_calls_again_with_same_input(src):
    """같은 입력이어도 다시 쓰기는 새로 부른다(저장된 결과를 재사용하지 않음)"""
    cid = contested(src)
    conn = src.app()
    judged(conn, cid)
    ca.draft(conn, FakeGw(), cid, DAY)
    gw = FakeGw()
    ca.draft(conn, gw, cid, DAY)
    assert len(gw.calls) == 1


def test_review_requires_contestable(src):
    cid = contested(src)
    conn = src.app()
    judged(conn, cid)
    ca.draft(conn, FakeGw(), cid, DAY)
    judged(conn, cid, "justified")
    with pytest.raises(ValueError, match="재심 신청"):
        ca.review(conn, cid, "데스크", AT, True, DAY)


def _make_internal(src):
    """기준 문서의 그 절을 병원 내부 점검 기준으로 바꾼다 — 저장된 초안이 지금 규칙과 어긋나는 경우"""
    body = STD.replace(
        ROW["body"], "병원 내부 점검 기준 — 고시에 같은 문장 없음\n\n" + ROW["body"]
    )
    import sqlite3

    with sqlite3.connect(src.path) as w:  # src.app() 뒤라 source 연결은 닫혀 있다
        w.execute("UPDATE ax_documents SET body = ? WHERE id = 1", (body,))


def test_section_cards_mark_internal_after_split(src):
    """기준 문서를 절로 나눈 뒤의 본문(앞 줄바꿈 포함)으로 내부 기준을 알아본다"""
    from backend.claim_check import section_cards

    conn = src.app()
    assert section_cards(conn)["치석제거 뒤 치주치료 연결"]["internal"] is False
    _make_internal(src)
    assert section_cards(conn)["치석제거 뒤 치주치료 연결"]["internal"] is True


def test_stored_draft_against_current_rules_needs_redraft(src):
    """저장된 초안이 지금 규칙(입력·후검증)으로 떨어지면 다시 쓰기 필요 — 검토 확인·제출을 막는다"""
    cid = contested(src)
    conn = src.app()
    judged(conn, cid)
    assert ca.draft(conn, FakeGw(), cid, DAY)["valid"]  # 기준 절 인용이 든 초안
    _make_internal(src)
    got = ca.appeal(conn, cid)
    assert got["valid"] is False and got["stale"] is True and got["sentences"] is None
    assert ca.appeals(conn)[cid]["stale"] is True
    with pytest.raises(ValueError):
        ca.review(conn, cid, "데스크", AT, True, DAY)


def test_submitted_draft_kept_as_record(src):
    """제출 결정한 초안은 기록 — 규칙이 바뀌어도 그대로 보인다"""
    cid = contested(src)
    conn = src.app()
    judged(conn, cid)
    ca.draft(conn, FakeGw(), cid, DAY)
    ca.review(conn, cid, "데스크", AT, True, DAY)
    ca.submit(conn, cid, "데스크", AT, DAY)
    _make_internal(src)
    got = ca.appeal(conn, cid)
    assert got["valid"] is True and got["stale"] is False and got["submitted"]
