"""⑤ 청구 점검 API (backend/api.py GET /claims·GET /claims/pre·POST /claims/{claim_id}/judgment, 설계
docs/superpowers/specs/2026-11-10-claim-review-design.md §1)."""

import pytest

from backend import api, claims
from tests.backend.test_api_calls import _client
from tests.backend.test_claims import NOTE, add_claim, reviewed_for
from tests.backend.test_missed import Source


@pytest.fixture
def ids(tmp_path):
    """요건 조정(기한 20일)·상병 착오(기한 20일)·기한 지남 서류 조정·대기 줄(허용 상병 밖)"""
    s = Source(tmp_path / "source.sqlite")
    got = {
        "req": add_claim(s, 1, note=NOTE["requirement"][1], reviewed=reviewed_for(20)),
        "dx": add_claim(s, 2, note=NOTE["diagnosis"][1], reviewed=reviewed_for(20)),
        "old": add_claim(s, 3, note=NOTE["documentation"][1], reviewed=reviewed_for(-3)),
        "pre": add_claim(s, 4, day="2026-11-12", status="pending", dx="D07"),
    }
    s.app().close()
    return got


@pytest.fixture
def client(tmp_path, ids):
    yield _client(tmp_path)
    api.app.dependency_overrides.clear()


def test_get_claims_shape(client):
    r = client.get("/claims").json()
    assert set(r) == {"date", "appeal_days", "soon_days", "watch_days", "chips", "rows"}
    assert r["date"] == "2026-12-01"
    assert set(r["chips"]) == set(claims.CHIPS)
    assert len(r["rows"]) == 3


def test_get_pre_shape(client):
    r = client.get("/claims/pre").json()
    assert set(r) == {"counts", "ai_at", "ai_pending", "rows"}
    assert r["counts"] == {"lines": 1, "warning": 1, "ai": 0, "ai_missing": 0, "ai_checked": 0}
    assert r["rows"][0]["ai"] is None


def test_post_judgment_then_shown(client, ids):
    res = client.post(
        f"/claims/{ids['req']}/judgment", json={"judgment": "contestable", "part": "데스크"}
    )
    assert res.status_code == 200
    assert res.json() == {"ok": True}
    row = next(x for x in client.get("/claims").json()["rows"] if x["claim_id"] == ids["req"])
    assert row["judgment"] == {"value": "contestable", "by": "데스크", "at": "2026-11-05 14:30:00"}


def test_post_judgment_bad_400(client, ids):
    for cid, value in (
        (ids["req"], "maybe"),
        (ids["dx"], "contestable"),
        (ids["old"], "resubmit"),
    ):
        res = client.post(f"/claims/{cid}/judgment", json={"judgment": value, "part": "데스크"})
        assert res.status_code == 400
    assert all(x["judgment"] is None for x in client.get("/claims").json()["rows"])
