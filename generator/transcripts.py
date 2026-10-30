"""7단계 통화 전사 — 6단계 빈 슬롯(ax_calls)을 LLM 전사로 채운다 (가상 데이터 생성 설계 §4.1 '7단계 구현 결정').
건마다 입력(방향·날짜·라벨·사실)과 검증 기준(꼭 넣을 사실·라벨 어휘·금지 토픽·화자·길이)을 만들어 llm_jobs에 맡긴다.
통과한 전사는 ax_calls에 넣고 길이를 발화 글자 수로 다시 계산한다. 시나리오는 저장하지 않는다(라벨을 풀어 쓴 문장).
"""

import hashlib
import json
from datetime import date

from generator import llm_jobs
from generator.llm_jobs import Task
from generator.master import (
    AGE_BANDS,
    CALL_DURATION_SEC,
    CALL_FLAGS,
    CALL_LABEL_WORDS,
    CALL_PATIENT_RULES,
    CLINIC_NAME,
    COMPLAINT_TOPICS,
    CONDITIONAL_INSURED_MIN_AGE,
    EMERGENCY_ONSETS,
    EMERGENCY_SCENES,
    FULL_MOUTH,
    PROCEDURES,
    RECALL_CYCLE_MONTHS,
    SCALING_CODE,
    SCALING_MIN_AGE,
    THIRD_PARTY_RULE,
    TRANSCRIPT_CHARS_PER_SEC,
    insured_on,
)

SPEAKERS = ["환자:", "직원:"]
SCHEMA = {
    "type": "object",
    "properties": {
        "scenario": {"type": "string", "description": "라벨에서 정한 통화 상황 (2~4문장)"},
        "transcript": {
            "type": "string",
            "description": "줄마다 '환자: ' 또는 '직원: '으로 시작하는 대화",
        },
    },
    "required": ["scenario", "transcript"],
    "additionalProperties": False,
}
TASK = Task(name="transcript", prompt="gen_transcript", schema=SCHEMA, store=("transcript",))
INTERNAL_KEYS = (
    "appointment_id",
    "id",
)  # 사실에서 빼는 내부 번호 — 대화에 차트 번호·id가 새지 않게. 치아 '00'(전체 구강)은 None
# 새 예약 통화의 예약은 이 통화에서 정하는 날짜 — 'appointment'로 넘기면 모델이 이미 잡힌 예약으로 읽는다
NEW_BOOKING_NOTE = "통화 전에는 없던 예약 — 이 통화에서 처음 정하는 날짜"
# 통화 전 예약이 없는 새 예약 통화에서 쓰면 안 되는 말 — 정할 날짜를 이미 잡힌 예약처럼 말하는 꼴
ALREADY_BOOKED = r"잡혀 있|예약(이|은)? ?있으|예약되어 있|예약돼 있"
# 새 예약을 원하지 않는 취소 통화에서 쓰면 안 되는 말 — 직원이 새 날짜를 잡자고 하면 wants_appt로 읽힌다
REBOOK_OFFER = (  # 직원 줄만 — 환자가 '다시 잡을 생각은 없어요'라고 하는 것·직원의 '잡지 않고'는 괜찮다
    r"(?m)^직원:(?![^\n]*잡지 않)[^\n]*(새 ?(일정|날짜|예약)|다시 (잡|예약)|"
    r"일정(을)? (다시 )?(여쭤|맞춰|상의|잡)|(편하신|원하시는) (날|시간|요일))"
)
SITUATION_PURPOSES = (
    "emergency",
    "complaint",
)  # 상황(caller_situation)을 입력에 넣는 목적 — 부 주제 포함
THIRD_PARTY = ("insurer",)  # 진료 기록을 넘기지 않는 상대 — 이름과 병원 규칙만
THIRD_PARTY_KEEP = ("call_date", "topic_procedure", "topic_price", "caller")


def _pick(uid: str, salt: str, options: tuple):
    """통화 번호로 고르기 — 난수를 쓰지 않아 다른 단계·통화의 결과를 바꾸지 않는다"""
    return options[int(hashlib.sha256(f"{salt}:{uid}".encode()).hexdigest(), 16) % len(options)]


MISSED_TOPIC = next(
    t for t in COMPLAINT_TOPICS if "놓침" in t
)  # 직전 노쇼가 있어야 말이 되는 불만
MISSED_WINDOW_DAYS = CALL_PATIENT_RULES[("complaint", "inbound")][
    1
]  # 불만 통화가 보는 최근 내원 기간


def situation(uid: str, purpose: str, recent_noshow: bool = True) -> str:
    """응급은 시작 시점 × 상황, 불만은 주제 — 문자를 못 받아 놓쳤다는 불만은 직전 노쇼가 없으면 다른 주제로(기록에 없는 일)"""
    if purpose == "emergency":
        return f"{_pick(uid, 'onset', EMERGENCY_ONSETS)} {_pick(uid, 'scene', EMERGENCY_SCENES)}"
    topic = _pick(uid, "topic", COMPLAINT_TOPICS)
    if topic == MISSED_TOPIC and not recent_noshow:
        return _pick(uid, "topic-other", tuple(t for t in COMPLAINT_TOPICS if t != MISSED_TOPIC))
    return topic


def rows(conn) -> list[dict]:
    cur = conn.execute(
        "SELECT c.id, c.call_uid, c.direction, c.caller_name,"
        # 말하는 사람의 성별 — 보호자 통화는 그 환자의 보호자, 그 밖은 환자 본인(이름이 같을 때 — 처음 연락·보험사 등은
        # 환자 측이 아니라 기록이 없다). 상대로 고르므로 아이와 보호자 이름이 같아도 어긋나지 않는다
        " CASE WHEN l.counterpart = 'guardian' THEN (SELECT g.sex FROM emr_patients p"
        " JOIN emr_patients g ON g.id = p.guardian_patient_id WHERE p.id = c.patient_id)"
        " ELSE (SELECT p.sex FROM emr_patients p WHERE p.id = c.patient_id AND p.name = c.caller_name)"
        " END AS caller_sex,"
        # 통화 전 MISSED_WINDOW_DAYS일 안의 노쇼 — 문자를 못 받아 놓쳤다는 불만의 근거
        " (SELECT count(*) FROM emr_appointments a WHERE a.patient_id = c.patient_id AND a.status = 'noshow'"
        f" AND a.scheduled_at < c.started_at AND a.scheduled_at >= datetime(c.started_at, '-{MISSED_WINDOW_DAYS} days'))"
        " AS recent_noshow,"
        " l.* FROM ax_calls c JOIN truth.truth_call_labels l USING (call_uid)"
        " WHERE c.source = 'generated' ORDER BY c.id"
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in cur]


def _strip(obj):
    if isinstance(obj, dict):
        return {
            k: None if k == "tooth" and v == FULL_MOUTH else _strip(v)
            for k, v in obj.items()
            if k not in INTERNAL_KEYS
        }
    if isinstance(obj, list):
        return [_strip(v) for v in obj]
    return obj


PRICE_PURPOSES = (
    "price_q",
    "insurance_q",
)  # 주제 시술의 비용·급여(master)를 입력에 넣는 목적 — RAG 문서와 같은 값
_PROC = {p["name"]: p for p in PROCEDURES}
_CHILD_MAX = next(hi for name, _, hi, _ in AGE_BANDS if name == "child")
_AGE_RULE = {
    "age19plus": f"{SCALING_MIN_AGE}세 이상",
    "senior": f"{CONDITIONAL_INSURED_MIN_AGE}세 이상",
    "child": f"만 {_CHILD_MAX}세 이하",
}


def topic_price(name: str, age: int | None = None, scaling_used: bool = False) -> dict:
    """시술 비용·급여 — master 수가·본인부담률·적용 연령(문서 doc_catalog와 같은 원천). 환자 나이를 알면 그 나이에 급여인지
    (master.insured_on — 적용 연령 밖이면 비급여 가격), 모르면(처음 연락) 급여 시술인지로. 그해 급여 스케일링을 이미 받았으면
    (scaling_used) 스케일링은 비급여 가격(연 1회, §2.4)"""
    p = _PROC[name]
    insured = insured_on(p, age) if age is not None else p["is_insured"] != "no"
    out = {"procedure": name, "total": f"{p['fee_total']:,}원", "insured": insured}
    if insured and scaling_used and p["code"] == SCALING_CODE:
        return out | {"insured": False, "insured_limit": "연 1회 — 올해 이미 받음"}
    if out["insured"]:
        out["patient_pays"] = f"{round(p['fee_total'] * p['patient_share_rate']):,}원"
        if p["age_group"] in _AGE_RULE:
            out["insured_age"] = _AGE_RULE[p["age_group"]]
    return out


def payload(row: dict) -> dict:
    fact = json.loads(row["used_fact"])
    facts = _strip(fact)
    if set(json.loads(row["accepted_purposes"])) & set(PRICE_PURPOSES) and fact.get(
        "topic_procedure"
    ):  # 부 주제 포함
        facts["topic_price"] = (
            topic_price(
                fact["topic_procedure"]
            )  # 제3자에게는 일반 가격 — 나이·이력으로 정하면 진료 기록이 샌다
            if row["counterpart"] in THIRD_PARTY
            else topic_price(
                fact["topic_procedure"],
                (fact.get("patient") or {}).get("age"),
                bool(fact.get("scaling_used_this_year")),
            )
        )
    # 말하는 사람(보호자·보험사 직원 등) — 이름을 지어내지 않게. 환자 기록이 있으면 성별도('엄마/아빠'가 어긋나지 않게)
    facts["caller"] = {"name": row["caller_name"]} | (
        {"sex": row["caller_sex"]} if row["caller_sex"] else {}
    )
    if "recall_q" in json.loads(row["accepted_purposes"]):
        facts["recall_months"] = (
            RECALL_CYCLE_MONTHS  # 정기 검진 주기 — 없으면 모델이 상식('6개월')을 지어낸다
        )
    if row["primary_purpose"] == "book" and facts.get("appointment"):
        facts["new_booking"] = facts.pop("appointment") | {"before_call": NEW_BOOKING_NOTE}
    accepted = json.loads(row["accepted_purposes"])
    if purpose := next((p for p in accepted if p in SITUATION_PURPOSES), None):
        facts["caller_situation"] = situation(
            row["call_uid"], purpose, recent_noshow=bool(row.get("recent_noshow"))
        )
    if row["counterpart"] in THIRD_PARTY:  # 제3자에게 진료 기록이 새지 않게 — 환자 이름만
        facts = {k: v for k, v in facts.items() if k in THIRD_PARTY_KEEP} | {
            "patient": {"name": fact["patient"]["name"]} if fact.get("patient") else None,
            "third_party_rule": THIRD_PARTY_RULE,
        }
    return {
        "clinic": CLINIC_NAME,
        "direction": row["direction"],
        "call_date": fact["call_date"],
        "labels": {
            "primary_purpose": row["primary_purpose"],
            "accepted_purposes": json.loads(row["accepted_purposes"]),
            "counterpart": row["counterpart"],
            "urgency": row["urgency"],
            "flags": {f: bool(row[f]) for f in CALL_FLAGS},
            "noise_type": row["noise_type"],
        },
        "facts": facts,
    }


def simplify(p: dict) -> dict:
    """llm_jobs.SIMPLIFY_AFTER번 실패한 뒤 — 사실은 꼭 넣을 것(날짜·예약·주제 시술과 그 가격)과 ⑧-1 근거(이미 냈다)·
    리콜 주기·받은 리콜 문자·말하는 사람·통화 상황·제3자 규칙만"""
    keep = (
        "call_date",
        "appointment",
        "new_booking",
        "topic_procedure",
        "topic_price",
        "recall_months",
        "recall_text",
        "caller",
        "already_paid",
        "caller_situation",
        "third_party_rule",
    )
    return p | {"facts": {k: v for k, v in p["facts"].items() if k in keep}}


def korean_date(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.month}월 {d.day}일"


def must_include(fact: dict, names: dict, purpose: str) -> list[list[str]]:
    """꼭 넣을 사실 — 예약 날짜(예약 변경 통화는 원래 날짜도 — 변경으로 생긴 예약의 확인·취소 통화에 넣으면 변경 통화처럼
    읽힌다)·주제 시술(이름 또는 별칭)·⑧-1 근거(이미 냈다는 내원일)"""
    out = []
    appt = fact.get("appointment")
    if appt:
        out.append([korean_date(appt["date"])])
        if "from_date" in appt and purpose == "reschedule":
            out.append([korean_date(appt["from_date"])])
    if fact.get("topic_procedure"):
        out.append(names.get(fact["topic_procedure"], [fact["topic_procedure"]]))
    if fact.get("already_paid"):
        out.append([korean_date(fact["already_paid"]["date"])])
    return out


def checks(row: dict, names: dict) -> dict:
    lo, hi = CALL_DURATION_SEC
    fact = json.loads(row["used_fact"])
    if row["counterpart"] in THIRD_PARTY:  # 입력에서 숨긴 사실은 요구하지 않는다
        fact = {k: v for k, v in fact.items() if k in THIRD_PARTY_KEEP}
    new_only = row["primary_purpose"] == "book" and not fact.get("next_appointment")
    no_rebook = row["primary_purpose"] == "cancel" and not row["wants_appt"]
    return {
        "field": "transcript",
        "must_include": must_include(fact, names, row["primary_purpose"]),
        "forbidden_words": list(CALL_LABEL_WORDS),
        "forbidden_patterns": llm_jobs.forbidden_patterns()
        + ([ALREADY_BOOKED] if new_only else [])
        + ([REBOOK_OFFER] if no_rebook else []),
        "speakers": SPEAKERS,
        "length": [round(lo * TRANSCRIPT_CHARS_PER_SEC), round(hi * TRANSCRIPT_CHARS_PER_SEC)],
    }


def duration_sec(transcript: str) -> int:
    lo, hi = CALL_DURATION_SEC
    return min(
        hi, max(lo, round(len(llm_jobs.spoken(transcript, SPEAKERS)) / TRANSCRIPT_CHARS_PER_SEC))
    )


def stage(run, rng, fake) -> None:
    ctx, names = run.state["llm"], llm_jobs.procedure_names(run.conn)
    for row in rows(run.conn):
        out = ctx.resolve(TASK, row["call_uid"], payload(row), checks(row, names), simplify)
        if out is not None:
            run.conn.execute(
                "UPDATE ax_calls SET transcript = ?, has_transcript = 1, duration_sec = ? WHERE id = ?",
                (out["transcript"], duration_sec(out["transcript"]), row["id"]),
            )
