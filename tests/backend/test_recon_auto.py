"""⑧-1 일계표 자동 마감(backend/recon_auto.py) — 설계 docs/superpowers/specs/2026-11-11-finance-recon-design.md §3
"자동 마감". 규칙으로 되는 원인 묶음이고 규칙 = 에이전트 원인일 때만, 정책 기본 끔, 되돌린 건·사람 확정은 다시 닫지 않는다."""

import pytest

from backend import autoclose, recon, recon_agent, recon_auto, recon_match
from backend.harness.auto import revert_auto_action
from tests.backend.test_recon_agent import LOOK, Script, submit
from tests.backend.test_recon_match import DAY, RATE, Books

GROUPS = {"fee_mismatch": "rule", "deposit_delay": "rule", "amount_typo": "ambiguous"}


@pytest.fixture
def conn(tmp_path):
    b = Books(tmp_path / "source.sqlite")
    b.pay("2026-11-03 10:00:00", 100_000, "card", no="X")  # 수수료 차이 한 건
    b.sale("2026-11-03 10:00:00", 100_000, "X")
    b.deposit("2026-11-03", 100_000, fee=int(100_000 * (RATE + 0.003)))
    return b.app()


def key(conn) -> str:
    [item] = recon_match.detect(recon_match.load(conn, DAY))
    return item["key"]


def investigated(conn, cause="fee_mismatch"):
    recon_agent.investigate(conn, Script(conn, LOOK, submit(cause=cause)), key(conn), DAY)


def on(conn):
    autoclose.set_policy(conn, recon_auto.KIND, True)


def test_reason_only_rule_group_and_same():
    assert recon_auto.autoclose_reason("fee_mismatch", "fee_mismatch", GROUPS) == recon_auto.REASON
    assert recon_auto.autoclose_reason("fee_mismatch", "deposit_delay", GROUPS) is None
    assert recon_auto.autoclose_reason("amount_typo", "amount_typo", GROUPS) is None
    assert recon_auto.autoclose_reason("fee_mismatch", None, GROUPS) is None


def test_refresh_off_by_default(conn):
    investigated(conn)
    assert recon_auto.refresh(conn, DAY) == {"off": True}
    assert recon.open_count(conn, DAY) == 1


def test_refresh_closes_and_open_count_drops(conn):
    investigated(conn)
    on(conn)
    assert recon_auto.refresh(conn, DAY)["closed"] == 1
    assert recon.open_count(conn, DAY) == 0
    assert recon.ledger(conn, DAY)["chips"]["open_items"] == 0
    assert set(recon_auto.active(conn, {key(conn)})) == {key(conn)}


def test_different_cause_stays_open(conn):
    investigated(conn, cause="deposit_delay")
    on(conn)
    assert recon_auto.refresh(conn, DAY)["closed"] == 0
    assert recon.open_count(conn, DAY) == 1


def test_revert_stays_open(conn):
    investigated(conn)
    on(conn)
    recon_auto.refresh(conn, DAY)
    [row] = recon_auto.active(conn, {key(conn)}).values()
    revert_auto_action(conn, recon_auto.KINDS, row["id"], "데스크")
    recon_auto.refresh(conn, DAY)
    assert recon_auto.active(conn, {key(conn)}) == {}
    assert recon.open_count(conn, DAY) == 1


def test_confirmed_items_skipped(conn):
    investigated(conn)
    recon.confirm(conn, key(conn), "fee_mismatch", "데스크", "2026-12-01 09:00:00", DAY)
    on(conn)
    assert recon_auto.refresh(conn, DAY)["closed"] == 0


def test_auto_ignores_stale_keys(conn):
    investigated(conn)
    on(conn)
    recon_auto.refresh(conn, DAY)
    assert recon_auto.active(conn, {"other"}) == {}
