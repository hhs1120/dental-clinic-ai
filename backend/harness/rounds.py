"""라운드 배치 실행기 (설계 docs/superpowers/specs/2026-11-08-followup-autoclose-design.md "평가 실행 — 라운드 배치").
에이전트는 도구 결과를 보고 다음 호출을 정하므로 Batch 한 번으로 끝나지 않는다. 실행마다 Runner.drive를 기록 게이트웨이로
돌려, 결과가 준비된 실행은 이어 가고 아닌 실행은 요청만 모은 채 멈춘다(하네스는 저장된 단계로 다시 세워 이어 가므로 상태가
맞다). 모인 요청이 min_batch보다 많으면 Batch로 — 배치 id를 state_path에 남겨 끊겨도 다시 제출하지 않는다 — 적으면 일반
호출로 바로 부른다. 앞 라운드는 반값, 몇 건 안 남은 꼬리에서 Batch 대기를 되풀이하지 않게."""

import json
import time
from dataclasses import replace
from pathlib import Path

from backend.harness.loop import Outcome, Runner, external
from backend.harness.store import RunLocked

ROUND_BATCH_MIN = 30  # 남은 실행이 이 수 이하면 그 라운드부터 일반 호출(사람이 정함)


class Pending(Exception):
    """이 실행의 다음 턴 결과가 아직 없다 — drive를 멈추고 요청만 남긴다"""


class _Recorder:
    def __init__(self):
        self.ready, self.wanted = {}, {}

    def complete(self, alias, **kw):
        run = kw["ref"][1]
        if run in self.ready:
            return self.ready.pop(run)
        self.wanted[run] = (alias, kw)
        raise Pending


def create(runner: Runner, target: str, text: str) -> int:
    """실행만 만들고 입력을 남긴다(Runner.start에서 drive를 뺀 것)"""
    with runner.store.conn:
        run_id = runner.store.create_run(
            runner.spec.name, target, runner.spec.prompt_version, runner.owner
        )
        runner.store.add_step(run_id, "input", result=external("input", text))
    return run_id


def _request(run: int, alias: str, kw: dict) -> dict:
    return {
        "custom_id": str(run),
        "system": kw.get("system"),
        "messages": kw["messages"],
        "tools": kw.get("tools"),
    }


def run_rounds(
    runner: Runner,
    gw,
    run_ids: list[int],
    state_path: Path,
    min_batch: int = ROUND_BATCH_MIN,
    poll_seconds: int = 60,
    max_polls: int | None = None,
) -> dict[int, Outcome]:
    """run_ids를 모두 끝까지 — {실행 id: Outcome}. gw는 submit_batch·batch_results와 complete를 다 가진 게이트웨이"""
    rec = _Recorder()
    driver = replace(runner, gateway=rec)
    store = runner.store
    for run in run_ids:  # 다른 프로세스가 만든(끊긴) 실행은 잠금을 가져온다
        try:
            with store.conn:
                store.take_over(run, runner.owner)
        except RunLocked:
            pass  # 이미 이 실행기의 잠금이거나 끝난 실행
    active = [r for r in run_ids if store.run(r)["status"] == "running"]
    out = {r: Outcome(r, store.run(r)["status"]) for r in run_ids if r not in active}
    if state_path.exists():  # 끊기기 전에 제출한 배치부터 받는다
        _collect(gw, runner, rec, json.loads(state_path.read_text()), poll_seconds, max_polls)
        state_path.unlink()
    while active:
        for run in list(active):
            try:
                out[run] = driver.drive(run)
                active.remove(run)
            except Pending:
                pass
        if not active:
            break
        wanted, rec.wanted = rec.wanted, {}
        if len(wanted) > min_batch:
            alias = next(iter(wanted.values()))[0]
            kw0 = next(iter(wanted.values()))[1]
            bid = gw.submit_batch(
                alias,
                [_request(r, a, kw) for r, (a, kw) in wanted.items()],
                task=kw0["task"],
                prompt_version=kw0.get("prompt_version"),
            )
            state = {
                "batch_id": bid,
                "alias": alias,
                "task": kw0["task"],
                "prompt_version": kw0.get("prompt_version"),
            }
            state_path.write_text(json.dumps(state))
            _collect(gw, runner, rec, state, poll_seconds, max_polls)
            state_path.unlink()
        else:
            for run, (alias, kw) in wanted.items():
                rec.ready[run] = gw.complete(alias, **kw)
    return out


def _collect(gw, runner, rec, state: dict, poll_seconds: int, max_polls: int | None) -> None:
    polls = 0
    while gw.batch_status(state["batch_id"]) != "ended":
        polls += 1
        if max_polls and polls >= max_polls:
            raise TimeoutError(f"배치 {state['batch_id']} 진행 중 — 다시 실행하면 이어서 받는다")
        time.sleep(poll_seconds)
    results = gw.batch_results(
        state["batch_id"],
        state["alias"],
        task=state["task"],
        prompt_version=state.get("prompt_version"),
    )
    for cid, r in results.items():
        run = int(cid)
        if r is None:  # 처리 안 된 건 — 다음 라운드에 다시 모인다
            continue
        rec.ready[run] = r
