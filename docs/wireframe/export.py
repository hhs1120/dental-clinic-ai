"""와이어프레임(docs/wireframe/index-v2.html·index.html)용 데이터 — data/source.sqlite(+ 정답 DB 일부: AI 결과 칸 예시)에서 화면별로 뽑아
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

from generator.master import (  # noqa: E402
    ACCOUNTS,
    ADJUST_REASONS,
    CLINIC_NAME,
    PROCEDURE_DIAGNOSES,
    RECALL_CYCLE,
    SCALING_CODE,
    SCALING_MIN_AGE,
    VENDORS,
)

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "docs" / "wireframe" / "data.json"
TODAY = date.fromisoformat(
    str(yaml.safe_load((ROOT / "config" / "generator.yaml").read_text())["today"])
)
CALLS_FROM = (TODAY - timedelta(days=47)).isoformat()  # 통화 목록 — 최근 약 7주
RECON_FROM = (TODAY - timedelta(days=122)).isoformat()[:8] + "01"  # 재무 대조 — 최근 4개월
ADJUST_FROM = (TODAY - timedelta(days=183)).isoformat()[:7]  # 청구 점검 — 최근 6개월
c = sqlite3.connect(f"file:{ROOT}/data/source.sqlite?mode=ro", uri=True)  # 읽기만
c.row_factory = sqlite3.Row
c.execute(f"ATTACH 'file:{ROOT}/ground_truth/truth.sqlite?mode=ro' AS truth")


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
                "confirmed": bool(a["confirmed"]),
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
    # silver만 — 판정 풀(dev·test) 통화에 골드 라벨을 보이면 사람 판정이 오염된다(평가 설계 §1)
    " WHERE c.started_at >= ? AND l.split = 'silver' ORDER BY c.started_at DESC",
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
    and x["split"] == "dev"  # test 문항은 보이지 않는다
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
recon = [
    {
        "date": d,
        "ledger": dict(v),
        "card_approved": card.get(d, 0),
        # 정답이 아니라 카드 합계 대조만(test 달 격리). 이체·정산 대조는 ⑧ 코드 몫
        "card_diff_day": v.get("card", 0) != card.get(d, 0),
    }
    for d, v in sorted(ledger.items())
]

# ── 슬롯 ⑤-2 청구 전 점검 — 정답 저장소를 쓰지 않는다(⑤ 평가셋 분할 전). 규칙 칸만 실제 값, AI 칸은 자리 ──
dx_name = {r["code"]: r["name"] for r in rows("SELECT * FROM emr_diagnoses")}
dx_notes = [r["note"] for r in ADJUST_REASONS.values() if r["type"] == "diagnosis"]
ph = ",".join("?" * len(dx_notes))
dx_adjusted = rows(
    f"SELECT cl.*, n.dx_code FROM emr_claims cl JOIN emr_chart_notes n USING (treatment_id)"
    f" WHERE cl.adjust_note IN ({ph}) ORDER BY cl.reviewed_at",
    *dx_notes,
)
combo = defaultdict(lambda: {"n": 0, "lost": 0})
for r in dx_adjusted:
    k = (r["procedure_code"], r["dx_code"])
    combo[k]["n"] += 1
    combo[k]["lost"] += r["claim_amount"] - r["paid_amount"]
pending = rows(
    "SELECT cl.*, n.dx_code, n.note FROM emr_claims cl"
    " LEFT JOIN emr_chart_notes n USING (treatment_id)"
    " WHERE cl.status = 'pending' ORDER BY cl.service_date, cl.id"
)
precheck = []
for cl in pending:
    allowed = PROCEDURE_DIAGNOSES.get(cl["procedure_code"])
    wrong = bool(allowed and cl["dx_code"] and cl["dx_code"] not in allowed)
    past = combo.get((cl["procedure_code"], cl["dx_code"]), {"n": 0, "lost": 0})
    precheck.append(
        {
            "service": cl["service_date"][:10],
            "chart_no": cl["chart_no"],
            "name": pats[cl["patient_id"]]["name"],
            "procedure": pname[cl["procedure_code"]],
            "tooth": cl["tooth_no"],
            "amount": cl["claim_amount"],
            "dx": dx_name.get(cl["dx_code"]),
            "dx_wrong": wrong,
            "past_n": past["n"] if wrong else 0,
            "past_lost": past["lost"] if wrong else 0,
            "note": cl["note"],
        }
    )

# ── 슬롯 ⑥ 경영 리포트 — 달별 상병 착오(통보 월 기준) ──
report = defaultdict(lambda: {"n": 0, "lost": 0, "combos": defaultdict(lambda: [0, 0])})
for r in dx_adjusted:
    m = report[r["reviewed_at"][:7]]
    m["n"] += 1
    m["lost"] += r["claim_amount"] - r["paid_amount"]
    x = m["combos"][f"{pname[r['procedure_code']]} × {dx_name[r['dx_code']]}"]
    x[0] += 1
    x[1] += r["claim_amount"] - r["paid_amount"]
report = [
    {
        "month": k,
        "n": v["n"],
        "lost": v["lost"],
        "top": max(v["combos"].items(), key=lambda t: (t[1][0], t[1][1], t[0])),
    }
    for k, v in sorted(report.items())
    if k < TODAY.isoformat()[:7]
][-6:]

# ── 슬롯 ⑥ 경영 리포트 — 매출·수금·노쇼·신환·재료비는 쿼리, 인건비는 가정값으로 만든 [추정] ──
# 직원 1인 월 인건비 가정값(원, 4대보험 포함). 실제 앱은 ax_settings. 대표 원장 보수는 넣지 않는다
WAGE_ASSUMED = {"hygienist": 3_800_000, "desk": 3_000_000, "consultant": 3_800_000}
labor_est = sum(WAGE_ASSUMED.get(s["role"], 0) for s in staff.values() if s["active"])
VENDOR_ACCOUNT = {}
for v in VENDORS:
    VENDOR_ACCOUNT[v["name"]] = v["account"]
    if v.get("card_name"):
        VENDOR_ACCOUNT[v["card_name"]] = v["account"]
sales_rows = rows(
    "SELECT substr(performed_at, 1, 10) d, fee_total v FROM emr_treatments WHERE performed_at < ?",
    TODAY.isoformat(),
)
patient_paid = rows(
    "SELECT substr(paid_at, 1, 10) d, amount_paid v FROM emr_payments WHERE paid_at < ?",
    TODAY.isoformat(),
)
insurer_paid = rows(
    "SELECT substr(reviewed_at, 1, 10) d, paid_amount v FROM emr_claims"
    " WHERE reviewed_at IS NOT NULL AND reviewed_at < ?",
    TODAY.isoformat(),
)
appts_done = rows(
    "SELECT substr(scheduled_at, 1, 10) d, status FROM emr_appointments"
    " WHERE scheduled_at < ? AND status != 'cancelled'",
    TODAY.isoformat(),
)
new_pats = rows(
    "SELECT substr(registered_at, 1, 10) d FROM emr_patients WHERE registered_at < ?",
    TODAY.isoformat(),
)
spend = [
    {"d": r["occurred_at"][:10], "who": r["memo"], "v": r["amount"]}
    for r in rows("SELECT * FROM fin_bank_transactions WHERE direction = 'out'")
] + [
    {"d": r["used_at"][:10], "who": r["merchant"], "v": r["amount"]}
    for r in rows("SELECT * FROM fin_card_expenses")
]


def bucket(key):
    """기간 키(날짜 → 라벨)별로 운영 숫자를 묶는다."""
    b = defaultdict(lambda: defaultdict(int))
    for src, name in (
        (sales_rows, "sales"),
        (patient_paid, "patient_paid"),
        (insurer_paid, "insurer_paid"),
    ):
        for r in src:
            b[key(r["d"])][name] += r["v"]
    for r in appts_done:
        b[key(r["d"])]["visits"] += 1
        b[key(r["d"])]["noshow"] += r["status"] == "noshow"
    for r in new_pats:
        b[key(r["d"])]["new_patients"] += 1
    return b


def op_row(label, v):
    return {
        "label": label,
        "sales": v["sales"],
        "collected": v["patient_paid"] + v["insurer_paid"],
        "patient_paid": v["patient_paid"],
        "insurer_paid": v["insurer_paid"],
        "visits": v["visits"],
        "noshow": v["noshow"],
        "new_patients": v["new_patients"],
    }


by_month = bucket(lambda d: d[:7])
months_cost = defaultdict(lambda: defaultdict(int))
for x in spend:
    months_cost[x["d"][:7]][VENDOR_ACCOUNT.get(x["who"], "기타")] += x["v"]
monthly = []
for k in sorted(k for k in by_month if k < TODAY.isoformat()[:7])[-6:]:
    r = op_row(k, by_month[k])
    cost = months_cost[k]
    outflow = sum(cost.values())
    variable = cost["재료비"] + cost["외주가공비"]
    fixed = outflow - variable + labor_est
    rate = variable / r["sales"] if r["sales"] else 0
    monthly.append(
        r
        | {
            "material": cost["재료비"],
            "lab": cost["외주가공비"],
            "outflow": outflow,
            "profit_ex_labor": r["collected"] - outflow,
            "labor_est": labor_est,
            "profit_est": r["collected"] - outflow - labor_est,
            "bep_est": round(fixed / (1 - rate)) if rate < 1 else None,
        }
    )


def week_of(d):
    x = date.fromisoformat(d)
    return (x - timedelta(days=x.weekday())).isoformat()


weekly = [
    op_row(k, v)
    for k, v in sorted(bucket(week_of).items())
    if date.fromisoformat(k) + timedelta(days=7) <= TODAY
][-8:]
daily = [op_row(k, v) for k, v in sorted(bucket(lambda d: d).items()) if v["visits"]][-10:]
biz = {
    "monthly": monthly,
    "weekly": weekly,
    "daily": daily,
    "wage": {ROLE_KO[k]: v for k, v in WAGE_ASSUMED.items()},
    "headcount": {
        ROLE_KO[r]: sum(1 for s in staff.values() if s["active"] and s["role"] == r)
        for r in WAGE_ASSUMED
    },
}

# ── 슬롯 ⑧-2 지출 증빙 — 지난달 출금. 금액·날짜 규칙으로 찾은 후보만(기준선), 계정 과목은 사람·AI 칸 ──
EXP_MONTH = max(r["month"] for r in report)
docs_exp = rows(
    "SELECT * FROM fin_expense_documents WHERE issued_on >= date(? || '-01', '-5 days')"
    " AND issued_on < date(? || '-01', '+1 month')",
    EXP_MONTH,
    EXP_MONTH,
)
outflows = [
    {"at": r["occurred_at"][:10], "source": "통장", "who": r["memo"], "amount": r["amount"]}
    for r in rows(
        "SELECT * FROM fin_bank_transactions WHERE direction = 'out' AND substr(occurred_at, 1, 7) = ?"
        " ORDER BY occurred_at, id",
        EXP_MONTH,
    )
] + [
    {"at": r["used_at"][:10], "source": "병원 카드", "who": r["merchant"], "amount": r["amount"]}
    for r in rows(
        "SELECT * FROM fin_card_expenses WHERE substr(used_at, 1, 7) = ? ORDER BY used_at, id",
        EXP_MONTH,
    )
]
expense = []
for o in sorted(outflows, key=lambda x: x["at"]):
    one = [d for d in docs_exp if d["amount"] == o["amount"] and d["issued_on"] <= o["at"]]
    one = sorted(one, key=lambda d: d["issued_on"], reverse=True)[:1]
    same = [d for d in docs_exp if d["issuer"] == o["who"] and d["issued_on"][:7] == EXP_MONTH]
    sum_ok = not one and len(same) > 1 and sum(d["amount"] for d in same) == o["amount"]
    found = one or (same if sum_ok else [])
    expense.append(
        o
        | {
            "match": "1:N 합계" if sum_ok else "1:1" if one else None,
            "docs": [
                {
                    "kind": d["kind"],
                    "issuer": d["issuer"],
                    "on": d["issued_on"],
                    "amount": d["amount"],
                }
                for d in found
            ],
            "body": found[0]["body"] if found else None,
        }
    )

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
    "slots": {
        "adjust": adjust,
        "recon": recon,
        "follow": follow,
        "precheck": precheck,
        "report": report,
        "biz": biz,
        "expense": expense,
        "accounts": list(ACCOUNTS),
    },
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
