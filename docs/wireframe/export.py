"""와이어프레임(docs/wireframe/index.html)용 데이터 — data/source.sqlite(+ 정답 DB 일부: AI 결과 칸 예시)에서 화면별로 뽑아
JSON 한 파일로. make data 뒤에 make wireframe. 정답 라벨이 들어 있어 결과(data.json)는 커밋하지 않는다."""

import json
import sqlite3
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import yaml  # noqa: E402

from generator.master import CLINIC_NAME, RECALL_CYCLE, SCALING_CODE, SCALING_MIN_AGE  # noqa: E402

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "docs" / "wireframe" / "data.json"
TODAY = date.fromisoformat(
    str(yaml.safe_load((ROOT / "config" / "generator.yaml").read_text())["today"])
)
CALLS_FROM = (TODAY - timedelta(days=47)).isoformat()  # 통화 목록 — 최근 약 7주
RECON_FROM = (TODAY - timedelta(days=122)).isoformat()[:8] + "01"  # 재무 대조 — 최근 4개월
ADJUST_FROM = (TODAY - timedelta(days=183)).isoformat()[:7]  # 청구 점검 — 최근 6개월
c = sqlite3.connect(f"{ROOT}/data/source.sqlite")
c.row_factory = sqlite3.Row
c.execute(f"ATTACH '{ROOT}/ground_truth/truth.sqlite' AS truth")


def rows(sql, *p):
    return [dict(r) for r in c.execute(sql, p)]


def age(birth, on):
    b = date.fromisoformat(birth)
    return on.year - b.year - ((on.month, on.day) < (b.month, b.day))


PURPOSE_KO = {
    "book": "예약 잡기",
    "reschedule": "예약 변경",
    "confirm": "예약 확인",
    "cancel": "예약 취소",
    "recall_q": "리콜 문의",
    "treatment_q": "치료 문의",
    "insurance_q": "보험 문의",
    "price_q": "가격·비용 문의",
    "billing_q": "수납·청구 문의",
    "document_req": "서류·기록 요청",
    "emergency": "응급·통증",
    "complaint": "불만",
    "other": "기타",
    "unknown": "모르겠음·애매",
}
COUNTERPART_KO = {
    "patient": "환자",
    "guardian": "보호자",
    "new_caller": "처음 연락",
    "insurer": "보험사",
    "vendor": "업체",
    "other": "기타",
}
TYPE_KO = {"treatment": "진료", "hygiene": "위생", "consult": "상담"}
STATUS_KO = {"scheduled": "예정", "completed": "완료", "noshow": "노쇼", "cancelled": "취소"}
ROLE_KO = {"doctor": "원장", "hygienist": "위생사", "desk": "데스크", "consultant": "상담실장"}

proc = {r["code"]: r for r in rows("SELECT * FROM emr_procedures")}
pname = {k: v["name"] for k, v in proc.items()}
staff = {r["id"]: r for r in rows("SELECT * FROM emr_staff")}
pats = {r["id"]: r for r in rows("SELECT * FROM emr_patients")}
alert_types = {r["code"]: r for r in rows("SELECT * FROM emr_alert_types")}
alerts = defaultdict(list)
for a in rows("SELECT * FROM emr_patient_alerts"):
    alerts[a["patient_id"]].append(a)
appts_by = defaultdict(list)
for a in rows("SELECT * FROM emr_appointments ORDER BY scheduled_at"):
    appts_by[a["patient_id"]].append(a)
pays_by = defaultdict(list)
for p in rows("SELECT * FROM emr_payments ORDER BY paid_at"):
    pays_by[p["patient_id"]].append(p)
treat_by = defaultdict(list)
for t in rows("SELECT * FROM emr_treatments ORDER BY performed_at"):
    treat_by[t["patient_id"]].append(t)
fam = defaultdict(list)
for p in pats.values():
    if p["family_id"]:
        fam[p["family_id"]].append(p["id"])
items = rows(
    "SELECT i.*, p.patient_id, p.created_at FROM emr_treatment_plan_items i"
    " JOIN emr_treatment_plans p ON p.id = i.plan_id"
)
items_by = defaultdict(list)
for i in items:
    items_by[i["patient_id"]].append(i)


def pinfo(pid, on=TODAY):
    p = pats[pid]
    return {
        "id": pid,
        "chart_no": p["chart_no"],
        "name": p["name"],
        "age": age(p["birth_date"], on),
        "sex": "남" if p["sex"] == "M" else "여",
        "phone": p["phone"],
    }


def balance(pid, before):
    return sum(
        x["amount_due"] - x["amount_paid"] for x in pays_by[pid] if x["paid_at"][:10] < before
    )


def visible_alerts(pid, on):
    out = []
    for a in alerts[pid]:
        if a["recorded_at"] <= on and (a["ends_at"] is None or on < a["ends_at"]):
            t = alert_types[a["code"]]
            out.append(
                {
                    "label": t["label"],
                    "kind": t.get("category") if "category" in t.keys() else None,
                }
            )
    return out


def last_hygiene(pid, before):
    hs = [
        a["scheduled_at"][:10]
        for a in appts_by[pid]
        if a["type"] == "hygiene"
        and a["status"] == "completed"
        and a["scheduled_at"][:10] < before
    ]
    return max(hs) if hs else None


def family_recall(pid, on):
    p = pats[pid]
    out = []
    for o in fam.get(p["family_id"], []):
        if o == pid:
            continue
        lh = last_hygiene(o, on)
        future = any(
            a["scheduled_at"][:10] >= on and a["status"] != "cancelled" for a in appts_by[o]
        )
        if lh and date.fromisoformat(lh) + RECALL_CYCLE < date.fromisoformat(on) and not future:
            out.append({"name": pats[o]["name"], "last_hygiene": lh})
    return out


def scaling_unused(pid, on):
    d = date.fromisoformat(on)
    if age(pats[pid]["birth_date"], d) < SCALING_MIN_AGE:
        return False
    return not any(
        t["procedure_code"] == SCALING_CODE
        and t["performed_at"][:4] == on[:4]
        and t["performed_at"][:10] < on
        for t in treat_by[pid]
    )


def open_items(pid, on, today_code):
    out = []
    for i in items_by[pid]:
        if (
            i["created_at"][:10] <= on
            and i["status"] == "open"
            and i["procedure_code"] != today_code
        ):
            out.append(
                {
                    "procedure": pname[i["procedure_code"]],
                    "tooth": i["tooth_no"],
                    "est_fee": i["est_fee"],
                }
            )
    return out


# ── 준비표: 오늘 + 최근 진료일 7일 ──
days = sorted(
    {
        a["scheduled_at"][:10]
        for a in rows("SELECT scheduled_at FROM emr_appointments")
        if a["scheduled_at"][:10] <= TODAY.isoformat()
    }
)[-8:]
prep = {}
for d in days:
    lst = []
    for a in rows(
        "SELECT * FROM emr_appointments WHERE date(scheduled_at) = ? AND status != 'cancelled' ORDER BY scheduled_at, chair_no",
        d,
    ):
        pid = a["patient_id"]
        lst.append(
            {
                "appt_id": a["id"],
                "time": a["scheduled_at"][11:16],
                "duration": a["duration_min"],
                "chair": a["chair_no"],
                "staff": staff[a["staff_id"]]["name"],
                "role": ROLE_KO[staff[a["staff_id"]]["role"]],
                "type": TYPE_KO[a["type"]],
                "procedure": pname[a["planned_procedure_code"]],
                "status": STATUS_KO[a["status"]] if d < TODAY.isoformat() else "예정",
                "patient": pinfo(pid, date.fromisoformat(d)),
                "new_patient": pats[pid]["registered_at"] >= "2024-12-01"
                and not any(
                    x["status"] == "completed" and x["scheduled_at"][:10] < d
                    for x in appts_by[pid]
                ),
                "chips": {
                    "balance": balance(pid, d),
                    "scaling_unused": scaling_unused(pid, d),
                    "family_recall": family_recall(pid, d),
                    "alerts": visible_alerts(pid, d),
                    "open_plan": open_items(pid, d, a["planned_procedure_code"]),
                },
                "last_visit": max(
                    (
                        x["scheduled_at"][:10]
                        for x in appts_by[pid]
                        if x["status"] == "completed" and x["scheduled_at"][:10] < d
                    ),
                    default=None,
                ),
            }
        )
    prep[d] = lst

# ── 통화 ──
calls = []
for r in rows(
    "SELECT c.*, l.* FROM ax_calls c JOIN truth.truth_call_labels l USING (call_uid)"
    " WHERE c.started_at >= ? ORDER BY c.started_at DESC",
    CALLS_FROM,
):
    f = json.loads(r["used_fact"])
    calls.append(
        {
            "uid": r["call_uid"],
            "at": r["started_at"][:16],
            "direction": "수신" if r["direction"] == "inbound" else "발신",
            "caller": r["caller_name"],
            "phone": r["caller_phone"],
            "duration_sec": r["duration_sec"],
            "patient": pinfo(r["patient_id"]) if r["patient_id"] else None,
            "ai": {
                "purpose": PURPOSE_KO[r["primary_purpose"]],
                "also": [PURPOSE_KO[x] for x in json.loads(r["accepted_purposes"])[1:]],
                "counterpart": COUNTERPART_KO[r["counterpart"]],
                "urgency": {"urgent": "급함", "normal": "보통", "low": "낮음"}[r["urgency"]],
                "flags": [
                    k
                    for k in (
                        "emergency",
                        "complaint",
                        "price_inquiry",
                        "wants_appt",
                        "appt_confirmed",
                        "has_todo",
                    )
                    if r[k]
                ],
            },
            "facts": {
                k: f.get(k)
                for k in (
                    "appointment",
                    "topic_procedure",
                    "balance",
                    "recall_text",
                    "already_paid",
                )
                if f.get(k)
            },
            "transcript": r["transcript"],
        }
    )

# ── 문서함 ──
docs = [
    {
        "id": d["id"],
        "type": d["type"],
        "title": d["title"],
        "version": d["version"],
        "effective": d["effective_date"],
        "body": d["body"],
    }
    for d in rows("SELECT * FROM ax_documents ORDER BY type, id")
]
qa = json.load(open(f"{ROOT}/ground_truth/rag_qa.json", encoding="utf-8"))
ask_examples = [
    {"q": x["question"], "a": x["answer_text"], "sources": x["answer_doc_ids"], "type": x["type"]}
    for x in qa
    if x["type"] in ("manual", "notice", "data", "refusal")
][:24]

# ── EMR: 오늘·준비표 환자 + 통화 환자 ──
emr_ids = sorted(
    {x["patient"]["id"] for d in prep.values() for x in d}
    | {x["patient"]["id"] for x in calls if x["patient"]}
)
claims_by = defaultdict(list)
for cl in rows("SELECT * FROM emr_claims ORDER BY service_date"):
    claims_by[cl["patient_id"]].append(cl)
notes = {r["treatment_id"]: r for r in rows("SELECT * FROM emr_chart_notes")}
msgs_by = defaultdict(list)
for m in rows(
    'SELECT patient_id, sent_at, direction, "trigger" AS trig, body FROM emr_messages ORDER BY sent_at'
):
    msgs_by[m["patient_id"]].append(m)
emr = {}
for pid in emr_ids:
    p = pats[pid]
    emr[p["chart_no"]] = {
        "patient": pinfo(pid)
        | {
            "birth": p["birth_date"],
            "registered": p["registered_at"],
            "private_insurance": bool(p["has_private_insurance"]),
            "family": [pats[o]["name"] for o in fam.get(p["family_id"], []) if o != pid],
        },
        "alerts": [
            {
                "label": alert_types[a["code"]]["label"],
                "recorded": a["recorded_at"],
                "ends": a["ends_at"],
                "source": a["source"],
            }
            for a in alerts[pid]
        ],
        "appointments": [
            {
                "at": a["scheduled_at"][:16],
                "type": TYPE_KO[a["type"]],
                "procedure": pname[a["planned_procedure_code"]],
                "status": STATUS_KO[a["status"]],
                "staff": staff[a["staff_id"]]["name"],
                "chair": a["chair_no"],
            }
            for a in appts_by[pid]
        ],
        "treatments": [
            {
                "at": t["performed_at"][:10],
                "procedure": pname[t["procedure_code"]],
                "tooth": t["tooth_no"],
                "fee": t["fee_total"],
                "insured": t["insured_amount"],
                "patient_amount": t["patient_amount"],
                "dx": notes.get(t["id"], {}).get("dx_code"),
                "note": notes.get(t["id"], {}).get("note"),
            }
            for t in treat_by[pid]
        ],
        "payments": [
            {
                "at": x["paid_at"][:16],
                "kind": x["kind"],
                "due": x["amount_due"],
                "paid": x["amount_paid"],
                "method": x["method"],
            }
            for x in pays_by[pid]
        ],
        "claims": [
            {
                "month": cl["claim_month"],
                "service": cl["service_date"][:10],
                "procedure": pname[cl["procedure_code"]],
                "amount": cl["claim_amount"],
                "status": cl["status"],
                "paid": cl["paid_amount"],
                "adjust": cl["adjust_note"],
            }
            for cl in claims_by[pid]
        ],
        "plans": [
            {
                "procedure": pname[i["procedure_code"]],
                "tooth": i["tooth_no"],
                "status": i["status"],
                "est_fee": i["est_fee"],
                "created": i["created_at"][:10],
            }
            for i in items_by[pid]
        ],
        "messages": [
            {
                "at": m["sent_at"][:16],
                "dir": m["direction"],
                "trigger": m["trig"],
                "body": m["body"],
            }
            for m in msgs_by[pid][-30:]
        ],
    }

# ── 슬롯 ⑤ 조정 목록 ──
adjust = [
    {
        "chart_note": notes.get(cl["treatment_id"], {}).get("note"),
        "service": cl["service_date"][:10],
        "month": cl["claim_month"],
        "chart_no": cl["chart_no"],
        "name": pats[cl["patient_id"]]["name"],
        "procedure": pname[cl["procedure_code"]],
        "tooth": cl["tooth_no"],
        "claimed": cl["claim_amount"],
        "paid": cl["paid_amount"],
        "status": cl["status"],
        "code": cl["adjust_code"],
        "note": cl["adjust_note"],
        "reviewed": (cl["reviewed_at"] or "")[:10],
    }
    for cl in rows(
        "SELECT * FROM emr_claims WHERE status IN ('adjusted','denied') AND claim_month >= ? ORDER BY reviewed_at DESC",
        ADJUST_FROM,
    )
][:60]

# ── 슬롯 ⑧-1 일계표 (평가 달 8~11월) ──
ledger = defaultdict(lambda: defaultdict(int))
for x in rows(
    "SELECT paid_at, method, amount_paid FROM emr_payments WHERE paid_at >= ? AND paid_at < ?",
    RECON_FROM,
    TODAY.isoformat(),
):
    ledger[x["paid_at"][:10]][x["method"]] += x["amount_paid"]
card = defaultdict(int)
for x in rows(
    "SELECT approved_at, amount FROM fin_card_sales WHERE approved_at >= ? AND approved_at < ?",
    RECON_FROM,
    TODAY.isoformat(),
):
    card[x["approved_at"][:10]] += x["amount"]
mism = defaultdict(list)
for m in rows("SELECT occurred_on, cause FROM truth.truth_recon_mismatches"):
    mism[m["occurred_on"]].append(m["cause"])
recon = [
    {
        "date": d,
        "ledger": dict(v),
        "card_approved": card.get(d, 0),
        "mismatch_count": len(mism.get(d, [])),
    }
    for d, v in sorted(ledger.items())
]

# ── 슬롯 ⑨ 후속 관리 후보 ──
follow = []
for pid in pats:
    lh = last_hygiene(pid, TODAY.isoformat())
    future = any(
        a["scheduled_at"][:10] >= TODAY.isoformat() and a["status"] != "cancelled"
        for a in appts_by[pid]
    )
    if future:
        continue
    chips = []
    if lh and date.fromisoformat(lh) + RECALL_CYCLE < TODAY:
        chips.append("리콜 연체")
    if scaling_unused(pid, TODAY.isoformat()) and lh:
        chips.append("올해 스케일링 급여 미사용")
    past = [a for a in appts_by[pid] if a["scheduled_at"][:10] < TODAY.isoformat()]
    if past and past[-1]["status"] == "noshow":
        chips.append("노쇼 뒤 재예약 없음")
    b = balance(pid, TODAY.isoformat())
    if b > 0:
        chips.append("미수 잔액")
    if any(i["status"] == "open" for i in items_by[pid]):
        chips.append("진료계획 미예약")
    if len(chips) >= 2:
        last = max(
            (a["scheduled_at"][:10] for a in past if a["status"] == "completed"), default=None
        )
        follow.append(
            {
                "patient": pinfo(pid),
                "chips": chips,
                "balance": b,
                "last_visit": last,
                "last_hygiene": lh,
            }
        )
follow.sort(key=lambda x: (-len(x["chips"]), x["last_visit"] or ""))
follow = follow[:60]

out = {
    "meta": {
        "clinic": CLINIC_NAME,
        "today": TODAY.isoformat(),
        "now": "08:50",
        "staff": [{"name": s["name"], "role": ROLE_KO[s["role"]]} for s in staff.values()],
        "note": "가상 데이터(생성기 출력). AI 결과 칸은 정답 라벨로 채운 예시",
    },
    "prep": prep,
    "calls": calls,
    "docs": docs,
    "ask_examples": ask_examples,
    "emr": emr,
    "slots": {"adjust": adjust, "recon": recon, "follow": follow},
    "enums": {
        "purposes": list(PURPOSE_KO.values()),
        "counterparts": list(COUNTERPART_KO.values()),
        "urgency": ["급함", "보통", "낮음"],
        "flags": ["응급", "불만", "가격 문의", "예약 희망", "예약 확정", "할 일 있음"],
    },
}
json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
print(
    {k: (len(v) if hasattr(v, "__len__") else v) for k, v in out.items() if k != "meta"},
    "bytes",
    len(json.dumps(out, ensure_ascii=False)),
)
