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


def test_baseline_doctor_note():
    b = ev_followup.baseline
    assert b([{"text": "원장님께 앞니 파절 상황 미리 전달하기"}]) == "doctor_note"
    assert b([{"text": "원장님께 전달하고 회신 전화"}]) == "doctor_reply"


class Costly(BatchScript):
    """호출마다 비용 0.01 — 이번 실행 비용이 요약에 더해지는지 보려고"""

    def complete(self, alias, **kw):
        from dataclasses import replace

        return replace(super().complete(alias, **kw), cost_usd=0.01)


def test_reported_cost_is_this_run_only(paths):
    """요약 비용은 이번 실행의 통화만 — 사본에 남은 앞 버전 실행 비용은 빼고, 이번 실행 비용은 더한다"""
    import json
    import sqlite3

    _run(paths, {})
    c = sqlite3.connect(paths / "runs" / "copy.sqlite")
    c.execute("UPDATE ax_agent_runs SET cost_usd = 5.0")  # 앞 버전 실행이 남긴 비용
    c.commit()
    c.close()
    rows = ev_followup.load(paths / "runs.jsonl")
    (paths / "runs.jsonl").write_text(
        "".join(json.dumps(dict(r, prompt_version="v0")) + "\n" for r in rows)
    )
    r = ev_followup.run(
        paths / "app.sqlite",
        paths / "source.sqlite",
        paths / "truth.sqlite",
        out=paths / "runs.jsonl",
        copy=paths / "runs" / "copy.sqlite",
        state=paths / "runs" / "state.json",
        gw_factory=lambda c: Costly(c, [("tool", "submit_followup", SUBMIT)]),
        min_batch=0,
        poll_seconds=0,
    )
    new = [x for x in ev_followup.load(paths / "runs.jsonl") if x["prompt_version"] != "v0"]
    assert 0 < r["cost_usd"] < 5.0
    assert r["cost_usd"] == pytest.approx(sum(x["cost_usd"] for x in new))


def test_dev_report_flags_old_labels(tmp_path):
    """다시 라벨하다 실패해 옛 프롬프트 버전 라벨이 남으면 리포트에 그 수를 보인다"""
    from evaluation import eval as ev
    from tests.evaluation.test_rag import FakeApp, make_rag_db

    conn = make_rag_db(tmp_path)
    acts = tmp_path / "acts.jsonl"
    acts.write_text(
        '{"call_uid": "c-0001", "llm_action": "other", "human_action": null, "prompt_version": "v0"}\n'
    )
    out = ev.run(
        conn, reports_dir=tmp_path / "r", app=FakeApp(), split="dev", call_actions_path=acts
    )
    assert "옛 버전 라벨 1통화" in out["report_path"].read_text(encoding="utf-8")


# ── test 묶음 followup (평가 설계 §5·§7) ─────────────────────────────


def _todo_c3(c):
    """통화 3(test 판정 풀)을 골드·분류 모두 할 일로"""
    c.execute("UPDATE truth.truth_call_labels SET has_todo = 1 WHERE call_uid = 'c-0003'")
    c.execute(
        "UPDATE ax_call_classifications SET labels = json_set(labels, '$.has_todo', 1), todo = '[]'"
        " WHERE call_uid = 'c-0003' AND labels IS NOT NULL"
    )


def test_run_default_skips_test_and_split_test_runs_only_test(paths):
    """make followup-eval(기본)은 dev·검증 구간만, --split test는 test 판정 풀만 돈다"""
    import sqlite3

    c = sqlite3.connect(paths / "app.sqlite")
    c.execute("ATTACH DATABASE ? AS truth", (str(paths / "truth.sqlite"),))
    _todo_c3(c)
    c.commit()
    c.close()
    _run(paths, {})
    assert "c-0003" not in {r["call_uid"] for r in ev_followup.load(paths / "runs.jsonl")}
    n = len(ev_followup.load(paths / "runs.jsonl"))
    r = ev_followup.run(
        paths / "app.sqlite",
        paths / "source.sqlite",
        paths / "truth.sqlite",
        out=paths / "runs.jsonl",
        copy=paths / "runs" / "copy.sqlite",
        state=paths / "runs" / "state.json",
        gw_factory=lambda c: BatchScript(c, [("tool", "submit_followup", SUBMIT)]),
        parts=("test",),
        min_batch=0,
        poll_seconds=0,
    )
    rows = ev_followup.load(paths / "runs.jsonl")
    assert r["done"] == 1 and len(rows) == n + 1 and rows[-1]["call_uid"] == "c-0003"


def _test_conn(tmp_path):
    from tests.evaluation.test_rag import make_rag_db

    conn = make_rag_db(tmp_path)
    _todo_c3(conn)
    return conn


def _cls_id(conn, uid):
    return conn.execute(
        "SELECT id FROM ax_call_classifications WHERE call_uid = ? AND prompt_version = 'v1'"
        " AND model_alias = 'classify'",
        (uid,),
    ).fetchone()[0]


def _write(path, rows):
    import json

    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


def test_test_followup_refuses_without_labels_or_runs(tmp_path):
    """test 정답 라벨이나 test 실행이 없으면 리포트·이력을 쓰기 전에 멈춘다 — 1회 기회를 소진하지 않게"""
    from evaluation import EvalRefused, call_actions
    from evaluation import eval as ev
    from tests.evaluation.test_rag import FakeApp

    conn = _test_conn(tmp_path)
    reports, runs, acts = tmp_path / "r", tmp_path / "runs.jsonl", tmp_path / "acts.jsonl"
    kw = {"reports_dir": reports, "app": FakeApp(), "split": "test", "section": "followup"}
    with pytest.raises(EvalRefused, match="make action-label"):  # 라벨 파일 없음
        ev.run(conn, followup_runs=runs, call_actions_path=acts, **kw)
    dev_only = {"call_uid": "c-0001", "llm_action": "other", "human_action": None}
    _write(acts, [dev_only | {"prompt_version": call_actions.PROMPT_VERSION}])
    with pytest.raises(EvalRefused, match="make action-label"):  # test 라벨 없음
        ev.run(conn, followup_runs=runs, call_actions_path=acts, **kw)
    _write(acts, [dict(dev_only, call_uid="c-0003", prompt_version="v0")])
    with pytest.raises(EvalRefused, match="make action-label"):  # 옛 버전 라벨
        ev.run(conn, followup_runs=runs, call_actions_path=acts, **kw)
    _write(acts, [dict(dev_only, call_uid="c-0003", prompt_version=call_actions.PROMPT_VERSION)])
    with pytest.raises(EvalRefused, match="--split test"):  # 실행 없음
        ev.run(conn, followup_runs=runs, call_actions_path=acts, **kw)
    for bad in ({"prompt_version": "v1"}, {"condition": "reranker=off"}):
        with pytest.raises(EvalRefused):  # 묶음과 안 맞는 옵션
            ev.run(conn, followup_runs=runs, call_actions_path=acts, **kw, **bad)
    assert not reports.exists()


def test_test_followup_scores_test_only_once(tmp_path):
    """test 묶음은 test 판정 풀만 집계로 — 후속 프롬프트 버전 × 활성 분류 버전마다 한 번"""
    import json

    from evaluation import call_actions
    from evaluation import eval as ev
    from tests.evaluation.test_rag import FakeApp

    conn = _test_conn(tmp_path)
    reports, runs, acts = tmp_path / "r", tmp_path / "runs.jsonl", tmp_path / "acts.jsonl"
    pv = ev_followup.followup_version()
    _write(
        acts,
        [
            {
                "call_uid": u,
                "llm_action": "other",
                "human_action": None,
                "prompt_version": call_actions.PROMPT_VERSION,
            }
            for u in ("c-0001", "c-0003")
        ],
    )
    _write(
        runs,
        [
            {
                "call_uid": u,
                "classification_id": _cls_id(conn, u),
                "prompt_version": pv,
                "action": "other",
                "status": "done",
                "cost_usd": 0.01,
            }
            for u in ("c-0001", "c-0003")
        ],
    )
    kw = {"reports_dir": reports, "app": FakeApp(), "split": "test", "section": "followup"}
    out = ev.run(conn, followup_runs=runs, call_actions_path=acts, **kw)
    data = json.loads(out["report_path"].with_suffix(".json").read_text(encoding="utf-8"))
    f = data["followup"]
    assert list(f["score"]) == ["test"] and f["score"]["test"]["agent"]["k"] == 1
    assert f["labels"] == 1 and data["prompt_version"] == f"{pv}×v1"
    assert data["classification"] is None and data["rag"] is None
    assert "## ① 후속 처리" in out["report_path"].read_text(encoding="utf-8")
    with pytest.raises(ev.AlreadyRun):
        ev.run(conn, followup_runs=runs, call_actions_path=acts, **kw)
    ev.run(conn, followup_runs=runs, call_actions_path=acts, force=True, **kw)


def test_dev_report_followup_excludes_test(tmp_path):
    """dev 리포트의 후속 처리는 dev·검증 구간만 — test 라벨·실행이 있어도 보이지 않는다"""
    import json

    from evaluation import eval as ev
    from tests.evaluation.test_rag import FakeApp

    conn = _test_conn(tmp_path)
    runs, acts = tmp_path / "runs.jsonl", tmp_path / "acts.jsonl"
    _write(
        acts,
        [
            {"call_uid": u, "llm_action": "other", "human_action": None}
            for u in ("c-0001", "c-0003")
        ],
    )
    _write(runs, [{"call_uid": "c-0003", "action": "other", "status": "done", "cost_usd": 0}])
    out = ev.run(
        conn,
        reports_dir=tmp_path / "r",
        app=FakeApp(),
        split="dev",
        followup_runs=runs,
        call_actions_path=acts,
    )
    data = json.loads(out["report_path"].with_suffix(".json").read_text(encoding="utf-8"))
    f = data["followup"]
    assert "test" not in f["score"] and f["labels"] == 1
    assert (
        data["rag"]["retrieval"]["hit5"]["n"] == 3
    )  # 후속 처리 절이 검색 절 구간을 건드리지 않는다
