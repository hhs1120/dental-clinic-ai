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
    """fail_uids는 실패, raise_uids는 예외, fallback_uids는 폴백 응답, answers는 그 답(통화 → {항목: 선택})"""

    def __init__(self, tmp_path, fail_uids=(), answers=None, raise_uids=(), fallback_uids=()):
        super().__init__(JsonlLog(tmp_path / "llm.jsonl"), scope="runtime")
        self.fail_uids, self.answers, self.seen = set(fail_uids), answers or {}, []
        self.raise_uids, self.fallback_uids, self.kwargs = set(raise_uids), set(fallback_uids), []

    def complete(self, alias, **kw):
        msg = kw["messages"][0]["content"]
        uid = next(u for u in ("c-0002", "c-0004", "c-0005", "c-0001") if f"통화 {u[-1]}" in msg)
        self.seen.append((alias, uid))
        self.kwargs.append(kw)
        if uid in self.raise_uids:
            raise RuntimeError("예상 밖 오류")
        r = super().complete(alias, **kw)
        if uid in self.fail_uids:
            return replace(r, ok=False, data=None, error="실패")
        if uid in self.fallback_uids:
            return replace(r, fallback_used=True)
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


def _real(arbiter="judge", human=None, version="v1"):
    """대상 c-0002의 실제 항목(용건 complaint↔book, 불만 1↔0)으로 만든 기록"""
    return {
        "call_uid": "c-0002",
        "judge_version": version,
        "transcript_sha": gold_audit.transcript_sha("환자: 통화 2"),
        "items": {
            "primary_purpose": {
                "gold": "complaint",
                "judge": "book",
                "arbiter": arbiter,
                "human": human,
            },
            "complaint": {"gold": 1, "judge": 0, "arbiter": "gold", "human": human and "gold"},
        },
    }


def test_current_skips_other_version_and_stale(conn):
    recs = [_real(), _real(version="v2"), _rec("c-0004")]
    cur, stale = gold_audit.current(recs, conn, "v1")
    assert [r["call_uid"] for r in cur] == ["c-0002"] and stale == 1


def test_current_rejects_changed_items(conn):
    """녹취록은 같아도 골드·판정기 값이나 항목이 기록 때와 다르면 낡은 기록"""
    rec = _real()
    rec["items"]["primary_purpose"]["judge"] = "cancel"
    assert gold_audit.current([rec], conn, "v1") == ([], 1)
    rec = _real()
    del rec["items"]["complaint"]
    assert gold_audit.current([rec], conn, "v1") == ([], 1)


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


def _rec2(uid, items, version="v1", sha="x"):
    """items = {항목: (arbiter, human)}"""
    return {
        "call_uid": uid,
        "judge_version": version,
        "transcript_sha": sha,
        "items": {
            i: {"gold": 1, "judge": 0, "arbiter": a, "human": h} for i, (a, h) in items.items()
        },
    }


def test_errors_from_arbiter_only():
    rec = _rec2("c-0002", {"primary_purpose": ("judge", "gold"), "complaint": ("gold", "judge")})
    assert gold_audit.errors([rec]) == {("c-0002", "primary_purpose")}
    rec = _rec2("c-0002", {"counterpart": ("neither", None)})
    assert gold_audit.errors([rec]) == {("c-0002", "counterpart")}


def test_summarize_agreement():
    recs = [
        _rec2("c-1", {"primary_purpose": ("judge", "judge"), "has_todo": ("gold", "judge")}),
        _rec2("c-2", {"has_todo": ("neither", None)}),
    ]
    s = gold_audit.summarize(recs)
    assert s["calls"] == 2
    assert s["arbiter"]["has_todo"] == {"gold": 1, "judge": 0, "neither": 1}
    assert s["agreement"]["has_todo"] == {"k": 0, "n": 1}
    assert s["agreement"]["primary_purpose"] == {"k": 1, "n": 1}
    assert s["agreement_total"] == {"k": 1, "n": 2}
    assert s["human_errors"] == {"judge": 2, "neither": 0}
    # 중재 선택별 사람 일치 — 중재가 판정기 쪽을 고른 항목을 사람이 얼마나 따랐나
    assert s["by_arbiter"] == {"judge": {"k": 1, "n": 1}, "gold": {"k": 0, "n": 1}}
    assert s["human_pending"] == 1  # 사람이 아직 안 본 통화


def test_stale_records_are_skipped(conn):
    """재생성으로 녹취록이 바뀐 기록은 빠지고 수만 남는다"""
    ok = _real()
    old = _rec2("c-0004", {"urgency": ("judge", None)}, sha="옛 녹취록")
    cur, stale = gold_audit.current([ok, old], conn, "v1")
    assert cur == [ok] and stale == 1
    assert gold_audit.errors(cur) == {("c-0002", "primary_purpose")}


def test_stale_target_is_rearbitrated(conn, tmp_path):
    """녹취록이 바뀐 대상은 다시 중재해 옛 줄을 바꾼다 — 안 그러면 audit이 영원히 막힌다"""
    p = tmp_path / "a.jsonl"
    old = _real()
    old["transcript_sha"] = "옛 녹취록"
    gold_audit.save(p, [old])
    assert gold_audit.arbitrate(Fake(tmp_path), conn, p, "v1")["done"] == 1
    [rec] = gold_audit.load(p)
    assert rec["transcript_sha"] == gold_audit.transcript_sha("환자: 통화 2")
    gold_audit.audit(conn, p, "v1", read=_keys("1", "1"), write=lambda s: None)


def test_exception_keeps_finished_results(conn, tmp_path):
    """호출 하나가 예외를 내도 끝난 결과는 남고 실패로 센다"""
    p = tmp_path / "a.jsonl"
    _set_judge(conn, "c-0004", urgency="urgent")
    r = gold_audit.arbitrate(Fake(tmp_path, raise_uids={"c-0004"}), conn, p, "v1")
    assert (r["done"], r["failed"]) == (1, 1)
    assert [x["call_uid"] for x in gold_audit.load(p)] == ["c-0002"]


def test_fallback_is_failure(conn, tmp_path):
    """서버 측 폴백으로 다른 모델이 답하면 중재가 섞이므로 실패로 보고 다시 돈다"""
    p = tmp_path / "a.jsonl"
    r = gold_audit.arbitrate(Fake(tmp_path, fallback_uids={"c-0002"}), conn, p, "v1")
    assert (r["done"], r["failed"]) == (0, 1) and gold_audit.load(p) == []


def test_effort_and_limit(conn, tmp_path):
    """추론 단계는 프롬프트 머리말(기본), 인자로 덮어쓰면 그 값 — 기록에 남는다. limit은 해시 순 앞에서 N통화"""
    _set_judge(conn, "c-0004", urgency="urgent")
    p = tmp_path / "a.jsonl"
    gw = Fake(tmp_path)
    assert gold_audit.arbitrate(gw, conn, p, "v1", limit=1)["done"] == 1
    default = gold_audit.prompts.effort("gold_audit", "v1")
    assert default and gw.kwargs[0]["effort"] == default
    assert gold_audit.load(p)[0]["effort"] == default
    other = "low" if default != "low" else "high"
    p2 = tmp_path / "b.jsonl"
    gw = Fake(tmp_path)
    gold_audit.arbitrate(gw, conn, p2, "v1", effort=other)
    assert {k["effort"] for k in gw.kwargs} == {other}
    assert {r["effort"] for r in gold_audit.load(p2)} == {other}


def test_pending_order_mixes_groups():
    """사람 화면 순서는 해시 순 — 묶음(중재 결과) 순으로 보이면 결과가 미리 샌다"""
    recs = (
        [_rec(f"c-0{i:03d}", "judge") for i in range(20)]
        + [_rec(f"c-1{i:03d}", "neither") for i in range(20)]
        + [_rec(f"c-2{i:03d}", "gold") for i in range(20)]
    )
    order = gold_audit.pending(recs)
    assert sorted(order) == sorted(gold_audit.sample(recs))
    assert len({u[2] for u in order[:10]}) > 1


def test_sample_fill_is_even_whatever_ids():
    """모자란 묶음의 몫을 남은 묶음에 돌아가며 채운다 — uid 모양과 상관없이"""
    for pre in ("c-", "x-", "q9-"):
        recs = (
            [_rec(f"{pre}0{i:03d}", "judge") for i in range(5)]
            + [_rec(f"{pre}1{i:03d}", "neither") for i in range(40)]
            + [_rec(f"{pre}2{i:03d}", "gold") for i in range(40)]
        )
        s = gold_audit.sample(recs, 50)
        n1 = sum(u.startswith(pre + "1") for u in s)
        n2 = sum(u.startswith(pre + "2") for u in s)
        assert n1 + n2 == 45 and abs(n1 - n2) <= 1


class BatchFake(FakeGateway):
    """Batch 흉내 — missing의 custom_id는 결과 None, status가 'in_progress'면 아직 안 끝남"""

    def __init__(self, tmp_path, missing=(), status="ended"):
        super().__init__(JsonlLog(tmp_path / "llm.jsonl"), scope="runtime")
        self.missing, self.status, self.batches = set(missing), status, {}

    def submit_batch(self, alias, requests, *, task, prompt_version=None, max_tokens=None):
        bid = f"b{len(self.batches)}"
        self.batches[bid] = (alias, requests)
        return bid

    def batch_status(self, batch_id):
        return self.status

    def batch_results(self, batch_id, alias, *, task, prompt_version=None, schema=None):
        _, requests = self.batches[batch_id]
        return {
            r["custom_id"]: None
            if r["custom_id"] in self.missing
            else FakeGateway.complete(
                self,
                alias,
                task=task,
                system=r["system"],
                messages=r["messages"],
                schema=r["schema"],
            )
            for r in requests
        }


def test_batch_writes_records_and_retries_missing(conn, tmp_path):
    """배치는 같은 입력·추론 단계로 제출하고, 결과 없는 건은 기록하지 않아 다음에 다시"""
    _set_judge(conn, "c-0004", urgency="urgent")
    p, state = tmp_path / "a.jsonl", tmp_path / "batch.json"
    gw = BatchFake(tmp_path, missing={"c-0004"})
    r = gold_audit.arbitrate_batch(gw, conn, p, "v1", state_path=state, poll_seconds=0)
    assert (r["done"], r["failed"]) == (1, 1)
    [(alias, reqs)] = gw.batches.values()
    assert alias == "gold_audit" and {q["custom_id"] for q in reqs} == {"c-0002", "c-0004"}
    assert all(q["effort"] == gold_audit.prompts.effort("gold_audit", "v1") for q in reqs)
    assert [x["call_uid"] for x in gold_audit.load(p)] == ["c-0002"]
    assert not state.exists()  # 끝나면 진행 상태 파일을 지운다
    gw2 = BatchFake(tmp_path)
    assert (
        gold_audit.arbitrate_batch(gw2, conn, p, "v1", state_path=state, poll_seconds=0)["done"]
        == 1
    )
    [(_, reqs2)] = gw2.batches.values()
    assert [q["custom_id"] for q in reqs2] == ["c-0004"]


def test_batch_resumes_submitted(conn, tmp_path):
    """제출해 둔 배치가 있으면 다시 제출하지 않고 그 결과를 받는다(중간에 끊겨도 비용이 두 번 나가지 않게)"""
    p, state = tmp_path / "a.jsonl", tmp_path / "batch.json"
    gw = BatchFake(tmp_path, status="in_progress")
    with pytest.raises(TimeoutError):
        gold_audit.arbitrate_batch(
            gw, conn, p, "v1", state_path=state, poll_seconds=0, max_polls=2
        )
    assert state.exists() and len(gw.batches) == 1
    gw.status = "ended"
    assert (
        gold_audit.arbitrate_batch(gw, conn, p, "v1", state_path=state, poll_seconds=0)["done"]
        == 1
    )
    assert len(gw.batches) == 1


def test_audit_shows_label_guide(conn, tmp_path):
    """항목마다 고를 수 있는 라벨 전체와 뜻을 먼저 보인다 — 용건은 14가지 전부, 표시 항목은 판정 기준 문장"""
    from shared.call_labels import PURPOSE_KO

    p = tmp_path / "a.jsonl"
    gold_audit.arbitrate(Fake(tmp_path), conn, p, "v1")  # 대상 c-0002: 용건·불만
    out = []
    gold_audit.audit(conn, p, "v1", read=_keys("1", "1"), write=out.append)
    text = "\n".join(out)
    assert all(ko in text for ko in PURPOSE_KO.values())
    assert "불만을 드러냈다" in text  # complaint 표시 항목 기준
    assert gold_audit.guide("counterpart")[0].strip().startswith("환자")
    assert any("급하지 않음" in line for line in gold_audit.guide("urgency"))


def test_batch_state_is_bound_to_out_path(conn, tmp_path):
    """맛보기 배치(다른 기록 파일)를 받다 끊긴 상태로 본 기록을 돌리면 멈춘다 — 다른 설정의 결과가 섞이지 않게"""
    state = tmp_path / "batch.json"
    gw = BatchFake(tmp_path, status="in_progress")
    with pytest.raises(TimeoutError):
        gold_audit.arbitrate_batch(
            gw, conn, tmp_path / "pilot.jsonl", "v1", state_path=state, poll_seconds=0, max_polls=1
        )
    gw.status = "ended"
    with pytest.raises(SystemExit, match="pilot.jsonl"):
        gold_audit.arbitrate_batch(
            gw, conn, tmp_path / "a.jsonl", "v1", state_path=state, poll_seconds=0
        )


def test_batch_state_other_judge_version(conn, tmp_path):
    state = tmp_path / "batch.json"
    gw = BatchFake(tmp_path, status="in_progress")
    p = tmp_path / "a.jsonl"
    with pytest.raises(TimeoutError):
        gold_audit.arbitrate_batch(
            gw, conn, p, "v1", state_path=state, poll_seconds=0, max_polls=1
        )
    with pytest.raises(SystemExit, match="v1"):
        gold_audit.arbitrate_batch(gw, conn, p, "v2", state_path=state, poll_seconds=0)


def test_batch_drops_answers_for_changed_targets(conn, tmp_path):
    """제출 뒤 값이 바뀐 대상의 답, 대상 밖 통화의 답은 기록하지 않는다"""
    _set_judge(conn, "c-0004", urgency="urgent")
    state, p = tmp_path / "batch.json", tmp_path / "a.jsonl"
    gw = BatchFake(tmp_path, status="in_progress")
    with pytest.raises(TimeoutError):
        gold_audit.arbitrate_batch(
            gw, conn, p, "v1", state_path=state, poll_seconds=0, max_polls=1
        )
    _set_judge(conn, "c-0002", primary_purpose="cancel")  # 같은 항목, 판정기 값만 바뀜
    _set_judge(conn, "c-0004", urgency="normal")  # 대상에서 빠짐
    gw.status = "ended"
    r = gold_audit.arbitrate_batch(gw, conn, p, "v1", state_path=state, poll_seconds=0)
    assert (r["done"], r["failed"]) == (0, 2) and gold_audit.load(p) == []


def test_budget_exceeded_is_raised(conn, tmp_path):
    """하루 상한에 걸리면 실패로 묻지 않고 그대로 알린다"""
    from shared.llm import BudgetExceeded

    class Over(Fake):
        def complete(self, alias, **kw):
            raise BudgetExceeded("하루 상한")

    with pytest.raises(BudgetExceeded):
        gold_audit.arbitrate(Over(tmp_path), conn, tmp_path / "a.jsonl", "v1")


def test_guide_covers_every_item():
    """프롬프트 형식이 바뀌어도 판정 도중 멈추지 않게 — 9항목 전부, 용건은 14가지 다"""
    from shared.call_labels import ITEMS, PURPOSE_KO

    for item in ITEMS:
        assert gold_audit.guide(item)
    purpose = [
        line for line in gold_audit.guide("primary_purpose") if not line.strip().startswith("(")
    ]
    assert len(purpose) == len(PURPOSE_KO)


def test_reasoning_max_tokens_from_backend():
    from backend.classify import REASONING_MAX_TOKENS

    assert gold_audit.REASONING_MAX_TOKENS is REASONING_MAX_TOKENS
