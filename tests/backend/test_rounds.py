"""라운드 배치 실행기 (설계 "PR ② 평가 실행 — 라운드 배치") — 하네스가 저장된 단계로 다시 세워 이어 가는 것을 써서,
라운드마다 남은 실행의 다음 턴을 모아 Batch로(남은 실행이 min_batch 이하면 일반 호출로) 낸다."""

import pytest

from backend import followup
from backend.harness import rounds
from backend.harness.loop import Runner
from backend.harness.store import RunStore
from tests.backend.call_db import make_call_db
from tests.backend.test_followup import CLINIC, SUBMIT, Script


class BatchScript(Script):
    """Script + Batch 흉내 — custom_id는 실행 id. ended=False면 아직 안 끝남"""

    def __init__(self, conn, *runs):
        super().__init__(conn, *runs)
        self.batches, self.direct, self.ended = {}, 0, True

    def complete(self, alias, **kw):
        if kw.get("_batch") is None:
            self.direct += 1
        kw.pop("_batch", None)
        return super().complete(alias, **kw)

    def submit_batch(self, alias, requests, *, task, prompt_version=None, max_tokens=None):
        bid = f"b{len(self.batches)}"
        self.batches[bid] = (alias, requests)
        return bid

    def batch_status(self, bid):
        return "ended" if self.ended else "in_progress"

    def batch_results(self, bid, alias, *, task, prompt_version=None, schema=None):
        _, reqs = self.batches[bid]
        return {
            r["custom_id"]: self.complete(
                alias,
                _batch=True,
                task=task,
                messages=r["messages"],
                system=r.get("system"),
                tools=r.get("tools"),
                ref=("agent_run", int(r["custom_id"])),
            )
            for r in reqs
        }


@pytest.fixture
def conn(tmp_path):
    return make_call_db(tmp_path)


def _runner(conn, gw):
    reg, names = followup.registry(CLINIC)
    return Runner(followup.spec(names), gw, RunStore(conn), reg)


def _start(conn, runner, uids):
    return [rounds.create(runner, u, f"녹취록 {u}") for u in uids]


def test_rounds_switch_to_direct_below_min(conn, tmp_path):
    """실행 3개·min_batch 2 → 첫 라운드(3개)는 배치, 다음 라운드부터 남은 게 2개 이하라 일반 호출"""
    two = [("tool", "query_patient", {"chart_no": "C-00001"}), ("tool", "submit_followup", SUBMIT)]
    one = [("tool", "submit_followup", SUBMIT)]
    gw = BatchScript(conn, one, two, two)
    runner = _runner(conn, gw)
    ids = _start(conn, runner, ["c-0001", "c-0003", "c-0004"])
    out = rounds.run_rounds(runner, gw, ids, tmp_path / "s.json", min_batch=2, poll_seconds=0)
    assert set(out) == set(ids) and all(o.reason == "submitted" for o in out.values())
    assert len(gw.batches) == 1 and gw.direct == 2  # 두 번째 라운드의 실행 2개는 일반 호출


def test_rounds_resume_submitted_batch(conn, tmp_path):
    """제출 뒤 끊겨도 다시 돌리면 같은 배치를 이어 받는다 — 새로 제출하지 않음"""
    gw = BatchScript(conn, [("tool", "submit_followup", SUBMIT)])
    gw.ended = False
    runner = _runner(conn, gw)
    ids = _start(conn, runner, ["c-0001", "c-0003", "c-0004"])
    state = tmp_path / "s.json"
    with pytest.raises(TimeoutError):
        rounds.run_rounds(runner, gw, ids, state, min_batch=1, poll_seconds=0, max_polls=1)
    assert state.exists() and len(gw.batches) == 1
    gw.ended = True
    out = rounds.run_rounds(runner, gw, ids, state, min_batch=1, poll_seconds=0)
    assert len(gw.batches) == 1 and all(o.reason == "submitted" for o in out.values())
    assert not state.exists()


def test_rounds_outcome_matches_direct(conn, tmp_path):
    """같은 스크립트면 배치 경로와 일반 경로의 제출 인자가 같다"""
    steps = [
        ("tool", "query_patient", {"chart_no": "C-00001"}),
        ("tool", "submit_followup", SUBMIT),
    ]
    gw = BatchScript(conn, steps)
    runner = _runner(conn, gw)
    (rid,) = _start(conn, runner, ["c-0001"])
    rounds.run_rounds(runner, gw, [rid], tmp_path / "s.json", min_batch=0, poll_seconds=0)
    args, _ = followup._submitted(runner.store, rid)
    assert args == SUBMIT


def test_locked_by_other_is_skipped(conn, tmp_path):
    """다른 프로세스가 잠근(만료 전) 실행은 건드리지 않고 넘어간다"""
    gw = BatchScript(conn, [("tool", "submit_followup", SUBMIT)])
    runner = _runner(conn, gw)
    (rid,) = _start(conn, runner, ["c-0001"])
    conn.execute(
        "UPDATE ax_agent_runs SET locked_by = 'other', heartbeat_at = '2999-01-01 00:00:00' WHERE id = ?",
        (rid,),
    )
    conn.commit()
    assert (
        rounds.run_rounds(runner, gw, [rid], tmp_path / "s.json", min_batch=0, poll_seconds=0)
        == {}
    )


def test_batch_none_goes_direct_next(conn, tmp_path):
    """배치에서 결과가 없던(None) 실행은 다음 라운드에 일반 호출로 — 재제출이 끝없이 반복되지 않게"""

    class Drop(BatchScript):
        def batch_results(self, bid, alias, **kw):
            out = super().batch_results(bid, alias, **kw)
            return {k: None for k in out}

    gw = Drop(conn, [("tool", "submit_followup", SUBMIT)])
    runner = _runner(conn, gw)
    ids = _start(conn, runner, ["c-0001", "c-0003"])
    out = rounds.run_rounds(runner, gw, ids, tmp_path / "s.json", min_batch=0, poll_seconds=0)
    assert all(o.reason == "submitted" for o in out.values()) and len(gw.batches) == 1


def test_batch_wait_keeps_heartbeat(conn, tmp_path):
    """Batch를 기다리는 동안 하트비트를 갱신한다 — 오래 걸려도 잠금이 만료돼 다른 곳(화면 조사)에 뺏기지 않게"""
    from datetime import datetime, timedelta

    t = [datetime(2026, 12, 1, 9, 0, 0)]
    seen = []

    class Slow(BatchScript):
        def batch_status(self, bid):
            if not seen:
                t[0] += timedelta(seconds=RunStore.lock_ttl + 100)  # 기다리는 사이 시간이 흐름
                seen.append(None)
                return "in_progress"
            seen.append(store.run(rid)["heartbeat_at"])
            return "ended"

    gw = Slow(conn, [("tool", "submit_followup", SUBMIT)])
    reg, names = followup.registry(CLINIC)
    store = RunStore(conn, clock=lambda: t[0])
    runner = Runner(followup.spec(names), gw, store, reg)
    (rid,) = _start(conn, runner, ["c-0001"])
    rounds.run_rounds(runner, gw, [rid], tmp_path / "s.json", min_batch=0, poll_seconds=0)
    assert seen[1] == t[0].isoformat(sep=" ", timespec="seconds")


def test_batch_status_transient_error_keeps_polling(conn, tmp_path):
    """Batch 진행 상태를 묻다가 서버 일시 오류(5xx·연결)가 나면 멈추지 않고 다음 확인에서 다시 묻는다"""
    import anthropic
    import httpx

    calls = []

    class Flaky(BatchScript):
        def batch_status(self, bid):
            calls.append(bid)
            if len(calls) == 1:
                raise anthropic.InternalServerError(
                    "x",
                    response=httpx.Response(500, request=httpx.Request("GET", "http://x")),
                    body=None,
                )
            return "ended"

    gw = Flaky(conn, [("tool", "submit_followup", SUBMIT)])
    runner = _runner(conn, gw)
    (rid,) = _start(conn, runner, ["c-0001"])
    out = rounds.run_rounds(runner, gw, [rid], tmp_path / "s.json", min_batch=0, poll_seconds=0)
    assert out[rid].status == "done" and len(calls) == 2
