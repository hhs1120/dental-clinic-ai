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


def test_baseline_rules():
    b = ev_followup.baseline
    assert b([{"text": "스케일링 보험 적용 시기 회신"}]) == "scaling_timing"
    assert b(["영수증 재발급해서 보내기"]) == "document"
    assert b([{"text": "남은 금액 확인 후 회신"}]) == "billing"
    assert b([{"text": "실손 청구 가능 여부 회신"}]) == "insurance_cost"
    assert b([{"text": "예약 날짜 다시 잡아 안내"}]) == "appointment"
    assert b([{"text": "원장님께 여쭙고 회신"}]) == "doctor_reply"
    assert b([{"text": "업체에 다시 전화"}]) == "other"


def test_score(paths):
    import sqlite3

    c = sqlite3.connect(paths / "app.sqlite")
    c.row_factory = sqlite3.Row
    c.execute("ATTACH DATABASE ? AS source", (str(paths / "source.sqlite"),))
    c.execute("ATTACH DATABASE ? AS truth", (str(paths / "truth.sqlite"),))
    c.execute(
        "UPDATE ax_call_classifications SET todo = ? WHERE call_uid = 'c-0001' AND prompt_version = 'v1'",
        ('[{"kind": "contact", "text": "원장님께 여쭙고 회신", "who": "", "after": -1}]',),
    )
    runs = [
        {"call_uid": "c-0001", "action": "doctor_reply", "status": "done", "cost_usd": 0.01},
        {"call_uid": "c-0004", "action": "billing", "status": "done", "cost_usd": 0.01},
        {"call_uid": "c-0005", "action": None, "status": "failed", "cost_usd": 0.01},
    ]
    truth = {"c-0001": "doctor_reply", "c-0004": "appointment", "c-0005": "appointment"}
    split_calls = {"dev": ["c-0001"], "validation": ["c-0004", "c-0005"]}
    s = ev_followup.score(c, runs, truth, split_calls)
    assert s["dev"]["agent"]["k"] == 1 and s["dev"]["baseline"]["k"] == 1
    assert s["validation"]["agent"] == {
        "k": 0,
        "n": 2,
        "by_action": {"appointment": {"k": 0, "n": 2}},
    }
    assert s["validation"]["failed"] == 1


def test_dev_report_has_followup(tmp_path):
    from evaluation import eval as ev
    from tests.evaluation.test_rag import FakeApp, make_rag_db

    conn = make_rag_db(tmp_path)
    runs, acts = tmp_path / "runs.jsonl", tmp_path / "acts.jsonl"
    runs.write_text('{"call_uid": "c-0001", "action": "other", "status": "done", "cost_usd": 0}\n')
    acts.write_text('{"call_uid": "c-0001", "llm_action": "other", "human_action": null}\n')
    out = ev.run(
        conn,
        reports_dir=tmp_path / "r",
        app=FakeApp(),
        split="dev",
        followup_runs=runs,
        call_actions_path=acts,
    )
    md = out["report_path"].read_text(encoding="utf-8")
    assert "## ① 후속 처리" in md


def test_new_prompt_version_runs_again(paths):
    """줄이 있어도 프롬프트 버전이 다르면 다시 돈다 — 건너뛰는 키는 (통화, 분류 행, 프롬프트 버전)"""
    _run(paths, {})
    rows = ev_followup.load(paths / "runs.jsonl")
    old = [dict(r, prompt_version="v0") for r in rows]
    (paths / "runs.jsonl").write_text("".join(__import__("json").dumps(r) + "\n" for r in old))
    assert _run(paths, {})["done"] == len(rows)


def test_retry_cost_is_summed(paths):
    """후검증 실패로 두 번 돌면 두 실행의 비용을 합친다"""
    bad = SUBMIT | {"summary": "2031년 7월 9일 회신"}
    box = {}
    ev_followup.run(
        paths / "app.sqlite",
        paths / "source.sqlite",
        paths / "truth.sqlite",
        out=paths / "runs.jsonl",
        copy=paths / "runs" / "copy.sqlite",
        state=paths / "runs" / "s.json",
        gw_factory=lambda c: box.setdefault(
            "gw",
            BatchScript(
                c, [("tool", "submit_followup", bad)], [("tool", "submit_followup", SUBMIT)]
            ),
        ),
        min_batch=0,
        poll_seconds=0,
    )
    import sqlite3

    rows = ev_followup.load(paths / "runs.jsonl")
    c = sqlite3.connect(paths / "runs" / "copy.sqlite")
    total = c.execute("SELECT SUM(cost_usd) FROM ax_agent_runs").fetchone()[0]
    assert abs(sum(r["cost_usd"] for r in rows) - total) < 1e-9


def test_score_by_version_extra_and_reasons(paths):
    import sqlite3

    c = sqlite3.connect(paths / "app.sqlite")
    c.row_factory = sqlite3.Row
    c.execute("ATTACH DATABASE ? AS source", (str(paths / "source.sqlite"),))
    c.execute("ATTACH DATABASE ? AS truth", (str(paths / "truth.sqlite"),))
    c.execute(  # 분류기만 할 일(골드는 할 일 없음) — c-0002는 dev 풀
        "UPDATE ax_call_classifications SET labels = json_set(labels, '$.has_todo', 1)"
        " WHERE call_uid = 'c-0002' AND prompt_version = 'v1'"
    )
    runs = [
        {
            "call_uid": "c-0001",
            "action": "other",
            "status": "done",
            "cost_usd": 0,
            "prompt_version": "v1",
        },
        {
            "call_uid": "c-0001",
            "action": None,
            "status": "failed",
            "reason": "후검증 실패 — x",
            "cost_usd": 0,
            "prompt_version": "v2",
        },
    ]
    s1 = ev_followup.score(c, runs, {"c-0001": "other"}, {"dev": ["c-0001"]}, prompt_version="v1")
    s2 = ev_followup.score(c, runs, {"c-0001": "other"}, {"dev": ["c-0001"]}, prompt_version="v2")
    assert s1["dev"]["agent"]["k"] == 1 and s2["dev"]["agent"]["k"] == 0
    assert s2["dev"]["failed_by_reason"] == {"후검증 실패": 1}
    assert s1["dev"]["extra_by_classifier"] == 1
