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
    assert r["chips"] == {"days": 1, "mismatch_days": 1, "open_items": 1}
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
