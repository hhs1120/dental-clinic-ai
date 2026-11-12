"""⑧-1 조사 에이전트(backend/recon_agent.py) — 설계 docs/superpowers/specs/2026-11-11-finance-recon-design.md §3.
대본 게이트웨이로 글·도구 호출 순서를 정해 두고, 근거 훅·상한·조사 과정 줄·다시 조사 금지·일괄 대상을 본다."""

import json
from dataclasses import replace

import pytest

from backend import recon_agent, recon_match
from backend.harness.store import RunStore, SqliteLlmLog
from generator import master
from shared.llm import FakeGateway
from tests.backend.test_recon_match import DAY, Books

LOOK = ("tool", "ledger_day", {"day": "2026-11-03"})


def submit(cause="amount_typo", evidence=(1,), runner_up=None):
    return (
        "tool",
        recon_agent.SUBMIT,
        {
            "cause": cause,
            "runner_up": runner_up,
            "evidence": list(evidence),
            "reason": "금액이 10배",
        },
    )


class Script(FakeGateway):
    """실행마다 같은 대본 — ("tool", 이름, 인자) · ("say", 글, 이름, 인자) · ("text", 글)"""

    def __init__(self, conn, *steps):
        super().__init__(SqliteLlmLog(conn), scope="runtime")
        self.steps, self.pos, self.first = list(steps), {}, None

    def complete(self, alias, **kw):
        r = super().complete(alias, **kw)
        run = kw["ref"][1]
        if self.first is None:
            self.first = json.dumps(kw["messages"], ensure_ascii=False)
        i = self.pos.get(run, 0)
        self.pos[run] = i + 1
        kind, *rest = self.steps[min(i, len(self.steps) - 1)]
        if kind == "text":
            return replace(
                r, text=rest[0], tool_calls=(), content=[{"type": "text", "text": rest[0]}]
            )
        text = rest.pop(0) if kind == "say" else ""
        name, args = rest
        call = {"id": f"tu{run}-{i}", "name": name, "input": args}
        content = ([{"type": "text", "text": text}] if text else []) + [
            {"type": "tool_use", **call}
        ]
        return replace(r, text=text, tool_calls=(call,), content=content)


@pytest.fixture
def conn(tmp_path):
    b = Books(tmp_path / "source.sqlite")
    b.pay("2026-11-03 10:00:00", 3_500, "card")  # 금액 오입력 한 건
    b.sale("2026-11-03 10:00:00", 35_000, "R1")
    b.settle("2026-11-03")
    return b.app()


def key(conn) -> str:
    [item] = recon_match.detect(recon_match.load(conn, DAY))
    return item["key"]


def run(conn, *steps, on_line=None):
    gw = Script(conn, *steps)
    return recon_agent.investigate(conn, gw, key(conn), DAY, on_line), gw


def test_causes_match_master():
    assert set(recon_agent.CAUSES) == set(master.RECON_CAUSE_GROUPS) | {"unknown"}


def test_submit_after_lookup_saves_cause(conn):
    r, _ = run(conn, ("say", "가설: 금액 오입력 같다", *LOOK[1:]), submit())
    assert r["status"] == "done" and r["cause"] == "amount_typo" and r["evidence"] == [1]
    assert recon_agent.result(conn, key(conn)) == r


def test_evidence_must_be_lookup(conn):
    r, _ = run(conn, LOOK, submit(evidence=[3]), submit(evidence=[1]))
    assert r["cause"] == "amount_typo"
    steps = RunStore(conn).steps(r["run_id"])
    assert [s["rule_id"] for s in steps if s["kind"] == "tool"] == [
        None,
        recon_agent.EVIDENCE_RULE,
        None,
    ]


def test_cause_needs_evidence_but_unknown_does_not(conn):
    r, _ = run(conn, submit(evidence=[]), submit(cause="unknown", evidence=[]))
    assert r["cause"] == "unknown" and r["status"] == "done"


def test_call_limit_reads_unknown(conn):
    r, _ = run(conn, LOOK)  # 조회만 되풀이
    assert r["status"] == "handed_off" and r["cause"] == "unknown"
    assert r["tool_calls"] == recon_agent.RECON_MAX_TOOL_CALLS


def test_lines_kinds(conn):
    r, _ = run(conn, ("say", "가설: 금액 오입력\n\n비교: 10배 차이", *LOOK[1:]), submit())
    ls = recon_agent.lines(RunStore(conn).steps(r["run_id"]))
    assert [x["kind"] for x in ls] == ["hypothesis", "compare", "lookup", "conclusion"]
    assert ls[0]["text"] == "금액 오입력" and ls[2]["lookup_no"] == 1
    assert ls[3]["cause"] == "amount_typo"


def test_unprefixed_text_is_note_and_limit_gets_conclusion(conn):
    r, _ = run(conn, ("say", "장부부터 본다", *LOOK[1:]))
    ls = recon_agent.lines(RunStore(conn).steps(r["run_id"]))
    assert ls[0]["kind"] == "note"
    assert ls[-1]["kind"] == "conclusion" and ls[-1]["cause"] == "unknown"


def test_live_lines_match_saved(conn):
    got = []
    r, _ = run(conn, ("say", "가설: 금액 오입력", *LOOK[1:]), submit(), on_line=got.append)
    assert got == recon_agent.lines(RunStore(conn).steps(r["run_id"]))


def test_input_has_no_rule_cause(conn):
    _, gw = run(conn, LOOK, submit())
    assert "amount_typo" not in gw.first and "금액 오입력" not in gw.first
    assert "35,000" in gw.first


def test_investigate_twice_raises(conn):
    run(conn, LOOK, submit())
    with pytest.raises(recon_agent.AlreadyDone):
        run(conn, LOOK, submit())


def test_unknown_key_raises(conn):
    with pytest.raises(ValueError):
        recon_agent.investigate(conn, Script(conn, LOOK), "nope", DAY)


def test_targets_skip_done_and_handed_off(conn):
    assert len(recon_agent.targets(conn, DAY)) == 1
    run(conn, LOOK)  # handed_off
    assert recon_agent.targets(conn, DAY) == []
    conn.execute("UPDATE ax_agent_runs SET status = 'failed'")
    assert len(recon_agent.targets(conn, DAY)) == 1


def test_targets_month_filter(conn):
    assert recon_agent.targets(conn, DAY, ["2026-10"]) == []
    assert len(recon_agent.targets(conn, DAY, ["2026-11"])) == 1


def test_batch_runs_targets(conn, tmp_path):
    gw = Script(conn, LOOK, submit())
    out = recon_agent.run_batch(conn, gw, DAY, batch=False, state=tmp_path / "s.json")
    assert out["done"] == 1
    assert recon_agent.result(conn, key(conn))["cause"] == "amount_typo"
