import json
import sqlite3

from evaluation import gate


def report(tool=(6, 6), hit5=(20, 20), agent=44, baseline=37, warnings=(), chat_skipped=False,
           n=54, not_run=0, fu_failed=0, stale=0):  # fmt: skip
    return {
        "rag": {
            "retrieval": {"hit5": {"k": hit5[0], "n": hit5[1]}},
            "chat": {"tool": {"k": tool[0], "n": tool[1]}},
            "warnings": list(warnings),
            "chat_skipped": chat_skipped,
        },
        "followup": {
            "score": {"dev": {"n": n, "agent": {"k": agent, "n": n},
                              "baseline": {"k": baseline, "n": n},
                              "not_run": not_run, "failed": fu_failed}},
            "stale_labels": stale,
        },
    }  # fmt: skip


def failed(result):
    return [name for name, (ok, _) in gate.check_report(result).items() if not ok]


def test_report_passes():
    assert failed(report()) == []


def test_tool_below_half_fails():
    assert failed(report(tool=(2, 5))) == ["tool"]


def test_hit5_exactly_half_passes():
    assert failed(report(hit5=(10, 20))) == []


def test_hit5_below_half_fails():
    assert failed(report(hit5=(9, 20))) == ["hit5"]


def test_followup_below_baseline_fails():
    assert failed(report(agent=36)) == ["followup"]


def test_followup_tie_passes():
    assert failed(report(agent=37)) == []


def test_chat_skipped_fails():
    assert "tool" in failed(report(tool=(0, 0), chat_skipped=True))


def test_followup_empty_population_fails():
    assert failed(report(agent=0, baseline=0, n=0)) == ["followup"]


def test_missing_followup_fails_with_reason():
    checks = gate.check_report(report() | {"followup": None})
    assert not checks["followup"][0] and "make followup-eval" in checks["followup"][1]


def test_missing_rag_fails():
    assert failed(report() | {"rag": None}) == ["warnings", "tool", "hit5"]


def test_followup_text_shows_not_run_failed_stale():
    text = gate.check_report(report(not_run=2, fu_failed=3, stale=1))["followup"][1]
    assert "미실행 2" in text and "실패 3" in text and "낡은 라벨 1" in text


def test_app_warning_fails():
    assert failed(report(warnings=["빈 색인"])) == ["warnings"]


def _app(tmp_path, rows, pool=("c1", "c2"), active="v2"):
    src = sqlite3.connect(tmp_path / "s.sqlite")
    src.execute("CREATE TABLE ax_calls (call_uid, in_eval_pool)")
    src.executemany("INSERT INTO ax_calls VALUES (?, ?)",
                    [(u, 1) for u in pool] + [("c9", 0)])  # fmt: skip
    src.commit()
    app = sqlite3.connect(tmp_path / "a.sqlite")
    app.execute("CREATE TABLE ax_settings (key, value)")
    app.execute(
        "INSERT INTO ax_settings VALUES ('active_prompt_version', ?)", (json.dumps(active),)
    )
    app.execute(
        "CREATE TABLE ax_call_classifications (call_uid, model_alias, prompt_version, status)"
    )
    app.executemany("INSERT INTO ax_call_classifications VALUES (?, 'classify', ?, ?)", rows)
    app.commit()
    return tmp_path / "a.sqlite", tmp_path / "s.sqlite"


def test_pool_all_ok(tmp_path):
    assert gate.pool_not_ok(*_app(tmp_path, [("c1", "v2", "ok"), ("c2", "v2", "ok")])) == 0


def test_pool_failed_or_old_version_counts(tmp_path):
    rows = [("c1", "v2", "failed"), ("c2", "v1", "ok")]
    assert gate.pool_not_ok(*_app(tmp_path, rows)) == 2


def test_pool_failure_stops_before_paid_eval(tmp_path, monkeypatch, capsys):
    app, src = _app(tmp_path, [("c1", "v2", "ok")])
    called = []
    monkeypatch.setattr(gate.eval_, "main", lambda argv: called.append(argv) or 0)
    assert gate.main(["--app", str(app), "--source", str(src)]) == 1
    assert called == [] and "판정 풀" in capsys.readouterr().out


def _run(tmp_path, monkeypatch, new_lines, old=True):
    """옛 이력 1줄(합격 리포트 old.json)을 깔고, 가짜 eval이 new_lines(이름 → 리포트)를 덧붙인다"""
    app, src = _app(tmp_path, [("c1", "v2", "ok"), ("c2", "v2", "ok")])
    reports = tmp_path / "reports"
    reports.mkdir()
    runs = reports / "eval_runs.jsonl"
    line = {"split": "dev", "section": "all", "condition": "base"}
    if old:
        (reports / "old.json").write_text(json.dumps(report(agent=0)))
        runs.write_text(json.dumps(line | {"report_path": "old.md"}) + "\n")

    def fake_eval(argv):
        for name, (extra, result) in new_lines.items():
            (reports / f"{name}.json").write_text(json.dumps(result))
            with runs.open("a") as f:
                f.write(json.dumps(line | extra | {"report_path": f"{name}.md"}) + "\n")
        return 0

    monkeypatch.setattr(gate.eval_, "main", fake_eval)
    return gate.main(["--app", str(app), "--source", str(src), "--reports-dir", str(reports)])


def test_main_reads_new_report(tmp_path, monkeypatch, capsys):
    assert _run(tmp_path, monkeypatch, {"r": ({}, report(agent=30))}, old=False) == 1
    assert "followup" in capsys.readouterr().out


def test_main_passes_on_new_line_after_old(tmp_path, monkeypatch, capsys):
    assert _run(tmp_path, monkeypatch, {"r": ({}, report())}) == 0
    assert capsys.readouterr().out.rstrip().endswith("합격")


def test_main_no_new_line_fails(tmp_path, monkeypatch, capsys):
    assert _run(tmp_path, monkeypatch, {}) == 1
    assert "리포트" in capsys.readouterr().out


def test_main_skips_other_condition_line(tmp_path, monkeypatch):
    other = ({"condition": "reranker=off"}, report(agent=0))
    assert _run(tmp_path, monkeypatch, {"r": ({}, report()), "x": other}) == 0
