"""backend가 읽는 master 값 테이블 ax_master_values(④ 준비표 설계 "기준표 적재") — 생성 값과 verify 검사."""

import json
from datetime import date

from generator.clinic import master_values_rows
from generator.doc_catalog import CLAIM_HEADINGS
from generator.master import (
    ACCOUNTS,
    ADJUST_REASONS,
    AGE_GROUP_RANGE,
    ALERT_TYPES,
    APPEAL_DAYS,
    CARD_ISSUERS,
    CASH_DEPOSIT_MEMO,
    CASH_DEPOSIT_WEEKDAYS,
    CHAIRS,
    CHART_REQUIREMENT,
    CONDITIONAL_INSURED_MIN_AGE,
    DAYS_PER_MONTH,
    DOCTOR_CONCURRENCY,
    EXPENSE_ITEM_ACCOUNT_KINDS,
    FULL_MOUTH,
    NHIS_MEMO,
    ORAL_CARE_PRICES,
    PROCEDURE_DIAGNOSES,
    RECON_CAUSE_GROUPS,
    RECON_MISMATCH_COUNTS,
    RETREATMENT_REQUIREMENT,
    SCALING_CODE,
    SCALING_MIN_AGE,
    VENDORS,
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
    # ⑧-1 재무 대조(설계 2026-11-11 재무 대조 §1)
    assert got["recon_causes"] == RECON_CAUSE_GROUPS
    assert set(RECON_CAUSE_GROUPS) == set(RECON_MISMATCH_COUNTS)
    assert set(RECON_CAUSE_GROUPS.values()) == {"rule", "ambiguous"}
    assert got["card_issuers"] == {
        k: {"fee_rate": v["fee_rate"], "settle_days": v["settle_days"]}
        for k, v in CARD_ISSUERS.items()
    }
    assert got["cash_deposit"] == {
        "weekdays": list(CASH_DEPOSIT_WEEKDAYS),
        "memo": CASH_DEPOSIT_MEMO,
    }
    assert got["nhis_memo"] == NHIS_MEMO
    assert got["oral_care_prices"] == ORAL_CARE_PRICES
    # ⑧-2 지출 증빙(설계 2026-11-11 재무 대조 §2)
    assert got["expense_vendors"] == [
        {
            "name": v["name"],
            "card_name": v["card_name"],
            "pay": v["pay"],
            "account": v["account"],
            "by_item": v["kind"] in EXPENSE_ITEM_ACCOUNT_KINDS,
        }
        for v in VENDORS
    ]
    assert got["expense_accounts"] == list(ACCOUNTS)
    assert [v["name"] for v in got["expense_vendors"] if v["by_item"]] == ["모아덴탈몰"]
    # ⑤ 급여 나이·이미 backend에 박혀 있던 값
    assert got["age_group_range"] == {k: list(v) for k, v in AGE_GROUP_RANGE.items()}
    assert got["conditional_insured_min_age"] == CONDITIONAL_INSURED_MIN_AGE
    assert got["days_per_month"] == DAYS_PER_MONTH == 365.25 / 12
    assert got["full_mouth"] == FULL_MOUTH


def test_bank_holidays_from_argument():
    got = json.loads(dict(master_values_rows(frozenset({date(2026, 10, 3)})))["bank_holidays"])
    assert got == ["2026-10-03"]
    assert json.loads(dict(master_values_rows())["bank_holidays"]) == []


def test_no_generation_meta():
    got = {name: json.loads(value) for name, value in master_values_rows()}
    assert not any("link_failure" in n or "mismatch_counts" in n for n in got)
    assert all("weight" not in v for v in got["card_issuers"].values())
    assert all(
        set(v) == {"name", "card_name", "pay", "account", "by_item"}
        for v in got["expense_vendors"]
    )


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
    assert len(check_master_values(conn, CTX)) == len(master_values_rows())


def test_master_values_has_capacity():
    """AI 비서 슬롯 검사 기준(체어 수·원장 동시 건수)이 적재된다"""
    got = dict(master_values_rows())
    assert json.loads(got["chairs"]) == CHAIRS
    assert json.loads(got["doctor_concurrency"]) == DOCTOR_CONCURRENCY
