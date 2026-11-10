"""1단계 마스터 — emr_clinic·emr_staff·emr_procedures·emr_alert_types·emr_diagnoses (가상 데이터 생성 설계 §1·§2.1),
마지막 단계 ax_master_values(backend가 읽는 master 값 — §0). 값은 generator/master.py."""

import json

from generator.doc_catalog import CLAIM_HEADINGS
from generator.master import (
    ADJUST_REASONS,
    ALERT_TYPES,
    APPEAL_DAYS,
    CLINIC_NAME,
    DIAGNOSES,
    HOURS,
    PROCEDURE_DIAGNOSES,
    PROCEDURES,
    SCALING_CODE,
    SCALING_MIN_AGE,
    STAFF,
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


def master_values_rows() -> list[tuple[str, str]]:
    """(이름, JSON) — ④ 주의 칩 기준표(관련 시술·행동)와 보험 잔여 칩 기준, ⑨ 외부 이력 사유,
    ⑤ 재심 기한·허용 상병·사유 유형·기준 절·요건 사유. verify도 이것과 비교한다"""
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
    }
    return [(name, json.dumps(v, ensure_ascii=False)) for name, v in values.items()]


def stage_master_values(run, rng, fake) -> None:
    run.conn.executemany("INSERT INTO ax_master_values VALUES (?, ?)", master_values_rows())
