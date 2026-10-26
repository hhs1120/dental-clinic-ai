"""1단계 마스터 — emr_clinic·emr_staff·emr_procedures·emr_alert_types·emr_diagnoses (가상 데이터 생성 설계 §1·§2.1). 값은 generator/master.py."""
import json

from generator.master import ALERT_TYPES, CLINIC_NAME, DIAGNOSES, HOURS, PROCEDURES, STAFF


def stage(run, rng, fake) -> None:
    run.conn.execute("INSERT INTO emr_clinic VALUES (?, ?)", (CLINIC_NAME, json.dumps(HOURS, ensure_ascii=False)))
    run.conn.executemany("INSERT INTO emr_staff (name, role) VALUES (?, ?)",
                         [(fake.name(), role) for role, n in STAFF for _ in range(n)])
    cols = list(PROCEDURES[0])
    run.conn.executemany(f"INSERT INTO emr_procedures ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                         [tuple(p[c] for c in cols) for p in PROCEDURES])
    run.conn.executemany("INSERT INTO emr_alert_types VALUES (?, ?, ?)",
                         [(code, a["label"], a["category"]) for code, a in ALERT_TYPES.items()])
    run.conn.executemany("INSERT INTO emr_diagnoses VALUES (?, ?)", sorted(DIAGNOSES.items()))
