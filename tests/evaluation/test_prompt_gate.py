"""① 분류 프롬프트 관문 (evaluation/prompt_gate.py, 설계 docs/superpowers/specs/2026-11-09-prompt-gate-design.md)."""

import pytest

from evaluation import prompt_gate as pg

BASE = {
    "primary_purpose": "book",
    "counterpart": "patient",
    "urgency": "normal",
    "emergency": 0,
    "complaint": 0,
    "price_inquiry": 0,
    "wants_appt": 0,
    "appt_confirmed": 0,
    "has_todo": 0,
}


def lab(**kw):
    return BASE | kw


def _item(b=0, c=0, base_wrong=0, cand_wrong=0, **kw):
    from evaluation.metrics import mcnemar_exact

    return {"b": b, "c": c, "p": mcnemar_exact(b, c), "base_wrong": base_wrong,
            "cand_wrong": cand_wrong, **kw}  # fmt: skip


def stats(**over):
    s = {i: _item() for i in pg.ITEMS}
    for i in ("emergency", "complaint"):
        s[i] |= {"new_miss": 0, "false_alarm_delta": 0}
    s["has_todo"] = _item(b=6, c=0)
    for k, v in over.items():
        s[k] = s[k] | v
    return s


def test_value_urgency_and_flags():
    assert pg.value({"urgency": "low"}, "urgency") == 0
    assert pg.value({"urgency": "urgent"}, "urgency") == 1
    assert pg.value({"has_todo": True}, "has_todo") == 1
    assert pg.value({"has_todo": 0}, "has_todo") == 0
    assert pg.value({"primary_purpose": "book"}, "primary_purpose") == "book"


def test_majority_two_to_one():
    runs = [{"c1": lab(has_todo=1)}, {"c1": lab(has_todo=0)}, {"c1": lab(has_todo=True)}]
    assert pg.majority(runs)["c1"]["has_todo"] == 1


def test_majority_three_way_takes_first():
    runs = [{"c1": lab(primary_purpose=p)} for p in ("book", "cancel", "other")]
    assert pg.majority(runs)["c1"]["primary_purpose"] == "book"


def test_compare_counts_b_c():
    gold = {f"c{i}": lab(has_todo=1) for i in range(6)} | {"c9": lab()}
    base = {u: {i: pg.value(lab(), i) for i in pg.ITEMS} for u in gold}
    cand = {u: {i: pg.value(g, i) for i in pg.ITEMS} for u, g in gold.items()}
    s = pg.compare(base, cand, gold)["has_todo"]
    assert (s["b"], s["c"], s["base_wrong"], s["cand_wrong"]) == (6, 0, 6, 0)
    assert s["p"] == pytest.approx(0.03125)


def test_compare_new_miss_and_false_alarm():
    gold = {"e": lab(emergency=1), "n1": lab(), "n2": lab()}
    base = {u: {i: pg.value(g, i) for i in pg.ITEMS} for u, g in gold.items()}
    cand = {u: dict(v) for u, v in base.items()}
    cand["e"]["emergency"] = 0
    cand["n1"]["emergency"] = 1
    cand["n2"]["emergency"] = 1
    s = pg.compare(base, cand, gold)["emergency"]
    assert s["new_miss"] == 1 and s["false_alarm_delta"] == 2


def test_decide_pass():
    assert pg.decide(stats(), "has_todo") == []


def test_decide_target_not_significant():
    assert "target_up" in pg.decide(stats(has_todo=_item(b=5, c=0)), "has_todo")


def test_decide_target_down_not_target_up():
    assert "target_up" in pg.decide(stats(has_todo=_item(b=0, c=6)), "has_todo")


def test_decide_new_emergency_miss():
    assert "no_new_miss" in pg.decide(stats(emergency={"new_miss": 1}), "has_todo")


def test_decide_false_alarm_cap():
    assert "false_alarm_cap" in pg.decide(stats(complaint={"false_alarm_delta": 3}), "has_todo")
    assert pg.decide(stats(complaint={"false_alarm_delta": 2}), "has_todo") == []


def test_decide_regression_cap():
    assert "no_regression" in pg.decide(stats(counterpart=_item(b=0, c=4)), "has_todo")


def test_decide_regression_within_cap():
    assert pg.decide(stats(counterpart=_item(b=0, c=3)), "has_todo") == []


# ── 모수·실행 저장(Task 2) ──
from shared.llm import LLMResult  # noqa: E402
from tests.backend.script import Script  # noqa: E402

FP = {"prompt_sha256": "aaa", "model": "claude-haiku-5-5"}
CALLS = {f"c{i}": (f"직원: 안녕하세요 {i}", "inbound") for i in range(3)}


def ans(labels=None, fallback=False, model="claude-haiku-5-5", ok=True):
    data = labels or lab()
    return LLMResult(ok, "", data if ok else None, model, fallback, 1, 1, 0.001, 1,
                     error=None if ok else "x")  # fmt: skip


def gold_rows(n=40, extra=()):
    rows = [
        {"call_uid": f"g{i:03d}", "primary_purpose": "book", "emergency": 0, "complaint": 0,
         "split": "silver"} | {k: v for k, v in lab().items() if k != "primary_purpose"}
        for i in range(n)
    ]  # fmt: skip
    return rows + list(extra)


@pytest.mark.parametrize("split", ["dev", "test"])
def test_population_rejects_holdout(split):
    with pytest.raises(SystemExit, match="holdout_in_population"):
        pg.population(gold_rows(extra=[gold_rows(1)[0] | {"call_uid": "d1", "split": split}]))


def test_population_is_validation_only():
    from evaluation.splits import silver_split

    rows = gold_rows()
    want = {u for u, part in silver_split(rows).items() if part == "validation"}
    assert set(pg.population(rows)) == want and 0 < len(want) < len(rows)


def test_fill_calls_only_missing(tmp_path):
    (tmp_path / "v2").mkdir()
    (tmp_path / "v2" / "meta.json").write_text(__import__("json").dumps(FP))
    (tmp_path / "v2" / "run1.jsonl").write_text(
        "".join(
            __import__("json").dumps({"call_uid": u, "labels": lab()}) + "\n" for u in ("c0", "c1")
        )
    )
    gw = Script(*[ans() for _ in range(3 * 3 - 2)])
    runs = pg.fill_runs(gw, "v2", CALLS, FP, root=tmp_path, workers=1)
    assert len(gw.calls) == 7 and all(set(r) == set(CALLS) for r in runs)


def test_saved_runs_reused_without_calls(tmp_path):
    pg.fill_runs(Script(*[ans() for _ in range(9)]), "v2", CALLS, FP, root=tmp_path, workers=1)
    runs = pg.fill_runs(Script(), "v2", CALLS, FP, root=tmp_path, workers=1)
    assert len(runs) == 3 and all(len(r) == 3 for r in runs)


def test_prompt_change_discards_runs(tmp_path):
    pg.fill_runs(Script(*[ans() for _ in range(9)]), "v2", CALLS, FP, root=tmp_path, workers=1)
    gw = Script(*[ans() for _ in range(9)])
    pg.fill_runs(gw, "v2", CALLS, FP | {"prompt_sha256": "bbb"}, root=tmp_path, workers=1)
    assert len(gw.calls) == 9


def test_fallback_answer_is_retried(tmp_path):
    calls = {"c0": CALLS["c0"]}
    gw = Script(ans(lab(has_todo=1), fallback=True), ans(lab(has_todo=0), model="claude-sonnet-5-5"),
                ans(lab(has_todo=0)), ans(), ans())  # fmt: skip
    runs = pg.fill_runs(gw, "v2", calls, FP, root=tmp_path, workers=1)
    assert len(gw.calls) == 5 and runs[0]["c0"]["has_todo"] == 0


def test_empty_after_three_failures_stops(tmp_path):
    calls = {"c0": CALLS["c0"]}
    with pytest.raises(SystemExit, match="빈 통화 1건"):
        pg.fill_runs(Script(ans(ok=False), ans(ok=False), ans(ok=False)), "v2", calls, FP,
                     root=tmp_path, workers=1)  # fmt: skip
    assert (
        not (tmp_path / "v2" / "run1.jsonl").exists()
        or "c0" not in (tmp_path / "v2" / "run1.jsonl").read_text()
    )


# ── 판정 실행·시도 수·CLI(Task 3) ──
import json  # noqa: E402
import sqlite3  # noqa: E402

CONFIG = {"aliases": {"classify": ["claude-haiku-5-5", "claude-sonnet-5-5"]}}


def _gold(n=40):
    return [r | {"has_todo": 1} for r in gold_rows(n)]


def _scripted(rows, base_todo=0, cand_todo=1):
    """기준(v1) 3벌 다음 후보(v2) 3벌 — fill_runs가 sorted 통화 순으로 부른다"""
    uids = sorted(pg.population(rows))
    return Script(*[ans(lab(has_todo=base_todo)) for _ in range(3 * len(uids))],
                  *[ans(lab(has_todo=cand_todo)) for _ in range(3 * len(uids))])  # fmt: skip


def _calls(rows):
    return {r["call_uid"]: ("직원: 네", "inbound") for r in rows}


def _gate(tmp_path, gw, a="v1", b="v2", policy=None, rows=None, item="has_todo"):
    rows = rows or _gold()
    return pg.run_gate(gw, a, b, item, gold_rows=rows, calls=_calls(rows), policy=policy or {},
                       config=CONFIG, runs_root=tmp_path / "runs", report=tmp_path / "r.jsonl",
                       workers=1)  # fmt: skip


def _history(tmp_path, *cands, base="v1", gate_hash=None, sha=None):
    lines = [
        {"base": base, "candidate": c, "candidate_sha256": sha or f"sha-{c}",
         "gate_hash": gate_hash or pg.gate_hash()}
        for c in cands
    ]  # fmt: skip
    (tmp_path / "r.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))


@pytest.fixture
def fake_fp(monkeypatch):
    """버전 → 프롬프트 해시 — 기본은 sha-<버전>, 고친 프롬프트는 값을 바꿔 흉내 낸다"""
    shas = {}
    monkeypatch.setattr(pg, "fingerprint", lambda v, config: {
        "prompt_sha256": shas.get(v, f"sha-{v}"), "model": "claude-haiku-5-5"})  # fmt: skip
    return shas


def test_gate_pass_records_row(tmp_path):
    out = _gate(tmp_path, _scripted(_gold()))
    rows = (tmp_path / "r.jsonl").read_text().splitlines()
    assert out["passed"] is True and out["failed"] == [] and len(rows) == 1
    assert json.loads(rows[0])["items"]["has_todo"]["b"] == out["n"] > 0


def test_policy_on_needs_auto_inputs(tmp_path):
    """켜진 자동 처리는 멈춤이 아니라 조건 — 조건을 잴 입력이 없으면 부르기 전에 멈춘다"""
    gw = Script()
    with pytest.raises(SystemExit, match="auto_inputs_missing"):
        _gate(tmp_path, gw, policy={"call_autoclose": True})
    assert gw.calls == [] and not (tmp_path / "r.jsonl").exists()


def test_policy_on_no_longer_stops(tmp_path):
    rows = _gold()
    uids = sorted(pg.population(rows))
    auto = {"base_todo": {u: ([], False) for u in uids}, "judge": {}, "trust": None}
    out = pg.run_gate(_scripted(rows), "v1", "v2", "has_todo", gold_rows=rows, calls=_calls(rows),
                      policy={"call_autoclose": True}, config=CONFIG, runs_root=tmp_path / "runs",
                      report=tmp_path / "r.jsonl", workers=1, auto=auto)  # fmt: skip
    assert "trust_unmeasured" in out["failed"] and (tmp_path / "r.jsonl").exists()


def test_attempt_cap_stops_third_candidate(tmp_path, fake_fp):
    _history(tmp_path, "v2", "v9")
    gw = Script()
    with pytest.raises(SystemExit, match="attempt_cap"):
        _gate(tmp_path, gw, b="v5")
    assert gw.calls == []


def test_same_candidate_rerun_not_counted(tmp_path, fake_fp):
    _history(tmp_path, "v2", "v9")
    out = _gate(tmp_path, _scripted(_gold()))
    assert out["candidate_sha256"] == "sha-v2"
    assert pg.attempts("v1", tmp_path / "r.jsonl") == {("v2", "sha-v2"), ("v9", "sha-v9")}


def test_edited_candidate_counts_as_new_attempt(tmp_path, fake_fp):
    _history(tmp_path, "v2", "v9")
    fake_fp["v2"] = "sha-v2-edited"
    gw = Script()
    with pytest.raises(SystemExit, match="attempt_cap"):
        _gate(tmp_path, gw)
    assert gw.calls == []


def test_version_outside_folder_rejected(tmp_path):
    gw = Script()
    with pytest.raises(SystemExit, match="버전 이름"):
        _gate(tmp_path, gw, a="../v1")
    assert gw.calls == []


def test_gate_hash_change_resets_count(tmp_path):
    _history(tmp_path, "v2", "v9", gate_hash="old")
    assert pg.attempts("v1", tmp_path / "r.jsonl") == set()


def test_self_comparison_not_counted(tmp_path):
    _history(tmp_path, "v1")
    assert pg.attempts("v1", tmp_path / "r.jsonl") == set()


def test_unknown_item_rejected(tmp_path):
    with pytest.raises(SystemExit):
        _gate(tmp_path, Script(), item="foo")


def test_output_and_record_have_no_call_uid(tmp_path, monkeypatch, capsys):
    rows = _gold()
    truth = sqlite3.connect(tmp_path / "t.sqlite")
    cols = list(rows[0])
    truth.execute(f"CREATE TABLE truth_call_labels ({', '.join(cols)})")
    truth.executemany(f"INSERT INTO truth_call_labels VALUES ({', '.join('?' * len(cols))})",
                      [tuple(r[c] for c in cols) for r in rows])  # fmt: skip
    truth.commit()
    src = sqlite3.connect(tmp_path / "s.sqlite")
    src.execute("CREATE TABLE ax_calls (call_uid, transcript, direction)")
    src.executemany(
        "INSERT INTO ax_calls VALUES (?, '직원: 네', 'inbound')", [(r["call_uid"],) for r in rows]
    )
    src.commit()
    app = sqlite3.connect(tmp_path / "a.sqlite")
    app.execute("CREATE TABLE ax_settings (key, value)")
    app.execute("""INSERT INTO ax_settings VALUES ('auto_policy', '{"call_autoclose": false}')""")
    app.commit()
    gw = _scripted(rows)
    monkeypatch.setattr(pg, "make_gateway", lambda log: gw)
    monkeypatch.setattr(pg, "load_config", lambda: CONFIG)
    pg.main(["--a", "v1", "--b", "v2", "--item", "has_todo", "--truth", str(tmp_path / "t.sqlite"),
             "--source", str(tmp_path / "s.sqlite"), "--app", str(tmp_path / "a.sqlite"),
             "--report", str(tmp_path / "r.jsonl"), "--runs", str(tmp_path / "runs"), "--workers", "1"])  # fmt: skip
    text = capsys.readouterr().out + (tmp_path / "r.jsonl").read_text()
    assert "합격" in text and not any(r["call_uid"] in text for r in rows)


# ── 켜진 자동 처리 조건(자동 개선 설계 §5) ──
def _judge(**kw):
    return {"labels": lab(**kw), "ok": True, "fallback": False, "todo": []}


def _closing(n, wrong=0):
    """n통화가 모두 자동 종결되고 그중 wrong통화는 골드가 할 일 있음"""
    uids = [f"g{i:03d}" for i in range(n)]
    cand = {u: lab() for u in uids}
    gold = {u: lab(has_todo=int(i < wrong)) for i, u in enumerate(uids)}
    return dict(cand=cand, gold=gold, base_todo={u: ([], False) for u in uids},
                judge={u: _judge() for u in uids})  # fmt: skip


def test_gate_values_include_autoclose_cap():
    assert pg.GATE_VALUES["autoclose_error_cap"] == 0.05


def test_autoclose_condition_fails_on_one_error_of_88():
    on = {"call_autoclose": True}
    trust = {"violations": 0}
    assert "autoclose_error" in pg.auto_conditions(**_closing(88, wrong=1), policy=on, trust=trust)
    assert pg.auto_conditions(**_closing(88), policy=on, trust=trust) == []


def test_autoclose_condition_uses_candidate_labels():
    on = {"call_autoclose": True}
    c = _closing(88, wrong=1)
    c["cand"]["g000"] = lab(has_todo=1)  # 후보가 할 일 있음 → 닫지 않음
    assert pg.auto_conditions(**c, policy=on, trust={"violations": 0}) == []
    c = _closing(88, wrong=1)
    c["base_todo"]["g000"] = (["회신"], False)  # 빌린 할 일 목록이 있으면 닫지 않음
    assert pg.auto_conditions(**c, policy=on, trust={"violations": 0}) == []
    c = _closing(88, wrong=1)
    c["base_todo"]["g000"] = ([], True)  # 요약 폴백이면 닫지 않음
    assert pg.auto_conditions(**c, policy=on, trust={"violations": 0}) == []


def test_trust_condition():
    on = {"call_autoclose": True}
    assert "trust_unmeasured" in pg.auto_conditions(**_closing(1), policy=on, trust=None)
    assert "trust_violation" in pg.auto_conditions(
        **_closing(1), policy=on, trust={"violations": 1}
    )
    off = {"call_autoclose": False}
    assert pg.auto_conditions(**_closing(1, wrong=1), policy=off, trust=None) == []


def test_majority_labels_keep_raw_values():
    runs = [
        {"c1": lab(urgency="low")},
        {"c1": lab(urgency="normal")},
        {"c1": lab(urgency="urgent")},
    ]
    assert pg.majority_labels(runs)["c1"]["urgency"] == "low"  # urgent 아님 2 → 첫 값
    runs = [{"c1": lab(has_todo=1)}, {"c1": lab(has_todo=0)}, {"c1": lab(has_todo=True)}]
    assert pg.majority_labels(runs)["c1"]["has_todo"] == 1


def test_attempts_counts_precheck_rejects(tmp_path):
    imp = tmp_path / "imp.jsonl"
    rows = [{"base": "v1", "candidate": "v1-imp1", "candidate_sha256": "s1", "gate_hash": pg.gate_hash(),
             "gate": None},
            {"base": "v1", "candidate": "v1-imp2", "candidate_sha256": "s2", "gate_hash": "old",
             "gate": None},
            {"base": "v1", "candidate": "v1-imp3", "candidate_sha256": "s3", "gate_hash": pg.gate_hash(),
             "gate": {"passed": False}}]  # fmt: skip
    imp.write_text("".join(json.dumps(r) + "\n" for r in rows))
    got = pg.attempts("v1", tmp_path / "none.jsonl", imp)
    assert got == {("v1-imp1", "s1")}
