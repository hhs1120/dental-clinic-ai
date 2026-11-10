import json
import sqlite3

from evaluation import gate


def report(tool=(6, 6), hit5=(20, 20), agent=44, baseline=37, warnings=(), chat_skipped=False):
    return {
        "rag": {
            "retrieval": {"hit5": {"k": hit5[0], "n": hit5[1]}},
            "chat": {"tool": {"k": tool[0], "n": tool[1]}},
            "warnings": list(warnings),
            "chat_skipped": chat_skipped,
        },
        "followup": {
            "score": {"dev": {"n": 54, "agent": {"k": agent, "n": 54},
                              "baseline": {"k": baseline, "n": 54}}}
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


def test_main_reads_new_report(tmp_path, monkeypatch, capsys):
    app, src = _app(tmp_path, [("c1", "v2", "ok"), ("c2", "v2", "ok")])
    reports = tmp_path / "reports"
    reports.mkdir()

    def fake_eval(argv):
        (reports / "r.json").write_text(json.dumps(report(agent=30)))
        (reports / "eval_runs.jsonl").write_text(json.dumps({"report_path": "r.md"}) + "\n")
        return 0

    monkeypatch.setattr(gate.eval_, "main", fake_eval)
    code = gate.main(["--app", str(app), "--source", str(src), "--reports-dir", str(reports)])
    assert code == 1 and "followup" in capsys.readouterr().out
