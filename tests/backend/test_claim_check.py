"""⑤-2 AI 점검(backend/claim_check.py) — 설계 docs/superpowers/specs/2026-11-10-claim-review-design.md §2.
입력에 청구 상태·조정 결과가 없는지, 인용 글자 대조 후검증, 저장·재사용·재시도를 본다."""

import json
import sqlite3
from datetime import date, timedelta

import pytest

from backend import claim_check as cc
from backend.prep import master_values
from generator.clinic import master_values_rows
from generator.master import ADJUST_REASONS, APPEAL_DAYS
from shared.llm import LLMResult
from tests.backend.test_claims import add_claim
from tests.backend.test_missed import Source

MV = {name: json.loads(value) for name, value in master_values_rows()}
SECS = {
    "치주치료 산정 소견": "- 치주치료는 치주낭 깊이 4mm 이상 소견을 진료기록에 적는다.",
    "치석제거 뒤 치주치료 연결": "- 치석제거 뒤 후속 치주치료가 이어졌는지 기록한다.",
    "발치 난이도 산정 근거": "- 난이도를 산정한 경우 매복 정도와 골삭제 여부를 적는다.",
}
ROW = {
    "procedure_code": "U0012",
    "procedure_name": "사랑니 발치",
    "tooth_no": "38",
    "dx_name": "매복치",
    "note": "38번 수평 매복, 골삭제 후 분할 발치함.\n지혈 확인",
    "age": 31,
}


def facts(**kw):
    return cc.claim_facts(ROW | kw, MV, SECS)


# ── 입력 ──────────────────────────────────────────────────────────


def test_facts_hide_claim_status():
    f = facts(status="adjusted", adjust_note="발치 난이도 산정 근거 부족으로 하향 인정")
    assert set(f) == {"procedure", "tooth", "diagnosis", "age", "note", "standards"}
    blob = json.dumps(f, ensure_ascii=False)
    assert "adjusted" not in blob and "하향 인정" not in blob
    assert [s["heading"] for s in f["standards"]] == ["발치 난이도 산정 근거"]


def test_facts_two_requirements():
    f = facts(procedure_code="U0018", procedure_name="잇몸치료")
    assert [s["heading"] for s in f["standards"]] == [
        "치석제거 뒤 치주치료 연결",
        "치주치료 산정 소견",
    ]


# ── 인용 대조 ─────────────────────────────────────────────────────


def test_found_whitespace():
    text = "…치주낭 깊이 5mm 측정, 출혈 있음"
    assert cc.found("치주낭  깊이\n5mm", text)
    assert cc.found("“치주낭 깊이 5mm”.", text)
    assert not cc.found("", text)
    assert not cc.found("  ", text)
    assert not cc.found("6mm", text)


def test_found_rejects_short_fragment():
    """'출혈 있음'처럼 어느 서술에나 있을 조각은 근거가 못 된다 — 공백 뺀 MIN_QUOTE자 미만은 거짓"""
    text = "치주낭 깊이 5mm 측정, 출혈 있음"
    assert not cc.found("출혈 있음", text)
    assert not cc.found("“있음”", text)
    assert cc.found("깊이 5mm 측정", text)


# ── 후검증 ────────────────────────────────────────────────────────

CHART = {"source": "chart", "quote": "골삭제 후 분할 발치함"}
STD = {"source": "standard", "quote": "매복 정도와 골삭제 여부를 적는다"}


def res(requirement, evidence=(), omissions=()):
    return {"requirement": requirement, "evidence": list(evidence), "omissions": list(omissions)}


def test_check_met_needs_chart_quote():
    f = facts()
    assert cc.check_result(f, res("met", [CHART]))
    assert not cc.check_result(f, res("met", [STD]))
    assert not cc.check_result(f, res("met", [{"source": "chart", "quote": "골삭제 없이 발치"}]))


def test_check_missing_needs_standard_quote():
    f = facts()
    assert cc.check_result(f, res("missing", [STD]))
    assert not cc.check_result(f, res("missing", [CHART]))
    assert not cc.check_result(f, res("missing", [{"source": "standard", "quote": "65세 이상"}]))


def test_check_omission_quote_in_chart():
    f = facts()
    ok = {"procedure": "봉합", "quote": "수평 매복, 골삭제"}
    assert cc.check_result(f, res("met", [CHART], [ok]))
    assert not cc.check_result(f, res("met", [CHART], [{"procedure": "봉합", "quote": "봉합함"}]))


def test_check_na_no_evidence_ok():
    assert cc.check_result(facts(), res("na"))


# ── 저장·재사용·재시도 (Task 2) ───────────────────────────────────

DAY = date(2026, 12, 1)
MODEL = "claude-sonnet-5-5"
EXTRACT = "U0012"
REQ_NOTE = ADJUST_REASONS["R05"]["note"]  # 발치 난이도 — 요건 사유
DOC_NOTE = ADJUST_REASONS["D01"]["note"]  # 내역설명 — 서류 사유
STD_BODY = (
    "# 외과임플란트 분야 청구 기준\n\n## 발치 난이도 산정 근거\n\n"
    "- 난이도를 산정한 경우 매복 정도와 골삭제 여부를 적는다.\n\n"
    "## 임플란트 급여 대상 부위\n\n- 65세 이상, 무치악 부위.\n"
)
OK = {"requirement": "met", "evidence": [CHART], "omissions": []}
BAD = {
    "requirement": "met",
    "evidence": [{"source": "chart", "quote": "없는 문장"}],
    "omissions": [],
}


def llm(data):
    return LLMResult(True, "", data, MODEL, False, 1, 1, 0.001, 1)


class FakeGw:
    """complete는 대본 순서대로(대본이 비면 OK), 받은 인자를 남긴다"""

    config = {"aliases": {"chat": [MODEL]}, "models": {MODEL: {}}}

    def __init__(self, *turns):
        self.turns, self.calls = list(turns), []

    def complete(self, alias, **kw):
        self.calls.append(kw)
        return self.turns.pop(0) if self.turns else llm(OK)


@pytest.fixture
def src(tmp_path):
    s = Source(tmp_path / "source.sqlite")
    s.conn.execute(
        "INSERT INTO ax_documents (id, type, title, version, effective_date, path, body)"
        " VALUES (1, 'claim_standard', '외과임플란트 분야 청구 기준', '1',"
        " '2024-01-01', 'x.md', ?)",
        (STD_BODY,),
    )
    return s


def claim(src, pid, *, note_text=ROW["note"], status="pending", **kw):
    cid = add_claim(src, pid, code=EXTRACT, status=status, tooth="38", **kw)
    src.conn.execute(
        "UPDATE emr_chart_notes SET note = ? WHERE treatment_id ="
        " (SELECT treatment_id FROM emr_claims WHERE id = ?)",
        (note_text, cid),
    )
    return cid


def set_note(src, cid, text):
    """app()이 source 연결을 닫은 뒤에도 고칠 수 있게 따로 연다"""
    with sqlite3.connect(src.path) as c:
        c.execute(
            "UPDATE emr_chart_notes SET note = ? WHERE treatment_id ="
            " (SELECT treatment_id FROM emr_claims WHERE id = ?)",
            (text, cid),
        )


def build(conn, gw, scope="all"):
    return cc.build(conn, gw, cc.targets(conn, DAY, scope=scope), use_batch=False)


def test_build_saves_and_reuses(src):
    ids = [claim(src, 1), claim(src, 2)]
    conn = src.app()
    out = build(conn, FakeGw())
    assert (out["called"], out["skipped"], out["failed"]) == (2, 0, 0)
    out = build(conn, FakeGw())
    assert (out["called"], out["skipped"]) == (0, 2)
    got = cc.results(conn, ids)
    assert all(v["valid"] and v["requirement"] == "met" for v in got.values())


def test_build_retries_once_then_null(src):
    cid = claim(src, 1)
    conn = src.app()
    out = build(conn, FakeGw(llm(BAD), llm(BAD)))
    assert out["failed"] == 1
    row = conn.execute(
        "SELECT result, valid FROM ax_claim_checks WHERE claim_id = ?", (cid,)
    ).fetchone()
    assert row["result"] is None and row["valid"] == 0
    got = cc.results(conn, [cid])[cid]
    assert got.pop("at") and got.pop("prompt_version") == cc.PROMPT_VERSION
    assert got == {"requirement": None, "evidence": None, "omissions": None, "valid": False}


def test_retry_succeeds(src):
    claim(src, 1)
    gw = FakeGw(llm(BAD), llm(OK))
    out = build(src.app(), gw)
    assert (out["called"], out["failed"]) == (1, 0) and len(gw.calls) == 2


def test_empty_note_not_called(src):
    cid = claim(src, 1, note_text="")
    gw = FakeGw()
    conn = src.app()
    out = build(conn, gw)
    assert gw.calls == [] and out["empty"] == 1
    assert cc.results(conn, [cid])[cid]["requirement"] == "na"


def test_changed_note_recalled(src):
    a = claim(src, 1)
    claim(src, 2)
    conn = src.app()
    build(conn, FakeGw())
    set_note(src, a, "38번 수평 매복, 골삭제 후 분할 발치함. 봉합")
    out = build(conn, FakeGw())
    assert (out["called"], out["skipped"]) == (1, 1)


def reviewed_for(days_left: int) -> str:
    return (DAY + timedelta(days=days_left) - timedelta(days=APPEAL_DAYS)).isoformat()


def test_targets_app_scope(src):
    pend = claim(src, 1)
    live = claim(src, 2, status="adjusted", note=REQ_NOTE, reviewed=reviewed_for(10))
    claim(src, 3, status="adjusted", note=REQ_NOTE, reviewed=reviewed_for(-1))  # 기한 지남
    claim(src, 4, status="adjusted", note=DOC_NOTE, reviewed=reviewed_for(10))  # 서류 사유
    claim(src, 5, status="paid")
    add_claim(src, 6, status="pending")  # 요건 없는 시술(스케일링)
    conn = src.app()
    assert {t["claim_id"] for t in cc.targets(conn, DAY, scope="app")} == {pend, live}
    assert len(cc.targets(conn, DAY, scope="all")) == 5


def test_targets_hide_status_from_facts(src):
    claim(src, 1, status="adjusted", note=REQ_NOTE, reviewed=reviewed_for(10))
    conn = src.app()
    (t,) = cc.targets(conn, DAY, scope="app")
    f = cc.claim_facts(t, master_values(conn), cc.sections(conn))
    blob = json.dumps(f, ensure_ascii=False)
    assert REQ_NOTE not in blob and "adjusted" not in blob
    assert f["standards"][0]["body"].startswith("- 난이도를 산정한 경우")


def test_ref_logged(src):
    cid = claim(src, 1)
    gw = FakeGw()
    build(src.app(), gw)
    assert gw.calls[0]["ref"] == ("claim", cid)


@pytest.fixture
def client(src, monkeypatch):
    from fastapi.testclient import TestClient

    from backend import api
    from shared.clinic import Clinic

    claim(src, 1)
    src.app().close()
    gw = FakeGw()
    monkeypatch.setattr(cc, "load_config", lambda: gw.config)
    api.app.dependency_overrides[api.get_db_paths] = lambda: (
        src.path.parent / "app.sqlite",
        src.path,
    )
    api.app.dependency_overrides[api.get_clinic] = lambda: Clinic(DAY, date(2026, 1, 1), 4)
    api.app.dependency_overrides[api.get_gateway_factory] = lambda: lambda conn: gw
    yield TestClient(api.app)
    api.app.dependency_overrides.clear()


def wait(client, job_id):
    import time

    for _ in range(100):
        job = client.get(f"/jobs/{job_id}").json()
        if job["status"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError("작업이 끝나지 않음")


def test_job_api_builds_app_scope(client, src):
    job = wait(client, client.post("/jobs/claim-check").json()["job_id"])
    assert job["status"] == "done" and job["done"] == job["total"] == 1
    from backend import db

    conn = db.connect(src.path.parent / "app.sqlite", src.path)
    assert cc.pending(conn, DAY) == {"pre": 0, "adjust": 0}


def test_job_api_reuses_running(client, src):
    from backend import db

    conn = db.connect(src.path.parent / "app.sqlite", src.path)
    with conn:
        conn.execute(
            "INSERT INTO ax_jobs (id, kind, arg, status, started_at)"
            " VALUES (7, 'claim_check', '', 'running', datetime('now', 'localtime'))"
        )
    conn.close()
    assert client.post("/jobs/claim-check").json()["job_id"] == 7


def test_pending_count(src):
    claim(src, 1)
    claim(src, 2, note_text="")
    conn = src.app()
    assert cc.pending(conn, DAY) == {"pre": 1, "adjust": 0}
    build(conn, FakeGw(), scope="app")
    assert cc.pending(conn, DAY) == {"pre": 0, "adjust": 0}


def test_pending_split_by_tab(src):
    """갱신 버튼이 탭별로 적는다 — 청구 전 대기 줄과 삭감 대응 요건 사유 건"""
    claim(src, 1)
    claim(src, 2, status="adjusted", note=REQ_NOTE, reviewed=reviewed_for(10))
    claim(src, 3, status="adjusted", note=REQ_NOTE, reviewed=reviewed_for(20))
    assert cc.pending(src.app(), DAY) == {"pre": 1, "adjust": 2}


class BatchGw(FakeGw):
    """Batch는 custom_id → 답(None이면 빠진 줄), 일반 호출은 FakeGw 대본"""

    def __init__(self, batch, *turns):
        super().__init__(*turns)
        self.batch, self.batches = batch, []

    def submit_batch(self, alias, requests, **kw):
        self.batches.append(requests)
        return "b1"

    def batch_status(self, batch_id):
        return "ended"

    def batch_results(self, batch_id, alias, **kw):
        return {r["custom_id"]: self.batch.get(r["custom_id"]) for r in self.batches[-1]}


def test_batch_then_plain_for_missing(src):
    """Batch 답은 custom_id(청구 id)로 짝짓고, Batch에서 빠진 줄만 일반 호출"""
    a, b = claim(src, 1), claim(src, 2)
    gw = BatchGw({str(a): llm(OK)})
    conn = src.app()
    out = cc.build(conn, gw, cc.targets(conn, DAY, scope="all"), use_batch=True)
    assert (out["called"], out["failed"]) == (2, 0)
    assert [c["ref"] for c in gw.calls] == [("claim", b)]
    assert len(gw.batches[0]) == 2
