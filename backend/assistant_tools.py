"""AI 비서 도구 모음 (AI 비서 설계 §4.2) — 하네스 도구 단계에서 쓰는 조회·초안 도구.

조회는 그 화면이 쓰는 함수(calls·prep·claims·recon·missed·emr_bridge)를 그대로 불러 숫자가 화면과 어긋나지
않게 하고, 결과는 요약 필드를 앞에 둔 짧은 모양으로 돌려준다. 쓰기는 `draft_appointment` 하나 — emr_bridge
초안만 만들고 승인은 사람(§5.2). 도구는 LLM을 부르지 않는다(Tool 규칙) — `ask_records`는 질문을 남기기만 하고
③ `chat.chat` 호출은 도구 단계 뒤 턴이 한다. 도구 이름·인자는 `TOOLS_VERSION`으로 고정(§7.1)."""

import datetime as dt
from typing import Literal

from pydantic import BaseModel, Field

from backend import calls, claims, emr_bridge, missed, prep, recon
from backend.emr import _hours
from backend.harness.tools import Tool
from backend.query_tools import _procedure_codes, query_tools
from shared.clinic import Clinic

TOOLS_VERSION = "v2"  # v2: free_slots에 date_to(기간을 한 번에)
ROLES = {"원장": "doctor", "위생사": "hygienist", "상담실장": "consultant"}
LIST_CAP = 20  # 목록 도구가 한 번에 돌려주는 행 수 — count는 전체
CLAIM_KINDS = ("appeal_soon", "adjusted", "pre_claim_warning")


class FindCallsArgs(BaseModel):
    date_from: dt.date
    date_to: dt.date
    wants_appt: bool | None = None
    chart_no: str | None = None


class RangeArgs(BaseModel):
    date_from: dt.date
    date_to: dt.date


FREE_SPAN_DAYS = 31  # free_slots 기간 조회 한 번에 보는 최대 날 수(한 달)


class FreeSlotsArgs(BaseModel):
    date: dt.date
    date_to: dt.date | None = None
    procedure: str = Field(min_length=1)
    staff: Literal["원장", "위생사", "상담실장"] | None = None


class DayArgs(BaseModel):
    date: dt.date


class ClaimItemsArgs(BaseModel):
    kind: Literal["appeal_soon", "adjusted", "pre_claim_warning"]


class ReconArgs(BaseModel):
    month: str = Field(pattern=r"^\d{4}-\d{2}$")


class MissedArgs(BaseModel):
    chip: (
        Literal["recall", "scaling_unused", "noshow", "balance", "open_plan", "other_clinic"]
        | None
    ) = None
    limit: int = Field(default=5, ge=1, le=10)


class AskArgs(BaseModel):
    question: str = Field(min_length=1)


class DraftArgs(BaseModel):
    chart_no: str
    date: dt.date
    start: str = Field(pattern=r"^\d{2}:\d{2}$")
    staff: str = Field(min_length=1)
    procedure: str = Field(min_length=1)
    source_call_uid: str | None = None


class NoArgs(BaseModel):
    pass


def _procedure(conn, name: str) -> tuple[str | None, dict | None]:
    """시술 이름·별칭 → (코드, None) 또는 (None, 오류). 이름이 둘 이상에 걸리면 후보를 돌려준다"""
    codes, names, candidates = _procedure_codes(conn, name)
    if len(codes) == 1:
        return codes[0], None
    if codes or candidates:
        return None, {"error": ["ambiguous_procedure"], "candidates": names or candidates}
    return None, {"error": ["unknown_procedure"]}


def _find_calls(conn, args: dict) -> dict:
    where = "date(calls.at) BETWEEN :f AND :t"
    params = {"f": args["date_from"], "t": args["date_to"]}
    if args["chart_no"]:
        where += " AND calls.chart_no = :c"
        params["c"] = args["chart_no"]
    rows = calls._rows(conn, where, params)
    checks = calls._checks(conn, [r["classification_id"] for r in rows if r["classification_id"]])
    out = []
    for r in rows:
        labels = calls._labels(r)
        wants = bool(labels["wants_appt"]) if labels else None
        if args["wants_appt"] is not None and wants != args["wants_appt"]:
            continue
        card = calls._card(r, checks)
        out.append(
            {
                "call_uid": card["call_uid"],
                "started_at": card["at"],
                "chart_no": card["chart_no"],
                "name": card["name"],
                "purpose": card["purpose"],
                "wants_appt": wants,
                "summary": card["summary"],
                # 통화 화면 할 일과 같은 목록 — "다시 연락할 곳"을 요약 문장에서 짐작하지 않게(contact = 연락)
                "todo": [
                    {"kind": t["kind"], "who": t["who"], "text": t["text"], "done": t["done"]}
                    for t in calls._todo(r, checks)
                ],
            }
        )
    return {"count": len(out), "calls": out[:LIST_CAP]}


def _unbooked(conn, args: dict) -> dict:
    rows = emr_bridge.unbooked_requests(
        conn, dt.date.fromisoformat(args["date_from"]), dt.date.fromisoformat(args["date_to"])
    )
    return {"count": len(rows), "patients": rows[:LIST_CAP]}


def _day_slots(clinic: Clinic, conn, day: dt.date, code: str, staff: str | None) -> list | None:
    """그날 빈 시작 시각×담당(시각·담당 순), 휴진·공휴일이면 None"""
    mv = prep.master_values(conn)
    if _hours(conn, day)["closed"] or day.isoformat() in mv["bank_holidays"]:
        return None
    ids = [None]
    if staff:
        ids = [s["id"] for s in emr_bridge._staff(conn) if s["role"] == ROLES[staff]]
    slots = [s for i in ids for s in emr_bridge.free_slots(conn, clinic, day, code, i)]
    return sorted(slots, key=lambda s: (s["start"], s["staff_id"]))


def _free_slots(clinic: Clinic, conn, args: dict) -> dict:
    """date 하루면 그날 빈 시각 전부. date_to가 있으면 그 기간(최대 FREE_SPAN_DAYS일)을 한 번에 — 휴진일은 건너뛰고
    날마다 빈 시각 수, 가까운 순 빈 시각 LIST_CAP개(날짜를 붙여). "다음 주에 잡아 줘"를 날마다 부르지 않게"""
    code, err = _procedure(conn, args["procedure"])
    if err:
        return err
    first = dt.date.fromisoformat(args["date"])
    if not args.get("date_to"):
        slots = _day_slots(clinic, conn, first, code, args["staff"])
        return {"date": first.isoformat(), "closed": slots is None, "slots": slots or []}
    last = min(dt.date.fromisoformat(args["date_to"]), first + dt.timedelta(FREE_SPAN_DAYS - 1))
    days, found = [], []
    for n in range((last - first).days + 1):
        day = first + dt.timedelta(n)
        slots = _day_slots(clinic, conn, day, code, args["staff"])
        days.append({"date": day.isoformat(), "closed": slots is None, "free": len(slots or [])})
        found += [{"date": day.isoformat(), **s} for s in slots or []]
    return {
        "date": first.isoformat(),
        "date_to": last.isoformat(),
        "days": days,
        "count": len(found),
        "slots": found[:LIST_CAP],
    }


def _prep_rows(clinic: Clinic, conn, args: dict) -> dict:
    rows = prep.prep_facts(
        conn, dt.date.fromisoformat(args["date"]), clinic.today, include_ax=True
    )
    return {
        "count": len(rows),
        "rows": [
            {
                "time": r["time"],
                "chart_no": r["patient"]["chart_no"],
                "name": r["patient"]["name"],
                "procedure": r["procedure"],
                "chips": r["chips"],
            }
            for r in rows[:LIST_CAP]
        ],
    }


def _claim_items(clinic: Clinic, conn, args: dict) -> dict:
    kind = args["kind"]
    if kind == "pre_claim_warning":
        rows = [r for r in claims.pre_claim(conn, clinic.today)["rows"] if r["warning"]]
        keep = ("claim_id", "service_date", "chart_no", "name", "procedure_name", "claim_amount")
        items = [{k: r[k] for k in keep} | {"dx_name": r["dx_name"], **r["warning"]} for r in rows]
    else:
        rows = claims.adjusted(conn, clinic.today)["rows"]
        if (
            kind == "appeal_soon"
        ):  # 청구 점검 soon 칩·오늘 화면과 같은 기준 — 상병 착오는 재청구할 수 없어 뺀다
            rows = [r for r in rows if "soon" in r["chips"]]
        keep = ("claim_id", "chart_no", "name", "procedure_name", "adjust_note", "unpaid")
        items = [
            {k: r[k] for k in keep}
            | {"deadline": r["deadline"], "days_left": r["days_left"], "expired": r["expired"]}
            for r in rows
        ]
    return {"kind": kind, "count": len(items), "items": items[:LIST_CAP]}


def _recon_days(clinic: Clinic, conn, args: dict) -> dict:
    try:
        led = recon.ledger(conn, clinic.today, args["month"])
    except ValueError:
        return {"error": ["no_books"], "months": recon.ledger(conn, clinic.today)["months"]}
    days = [
        {
            "date": d["date"],
            "count": len(d["items"]),
            "items": [
                {
                    "key": i["key"],
                    "channel": i["channel"],
                    "ledger_total": i["ledger_total"],
                    "fin_total": i["fin_total"],
                    "rule_cause": i["rule_cause"],
                    "confirmed": i["confirmation"] is not None or i["auto"] is not None,
                }
                for i in d["items"]
            ],
        }
        for d in led["days"]
        if d["items"]
    ]
    return {"month": led["month"], "mismatch_days": len(days), "days": days[:LIST_CAP]}


def _missed(clinic: Clinic, conn, args: dict) -> dict:
    sheet = missed.sheet(conn, clinic.today, dt.datetime.now())
    rows = [r for r in sheet["rows"] if not args["chip"] or args["chip"] in r["chips"]]
    keep = ("rank", "chart_no", "name", "last_visit", "balance", "chips", "score")
    return {
        "count": len(rows),
        # 최근 연락(결과·시각·감점 기간 남음 active) — 화면이 보여 주는 연락 기록, 방금 연락한 사람을 다시 권하지 않게
        "patients": [
            {k: r[k] for k in keep}
            | {
                "contact": r["contact"]
                and {k: r["contact"][k] for k in ("result", "at", "active")}
            }
            for r in rows[: args["limit"]]
        ],
    }


def _draft(clinic: Clinic, conn, turn: dict, args: dict) -> dict:
    """근거 통화(source_call_uid)는 있는 통화여야 한다 — 생성 통화(source.ax_calls)나 새로 추가한 통화
    (ax_pasted_calls, find_calls가 같이 돌려준다). 없으면 {error: [unknown_call]}(행 없음, 모델이 고쳐 다시
    부르거나 통화 없이 만든다). 지어낸 통화 id가 초안·카드 링크에 남지 않게"""
    code, err = _procedure(conn, args["procedure"])
    if err:
        return err
    uid = args["source_call_uid"]
    if (
        uid is not None
        and not conn.execute(
            "SELECT 1 FROM source.ax_calls WHERE call_uid = :u"
            " UNION ALL SELECT 1 FROM ax_pasted_calls WHERE call_uid = :u",
            {"u": uid},
        ).fetchone()
    ):
        return {"error": ["unknown_call"]}
    patient = conn.execute(
        "SELECT id FROM source.emr_patients WHERE chart_no = ?", (args["chart_no"],)
    ).fetchone()
    if patient is None:
        return {"error": ["unknown_patient"]}
    staff = emr_bridge._staff(conn)
    role = ROLES.get(args["staff"])
    cands = [s for s in staff if (s["role"] == role if role else s["name"] == args["staff"])]
    if not cands:
        return {"error": ["unknown_staff"]}
    start = dt.datetime.combine(
        dt.date.fromisoformat(args["date"]), dt.time.fromisoformat(args["start"])
    )
    first = None
    for s in cands:  # 역할이면 검사를 통과하는 첫 담당 — 실패는 행을 남기지 않는다
        got = emr_bridge.create_draft(
            conn,
            clinic,
            patient_id=patient["id"],
            staff_id=s["id"],
            start=start,
            procedure_code=code,
            source_call_uid=args["source_call_uid"],
            conversation_id=turn["conversation_id"],
            turn=turn["turn"],
        )
        if "error" not in got:
            return got
        first = first or got
    return first


def assistant_tools(clinic: Clinic, searcher, ctx_turn: dict) -> list[Tool]:
    """searcher는 ③ 위임용 자리 — 도구는 쓰지 않는다(ask_records는 질문만 남긴다). ctx_turn = 초안에 남길
    {conversation_id, turn}"""
    today = clinic.today.isoformat()
    conn = lambda ctx: ctx.store.conn  # noqa: E731
    return [
        *(t for t in query_tools(clinic, include_ax=True) if t.name != "refuse"),
        Tool(
            "find_calls",
            f"기간(YYYY-MM-DD, 오늘 {today})의 통화 목록 — 목적·요약·예약 원함·할 일(kind contact = 연락할"
            " 사람에게 할 일, internal = 안에서 할 일, done = 끝냄). 차트번호·예약 원함으로 거른다.",
            FindCallsArgs,
            lambda ctx, a: _find_calls(conn(ctx), a),
        ),
        Tool(
            "unbooked_requests",
            f"예약을 원했는데 그 뒤 예약이 없는 환자(통화 기간 YYYY-MM-DD, 오늘 {today}).",
            RangeArgs,
            lambda ctx, a: _unbooked(conn(ctx), a),
        ),
        Tool(
            "free_slots",
            f"시술이 들어갈 빈 시작 시각×담당(date YYYY-MM-DD, 오늘 {today}). 하루면 date만, '다음 주'처럼"
            " 기간이면 date~date_to를 한 번에(휴진일 건너뜀, 날마다 빈 수 days, 가까운 순 slots)."
            " 예약은 이 안에서만 고른다.",
            FreeSlotsArgs,
            lambda ctx, a: _free_slots(clinic, conn(ctx), a),
        ),
        Tool(
            "prep_rows",
            f"그날 준비표 행(시각·환자·시술·주의 칩). date YYYY-MM-DD, 오늘 {today}.",
            DayArgs,
            lambda ctx, a: _prep_rows(clinic, conn(ctx), a),
        ),
        Tool(
            "claim_items",
            "청구 목록 — appeal_soon(재심 기한 임박)·adjusted(조정·불능 전부)·pre_claim_warning(청구 전 상병 경고).",
            ClaimItemsArgs,
            lambda ctx, a: _claim_items(clinic, conn(ctx), a),
        ),
        Tool(
            "recon_days",
            "그 달(YYYY-MM) 일계표에서 불일치가 있는 날·건수·규칙 원인.",
            ReconArgs,
            lambda ctx, a: _recon_days(clinic, conn(ctx), a),
        ),
        Tool(
            "missed_patients",
            "미예약 환자 상위(칩으로 거름, 최대 10명). contact = 최근 연락(active면 감점 기간이라 다시 연락을"
            " 권하지 않는다).",
            MissedArgs,
            lambda ctx, a: _missed(clinic, conn(ctx), a),
        ),
        Tool(
            "ask_records",
            "병원 기록·문서·규정 질문을 기록 검색 에이전트에 맡긴다. 답은 도구 단계 뒤에 붙는다.",
            AskArgs,
            lambda ctx, a: {"queued": a["question"]},
        ),
        Tool(
            "draft_appointment",
            "예약 초안을 만든다(확정 아님, 사람이 승인). 시각은 free_slots 결과에서. staff는 담당 이름이나"
            " 역할(원장·위생사·상담실장). 실패하면 {error: [코드]}.",
            DraftArgs,
            lambda ctx, a: _draft(clinic, conn(ctx), ctx_turn, a),
            writes=True,
        ),
        Tool(
            "done_tools",
            "조회를 끝낸다. 답은 다음 단계가 쓴다.",
            NoArgs,
            lambda ctx, a: None,
            ends_run=lambda a: "done",
        ),
    ]
