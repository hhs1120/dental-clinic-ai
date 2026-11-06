"""골드 점검 (평가 설계 §2.3, 골드 점검 설계) — fixture는 eval_db 머리말(판정기 v1: 통화 2 dev·3 test가 골드와 다름)."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from evaluation import gold_audit
from shared.llm import FakeGateway, JsonlLog
from tests.evaluation.eval_db import labels, make_db

ROOT = Path(__file__).resolve().parents[2]
HIDDEN = ("골드", "gold", "정답", "판정기", "judge")


@pytest.fixture
def conn(tmp_path):
    return make_db(tmp_path)


def _set_judge(conn, uid, **values):
    for k, v in values.items():
        conn.execute(
            "UPDATE ax_model_judgments SET labels = json_set(labels, ?, ?) WHERE call_uid = ?",
            (f"$.{k}", v, uid),
        )


def _target():
    return {
        "call_uid": "c-0002",
        "direction": "inbound",
        "transcript": "직원: 네\n환자: 불만이에요",
        "transcript_sha": "x",
        "items": {
            "primary_purpose": {"gold": "complaint", "judge": "book"},
            "complaint": {"gold": 1, "judge": 0},
        },
    }


def test_targets_skip_test_pool(conn):
    """c-0003은 test 판정 풀 — 평가 설계 §1 원칙 2"""
    ts = gold_audit.targets(conn, "v1")
    assert [t["call_uid"] for t in ts] == ["c-0002"]
    assert ts[0]["items"] == {
        "primary_purpose": {"gold": "complaint", "judge": "book"},
        "complaint": {"gold": 1, "judge": 0},
    }


def test_targets_compare_urgency_as_urgent_or_not(conn):
    """골드 normal ↔ low는 대상이 아니고 normal ↔ urgent만 대상(엇갈림과 같은 규칙)"""
    _set_judge(conn, "c-0004", urgency="low")
    assert "c-0004" not in {t["call_uid"] for t in gold_audit.targets(conn, "v1")}
    _set_judge(conn, "c-0004", urgency="urgent")
    [t] = [t for t in gold_audit.targets(conn, "v1") if t["call_uid"] == "c-0004"]
    assert t["items"] == {"urgency": {"gold": "normal", "judge": "urgent"}}


def test_request_hides_gold():
    msg, schema = gold_audit.arbiter_request(_target())
    for word in HIDDEN:
        assert word not in msg
    assert "직원: 네" in msg
    assert schema["properties"]["complaint"]["enum"] == ["가", "나"]
    assert schema["properties"]["primary_purpose"]["enum"] == ["가", "나", "둘 다 아님"]
    assert sorted(schema["required"]) == ["complaint", "primary_purpose"]


def test_request_shows_urgency_as_two_values():
    t = dict(_target(), items={"urgency": {"gold": "low", "judge": "urgent"}})
    msg, schema = gold_audit.arbiter_request(t)
    assert "급하지 않음" in msg and "낮음" not in msg
    assert schema["properties"]["urgency"]["enum"] == ["가", "나"]


def test_sides_are_mixed():
    sides = {gold_audit.gold_side(f"c-{i:04d}", "has_todo") for i in range(20)}
    assert sides == {"가", "나"}
    assert gold_audit.gold_side("c-0001", "has_todo") == gold_audit.gold_side("c-0001", "has_todo")


def test_prompt_file_hides_gold():
    text = (ROOT / "prompts" / "gold_audit" / "v1.md").read_text(encoding="utf-8")
    for word in HIDDEN[:4]:
        assert word not in text


def test_decode_maps_back():
    t = _target()
    side = gold_audit.gold_side("c-0002", "primary_purpose")
    other = "나" if side == "가" else "가"
    out = gold_audit.decode(t, {"primary_purpose": side, "complaint": other})
    assert out == {"primary_purpose": "gold", "complaint": "judge"}
    out = gold_audit.decode(t, {"primary_purpose": "둘 다 아님", "complaint": side})
    assert out == {"primary_purpose": "neither", "complaint": "gold"}
    with pytest.raises(ValueError):
        gold_audit.decode(t, {"primary_purpose": side, "complaint": "둘 다 아님"})
    with pytest.raises(ValueError):
        gold_audit.decode(t, {"primary_purpose": side})


def test_sample_constant():
    from evaluation.metrics import GOLD_AUDIT_SAMPLE

    assert GOLD_AUDIT_SAMPLE == 50


class Fake(FakeGateway):
    """fail_uids의 통화는 실패, answers가 있으면 그 답을 돌려준다(통화 → {항목: 선택})"""

    def __init__(self, tmp_path, fail_uids=(), answers=None):
        super().__init__(JsonlLog(tmp_path / "llm.jsonl"), scope="runtime")
        self.fail_uids, self.answers, self.seen = set(fail_uids), answers or {}, []

    def complete(self, alias, **kw):
        msg = kw["messages"][0]["content"]
        uid = next(u for u in ("c-0002", "c-0004", "c-0005", "c-0001") if f"통화 {u[-1]}" in msg)
        self.seen.append((alias, uid))
        r = super().complete(alias, **kw)
        if uid in self.fail_uids:
            return replace(r, ok=False, data=None, error="실패")
        if uid in self.answers:
            return replace(r, data=self.answers[uid])
        return r


def test_arbitrate_writes_records(conn, tmp_path):
    p = tmp_path / "a.jsonl"
    gw = Fake(tmp_path)
    r = gold_audit.arbitrate(gw, conn, p, "v1")
    assert (r["done"], r["failed"]) == (1, 0)
    assert gw.seen == [("gold_audit", "c-0002")]
    [rec] = gold_audit.load(p)
    assert (rec["call_uid"], rec["judge_version"], rec["prompt_version"]) == ("c-0002", "v1", "v1")
    assert rec["transcript_sha"] == gold_audit.transcript_sha("환자: 통화 2")
    assert set(rec["items"]) == {"primary_purpose", "complaint"}
    pp = rec["items"]["primary_purpose"]
    assert (pp["gold"], pp["judge"], pp["human"]) == ("complaint", "book", None)
    assert pp["gold_side"] == gold_audit.gold_side("c-0002", "primary_purpose")
    assert all(v["arbiter"] in ("gold", "judge", "neither") for v in rec["items"].values())


def test_failed_calls_retry_only_missing(conn, tmp_path):
    p = tmp_path / "a.jsonl"
    _set_judge(conn, "c-0004", urgency="urgent")
    r = gold_audit.arbitrate(Fake(tmp_path, fail_uids={"c-0004"}), conn, p, "v1")
    assert (r["done"], r["failed"]) == (1, 1)
    assert [x["call_uid"] for x in gold_audit.load(p)] == ["c-0002"]
    gw = Fake(tmp_path)
    assert gold_audit.arbitrate(gw, conn, p, "v1")["done"] == 1
    assert gw.seen == [("gold_audit", "c-0004")]
    assert [x["call_uid"] for x in gold_audit.load(p)] == ["c-0002", "c-0004"]


def test_invalid_choice_is_failure(conn, tmp_path):
    p = tmp_path / "a.jsonl"
    bad = {"c-0002": {"primary_purpose": "가", "complaint": "둘 다 아님"}}
    r = gold_audit.arbitrate(Fake(tmp_path, answers=bad), conn, p, "v1")
    assert (r["done"], r["failed"]) == (0, 1)
    assert gold_audit.load(p) == []


def test_new_judge_version_gets_new_lines(conn, tmp_path):
    p = tmp_path / "a.jsonl"
    gold_audit.arbitrate(Fake(tmp_path), conn, p, "v1")
    for cid, uid in ((4, "c-0004"), (2, "c-0002")):
        conn.execute(
            "INSERT INTO ax_llm_calls (id, task, model_alias, model_actual, prompt_version, ok, created_at)"
            " VALUES (?, 'judge', 'judge', 'solar-mini4', 'v2', 1, '2026-12-02 09:00:00')",
            (300 + cid,),
        )
        conn.execute(
            "INSERT INTO ax_model_judgments (call_uid, model_alias, model_actual, prompt_version, status,"
            " labels, llm_call_id) VALUES (?, 'judge', 'solar-mini4', 'v2', 'ok', ?, ?)",
            (uid, json.dumps(labels("reschedule")), 300 + cid),
        )
    gw = Fake(tmp_path)
    assert gold_audit.arbitrate(gw, conn, p, "v2")["done"] == 2
    recs = gold_audit.load(p)
    assert [(x["call_uid"], x["judge_version"]) for x in recs] == [
        ("c-0002", "v1"),
        ("c-0002", "v2"),
        ("c-0004", "v2"),
    ]


def test_arbitrate_does_not_write_app_db(conn, tmp_path):
    before = conn.execute("SELECT COUNT(*) FROM ax_llm_calls").fetchone()[0]
    gold_audit.arbitrate(Fake(tmp_path), conn, tmp_path / "a.jsonl", "v1")
    assert conn.execute("SELECT COUNT(*) FROM ax_llm_calls").fetchone()[0] == before
    assert (tmp_path / "llm.jsonl").exists()


def _rec(uid, arbiter="gold", version="v1", sha="x"):
    return {
        "call_uid": uid,
        "judge_version": version,
        "transcript_sha": sha,
        "items": {"has_todo": {"gold": 1, "judge": 0, "arbiter": arbiter, "human": None}},
    }


def _keys(*keys):
    """read 대역 — 다 쓰면 EOFError(사용자가 끊은 것처럼)"""
    it = iter(keys)

    def read(prompt=""):
        try:
            return next(it)
        except StopIteration:
            raise EOFError from None

    return read


def _two_targets(conn, tmp_path):
    """대상 c-0002(용건·불만)·c-0004(급함) — 중재까지 끝낸 기록"""
    _set_judge(conn, "c-0004", urgency="urgent")
    p = tmp_path / "a.jsonl"
    gold_audit.arbitrate(Fake(tmp_path), conn, p, "v1")
    return p


def test_group():
    assert gold_audit.group(_rec("c-1", "judge")) == "judge"
    assert gold_audit.group(_rec("c-1", "neither")) == "neither"
    assert gold_audit.group(_rec("c-1", "gold")) == "gold"


def test_sample_balanced_and_deterministic():
    recs = (
        [_rec(f"c-0{i:03d}", "judge") for i in range(5)]
        + [_rec(f"c-1{i:03d}", "neither") for i in range(40)]
        + [_rec(f"c-2{i:03d}", "gold") for i in range(40)]
    )
    s = gold_audit.sample(recs, 50)
    assert len(s) == len(set(s)) == 50
    assert sorted(s) == sorted(gold_audit.sample(list(reversed(recs)), 50))
    assert sum(u.startswith("c-0") for u in s) == 5  # judge 묶음은 5건뿐 — 전부
    n1, n2 = sum(u.startswith("c-1") for u in s), sum(u.startswith("c-2") for u in s)
    assert n1 + n2 == 45 and abs(n1 - n2) <= 1


def test_sample_takes_all_when_few():
    recs = [_rec(f"c-{i:04d}", "gold") for i in range(7)]
    assert sorted(gold_audit.sample(recs, 50)) == [r["call_uid"] for r in recs]


def test_current_skips_other_version_and_stale(conn):
    sha = gold_audit.transcript_sha("환자: 통화 2")
    recs = [_rec("c-0002", sha=sha), _rec("c-0002", version="v2", sha=sha), _rec("c-0004")]
    cur, stale = gold_audit.current(recs, conn, "v1")
    assert [r["call_uid"] for r in cur] == ["c-0002"] and stale == 1


def test_audit_saves_per_call_and_resumes(conn, tmp_path):
    p = _two_targets(conn, tmp_path)
    out = []
    with pytest.raises(EOFError):
        gold_audit.audit(conn, p, "v1", read=_keys("1", "1"), write=out.append)
    done = [r for r in gold_audit.load(p) if all(v["human"] for v in r["items"].values())]
    assert (
        len(done) == 1
    )  # 항목 2개·1개 통화 — 어느 쪽이 먼저든 키 2개면 첫 통화만 저장되고 둘째에서 끊김
    gold_audit.audit(conn, p, "v1", read=_keys("2", "2"), write=out.append)
    recs = gold_audit.load(p)
    assert all(v["human"] in ("gold", "judge") for r in recs for v in r["items"].values())
    first = next(r for r in recs if r["call_uid"] == done[0]["call_uid"])
    assert first["items"] == done[0]["items"]  # 저장된 답은 그대로


def test_audit_hides_gold_until_saved(conn, tmp_path):
    p = tmp_path / "a.jsonl"
    gold_audit.arbitrate(Fake(tmp_path), conn, p, "v1")  # 대상 c-0002 하나, 항목 2개
    out = []

    def read(prompt=""):
        out.append(f"<입력 {len(out)}>")
        return "1"

    gold_audit.audit(conn, p, "v1", read=read, write=out.append)
    before = "\n".join(out[: max(i for i, x in enumerate(out) if x.startswith("<입력"))])
    for word in ("골드", "판정기", "중재", "gold", "judge"):
        assert word not in before
    assert "중재" in "\n".join(out)  # 저장 뒤에는 보인다
    assert "환자: 통화 2" in before


def test_audit_reprompts_on_bad_key(conn, tmp_path):
    p = tmp_path / "a.jsonl"
    gold_audit.arbitrate(Fake(tmp_path), conn, p, "v1")
    # 용건(3값): 빈 줄·4·가 → 다시, 3 → 둘 다 아님 / 불만(2값): 3 → 다시, 1
    gold_audit.audit(conn, p, "v1", read=_keys("", "4", "가", "3", "3", "1"), write=lambda s: None)
    [rec] = gold_audit.load(p)
    assert rec["items"]["primary_purpose"]["human"] == "neither"
    side = gold_audit.gold_side("c-0002", "complaint")
    assert rec["items"]["complaint"]["human"] == ("gold" if side == "가" else "judge")


def test_audit_refuses_until_arbitration_complete(conn, tmp_path):
    p = tmp_path / "a.jsonl"
    gold_audit.arbitrate(Fake(tmp_path), conn, p, "v1")
    _set_judge(conn, "c-0004", urgency="urgent")  # 중재 안 된 대상이 생김
    with pytest.raises(SystemExit, match="gold-arbitrate"):
        gold_audit.audit(conn, p, "v1", read=_keys(), write=lambda s: None)
