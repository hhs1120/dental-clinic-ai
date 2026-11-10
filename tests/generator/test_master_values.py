"""backend가 읽는 master 값 테이블 ax_master_values(④ 준비표 설계 "기준표 적재") — 생성 값과 verify 검사."""

import json

from generator.clinic import master_values_rows
from generator.doc_catalog import CLAIM_HEADINGS
from generator.master import (
    ADJUST_REASONS,
    ALERT_TYPES,
    APPEAL_DAYS,
    CHART_REQUIREMENT,
    PROCEDURE_DIAGNOSES,
    RETREATMENT_REQUIREMENT,
    SCALING_CODE,
    SCALING_MIN_AGE,
)
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
    # ⑤ 청구 점검(설계 2026-11-10 청구 점검 §1)
    assert got["appeal_days"] == APPEAL_DAYS
    assert got["procedure_diagnoses"] == {p: sorted(dx) for p, dx in PROCEDURE_DIAGNOSES.items()}
    assert got["adjust_note_types"] == {r["note"]: r["type"] for r in ADJUST_REASONS.values()}
    assert len(got["adjust_note_types"]) == len(ADJUST_REASONS)  # 문구가 겹치지 않음
    assert got["adjust_note_headings"] == {
        r["note"]: CLAIM_HEADINGS[k] for k, r in ADJUST_REASONS.items()
    }


def test_requirement_notes_cover_chart_requirement():
    got = json.loads(dict(master_values_rows())["requirement_notes"])
    for code, key in CHART_REQUIREMENT.items():
        assert ADJUST_REASONS[key]["note"] in got[code]
    retreat = ADJUST_REASONS[RETREATMENT_REQUIREMENT]
    assert len(retreat["procedures"]) == 2  # 아말감/GI·신경치료
    for code in retreat["procedures"]:
        assert retreat["note"] in got[code]
    assert SCALING_CODE not in got  # 요건 사유가 없는 시술은 키 없음


def test_check_master_values_passes_on_loaded(tmp_path):
    assert check_master_values(make_db(tmp_path), CTX) == []


def test_check_master_values_flags_missing_and_wrong(tmp_path):
    conn = make_db(tmp_path)
    conn.execute("UPDATE ax_master_values SET value = '20' WHERE name = 'scaling_min_age'")
    assert [r["name"] for r in check_master_values(conn, CTX)] == ["scaling_min_age"]
    conn.execute("DELETE FROM ax_master_values")
    assert len(check_master_values(conn, CTX)) == 9
