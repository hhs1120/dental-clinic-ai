"""② 자동 개선 (evaluation/improve.py, 설계 docs/superpowers/specs/2026-11-17-auto-improve-design.md)."""

import json
import re
import sqlite3

import pytest

from evaluation import improve as im

RULE = "- has_todo: 통화 뒤 직원이 할 일(회신·전달·서류 준비 등)이 남음"
LABELS_MD = f"""당신은 통화를 분류합니다.

## 플래그(true/false)
- emergency: 응급
{RULE}
"""


def lab(**kw):
    base = {"primary_purpose": "book", "counterpart": "patient", "urgency": "normal",
            "emergency": 0, "complaint": 0, "price_inquiry": 0, "wants_appt": 0,
            "appt_confirmed": 0, "has_todo": 0}  # fmt: skip
    return base | kw


def gold_rows(n=40, extra=()):
    rows = [{"call_uid": f"c-{i:04d}", "split": "silver"} | lab(has_todo=i % 2) for i in range(n)]
    return rows + list(extra)


def app_db(rows, model_todo=0, status="ok", version="v2"):
    """기준 버전 분류(모델 has_todo는 모두 model_todo)와 녹취 — 평가 연결처럼 source를 붙인 모양"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("ATTACH DATABASE ':memory:' AS source")
    conn.execute("CREATE TABLE source.ax_calls (call_uid, transcript, direction)")
    conn.execute(
        "CREATE TABLE ax_call_classifications (call_uid, prompt_version, model_alias, status, labels)"
    )
    for r in rows:
        u = r["call_uid"]
        conn.execute("INSERT INTO source.ax_calls VALUES (?, ?, 'inbound')",
                     (u, f"직원: 네 {u} 원장님께 꼭 전달해 드리겠습니다 환자: 감사합니다"))  # fmt: skip
        conn.execute("INSERT INTO ax_call_classifications VALUES (?, ?, 'classify', ?, ?)",
                     (u, version, status, json.dumps(lab(has_todo=model_todo))))  # fmt: skip
    return conn


def test_material_only_silver_material_errors():
    rows = gold_rows()
    got = im.material_errors(app_db(rows), rows, "v2")
    part = im.silver_split(rows)
    uids = [g["call_uid"] for g in got]
    assert uids == sorted(uids) and uids
    assert all(part[u] == "material" for u in uids)
    gold = {r["call_uid"]: r["has_todo"] for r in rows}
    assert all(gold[u] == 1 for u in uids)  # 모델은 모두 0 — 골드 1인 재료만 오류
    assert {"transcript", "direction", "model", "gold"} <= set(got[0])
    assert got[0]["model"] == 0 and got[0]["gold"] == 1


@pytest.mark.parametrize("split", ["dev", "test", "hard"])
def test_material_rejects_holdout(split):
    rows = gold_rows(extra=[gold_rows(1)[0] | {"call_uid": "d-1", "split": split}])
    with pytest.raises(SystemExit, match="holdout_in_material"):
        im.material_errors(app_db(rows), rows, "v2")


def test_material_skips_failed_classifications():
    rows = gold_rows()
    assert im.material_errors(app_db(rows, status="failed"), rows, "v2") == []


def test_rule_line_unique():
    assert im.rule_line(LABELS_MD) == RULE
    with pytest.raises(ValueError):
        im.rule_line("- emergency: 응급")
    with pytest.raises(ValueError):
        im.rule_line(LABELS_MD + RULE + "\n")


def _base(root, name="v2"):
    d = root / "classify" / name
    d.mkdir(parents=True)
    (d / "labels.md").write_text(LABELS_MD, encoding="utf-8")
    (d / "summary.md").write_text("요약하세요", encoding="utf-8")
    return d


def test_candidate_changes_only_rule_line(tmp_path):
    base = _base(tmp_path)
    new = "- has_todo: 통화 뒤 직원이 해야 할 후속 행동이 남음"
    d = im.write_candidate("v2", "v2-imp1", new, root=tmp_path)
    old_lines, new_lines = LABELS_MD.splitlines(), (d / "labels.md").read_text().splitlines()
    assert [a for a, b in zip(old_lines, new_lines, strict=True) if a != b] == [RULE]
    assert new in new_lines
    assert (d / "summary.md").read_text() == (base / "summary.md").read_text()


def test_candidate_name_never_overwrites(tmp_path):
    _base(tmp_path)
    runs = tmp_path / "runs"
    assert im.candidate_name("v2", root=tmp_path, runs=runs) == "v2-imp1"
    (tmp_path / "classify" / "v2-imp1").mkdir()
    assert im.candidate_name("v2", root=tmp_path, runs=runs) == "v2-imp2"
    (runs / "candidates" / "v2-imp2").mkdir(parents=True)
    assert im.candidate_name("v2", root=tmp_path, runs=runs) == "v2-imp3"
    with pytest.raises(FileExistsError):
        im.write_candidate("v2", "v2-imp1", RULE, root=tmp_path)


MATERIAL = [{"call_uid": "c-0001", "transcript": "직원: 네 원장님께 꼭 전달해 드리겠습니다 환자: 감사합니다",
             "direction": "inbound", "model": 0, "gold": 1}]  # fmt: skip


def _cand(rule=None, extra=""):
    return (
        LABELS_MD.replace(RULE, rule or "- has_todo: 직원이 통화 뒤 해야 할 후속 행동이 남음")
        + extra
    )


def test_check_clean_candidate():
    assert im.check_candidate(LABELS_MD, _cand(), MATERIAL) == []


def test_check_scope():
    assert im.check_candidate(LABELS_MD, _cand(extra="- 새 규칙\n"), MATERIAL) == ["scope"]
    other = _cand().replace("- emergency: 응급", "- emergency: 응급 또는 통증")
    assert im.check_candidate(LABELS_MD, other, MATERIAL) == ["scope"]


def test_check_example():
    rule = '- has_todo: 직원이 "다시 연락드릴게요"처럼 약속하면 할 일이 남음'
    assert im.check_candidate(LABELS_MD, _cand(rule), MATERIAL) == ["example"]
    rule = "- has_todo: 직원이 “연락드릴게요”처럼 약속하면 할 일이 남음"
    assert im.check_candidate(LABELS_MD, _cand(rule), MATERIAL) == ["example"]


def test_check_copy():
    rule = "- has_todo: 원장님께 꼭 전달해 드리겠습니다 같은 약속이 있으면 할 일이 남음"
    assert im.check_candidate(LABELS_MD, _cand(rule), MATERIAL) == ["copy"]


def test_leaks():
    assert im.leaks("원장님께 꼭 전달해 드리겠습니다 유형", MATERIAL)
    assert im.leaks("c-0123 같은 통화", MATERIAL)
    assert not im.leaks("전달 약속을 할 일로 보지 않음", MATERIAL)


# ── 루프·기록(Task 4) ──
from evaluation import prompt_gate as pg  # noqa: E402

GOOD = "- has_todo: 직원이 통화 뒤 해야 할 후속 행동을 약속했으면 참"


class FakePropose:
    def __init__(self, *outs):
        self.outs, self.calls = list(outs), []

    def __call__(self, gw, store, base_rule, errors, failures, target):
        self.calls.append({"base_rule": base_rule, "failures": [dict(f) for f in failures],
                           "target": target})  # fmt: skip
        rule, types = self.outs.pop(0)
        return {"status": "done", "rule": rule, "types": types, "cost_usd": 0.5}


class FakeGate:
    def __init__(self, *results):
        self.results, self.calls = list(results), []

    def __call__(self, gw, a, b, item, **kw):
        self.calls.append((a, b, item, kw))
        r = self.results.pop(0)
        if isinstance(r, BaseException):
            raise r
        return {"passed": not r, "failed": r, "items": {"has_todo": {"b": 1, "c": 0}},
                "cost_usd": 0.4}  # fmt: skip


def _loop(tmp_path, propose, gate, pending=None, policy=None):
    rows = gold_rows()
    conn = app_db(rows)
    conn.execute("CREATE TABLE ax_settings (key PRIMARY KEY, value)")
    if pending:
        conn.execute("INSERT INTO ax_settings VALUES ('pending_prompt_version', ?)",
                     (json.dumps(pending),))  # fmt: skip
    if policy:
        conn.execute("INSERT INTO ax_settings VALUES ('auto_policy', ?)", (json.dumps(policy),))
    _base(tmp_path / "p")
    out = im.improve(
        None, conn, rows, {}, base="v2", config={}, gate_runner=gate, propose_fn=propose,
        prompts_root=tmp_path / "p", runs=tmp_path / "runs", report=tmp_path / "imp.jsonl",
        gate_report=tmp_path / "gate.jsonl", store=None, auto_for=lambda v: {"x": v},
    )  # fmt: skip
    return out, conn


def _pending(conn):
    got = conn.execute(
        "SELECT value FROM ax_settings WHERE key = 'pending_prompt_version'"
    ).fetchone()
    return json.loads(got[0]) if got else None


def test_pass_sets_pending(tmp_path):
    out, conn = _loop(tmp_path, FakePropose((GOOD, [{"name": "전달 놓침", "n": 2}])), FakeGate([]))
    assert len(out) == 1 and out[0]["adopted"] and _pending(conn) == "v2-imp1"
    assert (tmp_path / "p" / "classify" / "v2-imp1" / "labels.md").exists()
    row = json.loads((tmp_path / "imp.jsonl").read_text())
    assert row["candidate"] == "v2-imp1" and row["gate"]["passed"] and row["attempt"] == 1
    assert row["changed_rule"] == GOOD and row["error_types"] == [{"name": "전달 놓침", "n": 2}]
    assert row["gate_hash"] == pg.gate_hash() and row["cost_usd"] == 0.9


def test_fail_twice_no_pending(tmp_path):
    other = "- has_todo: 회신·서류처럼 직원이 해야 할 후속 행동이 남으면 참"
    prop = FakePropose((GOOD, []), (other, []))
    out, conn = _loop(tmp_path, prop, FakeGate(["target_up"], ["no_new_miss"]))
    assert len(out) == 2 and _pending(conn) is None and not any(r["adopted"] for r in out)
    assert prop.calls[1]["failures"] == [{"rule": GOOD, "failed": ["target_up"]}]
    assert not (tmp_path / "p" / "classify" / "v2-imp1").exists()
    assert (tmp_path / "runs" / "candidates" / "v2-imp1" / "labels.md").exists()
    assert prop.calls[1]["target"] == "v2-imp2"


def test_precheck_reject_counts_and_skips_gate(tmp_path):
    bad = '- has_todo: 직원이 "연락드릴게요"라 하면 참'
    gate = FakeGate([])
    out, conn = _loop(tmp_path, FakePropose((bad, []), (GOOD, [])), gate)
    assert out[0]["gate"] is None and out[0]["failed"] == ["example"]
    assert len(gate.calls) == 1 and gate.calls[0][1] == "v2-imp2"
    assert pg.attempts("v2", tmp_path / "gate.jsonl", tmp_path / "imp.jsonl") == {
        ("v2-imp1", out[0]["candidate_sha256"])
    }


def test_no_rule_counts_as_attempt(tmp_path):
    out, conn = _loop(tmp_path, FakePropose((None, []), (GOOD, [])), FakeGate([]))
    assert out[0]["failed"] == ["no_rule"] and out[0]["gate"] is None and out[1]["adopted"]


def test_candidate_moved_on_gate_error(tmp_path):
    with pytest.raises(SystemExit):
        _loop(tmp_path, FakePropose((GOOD, [])), FakeGate(SystemExit("빈 통화 1건")))
    assert not (tmp_path / "p" / "classify" / "v2-imp1").exists()
    assert (tmp_path / "runs" / "candidates" / "v2-imp1").exists()


def test_stops_before_llm(tmp_path):
    prop = FakePropose()
    with pytest.raises(SystemExit, match="pending_exists"):
        _loop(tmp_path, prop, FakeGate(), pending="v2-imp1")
    lines = [{"base": "v2", "candidate": f"v{i}", "candidate_sha256": f"s{i}",
              "gate_hash": pg.gate_hash()} for i in (7, 8)]  # fmt: skip
    (tmp_path / "gate.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
    conn = app_db(gold_rows())
    conn.execute("CREATE TABLE ax_settings (key PRIMARY KEY, value)")
    with pytest.raises(SystemExit, match="attempt_cap"):
        im.improve(None, conn, gold_rows(), {}, base="v2", config={}, gate_runner=FakeGate(),
                   propose_fn=prop, prompts_root=tmp_path / "p", runs=tmp_path / "runs",
                   report=tmp_path / "imp.jsonl", gate_report=tmp_path / "gate.jsonl",
                   store=None)  # fmt: skip
    assert prop.calls == []


def test_previous_failures_carried(tmp_path):
    prev = {"base": "v2", "candidate": "v2-imp9", "candidate_sha256": "s", "gate_hash": pg.gate_hash(),
            "changed_rule": "- has_todo: 예전 후보", "failed": ["copy"], "gate": None}  # fmt: skip
    (tmp_path / "imp.jsonl").write_text(json.dumps(prev) + "\n")
    prop = FakePropose((GOOD, []))
    _loop(tmp_path, prop, FakeGate([]))
    assert prop.calls[0]["failures"] == [{"rule": "- has_todo: 예전 후보", "failed": ["copy"]}]


def test_policy_on_passes_auto(tmp_path):
    gate = FakeGate([])
    _loop(tmp_path, FakePropose((GOOD, [])), gate, policy={"call_autoclose": True})
    assert gate.calls[0][3]["auto"] == {"x": "v2-imp1"}
    assert gate.calls[0][3]["policy"] == {"call_autoclose": True}


def test_free_text_leak_dropped(tmp_path):
    types = [{"name": "원장님께 꼭 전달해 드리겠습니다", "n": 1}]
    out, _ = _loop(tmp_path, FakePropose((GOOD, types)), FakeGate([]))
    assert out[0]["error_types"] is None and "leak" in out[0]["failed"]


def test_rows_have_no_call_ids(tmp_path):
    _loop(tmp_path, FakePropose((GOOD, [{"name": "c-0001 같은 통화", "n": 1}])), FakeGate([]))
    assert not re.search(r"c-\d+", (tmp_path / "imp.jsonl").read_text())


def test_plan_no_gateway(tmp_path):
    rows = gold_rows()
    conn = app_db(rows)
    conn.execute("CREATE TABLE ax_settings (key PRIMARY KEY, value)")
    got = im._plan(conn, rows, "v2", tmp_path / "gate.jsonl", tmp_path / "imp.jsonl")
    assert got["material_errors"] > 0 and got["attempts_left"] == 2 and got["pending"] is None
