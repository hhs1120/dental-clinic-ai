"""어려운 통화 정답 라벨(evaluation/hard_labels.py, 어려운 통화 설계 §4) — Opus 독립 라벨은 녹취·방향만 보고, 엇갈림은
출처를 가린 사람 검토, 검토가 끝나야 최종 라벨 JSON"""

import json
import sqlite3

import pytest

from evaluation import hard_labels as hl
from shared.llm import LLMResult
from tests.evaluation.eval_db import SOURCE_SCHEMA, TRUTH_SCHEMA, labels

GEN = {
    "hc-0001": labels("book", wants_appt=1, has_todo=1),
    "hc-0002": labels("treatment_q", urgency="normal"),
    "hc-0003": labels("price_q", price_inquiry=1),
}
OPUS = {  # hc-0001은 할 일이 엇갈림, hc-0002는 normal·low(같음), hc-0003은 일치
    "hc-0001": labels("book", wants_appt=1, has_todo=0),
    "hc-0002": labels("treatment_q", urgency="low"),
    "hc-0003": labels("price_q", price_inquiry=1),
}


@pytest.fixture
def conn(tmp_path):
    truth = sqlite3.connect(tmp_path / "truth.sqlite")
    truth.executescript(TRUTH_SCHEMA)
    for uid, g in GEN.items():
        truth.execute(
            "INSERT INTO truth_call_labels VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, '{}', 'hard')",
            (uid, g["primary_purpose"], json.dumps([g["primary_purpose"]]), g["counterpart"])
            + tuple(g[k] for k in hl.ITEMS[2:]),
        )
        truth.execute(
            "INSERT INTO truth_hard_calls VALUES (?, 'indirect', NULL, 'generated')", (uid,)
        )
    truth.commit()
    truth.close()
    src = sqlite3.connect(tmp_path / "source.sqlite")
    src.executescript(SOURCE_SCHEMA)
    for k, uid in enumerate(GEN, start=1):
        src.execute(
            "INSERT INTO ax_calls (id, call_uid, source, direction, started_at, duration_sec,"
            " has_transcript, transcript) VALUES (?, ?, 'generated', 'inbound', '2026-11-20 10:00:00',"
            " 100, 1, ?)",
            (k, uid, f"환자: 통화 {uid}\n직원: 네"),
        )
    src.commit()
    src.close()
    return hl.connect(tmp_path / "source.sqlite", tmp_path / "truth.sqlite")


class FakeBatch:
    def __init__(self):
        self.requests = None

    def submit_batch(self, alias, requests, *, task, prompt_version=None):
        self.alias, self.requests = alias, requests
        return "b1"

    def batch_status(self, batch_id, alias=None):
        return "ended"

    def batch_results(self, batch_id, alias, *, task, prompt_version=None, schema=None):
        out = {}
        for r in self.requests:
            data = {k: bool(v) if k in hl.FLAGS else v for k, v in OPUS[r["custom_id"]].items()}
            out[r["custom_id"]] = LLMResult(
                True, json.dumps(data), data, "opus", False, 1, 1, 0.01, 0
            )
        return out


def _labelled(conn, tmp_path):
    path = tmp_path / "hard_labels.jsonl"
    gw = FakeBatch()
    hl.label(gw, conn, path, state_path=tmp_path / "state.json", poll_seconds=0)
    return gw, path


def test_label_input_has_no_answer(conn, tmp_path):
    """Opus 입력은 분류기 라벨 프롬프트·녹취·방향뿐 — 생성 라벨·종류가 새지 않는다"""
    gw, path = _labelled(conn, tmp_path)
    assert gw.alias == hl.ALIAS and len(gw.requests) == 3
    for r in gw.requests:
        text = json.dumps(r, ensure_ascii=False)
        assert "indirect" not in text and "hard" not in text and "'hard'" not in text
        assert r["messages"][0]["content"].startswith("방향: inbound")
    assert len(hl.load(path)) == 3


def test_compare_urgency_only_urgent(conn, tmp_path):
    _, path = _labelled(conn, tmp_path)
    assert hl.compare(hl.load(path)) == {"hc-0001": ["has_todo"]}


def test_review_blinds_source(conn, tmp_path):
    """엇갈림은 가·나로만 — 어느 쪽이 생성·Opus인지 보이지 않는다. 일치 표본은 맞음/틀림"""
    _, path = _labelled(conn, tmp_path)
    out = []
    hl.review(conn, path, read=lambda prompt: "1", write=out.append, sample_size=1)
    text = "\n".join(map(str, out))
    assert "생성" not in text and "Opus" not in text and "opus" not in text
    recs = {r["call_uid"]: r for r in hl.load(path)}
    assert recs["hc-0001"]["human"]["has_todo"] in (0, 1)
    assert sum("sample_ok" in r for r in recs.values()) == 1


def test_finalize_requires_review_done(conn, tmp_path):
    _, path = _labelled(conn, tmp_path)
    with pytest.raises(SystemExit):
        hl.finalize(conn, path, tmp_path / "final.json", sample_size=1)


def test_finalize_writes_final_labels(conn, tmp_path):
    _, path = _labelled(conn, tmp_path)
    hl.review(conn, path, read=lambda prompt: "1", write=lambda *_: None, sample_size=1)
    report = hl.finalize(conn, path, tmp_path / "final.json", sample_size=1)
    rows = {r["call_uid"]: r for r in json.loads((tmp_path / "final.json").read_text())}
    assert set(rows) == set(GEN)
    assert rows["hc-0001"]["label_source"] == "human"
    assert rows["hc-0003"]["labels"]["price_inquiry"] == 1
    assert report["agreed_calls"] == 2 and report["n"] == 3
