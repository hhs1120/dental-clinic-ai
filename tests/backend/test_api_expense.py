"""⑧-2 지출 증빙 API (backend/api.py GET /finance/expenses·후보·확정, 설계
docs/superpowers/specs/2026-11-11-finance-recon-design.md §2)."""

import pytest

from backend import api
from tests.backend.test_api_calls import _client
from tests.backend.test_expense import Outflows

T = "fin_card_expenses"


@pytest.fixture
def client(tmp_path):
    o = Outflows(tmp_path / "source.sqlite")
    o.card("2026-11-05 12:00:00", "솔밭식당", 50_000)  # 영수증 없음
    o.doc("receipt", "솔밭식당", "2026-11-04", 48_000)  # 금액이 달라 규칙은 못 찾음 — 후보
    o.doc("receipt", "솔밭식당", "2026-08-01", 50_000)  # 창 밖
    o.app().close()
    yield _client(tmp_path)
    api.app.dependency_overrides.clear()


def _row(client) -> dict:
    return client.get("/finance/expenses").json()["rows"][0]


def test_get_expenses_shape(client):
    r = client.get("/finance/expenses").json()
    assert set(r) == {"months", "month", "accounts", "chips", "rows"}
    assert r["month"] == "2026-11"
    assert r["chips"]["none"] == 1
    assert client.get("/finance/expenses?month=2026-11").json() == r


def test_get_expenses_bad_month_400(client):
    assert client.get("/finance/expenses?month=2025-01").status_code == 400


def test_candidates(client):
    row = _row(client)
    r = client.get(f"/finance/expenses/{T}/{row['id']}/candidates").json()
    assert [d["amount"] for d in r["docs"]] == [48_000]
    assert client.get(f"/finance/expenses/{T}/999/candidates").status_code == 400
    assert client.get(f"/finance/expenses/nope/{row['id']}/candidates").status_code == 400


def test_post_confirm_then_shown(client):
    row = _row(client)
    doc = client.get(f"/finance/expenses/{T}/{row['id']}/candidates").json()["docs"][0]
    r = client.post(
        f"/finance/expenses/{T}/{row['id']}/confirm",
        json={"document_ids": [doc["id"]], "account": "복리후생비", "part": "데스크"},
    )
    assert r.json() == {"ok": True}
    row = _row(client)
    assert row["confirmed"]["by"] == "데스크"
    assert (row["account"], row["account_from"]) == ("복리후생비", "confirmed")
    assert ([d["id"] for d in row["docs"]], row["diff"]) == ([doc["id"]], 2_000)


@pytest.mark.parametrize(
    "body",
    [
        {"document_ids": [], "account": "간식비", "part": "데스크"},  # 모르는 과목
        {"document_ids": [2], "account": "복리후생비", "part": "데스크"},  # 후보 밖 증빙(창 밖)
    ],
)
def test_post_confirm_bad_400(client, body):
    row = _row(client)
    assert client.post(f"/finance/expenses/{T}/{row['id']}/confirm", json=body).status_code == 400
    ok = {"document_ids": [], "account": "복리후생비", "part": "데스크"}
    assert client.post(f"/finance/expenses/{T}/999/confirm", json=ok).status_code == 400


def test_today_has_expense_line(client):
    people = client.get("/today").json()["people"]
    assert (people["expense_open"], people["expense_month"]) == (1, "2026-11")
