"""⑧-2 지출 증빙 — 규칙 기준선(backend/expense.py)·화면 집계·사람 확정(재무 대조 설계 §2)."""

import json

from backend import expense
from generator.clinic import master_values_rows
from generator.master import VENDORS


def test_rule_from_master_values_equals_master():
    """앱(ax_master_values 거래처 표)과 평가(master.VENDORS)가 같은 거래처 사전을 만든다 — 규칙 = 채점한 기준선"""
    mv = {n: json.loads(v) for n, v in master_values_rows()}
    a, b = expense.vendors_by_label(mv["expense_vendors"]), expense.vendors_by_label(VENDORS)
    assert set(a) == set(b)
    assert all(
        {k: a[x][k] for k in ("name", "pay", "account")}
        == {k: b[x][k] for k in ("name", "pay", "account")}
        for x in a
    )
