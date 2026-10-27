"""7단계 통화 전사 — 6단계 빈 슬롯(ax_calls)을 LLM 전사로 채운다 (가상 데이터 생성 설계 §4.1 '7단계 구현 결정').
건마다 입력(방향·날짜·라벨·사실)과 검증 기준(꼭 넣을 사실·라벨 어휘·금지 토픽·화자·길이)을 만들어 llm_jobs에 맡긴다.
통과한 전사는 ax_calls에 넣고 길이를 발화 글자 수로 다시 계산한다. 시나리오는 저장하지 않는다(라벨을 풀어 쓴 문장).
"""
import json
from datetime import date

from generator import llm_jobs
from generator.llm_jobs import Task
from generator.master import (AGE_BANDS, CALL_DURATION_SEC, CALL_FLAGS, CALL_LABEL_WORDS, CONDITIONAL_INSURED_MIN_AGE, FULL_MOUTH,
                              PROCEDURES, SCALING_MIN_AGE, TRANSCRIPT_CHARS_PER_SEC)

SPEAKERS = ["환자:", "직원:"]
SCHEMA = {
    "type": "object",
    "properties": {
        "scenario": {"type": "string", "description": "라벨에서 정한 통화 상황 (2~4문장)"},
        "transcript": {"type": "string", "description": "줄마다 '환자: ' 또는 '직원: '으로 시작하는 대화"},
    },
    "required": ["scenario", "transcript"],
    "additionalProperties": False,
}
TASK = Task(name="transcript", prompt="gen_transcript/v1", schema=SCHEMA, store=("transcript",))
INTERNAL_KEYS = ("appointment_id", "id")  # 사실에서 빼는 내부 번호 — 대화에 차트 번호·id가 새지 않게. 치아 '00'(전체 구강)은 None


def rows(conn) -> list[dict]:
    cur = conn.execute(
        "SELECT c.id, c.call_uid, c.direction, l.* FROM ax_calls c JOIN truth.truth_call_labels l USING (call_uid)"
        " WHERE c.source = 'generated' ORDER BY c.id")
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur]


def _strip(obj):
    if isinstance(obj, dict):
        return {k: None if k == "tooth" and v == FULL_MOUTH else _strip(v) for k, v in obj.items() if k not in INTERNAL_KEYS}
    if isinstance(obj, list):
        return [_strip(v) for v in obj]
    return obj


PRICE_PURPOSES = ("price_q", "insurance_q")  # 주제 시술의 비용·급여(master)를 입력에 넣는 목적 — RAG 문서와 같은 값
_PROC = {p["name"]: p for p in PROCEDURES}
_CHILD_MAX = next(hi for name, _, hi, _ in AGE_BANDS if name == "child")
_AGE_RULE = {"age19plus": f"{SCALING_MIN_AGE}세 이상", "senior": f"{CONDITIONAL_INSURED_MIN_AGE}세 이상",
             "child": f"만 {_CHILD_MAX}세 이하"}


def topic_price(name: str) -> dict:
    """시술 비용·급여 — master 수가·본인부담률·적용 연령(문서 doc_catalog와 같은 원천)"""
    p = _PROC[name]
    out = {"procedure": name, "total": f"{p['fee_total']:,}원", "insured": p["is_insured"] != "no"}
    if out["insured"]:
        out["patient_pays"] = f"{round(p['fee_total'] * p['patient_share_rate']):,}원"
        if p["age_group"] in _AGE_RULE:
            out["insured_age"] = _AGE_RULE[p["age_group"]]
    return out


def payload(row: dict) -> dict:
    fact = json.loads(row["used_fact"])
    facts = _strip(fact)
    if row["primary_purpose"] in PRICE_PURPOSES and fact.get("topic_procedure"):
        facts["topic_price"] = topic_price(fact["topic_procedure"])
    return {
        "direction": row["direction"],
        "call_date": fact["call_date"],
        "labels": {"primary_purpose": row["primary_purpose"], "accepted_purposes": json.loads(row["accepted_purposes"]),
                   "counterpart": row["counterpart"], "urgency": row["urgency"],
                   "flags": {f: bool(row[f]) for f in CALL_FLAGS}, "noise_type": row["noise_type"]},
        "facts": facts,
    }


def simplify(p: dict) -> dict:
    """llm_jobs.SIMPLIFY_AFTER번 실패한 뒤 — 사실은 꼭 넣을 것(날짜·예약·주제 시술)만"""
    keep = ("call_date", "appointment", "topic_procedure", "topic_price")
    return p | {"facts": {k: v for k, v in p["facts"].items() if k in keep}}


def korean_date(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.month}월 {d.day}일"


def must_include(fact: dict, names: dict) -> list[list[str]]:
    """꼭 넣을 사실 — 예약 날짜(변경은 원래 날짜도)·주제 시술(이름 또는 별칭)"""
    out = []
    appt = fact.get("appointment")
    if appt:
        out.append([korean_date(appt["date"])])
        if "from_date" in appt:
            out.append([korean_date(appt["from_date"])])
    if fact.get("topic_procedure"):
        out.append(names.get(fact["topic_procedure"], [fact["topic_procedure"]]))
    return out


def checks(row: dict, names: dict) -> dict:
    lo, hi = CALL_DURATION_SEC
    return {"field": "transcript", "must_include": must_include(json.loads(row["used_fact"]), names),
            "forbidden_words": list(CALL_LABEL_WORDS), "forbidden_patterns": llm_jobs.forbidden_patterns(),
            "speakers": SPEAKERS, "length": [lo * TRANSCRIPT_CHARS_PER_SEC, hi * TRANSCRIPT_CHARS_PER_SEC]}


def duration_sec(transcript: str) -> int:
    lo, hi = CALL_DURATION_SEC
    return min(hi, max(lo, round(len(llm_jobs.spoken(transcript, SPEAKERS)) / TRANSCRIPT_CHARS_PER_SEC)))


def stage(run, rng, fake) -> None:
    ctx, names = run.state["llm"], llm_jobs.procedure_names(run.conn)
    for row in rows(run.conn):
        out = ctx.resolve(TASK, row["call_uid"], payload(row), checks(row, names), simplify)
        if out is not None:
            run.conn.execute("UPDATE ax_calls SET transcript = ?, has_transcript = 1, duration_sec = ? WHERE id = ?",
                             (out["transcript"], duration_sec(out["transcript"]), row["id"]))
