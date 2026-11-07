"""Qwen 비교 기준선 (evaluation/finetune/qwen/baselines.py, 분류기 설계 "Qwen 비교")."""

import json
import sqlite3

from evaluation.finetune.qwen import baselines
from shared.call_labels import FLAGS
from shared.llm import FakeGateway, JsonlLog

LABELS = {"primary_purpose": "book", "counterpart": "patient", "urgency": "normal",
          **{f: 0 for f in FLAGS}}  # fmt: skip


def _app(path, rows):
    """rows: (call_uid, 표, prompt_version, status, fallback_used)"""
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE ax_llm_calls (id INTEGER PRIMARY KEY, fallback_used INTEGER);"
        "CREATE TABLE ax_call_classifications (call_uid, model_alias, prompt_version, status,"
        " labels, llm_call_id);"
        "CREATE TABLE ax_model_judgments (call_uid, model_alias, prompt_version, status,"
        " labels, llm_call_id);"
    )
    for i, (uid, table, version, status, fallback) in enumerate(rows):
        conn.execute("INSERT INTO ax_llm_calls VALUES (?, ?)", (i, fallback))
        labels = json.dumps(LABELS) if status == "ok" else None
        conn.execute(f"INSERT INTO {table} VALUES (?, 'x', ?, ?, ?, ?)",
                     (uid, version, status, labels, i))  # fmt: skip
    conn.commit()
    conn.close()


def test_from_app(tmp_path):
    db = tmp_path / "app.sqlite"
    c, j = "ax_call_classifications", "ax_model_judgments"
    _app(db, [("a", c, "v2", "ok", 0), ("b", c, "v2", "ok", 1), ("c", c, "v2", "failed", 0),
              ("d", c, "v1", "ok", 0), ("a", j, "v2", "ok", 0)])  # fmt: skip
    out = baselines.from_app(db, {"a", "b", "c", "d"})
    assert out["haiku_v2"] == {"a": LABELS, "b": None, "c": None, "d": None}
    assert out["solar_v2"] == {"a": LABELS, "b": None, "c": None, "d": None}


def _rows(n):
    return [{"id": f"s-{i}", "set": "s1", "direction": "inbound", "transcript": f"직원: {i}"}
            for i in range(n)]  # fmt: skip


def test_run_style_resumes(tmp_path):
    out = tmp_path / "style.jsonl"
    gw = FakeGateway(JsonlLog(tmp_path / "log.jsonl"), "runtime")
    baselines.run_style(_rows(2), out, gw)
    lines = [json.loads(x) for x in out.read_text().splitlines()]
    assert sorted((r["id"], r["model"]) for r in lines) == [
        ("s-0", "haiku_v2"), ("s-0", "solar_v2"), ("s-1", "haiku_v2"), ("s-1", "solar_v2")
    ]  # fmt: skip
    assert all(set(r["labels"]) == set(LABELS) for r in lines)
    assert all(type(r["labels"][f]) is int for r in lines for f in FLAGS)
    baselines.run_style(_rows(3), out, gw)
    assert len(out.read_text().splitlines()) == 6  # 이미 낸 4건은 건너뜀


def test_failed_retried_last_wins(tmp_path):
    out = tmp_path / "style.jsonl"
    out.write_text(json.dumps({"id": "s-0", "model": "haiku_v2", "labels": None}) + "\n")
    gw = FakeGateway(JsonlLog(tmp_path / "log.jsonl"), "runtime")
    baselines.run_style(_rows(1), out, gw)
    assert len(out.read_text().splitlines()) == 3  # 실패 1건은 다시, solar는 처음
    got = baselines.latest(out)
    assert got[("s-0", "haiku_v2")] is not None and got[("s-0", "solar_v2")] is not None
