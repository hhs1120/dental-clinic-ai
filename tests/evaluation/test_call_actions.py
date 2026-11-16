"""처리 정답 — Opus 라벨과 사람 표본 (설계 "PR ② 처리 정답"). fixture는 eval_db(분류 v1·v2, 판정기 v1)."""

import json

import pytest

from evaluation import call_actions
from evaluation.gold_audit import transcript_sha
from shared.followup import ACTIONS
from tests.evaluation.eval_db import make_db
from tests.evaluation.test_gold_audit import BatchFake


@pytest.fixture
def conn(tmp_path):
    """통화 1(dev 풀)·4·5(silver)를 골드·분류 모두 할 일로"""
    c = make_db(tmp_path)
    c.execute(
        "UPDATE truth.truth_call_labels SET has_todo = 1 WHERE call_uid IN ('c-0001','c-0004','c-0005')"
    )
    c.execute(
        "UPDATE ax_call_classifications SET labels = json_set(labels, '$.has_todo', 1)"
        " WHERE call_uid IN ('c-0001','c-0004','c-0005') AND labels IS NOT NULL"
    )
    c.execute("INSERT INTO ax_settings VALUES ('active_prompt_version', '\"v1\"')")
    return c


def test_eval_calls_population(conn):
    e = call_actions.eval_calls(conn)
    assert e["dev"] == ["c-0001"]
    assert set(e["validation"]) <= {"c-0004", "c-0005"} and e["validation"]
    # 분류기가 할 일 없음이면 빠진다
    conn.execute(
        "UPDATE ax_call_classifications SET labels = json_set(labels, '$.has_todo', 0) WHERE call_uid = 'c-0001'"
    )
    assert call_actions.eval_calls(conn)["dev"] == []


def test_label_request_hides_classifier_and_gold():
    msg, schema = call_actions.label_request("직원: 네\n환자: 영수증 주세요", "inbound")
    assert "영수증" in msg and "has_todo" not in msg and "골드" not in msg
    assert schema["properties"]["action"]["enum"] == list(ACTIONS)


def test_label_writes_and_skips_done(conn, tmp_path):
    p, state = tmp_path / "a.jsonl", tmp_path / "s.json"
    gw = BatchFake(tmp_path)
    r = call_actions.label(gw, conn, p, state_path=state, poll_seconds=0)
    recs = call_actions.load(p)
    assert r["done"] == len(recs) >= 2
    assert all(x["llm_action"] in ACTIONS and x["human_action"] is None for x in recs)
    assert recs[0]["transcript_sha"] == transcript_sha(
        conn.execute(
            "SELECT transcript FROM source.ax_calls WHERE call_uid = ?", (recs[0]["call_uid"],)
        ).fetchone()[0]
    )
    gw2 = BatchFake(tmp_path)
    assert call_actions.label(gw2, conn, p, state_path=state, poll_seconds=0)["done"] == 0
    assert gw2.batches == {}


def _keys(*keys):
    it = iter(keys)

    def read(prompt=""):
        try:
            return next(it)
        except StopIteration:
            raise EOFError from None

    return read


def test_audit_hides_label_and_saves(conn, tmp_path):
    p, state = tmp_path / "a.jsonl", tmp_path / "s.json"
    call_actions.label(BatchFake(tmp_path), conn, p, state_path=state, poll_seconds=0)
    out = []
    with pytest.raises(EOFError):
        call_actions.audit(conn, p, read=_keys("9", "", "2"), write=out.append, n=2)
    recs = call_actions.load(p)
    done = [r for r in recs if r["human_action"]]
    assert len(done) == 1 and done[0]["human_action"] == ACTIONS[1]
    before = "\n".join(out[: out.index(next(x for x in out if x.startswith("저장")))])
    assert "llm" not in before.lower() and "AI" not in before
    assert "다시" in "\n".join(out)
    call_actions.audit(conn, p, read=_keys("7"), write=out.append, n=2)
    assert sum(1 for r in call_actions.load(p) if r["human_action"]) == 2


def test_truth_prefers_human():
    recs = [
        {"call_uid": "a", "llm_action": "billing", "human_action": "document"},
        {"call_uid": "b", "llm_action": "billing", "human_action": None},
    ]
    assert call_actions.truth(recs) == {"a": "document", "b": "billing"}


def test_agreement():
    recs = [
        {"call_uid": "a", "llm_action": "billing", "human_action": "billing"},
        {"call_uid": "b", "llm_action": "billing", "human_action": "document"},
        {"call_uid": "c", "llm_action": "billing", "human_action": None},
    ]
    a = call_actions.agreement(recs)
    assert (a["k"], a["n"]) == (1, 2) and a["by_action"]["billing"] == {"k": 1, "n": 1}
    assert json.loads(json.dumps(a))


def test_new_prompt_version_relabels_and_keeps_human(conn, tmp_path, monkeypatch):
    """라벨 프롬프트 버전이 바뀌면 다시 라벨하고, 사람 답은 남긴다"""
    p, state = tmp_path / "a.jsonl", tmp_path / "s.json"
    call_actions.label(BatchFake(tmp_path), conn, p, state_path=state, poll_seconds=0)
    recs = call_actions.load(p)
    recs[0]["human_action"] = "billing"
    for r in recs:
        r["prompt_version"] = "v0"
    call_actions._save(p, recs)
    gw = BatchFake(tmp_path)
    assert call_actions.label(gw, conn, p, state_path=state, poll_seconds=0)["done"] == len(recs)
    after = {r["call_uid"]: r for r in call_actions.load(p)}
    assert after[recs[0]["call_uid"]]["human_action"] == "billing"
    assert {r["prompt_version"] for r in after.values()} == {call_actions.PROMPT_VERSION}


def test_reopen_human(tmp_path):
    """사람 답이 특정 종류인 표본만 다시 판정 대상으로"""
    p = tmp_path / "a.jsonl"
    call_actions._save(
        p,
        [
            {"call_uid": "a", "llm_action": "appointment", "human_action": "other"},
            {"call_uid": "b", "llm_action": "billing", "human_action": "billing"},
        ],
    )
    assert call_actions.reopen_human(p, "other") == 1
    assert {r["call_uid"]: r["human_action"] for r in call_actions.load(p)} == {
        "a": None,
        "b": "billing",
    }


def test_reopen_requires_known_action():
    """reopen은 --action이 꼭 있어야 하고 처리 종류 밖의 값은 받지 않는다"""
    for argv in (["reopen"], ["reopen", "--action", "othr"]):
        with pytest.raises(SystemExit):
            call_actions.main(argv)


def _todo_c3(conn):
    """통화 3(test 판정 풀)을 골드·분류 모두 할 일로"""
    conn.execute("UPDATE truth.truth_call_labels SET has_todo = 1 WHERE call_uid = 'c-0003'")
    conn.execute(
        "UPDATE ax_call_classifications SET labels = json_set(labels, '$.has_todo', 1)"
        " WHERE call_uid = 'c-0003' AND labels IS NOT NULL"
    )


def test_population_adds_test_pool_keeps_dev(conn):
    """test 판정 풀을 더해도 dev·검증 구간의 대상과 순서는 그대로(평가 설계 §5)"""
    before = call_actions.eval_calls(conn)
    _todo_c3(conn)
    after = call_actions.eval_calls(conn)
    assert list(after) == ["dev", "validation", "test"]
    assert after["test"] == ["c-0003"]
    dev = {k: before[k] for k in call_actions.DEV_PARTS}
    assert {k: after[k] for k in call_actions.DEV_PARTS} == dev and before["test"] == []
    assert call_actions.eval_calls(conn, ("test",)) == {"test": ["c-0003"]}
    assert call_actions.eval_calls(conn, call_actions.DEV_PARTS) == dev


def test_label_adds_test_without_relabeling(conn, tmp_path):
    """이미 라벨된 dev·검증 통화는 다시 나가지 않고 test 통화만 나간다(이어 돎)"""
    p, state = tmp_path / "a.jsonl", tmp_path / "s.json"
    first = BatchFake(tmp_path)
    call_actions.label(first, conn, p, state_path=state, poll_seconds=0)
    [(_, reqs)] = first.batches.values()
    assert "c-0003" not in {r["custom_id"] for r in reqs}
    before = call_actions.load(p)
    _todo_c3(conn)
    gw = BatchFake(tmp_path)
    assert call_actions.label(gw, conn, p, state_path=state, poll_seconds=0)["done"] == 1
    [(_, reqs)] = gw.batches.values()
    assert [r["custom_id"] for r in reqs] == ["c-0003"]
    after = {r["call_uid"]: r for r in call_actions.load(p)}
    assert [after[r["call_uid"]] for r in before] == before


def test_audit_skips_test_records(conn, tmp_path):
    """사람 표본은 dev·검증 구간 라벨에서만 — test 라벨이 더해져도 표본이 바뀌지 않는다"""
    p = tmp_path / "a.jsonl"
    call_actions._save(
        p,
        [
            {"call_uid": u, "llm_action": "other", "human_action": None}
            for u in ("c-0001", "c-0003", "c-0004")
        ],
    )
    out = []
    with pytest.raises(EOFError):
        call_actions.audit(conn, p, read=_keys(), write=out.append, n=5)
    assert out[0] == "표본 2통화 중 남은 2통화"
