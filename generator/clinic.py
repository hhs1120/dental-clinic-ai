"""1단계 마스터 — emr_clinic·emr_staff·emr_procedures·emr_alert_types·emr_diagnoses (가상 데이터 생성 설계 §1·§2.1),
마지막 단계 ax_master_values(backend가 읽는 master 값 — §0). 값은 generator/master.py."""

import json

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
    CLINIC_NAME,
    CONDITIONAL_INSURED_MIN_AGE,
    DAYS_PER_MONTH,
    DIAGNOSES,
    DOCTOR_CONCURRENCY,
    EXPENSE_ITEM_ACCOUNT_KINDS,
    FULL_MOUTH,
    HOURS,
    NHIS_MEMO,
    ORAL_CARE_PRICES,
    PROCEDURE_DIAGNOSES,
    PROCEDURES,
    RECON_CAUSE_GROUPS,
    SCALING_CODE,
    SCALING_MIN_AGE,
    STAFF,
    VENDORS,
)


def stage(run, rng, fake) -> None:
    run.conn.execute(
        "INSERT INTO emr_clinic VALUES (?, ?)",
        (CLINIC_NAME, json.dumps(HOURS, ensure_ascii=False)),
    )
    run.conn.executemany(
        "INSERT INTO emr_staff (name, role) VALUES (?, ?)",
        [(name, role) for role, names in STAFF for name in names],
    )
    cols = list(PROCEDURES[0])
    run.conn.executemany(
        f"INSERT INTO emr_procedures ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        [tuple(p[c] for c in cols) for p in PROCEDURES],
    )
    run.conn.executemany(
        "INSERT INTO emr_alert_types VALUES (?, ?, ?)",
        [(code, a["label"], a["category"]) for code, a in ALERT_TYPES.items()],
    )
    run.conn.executemany("INSERT INTO emr_diagnoses VALUES (?, ?)", sorted(DIAGNOSES.items()))


def master_values_rows(holidays: frozenset = frozenset()) -> list[tuple[str, str]]:
    """(이름, JSON) — ④ 주의 칩 기준표(관련 시술·행동)와 보험 잔여 칩 기준, ⑨ 외부 이력 사유,
    ⑤ 재심 기한·허용 상병·사유 유형·기준 절·요건 사유, ⑧-1 원인 묶음·정산·현금 입금·구강용품·은행 공휴일
    (holidays = config 공휴일), ⑧-2 거래처 표·계정 과목, 급여 나이·월 일수·치아 단위 아닌 번호. verify도 이것과 비교한다"""
    requirement_notes: dict[str, list[str]] = {}
    for r in ADJUST_REASONS.values():
        if r["type"] == "requirement":
            for code in r["procedures"]:
                requirement_notes.setdefault(code, []).append(r["note"])
    values = {
        "alert_rules": {
            code: {"action": a["action"], "procedures": sorted(a["procedures"])}
            for code, a in ALERT_TYPES.items()
        },
        "scaling_min_age": SCALING_MIN_AGE,
        "scaling_code": SCALING_CODE,
        # ⑨ 다른 치과 추정 칩 — 외부 이력 사유(병행 환자의 거절·조정 청구, §2.2)
        "external_adjust_notes": sorted(
            r["note"] for r in ADJUST_REASONS.values() if r["type"] == "external"
        ),
        # ⑤ 재심 신청 기한 = 심사결과 통보일 + 이 일수(가상 데이터 생성 설계 §2.4)
        "appeal_days": APPEAL_DAYS,
        # ⑤ 시술별 허용 상병 — 청구 전 상병 확인 경고(§4.3)
        "procedure_diagnoses": {p: sorted(dx) for p, dx in PROCEDURE_DIAGNOSES.items()},
        # ⑤ 상세 사유 문구 → 유형(requirement·documentation·diagnosis·external) — 규칙 판단 제안(§2.4)
        "adjust_note_types": {r["note"]: r["type"] for r in ADJUST_REASONS.values()},
        # ⑤ 상세 사유 문구 → 청구 기준 절 소제목(§4.3) — 보완 후 재청구의 채울 항목, 2·3단계 입력
        "adjust_note_headings": {r["note"]: CLAIM_HEADINGS[k] for k, r in ADJUST_REASONS.items()},
        # ⑤ 시술 → 요건 사유 문구(조건부 요건 포함, §4.3) — 2단계 요건 점검 대상
        "requirement_notes": {p: sorted(n) for p, n in sorted(requirement_notes.items())},
        # ⑧-1 원인 → 묶음(rule·ambiguous) — 규칙 기준선·채점(§2.7, 평가 설계 §5)
        "recon_causes": RECON_CAUSE_GROUPS,
        # ⑧-1 카드사 수수료율·정산 지연 은행 영업일(§2.7) — 생성 비중(weight)은 병원이 모르므로 뺀다
        "card_issuers": {
            k: {"fee_rate": v["fee_rate"], "settle_days": v["settle_days"]}
            for k, v in CARD_ISSUERS.items()
        },
        # ⑧-1 현금 입금 요일(월=0)·통장 적요(§2.7)
        "cash_deposit": {"weekdays": list(CASH_DEPOSIT_WEEKDAYS), "memo": CASH_DEPOSIT_MEMO},
        # ⑧-1 공단 급여비 입금 적요 — 대조 대상 밖(§2.7)
        "nhis_memo": NHIS_MEMO,
        # ⑧-1 구강용품 가격표 — 진료 외 매출 규칙(§2.7)
        "oral_care_prices": ORAL_CARE_PRICES,
        # ⑧-1 은행 공휴일 — 정산 예상일·현금 입금일·대조 경계(§2.7)
        "bank_holidays": sorted(d.isoformat() for d in holidays),
        # ⑧-2 거래처 표(설계 2026-11-11 재무 대조 §2) — 적요·가맹점명(card_name)으로 거래처를 가려 기본 계정 과목,
        # by_item = 품목으로 과목이 정해지는 거래처(과목 확인 필요). 건수·금액 범위·증빙 종류는 생성 값이라 뺀다
        "expense_vendors": [
            {
                "name": v["name"],
                "card_name": v["card_name"],
                "pay": v["pay"],
                "account": v["account"],
                "by_item": v["kind"] in EXPENSE_ITEM_ACCOUNT_KINDS,
            }
            for v in VENDORS
        ],
        # ⑧-2 계정 과목 목록 — 사람 확정에 고를 수 있는 값
        "expense_accounts": list(ACCOUNTS),
        # ⑤ 적용 연령별 급여 나이 범위(§2.1, None = 상한 없음)·조건부 급여 나이
        "age_group_range": {k: list(v) for k, v in AGE_GROUP_RANGE.items()},
        "conditional_insured_min_age": CONDITIONAL_INSURED_MIN_AGE,
        # ③·④ 리콜 기한 개월 → 일(§2.6 (1))
        "days_per_month": DAYS_PER_MONTH,
        # ④ 치아 단위가 아닌 시술의 치아 번호(§2.3)
        "full_mouth": FULL_MOUTH,
        # AI 비서 슬롯 검사 — 체어 수·원장 동시 건수(AI 비서 설계 §5.2)
        "chairs": CHAIRS,
        "doctor_concurrency": DOCTOR_CONCURRENCY,
    }
    return [(name, json.dumps(v, ensure_ascii=False)) for name, v in values.items()]


def stage_master_values(run, rng, fake) -> None:
    run.conn.executemany(
        "INSERT INTO ax_master_values VALUES (?, ?)", master_values_rows(run.holidays)
    )
