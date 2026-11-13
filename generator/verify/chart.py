"""주의사항·차트·청구 오류(5b단계) 검사, 디코이 비율 경고, 주의사항 보유·판정 분포·청구 오류 리포트
(가상 데이터 생성 설계 §2.2·§2.4·§4.3·§5.3 주의사항·차트)"""

import json
import re
from collections import Counter, defaultdict
from datetime import date

from generator import chart, llm_jobs
from generator.master import (
    ADJUST_REASONS,
    ALERT_TYPES,
    CHART_FORBIDDEN_WORDS,
    CHART_REQUIREMENT,
    EXTRACTION_CODES,
    FULL_MOUTH,
    MAX_ALERTS_PER_PATIENT,
    PERIO_LINK_DAYS,
    PREGNANCY_LEFT_DAYS,
    PROCEDURE_DIAGNOSES,
    RECALL_CYCLE_MONTHS,
    RETREATMENT_REQUIREMENT,
    SAME_DAY_EXTRA,
    SCALING_CODE,
    TOOTH_CHOICES,
    add_years,
    age_band,
    age_on,
    insured_on,
)
from generator.verify.common import CHILD_MAX_AGE, PROCEDURE, REASON_BY_TEXT, _dt, _end, _rows
from generator.verify.records import _open_items_at, _plan_items, _submitted_months

CHART_DECOY_RANGE = (1.0, 2.0)  # 디코이 / 특이 케이스 (§4.3 — 목표는 master.CHART_DECOY_RATIO)


# ── 주의사항 ──────────────────────────────────────────────────


def _alerts(conn) -> list[dict]:
    return _rows(
        conn,
        "SELECT al.*, p.birth_date, p.sex, p.registered_at FROM emr_patient_alerts al"
        " JOIN emr_patients p ON p.id = al.patient_id ORDER BY al.id",
    )


def check_alert_target(conn, ctx):
    """문진 주의사항은 master.ALERT_TYPES 대상 규칙(기록일 나이·성별) 중 하나에 맞음"""
    bad = []
    for r in _alerts(conn):
        rules = ALERT_TYPES.get(r["code"], {}).get("rules", [])
        age = age_on(date.fromisoformat(r["birth_date"]), date.fromisoformat(r["recorded_at"]))
        if r["source"] == "intake" and not any(
            lo <= age and (hi is None or age <= hi) and sex in (None, r["sex"])
            for lo, hi, sex, _ in rules
        ):
            bad.append({**r, "age": age})
    return bad


def check_alert_source(conn, ctx):
    """출처가 master 정의와 같음. intake는 내원 없음, visit은 같은 환자의 창 안 completed 내원 중
    관련 시술이고 기록일 = 그 내원일"""
    appts = {
        r["id"]: r
        for r in _rows(
            conn,
            "SELECT id, patient_id, scheduled_at, status, planned_procedure_code"
            " FROM emr_appointments",
        )
    }
    bad = []
    for r in _alerts(conn):
        a = ALERT_TYPES.get(r["code"])
        if a is None or a["source"] != r["source"]:
            bad.append({**r, "reason": "출처가 정의와 다름"})
        elif r["source"] == "intake" and r["appointment_id"] is not None:
            bad.append({**r, "reason": "문진에 내원 연결"})
        elif r["source"] == "visit":
            v = appts.get(r["appointment_id"])
            if (
                v is None
                or v["patient_id"] != r["patient_id"]
                or v["status"] != "completed"
                or _dt(v["scheduled_at"]).date() < ctx.range_start
                or v["planned_procedure_code"] not in a["procedures"]
                or _dt(v["scheduled_at"]).date().isoformat() != r["recorded_at"]
            ):
                bad.append({**r, "reason": "발견 내원이 맞지 않음", "appointment": v})
    return bad


def check_alert_recorded_at(conn, ctx):
    """기록일 ≥ 등록일. 문진 기록일 = 신환은 첫 completed 내원일(내원한 적 없으면 문진 없음),
    구환은 창 이전 마지막 completed 내원일(없으면 등록일)"""
    start = ctx.range_start.isoformat()
    pre = dict(
        conn.execute(
            "SELECT patient_id, max(date(scheduled_at)) FROM emr_appointments"
            " WHERE status = 'completed' AND date(scheduled_at) < ? GROUP BY patient_id",
            (start,),
        ).fetchall()
    )
    first = dict(
        conn.execute(
            "SELECT patient_id, min(date(scheduled_at)) FROM emr_appointments"
            " WHERE status = 'completed' GROUP BY patient_id"
        ).fetchall()
    )
    bad = []
    for r in _alerts(conn):
        expected = (
            first.get(r["patient_id"])
            if r["registered_at"] >= start
            else pre.get(r["patient_id"], r["registered_at"])
        )
        if r["recorded_at"] < r["registered_at"] or (
            r["source"] == "intake" and r["recorded_at"] != expected
        ):
            bad.append({**r, "expected": expected})
    return bad


def check_alert_ends(conn, ctx):
    """끝나는 날(ends_at): 임신은 기록일 + PREGNANCY_LEFT_DAYS 안, 협조 어려운 소아는 소아 구간을 벗어나는 생일,
    나머지는 없음"""
    lo, hi = PREGNANCY_LEFT_DAYS
    bad = []
    for r in _alerts(conn):
        if r["code"] == "PREGNANT":
            left = (
                r["ends_at"]
                and (date.fromisoformat(r["ends_at"]) - date.fromisoformat(r["recorded_at"])).days
            )
            ok = bool(left) and lo <= left <= hi
        elif r["code"] == "CHILD_COOP":
            ok = (
                r["ends_at"]
                == add_years(date.fromisoformat(r["birth_date"]), CHILD_MAX_AGE + 1).isoformat()
            )
        else:
            ok = r["ends_at"] is None
        if not ok:
            bad.append(r)
    return bad


def check_alerts_per_patient(conn, ctx):
    """환자당 주의사항 ≤ MAX_ALERTS_PER_PATIENT개"""
    return _rows(
        conn,
        "SELECT patient_id, count(*) AS n FROM emr_patient_alerts GROUP BY patient_id HAVING n > ?",
        MAX_ALERTS_PER_PATIENT,
    )


# ── 차트·청구 오류 (5b단계) ───────────────────────────────────────


def _month_bucket(ctx, month: str) -> str:
    """청구월 구간 — settled(확정) / submitted(제출 월) / pending(전월 이후)"""
    months = _submitted_months(ctx)
    return "pending" if month > max(months) else "submitted" if month >= min(months) else "settled"


def _chart_lines(conn, ctx) -> list[dict]:
    """진료마다 차트·차트 정답·청구·오류를 붙인 행. window = 창 안, cut = 확정 월 조정 사유(master 사유 dict)"""
    rows = _rows(
        conn,
        "SELECT t.id, t.appointment_id, t.patient_id, t.procedure_code, t.tooth_no, t.performed_at,"
        " n.dx_code, n.note, pt.name AS patient_name,"
        " tc.kind, tc.is_special, tc.requirement, tc.evidence_present, tc.is_decoy, tc.facts, co.course_id,"
        " c.id AS claim_id, c.claim_month, c.status, c.adjust_code, c.adjust_note,"
        " e.kind AS error, e.requirement AS error_requirement, e.extra_procedure_code, e.extra_tooth_no"
        " FROM emr_treatments t JOIN emr_patients pt ON pt.id = t.patient_id"
        " LEFT JOIN emr_chart_notes n ON n.treatment_id = t.id"
        " LEFT JOIN truth.truth_chart_notes tc ON tc.treatment_id = t.id"
        " LEFT JOIN truth.truth_appointment_courses co ON co.appointment_id = t.appointment_id"
        " LEFT JOIN emr_claims c ON c.treatment_id = t.id"
        " LEFT JOIN truth.truth_claim_errors e ON e.treatment_id = t.id"
        " ORDER BY t.patient_id, datetime(t.performed_at), t.id",
    )
    for r in rows:
        r["course"] = (
            r["course_id"] if r["course_id"] is not None else ("appt", r["appointment_id"])
        )  # 창 이전 내원
        r["day"] = _dt(r["performed_at"]).date()
        r["window"] = r["day"] >= ctx.range_start
        r["bucket"] = _month_bucket(ctx, r["claim_month"]) if r["claim_id"] else None
        r["cut"] = (
            REASON_BY_TEXT.get((r["adjust_code"], r["adjust_note"]))
            if r["bucket"] == "settled"
            else None
        )
    return rows


def _other_course(history: list, r) -> date | None:
    """같은 치아·같은 시술을 다른 코스에서 한 마지막 날"""
    days = [d for d, c in history if c != r["course"]]
    return max(days) if days else None


def _expected_requirement(rows: list[dict]) -> dict[int, str | None]:
    """진료 id → 요건 사유 키. 창 안 급여 줄만: 확정 월 요건 사유로 깎였으면 그 사유, 아니면 CHART_REQUIREMENT
    (잇몸치료는 직전 PERIO_LINK_DAYS일 안에 치석제거가 있으면 R02), 아말감/GI·신경치료는 같은 치아에 같은 시술을
    다른 코스에서 다시 할 때 R01 (같은 치아의 다른 시술 — 신경치료 뒤 충전 — 과 코스 안 반복은 이어지는 치료라 재시행이 아니다)"""
    expected, treated, scaled = {}, defaultdict(lambda: defaultdict(list)), defaultdict(list)
    retreat_codes = ADJUST_REASONS[RETREATMENT_REQUIREMENT]["procedures"]
    key_of = {id(r): k for k, r in ADJUST_REASONS.items()}
    for r in rows:  # 환자·시각순
        key = None
        if r["window"] and r["claim_id"]:
            if r["cut"] is not None and r["cut"]["type"] == "requirement":
                key = key_of[id(r["cut"])]
            elif r["procedure_code"] in CHART_REQUIREMENT:
                key = CHART_REQUIREMENT[r["procedure_code"]]
                if key == "R03" and any(
                    0 < (r["day"] - d).days <= PERIO_LINK_DAYS for d in scaled[r["patient_id"]]
                ):
                    key = "R02"
            elif r["procedure_code"] in retreat_codes and _other_course(
                treated[r["patient_id"]][(r["tooth_no"], r["procedure_code"])], r
            ):
                key = RETREATMENT_REQUIREMENT
        expected[r["id"]] = key
        if r["tooth_no"] != FULL_MOUTH:
            treated[r["patient_id"]][(r["tooth_no"], r["procedure_code"])].append(
                (r["day"], r["course"])
            )
        if r["procedure_code"] == SCALING_CODE:
            scaled[r["patient_id"]].append(r["day"])
    return expected


def check_chart_note_per_treatment(conn, ctx):
    """진료 1건당 emr_chart_notes·truth_chart_notes 정확히 1행 (emr 쪽은 UNIQUE, 남는 정답 행도 없음)"""
    return _rows(
        conn,
        "SELECT t.id, n.id AS note_id, tc.treatment_id AS truth_id FROM emr_treatments t"
        " LEFT JOIN emr_chart_notes n ON n.treatment_id = t.id"
        " LEFT JOIN truth.truth_chart_notes tc ON tc.treatment_id = t.id"
        " WHERE n.id IS NULL OR tc.treatment_id IS NULL",
    ) + _rows(
        conn,
        "SELECT tc.treatment_id FROM truth.truth_chart_notes tc"
        " LEFT JOIN emr_treatments t ON t.id = tc.treatment_id WHERE t.id IS NULL",
    )


def check_chart_dx(conn, ctx):
    """차트 상병이 master.PROCEDURE_DIAGNOSES 허용 밖 ⇔ 상병 착오 오류 줄"""
    return [
        {k: r[k] for k in ("id", "procedure_code", "dx_code", "error")}
        for r in _chart_lines(conn, ctx)
        if r["dx_code"] is not None
        and (r["dx_code"] not in PROCEDURE_DIAGNOSES[r["procedure_code"]])
        != (r["error"] == "diagnosis")
    ]


def check_claim_errors(conn, ctx):
    """truth_claim_errors는 창 안 급여 청구 줄에만. 임상 요건형 오류 ⇔ 요건 줄의 근거 없음(요건 키 일치).
    확정 월은 심사 결과에서: 상병 사유로 깎인 줄 = 상병 착오, 요건 사유로 깎인 줄만 요건 오류 가능, 그 밖은 오류 없음"""
    bad = []
    for r in _chart_lines(conn, ctx):
        why = None
        if r["error"] and not (r["window"] and r["claim_id"]):
            why = "창 안 급여 청구 줄이 아님"
        elif (r["error"] == "requirement") != (r["evidence_present"] == 0):
            why = "요건 오류 ⇔ 근거 없음이 아님"
        elif r["error"] == "requirement" and r["error_requirement"] != r["requirement"]:
            why = "오류 요건 ≠ 차트 요건"
        elif r["bucket"] == "settled" and r["window"]:
            cut_type = r["cut"]["type"] if r["cut"] else None
            if (cut_type == "diagnosis") != (r["error"] == "diagnosis"):
                why = "상병 사유 조정 ⇔ 상병 착오가 아님"
            elif r["error"] == "requirement" and cut_type != "requirement":
                why = "요건 사유로 깎이지 않은 확정 월 요건 오류"
        if why:
            bad.append(
                {
                    "id": r["id"],
                    "error": r["error"],
                    "status": r["status"],
                    "adjust_note": r["adjust_note"],
                    "reason": why,
                }
            )
    return bad


def check_omitted_extra(conn, ctx):
    """추가 처치 누락(omitted_extra)은 결과가 없는 달(제출 월·전월)의 창 안 급여 청구 줄에만. 추가 처치는
    master.SAME_DAY_EXTRA 시술·그 시술의 치아 후보·그날 나이에 급여, 치아는 그 환자 장부의 진료·치료계획 항목·다른 추가 처치에
    없는 치아.
    차트 사실 extra = 그 처치(시술 이름·치아), 다른 줄은 extra 없음 (§4.3)"""
    birth = dict(conn.execute("SELECT id, birth_date FROM emr_patients"))
    teeth = defaultdict(set)
    for pid, tooth in conn.execute(
        "SELECT patient_id, tooth_no FROM emr_treatments UNION ALL SELECT p.patient_id, i.tooth_no"
        " FROM emr_treatment_plan_items i JOIN emr_treatment_plans p ON p.id = i.plan_id"
    ):
        teeth[pid].add(tooth)
    extra_teeth = Counter(
        (pid, tooth)
        for pid, tooth in conn.execute(
            "SELECT t.patient_id, e.extra_tooth_no FROM truth.truth_claim_errors e"
            " JOIN emr_treatments t ON t.id = e.treatment_id WHERE e.kind = 'omitted_extra'"
        )
    )
    bad = []
    for r in _chart_lines(conn, ctx):
        extra = (json.loads(r["facts"]) if r["facts"] else {}).get("extra")
        if r["error"] != "omitted_extra":
            if extra is not None:
                bad.append({"id": r["id"], "problem": "추가 처치 누락이 아닌데 차트 사실에 extra"})
            continue
        code, tooth = r["extra_procedure_code"], r["extra_tooth_no"]
        age = age_on(date.fromisoformat(birth[r["patient_id"]]), r["day"])
        why = (
            "결과가 없는 달의 창 안 급여 청구 줄이 아님"
            if not (r["window"] and r["claim_id"] and r["bucket"] in ("submitted", "pending"))
            else "추가 처치 시술·치아 후보·급여 조건 밖"
            if code not in SAME_DAY_EXTRA
            or tooth not in TOOTH_CHOICES[code]
            or not insured_on(PROCEDURE[code], age)
            else "추가 처치 치아가 장부의 진료·치료계획 항목·다른 추가 처치에 나옴"
            if tooth in teeth[r["patient_id"]] or extra_teeth[(r["patient_id"], tooth)] > 1
            else "차트 사실 extra ≠ 정답"
            if extra != {"procedure": PROCEDURE[code]["name"], "tooth": tooth}
            else None
        )
        if why:
            bad.append({"id": r["id"], "extra": [code, tooth], "problem": why})
    return bad


def check_chart_requirement(conn, ctx):
    """요건 줄(requirement)이 규칙대로 — _expected_requirement와 같고, 요건 줄만 evidence_present(0/1)를 가진다"""
    rows = _chart_lines(conn, ctx)
    expected = _expected_requirement(rows)
    return [
        {
            "id": r["id"],
            "procedure_code": r["procedure_code"],
            "requirement": r["requirement"],
            "expected": expected[r["id"]],
            "evidence_present": r["evidence_present"],
        }
        for r in rows
        if r["kind"] is not None
        and (
            r["requirement"] != expected[r["id"]]
            or (r["requirement"] is None) != (r["evidence_present"] is None)
            or r["evidence_present"] not in (None, 0, 1)
        )
    ]


def check_requirement_facts(conn, ctx):
    """요건 사실이 장부와 모순되지 않음 — 장부에 근거 사건이 있으면 그 날짜에서 계산한 값(other_clinic 아님),
    없으면 '타 기관' 이력(other_clinic — R01은 같은 코스의 이전 진료도 없어야). R01 = 다른 코스의 같은 치아·같은 시술 이전 진료, R02 = 직전 PERIO_LINK_DAYS일 안
    치석제거, R06 = 같은 치아 발치(근거 있는 줄만)"""
    bad = []
    last, scaled, extracted = (
        defaultdict(lambda: defaultdict(list)),
        defaultdict(list),
        defaultdict(dict),
    )
    for r in _chart_lines(conn, ctx):  # 환자·시각순
        pid, day = r["patient_id"], r["day"]
        f = json.loads(r["facts"]).get("requirement") if r["facts"] else None
        if f:
            ledger, field = {
                "R01": (
                    _other_course(last[pid][(r["tooth_no"], r["procedure_code"])], r),
                    "prev_days",
                ),
                "R02": (
                    min(
                        [
                            (day - d).days
                            for d in scaled[pid]
                            if 0 < (day - d).days <= PERIO_LINK_DAYS
                        ],
                        default=None,
                    ),
                    "scaling_days_ago",
                ),
                "R06": (extracted[pid].get(r["tooth_no"]), "extracted_days_ago"),
            }.get(f["key"], (None, None))
            if isinstance(ledger, date):
                ledger = (day - ledger).days
            if (
                f["key"] == "R01"
                and f.get("other_clinic")
                and last[pid][(r["tooth_no"], r["procedure_code"])]
            ):
                bad.append(
                    {
                        "id": r["id"],
                        "requirement": f,
                        "problem": "장부에 같은 치아·시술이 있는데 타 기관 이력",
                    }
                )
            elif field and not (f["key"] == "R06" and not f["evidence"]):
                ok = (
                    (not f.get("other_clinic") and f.get(field) == ledger)
                    if ledger is not None
                    else bool(f.get("other_clinic"))
                )
                if not ok:
                    bad.append({"id": r["id"], "requirement": f, "ledger_days": ledger})
        if r["tooth_no"] != FULL_MOUTH:
            last[pid][(r["tooth_no"], r["procedure_code"])].append((day, r["course"]))
        if r["procedure_code"] == SCALING_CODE:
            scaled[pid].append(day)
        if r["procedure_code"] in EXTRACTION_CODES:
            extracted[pid][r["tooth_no"]] = day
    return bad


def check_chart_special(conn, ctx):
    """특이 ⇔ 오류 줄 ∪ 요건 줄, 서술 ⇔ 특이 ∪ 디코이 ∪ 응급 내원(§2.6 (5)), 특이와 디코이는 겹치지 않음, 창 이전 내원은 템플릿만"""
    emergency = {
        r[0]
        for r in conn.execute(
            "SELECT t.id FROM emr_treatments t WHERE t.appointment_id IN"
            " (SELECT json_extract(used_fact, '$.emergency_visit.appointment_id') FROM truth.truth_call_labels)"
        )
    }
    bad = []
    for r in _chart_lines(conn, ctx):
        if r["kind"] is None:
            continue
        special = bool(r["error"]) or r["requirement"] is not None
        if (
            bool(r["is_special"]) != special
            or (r["kind"] == "narrative")
            != bool(r["is_special"] or r["is_decoy"] or r["id"] in emergency)
            or (r["is_special"] and r["is_decoy"])
            or (not r["window"] and r["kind"] != "template")
        ):
            bad.append(
                {
                    k: r[k]
                    for k in (
                        "id",
                        "kind",
                        "is_special",
                        "is_decoy",
                        "requirement",
                        "error",
                        "window",
                    )
                }
            )
    return bad


def check_chart_text(conn, ctx):
    """차트 본문: 환자 이름·정답 어휘(CHART_FORBIDDEN_WORDS) 없음, 치아 단위 시술은 'NN번', 시술 표준 이름 있음(별칭 아님),
    그 내원에서 발견한 주의사항(visit 출처) 이름 있음(§2.2)"""
    names = {r["code"]: r["name"] for r in _rows(conn, "SELECT code, name FROM emr_procedures")}
    discovered = defaultdict(list)
    for r in _rows(
        conn,
        "SELECT t.id, ty.label FROM emr_patient_alerts al JOIN emr_alert_types ty ON ty.code = al.code"
        " JOIN emr_treatments t ON t.appointment_id = al.appointment_id WHERE al.source = 'visit'",
    ):
        discovered[r["id"]].append(r["label"])
    bad = []
    for r in _chart_lines(conn, ctx):
        note = r["note"]
        if note is None:
            continue
        why = (
            "환자 이름"
            if r["patient_name"] in note
            else "정답 어휘"
            if any(w in note for w in CHART_FORBIDDEN_WORDS)
            else "치아 번호 없음"
            if r["tooth_no"] != FULL_MOUTH and f"{r['tooth_no']}번" not in note
            else "시술 이름 없음"
            if names[r["procedure_code"]] not in note
            else "발견한 주의사항 없음"
            if not all(label in note for label in discovered[r["id"]])
            else None
        )
        if why:
            bad.append({"id": r["id"], "reason": why, "note": note[:60]})
    return bad


def check_chart_plan(conn, ctx):
    """서술 줄의 다음 계획 = 그 내원을 마친 시각에 열린 치료계획 항목(시술·치아, 전체 구강은 None — 생성 설계 §4.3 (5)).
    열린 항목이 없으면 위생 내원은 정기 검진 주기, 그 밖은 없음"""
    items = _plan_items(conn)
    bad = []
    for r in _rows(
        conn,
        "SELECT c.treatment_id, c.facts, t.patient_id, t.procedure_code, a.scheduled_at, a.duration_min"
        " FROM truth.truth_chart_notes c JOIN emr_treatments t ON t.id = c.treatment_id"
        " JOIN emr_appointments a ON a.id = t.appointment_id WHERE c.kind = 'narrative'",
    ):
        open_items = [
            i | {"tooth": None if i["tooth"] == FULL_MOUTH else i["tooth"]}
            for i in _open_items_at(items, r["patient_id"], _end(r))
        ]
        if open_items:
            want = {"items": open_items}
        elif r["procedure_code"] in chart.HYGIENE_CODES:
            want = {"recall_months": RECALL_CYCLE_MONTHS}
        else:
            want = None
        got = json.loads(r["facts"]).get("plan")
        if got != want:
            bad.append({"treatment_id": r["treatment_id"], "plan": got, "expected": want})
    return bad


NO_BLEEDING = re.compile(r"출혈\s*(은|이|도)?\s*(없|\(-\)|-|음성)")  # R03 부분 소견의 출혈 없음


def check_chart_evidence_words(conn, ctx):
    """근거 없는 요건 서술 줄에 근거 말(chart.EVIDENCE_WORDS) 없음 — 9b 건별 검증과 같은 기준(R05 부분 소견도 X-선 말 없음).
    R03 부분 소견(near_miss) 줄은 출혈 없음을 담음(빠지면 근거 있음으로 읽힘)"""
    bad = []
    for r in _rows(
        conn,
        "SELECT n.treatment_id, n.note, c.facts FROM emr_chart_notes n"
        " JOIN truth.truth_chart_notes c USING (treatment_id) WHERE c.kind = 'narrative'",
    ):
        hits = [
            w
            for w in chart.evidence_forbidden(json.loads(r["facts"]))
            if llm_jobs.has_word(r["note"], w)
        ]
        if hits:
            bad.append({"treatment_id": r["treatment_id"], "words": hits})
        req = json.loads(r["facts"]).get("requirement") or {}
        if (
            req.get("key") == "R03"
            and req.get("style") == "near_miss"
            and not NO_BLEEDING.search(r["note"])
        ):
            bad.append(
                {"treatment_id": r["treatment_id"], "problem": "부분 소견에 출혈 없음 없음"}
            )
    return bad


def check_chart_rules(conn, ctx):
    """9b 검증의 금지 꼴이 저장된 서술 차트에도 없음 — chart.narrative_checks의 금지 패턴(메모체가 아닌 끝맺음·초안에 없는 시점·
    부위 말 등). 캐시 줄을 손으로 고치거나 옛 통과 줄이 살아나면 잡는다 (경고)"""
    bad = []
    for r in _rows(
        conn,
        "SELECT n.treatment_id, n.note, c.facts, p.name FROM emr_chart_notes n"
        " JOIN truth.truth_chart_notes c USING (treatment_id) JOIN emr_treatments t ON t.id = n.treatment_id"
        " JOIN emr_patients p ON p.id = t.patient_id WHERE c.kind = 'narrative' ORDER BY n.treatment_id",
    ):
        patterns = chart.narrative_checks(json.loads(r["facts"]), r["name"])["forbidden_patterns"]
        hits = [p for p in patterns if re.search(p, r["note"])]
        if hits:
            bad.append({"treatment_id": r["treatment_id"], "patterns": hits[:2]})
    return bad


def check_chart_narrative_facts(conn, ctx):
    """서술 줄 본문에 꼭 넣을 사실(9b 건별 검증과 같은 기준 — chart.narrative_must_include)이 있음"""
    bad = []
    for r in _rows(
        conn,
        "SELECT n.treatment_id, n.note, c.facts FROM emr_chart_notes n"
        " JOIN truth.truth_chart_notes c USING (treatment_id) WHERE c.kind = 'narrative'"
        " ORDER BY n.treatment_id",
    ):
        missing = [
            alts[0]
            for alts in chart.narrative_must_include(json.loads(r["facts"]))
            if not any(llm_jobs.contains(r["note"], a) for a in alts)
        ]
        if missing:
            bad.append({"treatment_id": r["treatment_id"], "missing": missing})
    return bad


# emr_chart_notes 허용 컬럼 — 특이·디코이·요건 충족 컬럼은 정답 누출 (§5.3 누출)
CHART_NOTE_COLUMNS = {"id", "treatment_id", "dx_code", "note"}


def check_chart_note_columns(conn, ctx):
    """emr_chart_notes에 허용 컬럼만"""
    return [
        {"column": r["name"]}
        for r in conn.execute("PRAGMA main.table_info(emr_chart_notes)")
        if r["name"] not in CHART_NOTE_COLUMNS
    ]


# ── 분포·리포트 ────────────────────────────────────────────────


def check_chart_decoy_ratio(conn, ctx):
    """디코이 / 특이 케이스(창 안)가 CHART_DECOY_RANGE 안 — 특이가 없으면 건너뜀"""
    specials, decoys = conn.execute(
        "SELECT coalesce(sum(is_special), 0), coalesce(sum(is_decoy), 0)"
        " FROM truth.truth_chart_notes"
    ).fetchone()
    if not specials:
        return []
    ratio = decoys / specials
    lo, hi = CHART_DECOY_RANGE
    return (
        []
        if lo <= ratio <= hi
        else [{"specials": specials, "decoys": decoys, "ratio": round(ratio, 3)}]
    )


def report_adjust_judgment(conn, ctx):
    """확정 월 깎인 줄(창 안)의 판정 분포 — 정당(오류 있음·외부 이력) / 다툴 만함(요건 사유·오류 없음) /
    보완 후 재청구(장부 밖 사유·오류 없음). 목표는 master.ADJUST_JUDGMENT_TARGET (생성 설계 §2.4)"""
    counts = defaultdict(int)
    for r in _chart_lines(conn, ctx):
        if r["cut"] is None or not r["window"]:
            continue
        judged = (
            "justified"
            if r["error"] or r["cut"]["type"] == "external"
            else "contestable"
            if r["cut"]["type"] == "requirement"
            else "resubmit"
        )
        counts[judged] += 1
    n = sum(counts.values())
    if not n:
        return {"n": 0}
    return {
        "n": n,
        **{k: round(counts[k] / n, 3) for k in ("justified", "contestable", "resubmit")},
    }


def report_claim_errors(conn, ctx):
    """청구 오류 수 — 월 구간별(확정·제출·전월) + 전월 급여 줄 수(⑤-2 평가 모수)"""
    counts = defaultdict(int)
    for r in _chart_lines(conn, ctx):
        if r["bucket"] and r["window"]:
            counts[r["bucket"]] += bool(r["error"])
            counts["pending_lines"] += r["bucket"] == "pending"
    return {k: counts[k] for k in ("settled", "submitted", "pending", "pending_lines")}


def report_alert_holder_share(conn, ctx):
    """주의사항 1개 이상 보유 비율 — TODAY까지 등록한 환자 전체와 연령 구간별(TODAY 나이). 기록일과 무관하게 센다"""
    holders = {r[0] for r in conn.execute("SELECT DISTINCT patient_id FROM emr_patient_alerts")}
    by_band = defaultdict(list)
    for r in _rows(
        conn,
        "SELECT id, birth_date FROM emr_patients WHERE registered_at <= ?",
        ctx.today.isoformat(),
    ):
        by_band[age_band(age_on(date.fromisoformat(r["birth_date"]), ctx.today))].append(
            r["id"] in holders
        )
    flags = [f for fs in by_band.values() for f in fs]
    if not flags:
        return {"n": 0}
    return {
        "n": len(flags),
        "share": round(sum(flags) / len(flags), 3),
        **{band: round(sum(fs) / len(fs), 3) for band, fs in by_band.items()},
    }
