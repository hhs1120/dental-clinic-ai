"""1단계 마스터 — emr_clinic·emr_staff·emr_procedures·emr_alert_types·emr_diagnoses (가상 데이터 생성 설계 §1·§2.1),
마지막 단계 ax_master_values(backend가 읽는 master 값 — §0). 값은 generator/master.py."""

import json

from generator.master import (
    ALERT_TYPES,
    CLINIC_NAME,
    DIAGNOSES,
    HOURS,
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
    """(이름, JSON) — ④ 주의 칩 기준표(관련 시술·행동)와 보험 잔여 칩 기준. verify도 이것과 비교한다"""
    values = {
        "alert_rules": {
            code: {"action": a["action"], "procedures": sorted(a["procedures"])}
            for code, a in ALERT_TYPES.items()
        },
        "scaling_min_age": SCALING_MIN_AGE,
        "scaling_code": SCALING_CODE,
    }
    return [(name, json.dumps(v, ensure_ascii=False)) for name, v in values.items()]


def stage_master_values(run, rng, fake) -> None:
    run.conn.executemany("INSERT INTO ax_master_values VALUES (?, ?)", master_values_rows())
