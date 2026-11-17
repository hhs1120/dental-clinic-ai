"""② 자동 개선 (evaluation/improve.py, 설계 docs/superpowers/specs/2026-11-17-auto-improve-design.md)."""

import json
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
