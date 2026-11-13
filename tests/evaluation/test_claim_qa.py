"""⑤-3 청구 기준 질문 평가셋(evaluation/claim_qa.py) — 설계 docs/superpowers/specs/2026-11-10-claim-review-design.md §3.
사유 단위 분할, 베끼기 경고(평가 설계 §3.1), 초안 쓰기(가짜 게이트웨이)를 본다."""

import json

from evaluation import claim_qa as cq
from generator.doc_catalog import CLAIM_HEADINGS
from shared.llm import LLMResult

R02 = {
    "reason": "R02",
    "heading": CLAIM_HEADINGS["R02"],
    "note": "치석제거 후 치주치료 연결 근거 부족",
    "body": "- 치석제거를 시행한 날부터 90일 이내에 이어서 치주치료를 하는 경우에는 진료기록부에 재평가 소견을 적는다.",
}


def llm(data):
    return LLMResult(True, "", data, "claude-sonnet-5-5", False, 1, 1, 0.001, 1)


class FakeGw:
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def complete(self, alias, **kw):
        self.calls.append((alias, kw))
        return llm({"questions": self.answers.pop(0)})


def test_split_alternates():
    sides = [cq.split_of(k) for k in sorted(CLAIM_HEADINGS)]
    assert sides.count("dev") == 6 and sides.count("test") == 5
    assert sides[0] == "dev" and sides[1] == "test"


def test_copy_warnings():
    assert cq.copy_warnings("치석제거 다음 달로 넘어가면 왜 깎여?", R02) == []
    assert cq.copy_warnings("90일 이내에 이어서 치주치료를 하면 어떻게 돼?", R02)  # 본문 4어절
    assert cq.copy_warnings("치석제거 뒤 치주치료 연결이 뭐야?", R02)  # 절 제목
    assert cq.copy_warnings("R02로 깎이면 어떻게 해?", R02)  # 사유 코드
    assert cq.copy_warnings(
        "치석제거 후 치주치료 연결 근거 부족이 무슨 뜻이야?", R02
    )  # 상세 사유 문구


def test_draft_keeps_clean_and_resumes(tmp_path):
    path = tmp_path / "drafts.jsonl"
    gw = FakeGw(
        ["치석제거 다음 달로 넘어가면 왜 깎여?", "치석제거 뒤 치주치료 연결이 뭐야?"],
        ["스케일링하고 한참 뒤에 잇몸치료 하면 문제야?"],
    )
    out = cq.draft(gw, path, reasons=[R02])
    assert out == {"written": 2, "skipped": 0, "short": []}
    rows = [json.loads(x) for x in path.read_text("utf-8").splitlines()]
    assert [r["id"] for r in rows] == ["cq-R02-1", "cq-R02-2"]
    assert all(cq.copy_warnings(r["question"], R02) == [] for r in rows)
    assert gw.calls[0][0] == "gen" and "문제" in json.dumps(gw.calls[1][1], ensure_ascii=False)
    gw2 = FakeGw()
    assert cq.draft(gw2, path, reasons=[R02])["skipped"] == 1 and gw2.calls == []


def test_draft_gives_up_short(tmp_path):
    bad = ["치석제거 뒤 치주치료 연결이 뭐야?"]
    out = cq.draft(FakeGw(bad, bad, bad), tmp_path / "d.jsonl", reasons=[R02])
    assert out["short"] == ["R02"] and out["written"] == 0


# ── 검토(로컬 페이지) ─────────────────────────────────────────────

DRAFT = [
    {"id": "cq-R02-1", "reason": "R02", "question": "치석제거 다음 달로 넘어가면 왜 깎여?"},
    {"id": "cq-R02-2", "reason": "R02", "question": "스케일링하고 한참 뒤 잇몸치료 하면 문제야?"},
]


def test_decide_edit_rechecks():
    """고친 문장은 베끼기 검사를 다시 — 경고가 있어도 행은 남고 warnings에 적힌다(막지 않음)"""
    rows = cq.decide(
        DRAFT,
        [
            {"id": "cq-R02-1", "status": "approved", "question": DRAFT[0]["question"]},
            {
                "id": "cq-R02-2",
                "status": "edited",
                "question": "치석제거 뒤 치주치료 연결이 뭐야?",
            },
        ],
        [R02],
    )
    assert [r["id"] for r in rows] == ["cq-R02-1", "cq-R02-2"]
    assert rows[0] | {} == {
        "id": "cq-R02-1",
        "reason": "R02",
        "question": DRAFT[0]["question"],
        "answer_heading": CLAIM_HEADINGS["R02"],
        "warnings": [],
        "split": cq.split_of("R02"),
    }
    assert rows[1]["warnings"] and rows[1]["question"] == "치석제거 뒤 치주치료 연결이 뭐야?"


def test_decide_drops():
    rows = cq.decide(
        DRAFT,
        [
            {"id": "cq-R02-1", "status": "dropped", "question": DRAFT[0]["question"]},
            {"id": "cq-R02-2", "status": "approved", "question": DRAFT[1]["question"]},
        ],
        [R02],
    )
    assert [r["id"] for r in rows] == ["cq-R02-2"]


def test_review_server_roundtrip(tmp_path):
    import threading
    import urllib.request

    drafts, out = tmp_path / "drafts.jsonl", tmp_path / "claim_qa.json"
    drafts.write_text("".join(json.dumps(d, ensure_ascii=False) + "\n" for d in DRAFT), "utf-8")
    server = cq.make_server(drafts, out, [R02], port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        page = urllib.request.urlopen(base + "/").read().decode("utf-8")
        assert "청구 기준 질문 검토" in page
        items = json.loads(urllib.request.urlopen(base + "/drafts").read())["items"]
        assert [i["id"] for i in items] == ["cq-R02-1", "cq-R02-2"]
        assert items[0]["heading"] == CLAIM_HEADINGS["R02"] and items[0]["body"]

        def post(path, body):
            req = urllib.request.Request(
                base + path,
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json"},
            )
            return json.loads(urllib.request.urlopen(req).read())

        assert post("/check", {"reason": "R02", "question": "R02가 뭐야?"})["warnings"]
        decisions = [
            {"id": d["id"], "status": "approved", "question": d["question"]} for d in DRAFT
        ]
        assert post("/save", {"decisions": decisions})["saved"] == 2
        assert [r["id"] for r in json.loads(out.read_text("utf-8"))] == ["cq-R02-1", "cq-R02-2"]
    finally:
        server.shutdown()


# ── 채점 ──────────────────────────────────────────────────────────


class FakeApp:
    """질문 → 절 제목 순위(backend/eval_hooks.py App.claim_search 꼴)"""

    def __init__(self, ranked: dict):
        self.ranked = ranked

    def claim_search(self, question, condition=None):
        return self.ranked[question]


def qa_conn(rows):
    import sqlite3

    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("ATTACH DATABASE ':memory:' AS truth")
    c.execute(
        "CREATE TABLE truth.truth_claim_qa (id, reason, question, answer_heading, warnings, split)"
    )
    c.executemany("INSERT INTO truth.truth_claim_qa VALUES (?, ?, ?, ?, '[]', ?)", rows)
    c.execute("CREATE TABLE ax_chunks (id, doc_type, text)")
    c.executemany(
        "INSERT INTO ax_chunks VALUES (?, 'claim_standard', ?)",
        [
            ("doc:1:0", "치주 분야 청구 기준\n머리말"),
            ("doc:1:1", "치주 분야 청구 기준 > 치주치료 산정 소견\n- …"),
        ],
    )
    return c


def test_score_section_level():
    """같은 문서의 다른 절이 1위여도 맞음이 아니다 — 절 단위(문서 단위로 세면 거의 저절로 맞는다)"""
    h = CLAIM_HEADINGS
    conn = qa_conn(
        [("q1", "R02", "질문1", h["R02"], "dev"), ("q2", "R03", "질문2", h["R03"], "dev")]
    )
    app = FakeApp({"질문1": [h["R03"], h["E01"]], "질문2": [h["R03"]]})
    out = cq.score(conn, app, "dev")
    assert (out["n"], out["hit5"], out["mrr"]) == (2, 0.5, 0.5)
    assert out["by_reason"]["R02"] == {"n": 1, "hit": 0} and out["candidates"] == 1
    assert [r["rank"] for r in out["rows"]] == [None, 1]


def test_score_mrr_and_test_hides_rows():
    h = CLAIM_HEADINGS
    conn = qa_conn([("q1", "R02", "질문1", h["R02"], "test")])
    out = cq.score(conn, FakeApp({"질문1": [h["R03"], h["R02"]]}), "test")
    assert (out["hit5"], out["mrr"], out["rows"]) == (1.0, 0.5, None)


def test_render():
    from evaluation.eval import _render_claim_basis

    text = "\n".join(
        _render_claim_basis(
            {
                "qa": {"n": 2, "candidates": 11, "hit5": 0.5, "mrr": 0.5, "by_reason": {}},
                "evidence": {"n": 3, "checked": 3, "valid": 3, "answered": 2, "empty": 1},
            }
        )
    )
    assert "⑤-3" in text and "Hit@5" in text and "11" in text
