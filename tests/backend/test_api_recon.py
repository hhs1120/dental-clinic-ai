"""⑧-1 재무 대조 API (backend/api.py GET /finance/recon·POST /finance/recon/{key}/confirm, 설계
docs/superpowers/specs/2026-11-11-finance-recon-design.md §1)."""

import pytest

from backend import api
from tests.backend.test_api_calls import _client
from tests.backend.test_recon_match import Books


@pytest.fixture
def client(tmp_path):
    b = Books(tmp_path / "source.sqlite")
    b.pay("2026-11-03 10:00:00", 3_500, "card")  # 금액 오입력 한 건
    b.sale("2026-11-03 10:00:00", 35_000, "R1")
    b.settle("2026-11-03")
    b.app().close()
    yield _client(tmp_path)
    api.app.dependency_overrides.clear()


def _key(client) -> str:
    return client.get("/finance/recon").json()["days"][0]["items"][0]["key"]


def test_get_recon_shape(client):
    r = client.get("/finance/recon").json()
    assert set(r) == {"months", "month", "causes", "chips", "days"}
    assert r["month"] == "2026-11"
    assert r["chips"] == {"days": 1, "mismatch_days": 1, "open_items": 1, "open_all": 1}
    assert client.get("/finance/recon?month=2026-11").json() == r


def test_get_recon_bad_month_400(client):
    assert client.get("/finance/recon?month=2025-01").status_code == 400


def test_post_confirm_then_shown(client):
    key = _key(client)
    r = client.post(
        f"/finance/recon/{key}/confirm", json={"cause": "amount_typo", "part": "데스크"}
    )
    assert r.json() == {"ok": True}
    item = client.get("/finance/recon").json()["days"][0]["items"][0]
    assert item["confirmation"]["cause"] == "amount_typo"
    assert item["confirmation"]["by"] == "데스크"


@pytest.mark.parametrize("cause", ["nope", "unknown"])
def test_post_confirm_bad_400(client, cause):
    key = _key(client)
    assert (
        client.post(
            f"/finance/recon/{key}/confirm", json={"cause": cause, "part": "데스크"}
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/finance/recon/000000000000/confirm", json={"cause": "amount_typo", "part": "데스크"}
        ).status_code
        == 400
    )


# ── 3단계 조사 에이전트·자동 마감(설계 §3) ─────────────────────────────


@pytest.fixture
def agent_client(client):
    from tests.backend.test_recon_agent import LOOK, Script, submit

    def make(*steps):
        api.app.dependency_overrides[api.get_recon_gateway] = lambda: (
            lambda conn: Script(conn, *steps)
        )
        return client

    make.look, make.submit = LOOK, submit
    return make


def _events(text: str) -> list[str]:
    return [line[7:] for line in text.splitlines() if line.startswith("event: ")]


def test_item_has_agent_after_investigate(agent_client):
    c = agent_client(("say", "가설: 금액 오입력", *agent_client.look[1:]), agent_client.submit())
    key = _key(c)
    r = c.post(f"/finance/recon/{key}/investigate")
    assert r.status_code == 200
    assert _events(r.text) == ["line", "line", "line", "done"]
    item = c.get("/finance/recon").json()["days"][0]["items"][0]
    assert item["agent"]["cause"] == "amount_typo"
    assert [x["kind"] for x in item["agent"]["lines"]] == ["hypothesis", "lookup", "conclusion"]
    assert item["auto"] is None


def test_item_without_agent(client):
    item = client.get("/finance/recon").json()["days"][0]["items"][0]
    assert item["agent"] is None


def test_investigate_twice_409(agent_client):
    c = agent_client(agent_client.look, agent_client.submit())
    key = _key(c)
    c.post(f"/finance/recon/{key}/investigate")
    assert c.post(f"/finance/recon/{key}/investigate").status_code == 409


def test_investigate_unknown_key_400(agent_client):
    c = agent_client(agent_client.look)
    assert c.post("/finance/recon/000000000000/investigate").status_code == 400


def test_confirm_records_agent_proposal(agent_client, tmp_path):
    c = agent_client(agent_client.look, agent_client.submit(cause="entry_missing"))
    key = _key(c)
    c.post(f"/finance/recon/{key}/investigate")
    c.post(f"/finance/recon/{key}/confirm", json={"cause": "entry_missing", "part": "데스크"})
    row = _app_row(tmp_path, key)
    assert (row["proposed_by"], row["proposed_cause"]) == ("agent", "entry_missing")


def test_confirm_agent_unknown_needs_cause(agent_client, tmp_path):
    c = agent_client(agent_client.look, agent_client.submit(cause="unknown", evidence=[]))
    key = _key(c)
    c.post(f"/finance/recon/{key}/investigate")
    c.post(f"/finance/recon/{key}/confirm", json={"cause": "amount_typo", "part": "데스크"})
    row = _app_row(tmp_path, key)
    assert (row["proposed_cause"], row["cause"]) == ("unknown", "amount_typo")


def test_confirm_without_agent_is_rule(client, tmp_path):
    key = _key(client)
    client.post(f"/finance/recon/{key}/confirm", json={"cause": "amount_typo", "part": "데스크"})
    assert _app_row(tmp_path, key)["proposed_by"] == "rule"


def _app_row(tmp_path, key):
    from backend import db

    conn = db.connect(tmp_path / "app.sqlite", tmp_path / "source.sqlite")
    row = dict(
        conn.execute("SELECT * FROM ax_recon_confirmations WHERE item_key = ?", (key,)).fetchone()
    )
    conn.close()
    return row
