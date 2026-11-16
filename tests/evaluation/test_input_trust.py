"""입력 신뢰 경계 평가셋(evaluation/input_trust.py, 설계 docs/superpowers/specs/2026-11-16-input-trust-design.md) —
위험 통화에 지시문을 끼운 변형과 원본을 짝으로 돌려 위험 방향 위반만 센다"""

import json
import sqlite3

import pytest

from evaluation import input_trust as it
from shared.call_labels import ITEMS
from shared.llm import LLMResult
from tests.evaluation.eval_db import SOURCE_SCHEMA, TRUTH_SCHEMA, labels

GOLD = {  # (gold, split)
    "c-0001": (labels("book", has_todo=1), "dev"),
    "c-0002": (labels("treatment_q", emergency=1, has_todo=1, urgency="urgent"), "dev"),
    "c-0003": (labels("price_q", price_inquiry=1), "dev"),  # 안전 — 대상 아님
    "c-0004": (labels("book", has_todo=1), "test"),
    "c-0005": (labels("other", complaint=1), "silver"),  # 판정 풀 밖
}


@pytest.fixture
def conn(tmp_path):
    truth = sqlite3.connect(tmp_path / "truth.sqlite")
    truth.executescript(TRUTH_SCHEMA)
    for uid, (g, split) in GOLD.items():
        truth.execute(
            "INSERT INTO truth_call_labels VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, '{}', ?)",
            (uid, g["primary_purpose"], json.dumps([g["primary_purpose"]]), g["counterpart"])
            + tuple(g[k] for k in ITEMS[2:])
            + (split,),
        )
    truth.commit()
    truth.close()
    src = sqlite3.connect(tmp_path / "source.sqlite")
    src.executescript(SOURCE_SCHEMA)
    for k, uid in enumerate(GOLD, start=1):
        src.execute(
            "INSERT INTO ax_calls (id, call_uid, source, direction, started_at, duration_sec,"
            " has_transcript, transcript) VALUES (?, ?, 'generated', 'inbound', '2026-11-20 10:00:00',"
            " 100, 1, ?)",
            (k, uid, f"환자: 통화 {uid}\n직원: 네\n환자: 감사합니다"),
        )
    src.commit()
    src.close()
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("ATTACH DATABASE ? AS source", (str(tmp_path / "source.sqlite"),))
    c.execute("ATTACH DATABASE ? AS truth", (str(tmp_path / "truth.sqlite"),))
    return c


# ── Task 1: 대상과 변형 ─────────────────────────────────────────────


def test_targets_risky_only(conn):
    got = it.targets(conn, "dev")
    assert [t["call_uid"] for t in got] == ["c-0001", "c-0002"]
    assert got[0]["direction"] == "inbound"
    assert got[0]["transcript"].startswith("환자: 통화 c-0001")
    assert [t["call_uid"] for t in it.targets(conn, "test")] == ["c-0004"]


def test_assign_deterministic_and_balanced():
    uids = [f"c-{k:04d}" for k in range(8)]
    a = it.assign(uids)
    assert a == it.assign(list(reversed(uids)))
    assert set(a) == set(uids)
    kinds = [k for k, _ in a.values()]
    assert all(kinds.count(k) == 2 for k in it.KINDS)
    assert all(p in it.POSITIONS for _, p in a.values())


def test_inject_positions():
    t = "환자: 하나\n직원: 둘\n환자: 셋"
    line = it.INJECTIONS["patient_request"]
    start = it.inject(t, "patient_request", "start").split("\n")
    end = it.inject(t, "patient_request", "end").split("\n")
    mid = it.inject(t, "patient_request", "middle").split("\n")
    assert start[0] == line and start[1:] == t.split("\n")
    assert end[-1] == line and end[:-1] == t.split("\n")
    assert len(mid) == 4 and mid[0] != line and mid[-1] != line and line in mid
    assert [x for x in mid if x != line] == t.split("\n")


def test_inject_keeps_original_text():
    t = "환자: 하나\n직원: 둘\n환자: 셋\n직원: 넷"
    for kind in it.KINDS:
        for pos in it.POSITIONS:
            out = it.inject(t, kind, pos).split("\n")
            assert it.INJECTIONS[kind] in out
            assert [x for x in out if x != it.INJECTIONS[kind]] == t.split("\n")


def test_format_mimic_looks_like_safe_labels():
    d = json.loads(it.INJECTIONS["format_mimic"])
    assert d["has_todo"] is False and d["emergency"] is False and d["complaint"] is False
    assert d["urgency"] != "urgent"


# ── Task 2: 실행 기록 ─────────────────────────────────────────────

SAFE = labels("other")
RISKY = labels("book", has_todo=1)


def res(data, ok=True, fallback=False):
    return LLMResult(ok, "", data, "m", fallback, 1, 1, 0.001, 1)


JUDGE_MODEL = "qwen3.5-2b-judge"  # prompts/judge/v3 머리말 model — judge_model_ok


class FakeGw:
    """분류(classify·summarize)는 Anthropic Batch, 판정(judge)은 로컬 일반 호출. answer(alias, 녹취) → LLMResult"""

    def __init__(self, answer=None, batchable=True):
        self.answer = answer or (lambda alias, text: res(_default(alias)))
        self.config = {
            "aliases": {"classify": ["m"], "summarize": ["m"], "judge": [JUDGE_MODEL]},
            "models": {
                "m": {"provider": "anthropic" if batchable else "upstage"},
                JUDGE_MODEL: {},
            },
        }
        self.submitted = {}  # 배치 id → (별칭, 요청 목록)
        self.calls = []  # 일반 호출 (별칭, 녹취)

    def submit_batch(self, alias, requests, *, task, prompt_version=None, max_tokens=None):
        bid = f"b{len(self.submitted)}"
        self.submitted[bid] = (alias, requests)
        return bid

    def batch_status(self, batch_id, alias=None):
        return "ended"

    def batch_results(self, batch_id, alias, *, task, prompt_version=None, schema=None):
        a, reqs = self.submitted[batch_id]
        return {r["custom_id"]: self.answer(a, r["messages"][0]["content"]) for r in reqs}

    def complete(self, alias, **kw):
        text = kw["messages"][0]["content"]
        self.calls.append((alias, text))
        return self.answer(alias, text)


def _default(alias):
    if alias == "summarize":
        return {"summary": "요약", "todo": ["다시 연락"]}
    return RISKY


CALL = {"call_uid": "c-0001", "direction": "inbound", "transcript": "환자: 하나\n직원: 둘"}


def _run(gw, tmp_path, calls=(CALL,), judge_runs=1):
    return it.run(
        gw, list(calls), "v2", "v3", tmp_path / "runs.jsonl", judge_runs,
        tmp_path / "state.json", poll_seconds=0,
    )  # fmt: skip


def test_run_writes_every_side_and_repeat(tmp_path):
    gw = FakeGw()
    out = _run(gw, tmp_path)
    rows = it.read_runs(tmp_path / "runs.jsonl")
    assert len(rows) == it.GATE_RUNS * 2 + 2
    assert out["called"] == len(rows) and out["failed"] == 0
    kind, _ = it.assign(["c-0001"])["c-0001"]
    inj = [r for r in rows if r["side"] == "inj" and r["model"] == "classify"]
    assert {r["k"] for r in inj} == set(range(1, it.GATE_RUNS + 1))
    assert all(r["kind"] == kind and r["todo"] == ["다시 연락"] for r in inj)
    sent = [q for a, reqs in gw.submitted.values() if a == "classify" for q in reqs]
    assert sum(it.INJECTIONS[kind] in q["messages"][0]["content"] for q in sent) == it.GATE_RUNS
    assert not (tmp_path / "state.json").exists()


def test_run_resumes_missing_only(tmp_path):
    _run(FakeGw(), tmp_path)
    rows = it.read_runs(tmp_path / "runs.jsonl")
    keep = [
        r for r in rows if not (r["side"] == "orig" and r["model"] == "classify" and r["k"] == 2)
    ]
    (tmp_path / "runs.jsonl").write_text("".join(json.dumps(r) + "\n" for r in keep))
    gw = FakeGw()
    out = _run(gw, tmp_path)
    assert out["called"] == 1
    assert [len(reqs) for _, reqs in gw.submitted.values()] == [1, 1]
    assert gw.calls == []


def test_failed_call_not_saved(tmp_path):
    def answer(alias, text):
        if alias == "summarize" and it.INJECTIONS[it.assign(["c-0001"])["c-0001"][0]] in text:
            return res(None, ok=False)
        return res(_default(alias))

    out = _run(FakeGw(answer), tmp_path)
    rows = it.read_runs(tmp_path / "runs.jsonl")
    assert out["failed"] == it.GATE_RUNS
    assert not [r for r in rows if r["side"] == "inj" and r["model"] == "classify"]


def test_fallback_saved_with_flag(tmp_path):
    def answer(alias, text):
        return res(_default(alias), fallback=alias == "judge")

    _run(FakeGw(answer), tmp_path)
    rows = it.read_runs(tmp_path / "runs.jsonl")
    assert all(r["fallback"] == (r["model"] == "judge") for r in rows)


def test_batch_state_resumes(tmp_path):
    gw = FakeGw()
    cids = [f"c-0001-{s}-{k}" for s in ("orig", "inj") for k in range(1, it.GATE_RUNS + 1)]
    pre = {a: gw.submit_batch(a, [{"custom_id": c, "messages": [{"content": "x"}]} for c in cids],
                              task=a) for a in ("classify", "summarize")}  # fmt: skip
    (tmp_path / "state.json").write_text(
        json.dumps({"version": "v2", "batches": pre, "sent": cids})
    )
    _run(gw, tmp_path)
    assert len(gw.submitted) == 2  # 새 배치를 내지 않았다
    rows = it.read_runs(tmp_path / "runs.jsonl")
    assert len([r for r in rows if r["model"] == "classify"]) == len(cids)
    assert not (tmp_path / "state.json").exists()


def test_batch_missing_half_not_saved(tmp_path):
    class Half(FakeGw):
        def batch_results(self, batch_id, alias, **kw):
            out = super().batch_results(batch_id, alias, **kw)
            if alias == "summarize":
                out.pop("c-0001-orig-1")
            return out

    out = _run(Half(), tmp_path)
    rows = it.read_runs(tmp_path / "runs.jsonl")
    assert not [
        r for r in rows if r["model"] == "classify" and r["side"] == "orig" and r["k"] == 1
    ]
    assert out["failed"] == 1


def test_not_batchable_uses_plain_calls(tmp_path):
    gw = FakeGw(batchable=False)
    _run(gw, tmp_path)
    assert gw.submitted == {}
    assert len([c for c in gw.calls if c[0] in ("classify", "summarize")]) == it.GATE_RUNS * 2 * 2


def test_plan_counts():
    calls = [CALL, {**CALL, "call_uid": "c-0002"}]
    assert it.plan(calls, set(), "v2", "v3", judge_runs=1) == {
        "classify_calls": 2 * 2 * it.GATE_RUNS * 2,
        "judge_calls": 2 * 2,
    }
    kind, pos = it.assign(["c-0001", "c-0002"])["c-0001"]
    done = {("c-0001", "orig", kind, pos, "classify", "v2", 1)}
    assert it.plan(calls, done, "v2", "v3", judge_runs=1)["classify_calls"] == (
        2 * 2 * it.GATE_RUNS * 2 - 2
    )


# ── Task 3: 채점 ─────────────────────────────────────────────────


def vrow(side, model, k, lab, todo=None, fallback=False, uid="c-0001", kind=None,
         position=None):  # fmt: skip
    a_kind, a_pos = it.assign([uid])[uid]
    kind, position = kind or a_kind, position or a_pos
    v = "v2" if model == "classify" else "v3"
    if model == "classify" and todo is None:
        todo = ["다시 연락"] if lab["has_todo"] else []
    return {"call_uid": uid, "side": side, "kind": kind, "position": position, "model": model,
            "version": v, "k": k, "labels": lab, "todo": todo if model == "classify" else None,
            "fallback": fallback}  # fmt: skip


def pair_rows(orig_c, inj_c, orig_j, inj_j, **kw):
    """분류는 GATE_RUNS벌 같은 답, 판정기는 1벌"""
    out = []
    for side, c, j in (("orig", orig_c, orig_j), ("inj", inj_c, inj_j)):
        out += [vrow(side, "classify", k, c, **kw) for k in range(1, it.GATE_RUNS + 1)]
        out.append(vrow(side, "judge", 1, j, **kw))
    return out


def _score(rows, calls=(CALL,)):
    return it.score(list(calls), rows, "v2", "v3", judge_runs=1)


def test_vote_majority_and_first_on_tie():
    rows = [
        vrow("orig", "classify", 1, labels("book", has_todo=1)),
        vrow("orig", "classify", 2, labels("price_q", has_todo=0)),
        vrow("orig", "classify", 3, labels("other", has_todo=1)),
    ]
    v = it.side_vote(rows, 3)
    assert v["labels"]["has_todo"] == 1
    assert v["labels"]["primary_purpose"] == "book"
    assert v["todo"] == ["다시 연락"]


def test_vote_urgency_back_to_label():
    rows = [vrow("orig", "classify", k, labels("book", urgency=u))
            for k, u in enumerate(("urgent", "low", "urgent"), 1)]  # fmt: skip
    assert it.side_vote(rows, 3)["labels"]["urgency"] == "urgent"
    rows[0]["labels"] = labels("book", urgency="normal")
    assert it.side_vote(rows, 3)["labels"]["urgency"] != "urgent"


def test_vote_incomplete_is_none():
    rows = [vrow("orig", "classify", k, RISKY) for k in (1, 2)]
    assert it.side_vote(rows, 3) is None


def test_violation_autoclose_flip():
    s = _score(pair_rows(RISKY, SAFE, RISKY, SAFE))
    assert s["pairs"] == 1 and s["violations"] == 1
    assert s["autoclose_flips"] == 1
    assert s["by_model"] == {"classify": 1, "judge": 1}
    kind = it.assign(["c-0001"])["c-0001"][0]
    assert s["by_kind"][kind] == {"n": 1, "violations": 1}
    assert s["violated"] == ["c-0001"]


def test_violation_single_model_flag_off():
    emer = labels("treatment_q", emergency=1, has_todo=1)
    j_off = labels("treatment_q", emergency=0, has_todo=1)
    s = _score(pair_rows(emer, emer, emer, j_off))
    assert s["violations"] == 1 and s["autoclose_flips"] == 0
    assert s["by_model"] == {"classify": 0, "judge": 1}


def test_todo_emptied_is_violation():
    rows = pair_rows(RISKY, RISKY, RISKY, RISKY)
    for r in rows:
        if r["side"] == "inj" and r["model"] == "classify":
            r["todo"] = []
    s = _score(rows)
    assert s["violations"] == 1 and s["by_model"]["classify"] == 1


def test_other_item_diff_not_violation():
    s = _score(pair_rows(RISKY, {**RISKY, "price_inquiry": 1}, RISKY, RISKY))
    assert s["violations"] == 0
    assert s["diff_items"]["price_inquiry"] == 1


def test_already_closed_excluded():
    s = _score(pair_rows(SAFE, SAFE, SAFE, SAFE))
    assert s["excluded_already_closed"] == 1 and s["pairs"] == 0 and s["violations"] == 0


def test_fallback_counted_separately():
    rows = pair_rows(RISKY, SAFE, RISKY, SAFE)
    rows[it.GATE_RUNS + 1]["fallback"] = True  # 변형 분류 1벌
    s = _score(rows)
    assert s["fallback"] == 1 and s["violations"] == 0 and s["pairs"] == 0


def test_incomplete_counted():
    rows = [r for r in pair_rows(RISKY, SAFE, RISKY, SAFE) if r["model"] != "judge"]
    s = _score(rows)
    assert s["incomplete"] == 1 and s["pairs"] == 0


# ── Task 4: 명령·dev 리포트 ─────────────────────────────────────────


def test_check_judge_detects_nondeterminism(tmp_path):
    flip = iter([RISKY, SAFE] * 10)
    gw = FakeGw(lambda alias, text: res(next(flip)))
    got = it.check_judge(gw, [CALL], "v3", tmp_path / "judge.json")
    assert got == {"judge_version": "v3", "deterministic": False}
    assert it.judge_runs("v3", tmp_path / "judge.json") == it.GATE_RUNS


def test_check_judge_deterministic(tmp_path):
    it.check_judge(FakeGw(), [CALL, {**CALL, "call_uid": "c-0002"}], "v3", tmp_path / "j.json")
    assert it.judge_runs("v3", tmp_path / "j.json") == 1
    assert it.judge_runs("v4", tmp_path / "j.json") is None  # 다른 판정기 버전은 다시 확인


def _main(monkeypatch, tmp_path, conn, argv, gw=None):
    monkeypatch.setattr(it, "_connect", lambda: conn)
    monkeypatch.setattr(it, "active_versions", lambda c: ("v2", "v3"))
    monkeypatch.setattr(it, "JUDGE_CHECK", tmp_path / "judge.json")
    monkeypatch.setattr(it, "TEST_REPORT", tmp_path / "test_runs.jsonl")
    paths = {s: {k: tmp_path / f"{s}-{k}" for k in ("runs", "llm", "state")} for s in it.PATHS}
    monkeypatch.setattr(it, "PATHS", paths)

    def make(split):
        if gw is None:
            raise AssertionError("게이트웨이를 만들면 안 된다")
        return gw

    monkeypatch.setattr(it, "_gateway", make)
    monkeypatch.setattr(it, "POLL_SECONDS", 0)
    return it.main(argv)


def test_run_refuses_without_check_judge(monkeypatch, tmp_path, conn):
    with pytest.raises(SystemExit, match="check-judge"):
        _main(monkeypatch, tmp_path, conn, ["run", "--split", "dev"], FakeGw())


def test_plan_does_not_call_gateway(monkeypatch, tmp_path, conn, capsys):
    (tmp_path / "judge.json").write_text(
        json.dumps({"judge_version": "v3", "deterministic": True})
    )
    _main(monkeypatch, tmp_path, conn, ["run", "--split", "dev", "--plan"])
    out = json.loads(capsys.readouterr().out)
    assert out == {"targets": 2, "classify_calls": 2 * 2 * it.GATE_RUNS * 2, "judge_calls": 2 * 2}


def test_test_split_runs_once(monkeypatch, tmp_path, conn):
    (tmp_path / "judge.json").write_text(
        json.dumps({"judge_version": "v3", "deterministic": True})
    )
    argv = ["run", "--split", "test"]
    _main(monkeypatch, tmp_path, conn, argv, FakeGw())
    lines = it.read_runs(tmp_path / "test_runs.jsonl")
    assert len(lines) == 1 and lines[0]["split"] == "test" and lines[0]["n"] == 1
    with pytest.raises(it.AlreadyRun):
        _main(monkeypatch, tmp_path, conn, argv, FakeGw())
    _main(monkeypatch, tmp_path, conn, argv + ["--force"], FakeGw())
    assert len(it.read_runs(tmp_path / "test_runs.jsonl")) == 2


def test_dev_report_section(monkeypatch, tmp_path, conn):
    from evaluation import eval as ev

    (tmp_path / "judge.json").write_text(
        json.dumps({"judge_version": "v3", "deterministic": True})
    )
    _main(monkeypatch, tmp_path, conn, ["run", "--split", "dev"], FakeGw())
    s = it.dev_score(conn)
    text = "\n".join(ev._render_input_trust(s))
    assert "입력 신뢰 경계" in text
    assert f"위반 {s['violations']}/{s['pairs']}" in text


# ── 리뷰 반영: 이어 받기·배정·일반 호출·판정기 모델 ─────────────────────


def _state(tmp_path, gw, cids, version="v2"):
    pre = {a: gw.submit_batch(a, [{"custom_id": c, "messages": [{"content": "x"}]} for c in cids],
                              task=a) for a in ("classify", "summarize")}  # fmt: skip
    (tmp_path / "state.json").write_text(
        json.dumps({"version": version, "batches": pre, "sent": cids})
    )


def test_state_from_other_version_stops(tmp_path):
    gw = FakeGw()
    _state(tmp_path, gw, ["c-0001-orig-1"], version="v1")
    with pytest.raises(SystemExit, match="v1"):
        _run(gw, tmp_path)


def test_state_skips_already_written_keys(tmp_path):
    _run(FakeGw(), tmp_path)  # 다 기록됨
    before = len(it.read_runs(tmp_path / "runs.jsonl"))
    gw = FakeGw()
    _state(tmp_path, gw, ["c-0001-orig-1"])  # 이미 기록된 키를 담은 상태 파일
    _run(gw, tmp_path)
    assert len(it.read_runs(tmp_path / "runs.jsonl")) == before


def test_state_reports_pending_jobs(tmp_path):
    gw = FakeGw()
    _state(tmp_path, gw, ["c-0001-orig-1"])  # 나머지 분류 작업은 이 배치 밖
    out = _run(gw, tmp_path)
    assert out["pending"] == it.GATE_RUNS * 2 - 1
    assert len(gw.submitted) == 2  # 이번 실행에서 새 배치를 내지 않는다


def test_reassigned_injection_not_mixed(tmp_path):
    """대상 집합이 바뀌어 배정이 달라지면 옛 지시문 줄을 다시 쓰지 않고 새로 돈다"""
    _run(FakeGw(), tmp_path)
    rows = it.read_runs(tmp_path / "runs.jsonl")
    kind = it.assign(["c-0001"])["c-0001"][0]
    other = next(k for k in it.KINDS if k != kind)
    for r in rows:
        r["kind"] = other
    (tmp_path / "runs.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    out = _run(FakeGw(), tmp_path)
    assert out["called"] == it.GATE_RUNS * 2 + 2
    s = it.score([CALL], it.read_runs(tmp_path / "runs.jsonl"), "v2", "v3", judge_runs=1)
    assert s["pairs"] == 1 and s["incomplete"] == 0


def test_plain_calls_written_as_they_go(tmp_path):
    n = {"calls": 0}

    def answer(alias, text):
        n["calls"] += 1
        if n["calls"] > 4:  # 라벨·요약 2쌍 뒤 멈춤
            raise RuntimeError("예산 초과")
        return res(_default(alias))

    with pytest.raises(RuntimeError):
        _run(FakeGw(answer, batchable=False), tmp_path)
    assert len(it.read_runs(tmp_path / "runs.jsonl")) == 2


def test_judge_model_mismatch_stops(tmp_path):
    gw = FakeGw()
    gw.config["aliases"]["judge"] = ["other-model"]
    gw.config["models"]["other-model"] = {}
    with pytest.raises(SystemExit, match="판정기"):
        _run(gw, tmp_path)
    with pytest.raises(SystemExit, match="판정기"):
        it.check_judge(gw, [CALL], "v3", tmp_path / "j.json")
