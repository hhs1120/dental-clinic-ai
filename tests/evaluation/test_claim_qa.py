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
