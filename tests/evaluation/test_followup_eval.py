"""후속 처리 평가 실행·채점 (평가 설계 §5) — fixture는 eval_db(통화 1 dev 풀, 4·5 silver)."""

import pytest

from evaluation import followup as ev_followup
from tests.backend.test_followup import SUBMIT
from tests.backend.test_rounds import BatchScript
from tests.evaluation.eval_db import make_db


@pytest.fixture
def paths(tmp_path):
    c = make_db(tmp_path)
    c.execute(
        "UPDATE truth.truth_call_labels SET has_todo = 1 WHERE call_uid IN ('c-0001','c-0004','c-0005')"
    )
    c.execute(
        "UPDATE ax_call_classifications SET labels = json_set(labels, '$.has_todo', 1), todo = '[]'"
        " WHERE call_uid IN ('c-0001','c-0004','c-0005') AND labels IS NOT NULL"
    )
    c.execute("INSERT INTO ax_settings VALUES ('active_prompt_version', '\"v1\"')")
    c.commit()
    c.close()
    return tmp_path


def _run(p, gw_box):
    return ev_followup.run(
        p / "app.sqlite",
        p / "source.sqlite",
        p / "truth.sqlite",
        out=p / "runs.jsonl",
        copy=p / "runs" / "copy.sqlite",
        state=p / "runs" / "state.json",
        gw_factory=lambda c: gw_box.setdefault(
            "gw", BatchScript(c, [("tool", "submit_followup", SUBMIT)])
        ),
        min_batch=0,
        poll_seconds=0,
    )


def test_run_writes_jsonl_and_skips_done(paths):
    import sqlite3

    before = (
        sqlite3.connect(paths / "app.sqlite")
        .execute("SELECT COUNT(*) FROM ax_agent_runs")
        .fetchone()[0]
    )
    box = {}
    r = _run(paths, box)
    rows = ev_followup.load(paths / "runs.jsonl")
    assert r["done"] == len(rows) >= 2 and all(x["action"] == "doctor_reply" for x in rows)
    box2 = {}
    assert _run(paths, box2)["done"] == 0 and box2["gw"].batches == {}
    after = (
        sqlite3.connect(paths / "app.sqlite")
        .execute("SELECT COUNT(*) FROM ax_agent_runs")
        .fetchone()[0]
    )
    assert before == after  # 원본 앱 DB에는 실행 기록을 쓰지 않는다
