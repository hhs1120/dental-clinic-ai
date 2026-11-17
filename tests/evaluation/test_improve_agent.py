"""② 개선 에이전트 (evaluation/improve_agent.py, 자동 개선 설계 §3)."""

from evaluation import improve_agent as ia
from tests.backend.script import Script, answer, call

RULE = "- has_todo: 통화 뒤 직원이 할 일이 남음"
ERRORS = [
    {"call_uid": "c-0001", "transcript": "직원: 원장님께 전달할게요", "direction": "inbound",
     "model": 0, "gold": 1},
    {"call_uid": "c-0002", "transcript": "직원: 네 알겠습니다", "direction": "inbound",
     "model": 1, "gold": 0},
]  # fmt: skip
NEW = "- has_todo: 직원이 통화 뒤 해야 할 후속 행동을 약속했으면 참"


def _propose(tmp_path, *turns, failures=()):
    gw = Script(*turns)
    store = ia.open_store(tmp_path / "harness.sqlite")
    return ia.propose(gw, store, RULE, ERRORS, list(failures), target="v2-imp1"), gw


def test_submit_rule_ends_run(tmp_path):
    out, _ = _propose(
        tmp_path,
        call(("read_call", {"call_uid": "c-0001"})),
        call(("submit_types", {"types": [{"name": "전달 약속을 놓침", "n": 1}]})),
        call(("submit_rule", {"rule": NEW})),
    )
    assert out["status"] == "done" and out["rule"] == NEW
    assert out["types"] == [{"name": "전달 약속을 놓침", "n": 1}]
    assert out["cost_usd"] > 0


def test_read_call_blocks_outside_material(tmp_path):
    out, gw = _propose(
        tmp_path,
        call(("read_call", {"call_uid": "c-0099"})),
        call(("submit_rule", {"rule": NEW})),
    )
    blocked = gw.calls[1]["messages"][-1]["content"][0]
    assert blocked["is_error"] and "재료" in str(blocked["content"])
    store = ia.open_store(tmp_path / "harness.sqlite")
    assert (
        store.execute(
            "SELECT blocked FROM ax_rule_counts WHERE rule_id = ?", (ia.MATERIAL_RULE,)
        ).fetchone()[0]
        == 1
    )


def test_bad_rule_rejected(tmp_path):
    out, gw = _propose(
        tmp_path,
        call(("submit_rule", {"rule": "- emergency: 응급이면 참"})),
        call(("submit_rule", {"rule": NEW + "\n- has_todo: 두 줄"})),
        call(("submit_rule", {"rule": NEW})),
    )
    assert out["rule"] == NEW
    assert gw.calls[1]["messages"][-1]["content"][0]["is_error"]
    assert gw.calls[2]["messages"][-1]["content"][0]["is_error"]


def test_no_submit_gives_none(tmp_path):
    out, _ = _propose(tmp_path, answer("모르겠다"), answer("그래도 모르겠다"))
    assert out["rule"] is None and out["types"] == []


def test_input_has_no_numbers_from_gate():
    failures = [{"rule": NEW, "failed": ["target_up"], "items": {"has_todo": {"b": 3, "c": 1}},
                 "p": 0.3}]  # fmt: skip
    text = ia.input_text(RULE, ERRORS, failures)
    assert NEW in text and "target_up" in text
    assert '"b"' not in text and "0.3" not in text and "items" not in text
    assert "c-0001" in text and "원장님께" not in text  # 녹취는 도구로만


def test_store_is_not_app_db():
    assert ia.STORE.parts[:3] == ("evaluation", "runs", "improve")
