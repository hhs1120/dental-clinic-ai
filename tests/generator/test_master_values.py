"""backend가 읽는 master 값 테이블 ax_master_values(④ 준비표 설계 "기준표 적재") — 생성 값과 verify 검사."""

import json

from generator.clinic import master_values_rows
from generator.master import ADJUST_REASONS, ALERT_TYPES, SCALING_CODE, SCALING_MIN_AGE
from generator.verify.common import check_master_values
from tests.generator.verify_db import CTX, make_db


def test_master_values_rows_match_master():
    got = {name: json.loads(value) for name, value in master_values_rows()}
    assert got["scaling_min_age"] == SCALING_MIN_AGE
    assert got["scaling_code"] == SCALING_CODE
    assert got["alert_rules"] == {
        code: {"action": a["action"], "procedures": sorted(a["procedures"])}
        for code, a in ALERT_TYPES.items()
    }
    assert got["external_adjust_notes"] == sorted(
        r["note"] for r in ADJUST_REASONS.values() if r["type"] == "external"
    )


def test_check_master_values_passes_on_loaded(tmp_path):
    assert check_master_values(make_db(tmp_path), CTX) == []


def test_check_master_values_flags_missing_and_wrong(tmp_path):
    conn = make_db(tmp_path)
    conn.execute("UPDATE ax_master_values SET value = '20' WHERE name = 'scaling_min_age'")
    assert [r["name"] for r in check_master_values(conn, CTX)] == ["scaling_min_age"]
    conn.execute("DELETE FROM ax_master_values")
    assert len(check_master_values(conn, CTX)) == 4
