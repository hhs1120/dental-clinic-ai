"""앱 게이트웨이 (앱 설계 §7.1) — 호출이 끝나기 전에 실행 잠금이 만료되지 않게 한다."""

from datetime import date

from backend.harness.llm import APP_RETRIES, APP_TIMEOUT, make_gateway
from backend.harness.store import RunStore
from shared import llm


def test_worst_case_call_is_shorter_than_lock():
    longest_chain = max(len(chain) for chain in llm.load_config()["aliases"].values())
    assert APP_TIMEOUT * (APP_RETRIES + 1) * longest_chain < RunStore.lock_ttl


def test_gateway_uses_app_limits_and_log(conn, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    gw = make_gateway(conn)
    assert gw.scope == "runtime"
    assert gw.client.timeout == APP_TIMEOUT and gw.client.max_retries == APP_RETRIES
    assert gw.log.spent(date(2026, 1, 1), "runtime") == 0


def test_gateway_keeps_clock(conn, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    clock = lambda: None  # noqa: E731
    gw = make_gateway(conn, clock)
    assert gw.clock is clock and gw.upstage_client is None
