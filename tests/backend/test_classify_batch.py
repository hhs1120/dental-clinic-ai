"""분류·판정 일괄 실행 (backend/classify_batch.py, 분류기 설계 "실행")."""

import sqlite3

import pytest

from backend import classify_batch, db
from backend.harness.store import SqliteLlmLog
from shared.llm import FakeGateway
from tests.backend.conftest import SOURCE_SCHEMA

CONFIG = {
    "aliases": {"classify": ["solar"], "summarize": ["haiku"], "judge": ["haiku"]},
    "models": {"solar": {"provider": "upstage"}, "haiku": {}},
}


class BatchFake(FakeGateway):
    """FakeGateway + Batch 흉내. missing의 custom_id는 결과 None, fail_uids는 일반 호출 실패"""

    def __init__(self, log, missing=(), fail_uids=()):
        super().__init__(log, scope="runtime")
        self.config, self.missing, self.fail_uids = CONFIG, set(missing), set(fail_uids)
        self.batches, self.direct = {}, []

    def complete(self, alias, **kw):
        from dataclasses import replace

        r = super().complete(alias, **kw)
        self.direct.append(alias)
        if any(u in kw["messages"][0]["content"] for u in self.fail_uids):
            return replace(r, ok=False, data=None)
        return r

    def submit_batch(self, alias, requests, *, task, prompt_version=None, max_tokens=None):
        bid = f"b{len(self.batches)}"
        self.batches[bid] = (alias, task, prompt_version, requests)
        return bid

    def batch_status(self, batch_id):
        return "ended"

    def batch_results(self, batch_id, alias, *, task, prompt_version=None, schema=None):
        _, _, _, requests = self.batches[batch_id]
        out = {}
        for r in requests:
            if r["custom_id"] in self.missing:
                out[r["custom_id"]] = None
                continue
            out[r["custom_id"]] = FakeGateway.complete(
                self,
                alias,
                task=task,
                system=r["system"],
                messages=r["messages"],
                schema=r["schema"],
                prompt_version=prompt_version,
                ref=("batch", r["custom_id"]),
            )
        return out


@pytest.fixture
def bconn(tmp_path):
    source = tmp_path / "source.sqlite"
    s = sqlite3.connect(source)
    s.executescript(SOURCE_SCHEMA.read_text(encoding="utf-8"))
    s.executemany(
        "INSERT INTO ax_calls (call_uid, source, direction, started_at, duration_sec,"
        " has_transcript, transcript) VALUES (?, 'generated', 'inbound', '2026-07-01 10:00:00',"
        " 60, ?, ?)",
        [
            ("c-0001", 1, "직원: 네\n환자: c-0001 예약"),
            ("c-0002", 1, "직원: 네\n환자: c-0002 취소"),
            ("c-0003", 1, "직원: 네\n환자: c-0003 아파요"),
            ("c-0004", 0, None),
        ],
    )
    s.commit()
    s.close()
    c = db.connect(tmp_path / "app.sqlite", source)
    db.init_schema(c)
    yield c
    c.close()


def _status(conn, table="ax_call_classifications"):
    return dict(conn.execute(f"SELECT call_uid, status FROM {table} ORDER BY call_uid"))


def test_run_no_batch_classifies_all(bconn):
    gw = BatchFake(SqliteLlmLog(bconn))
    out = classify_batch.run(bconn, gw, "classify", "v1", use_batch=False)
    assert out == {"done": 3, "failed": 0, "missing": 0}
    assert _status(bconn) == {"c-0001": "ok", "c-0002": "ok", "c-0003": "ok"}
    assert gw.batches == {}


def test_rerun_skips_ok_retries_failed(bconn):
    gw = BatchFake(SqliteLlmLog(bconn), fail_uids={"c-0002"})
    assert classify_batch.run(bconn, gw, "judge", "v1", use_batch=False) == {
        "done": 2,
        "failed": 1,
        "missing": 0,
    }
    assert _status(bconn, "ax_model_judgments")["c-0002"] == "failed"
    gw = BatchFake(SqliteLlmLog(bconn))
    assert classify_batch.run(bconn, gw, "judge", "v1", use_batch=False)["done"] == 1
    assert gw.direct == ["judge"]
    assert set(_status(bconn, "ax_model_judgments").values()) == {"ok"}


def test_batch_missing_entry_left_for_next_run(bconn):
    gw = BatchFake(SqliteLlmLog(bconn), missing={"c-0003"})
    out = classify_batch.run(bconn, gw, "classify", "v1", use_batch=True, poll_seconds=0)
    assert out == {"done": 2, "failed": 0, "missing": 1}
    # Upstage 라벨은 일반 호출, Anthropic 요약은 Batch
    ((alias, task, version, reqs),) = gw.batches.values()
    assert (alias, task, version, len(reqs)) == ("summarize", "summarize", "v1", 3)
    assert gw.direct.count("classify") == 3
    assert "c-0003" not in _status(bconn)
    gw = BatchFake(SqliteLlmLog(bconn))
    assert classify_batch.run(bconn, gw, "classify", "v1", use_batch=True, poll_seconds=0) == {
        "done": 1,
        "failed": 0,
        "missing": 0,
    }


def test_limit(bconn):
    gw = BatchFake(SqliteLlmLog(bconn))
    assert classify_batch.run(bconn, gw, "judge", "v1", use_batch=True, limit=2)["done"] == 2
    assert list(_status(bconn, "ax_model_judgments")) == ["c-0001", "c-0002"]
