"""4b단계 주의사항 — emr_patient_alerts (가상 데이터 생성 설계 §2.2). 정의는 master.ALERT_TYPES.
4단계 예약 뒤, 자기 시드로 만든다(예약·진료에 영향 없음).
- 문진(intake): 환자마다 코드별로 기록일 나이·성별에 맞는 규칙의 비율로 추첨. 기록일 = 신환은 등록일,
  구환은 창 이전 마지막 completed 내원일(그때 문진을 갱신한 것으로 봄 — 대상 나이가 지금과 거의 같고 창 안 내내 보이게)
- 진료 중 발견(visit): 관련 시술을 창 안에서 받은(completed) 환자 중 비율로 추첨, 그 내원 중 하나에 기록(기록일 = 내원일)
- 환자당 MAX_ALERTS_PER_PATIENT개까지 — 문진이 넘치면 그중 균등하게 남기고, 진료 중 발견은 자리가 있을 때만
"""
from collections import defaultdict
from datetime import date

from generator.master import ALERT_TYPES, MAX_ALERTS_PER_PATIENT, age_on

INTAKE = sorted(c for c, a in ALERT_TYPES.items() if a["source"] == "intake")
VISIT = sorted(c for c, a in ALERT_TYPES.items() if a["source"] == "visit")


def intake_rate(code: str, age: int, sex: str) -> float:
    return next((rate for lo, hi, s, rate in ALERT_TYPES[code]["rules"]
                 if lo <= age and (hi is None or age <= hi) and s in (None, sex)), 0.0)


def stage(run, rng, fake) -> None:
    conn = run.conn
    start = run.range_start.isoformat()
    pre = dict(conn.execute("SELECT patient_id, max(date(scheduled_at)) FROM emr_appointments"
                            " WHERE status = 'completed' AND date(scheduled_at) < ? GROUP BY patient_id", (start,)))
    visits = defaultdict(list)  # 환자 → 창 안 completed (예약 id, 날짜, 시술)
    for appt_id, pid, day, code in conn.execute(
            "SELECT id, patient_id, date(scheduled_at), planned_procedure_code FROM emr_appointments"
            " WHERE status = 'completed' AND date(scheduled_at) >= ? ORDER BY scheduled_at, id", (start,)):
        visits[pid].append((appt_id, day, code))
    rows = []
    for pid, birth, sex, registered in conn.execute(
            "SELECT id, birth_date, sex, registered_at FROM emr_patients ORDER BY id").fetchall():
        day = registered if registered >= start else pre.get(pid, registered)
        age = age_on(date.fromisoformat(birth), date.fromisoformat(day))
        codes = [c for c in INTAKE if rng.random() < intake_rate(c, age, sex)]
        if len(codes) > MAX_ALERTS_PER_PATIENT:
            codes = sorted(rng.choice(codes, MAX_ALERTS_PER_PATIENT, replace=False).tolist())
        alerts = [(pid, c, "intake", day, None) for c in codes]
        for c in VISIT:
            related = [v for v in visits[pid] if v[2] in ALERT_TYPES[c]["procedures"]]
            if not related or rng.random() >= ALERT_TYPES[c]["rate"] or len(alerts) >= MAX_ALERTS_PER_PATIENT:
                continue
            appt_id, visit_day, _ = related[int(rng.integers(len(related)))]
            alerts.append((pid, c, "visit", visit_day, appt_id))
        rows += alerts
    conn.executemany("INSERT INTO emr_patient_alerts (patient_id, code, source, recorded_at, appointment_id)"
                     " VALUES (?, ?, ?, ?, ?)", rows)
