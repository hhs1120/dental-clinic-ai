"""AI 비서 도구 모음 (AI 비서 설계 §4.2) — 하네스 도구 단계에서 쓰는 조회·초안 도구.

조회는 그 화면이 쓰는 함수(calls·prep·claims·recon·missed·emr_bridge)를 그대로 불러 숫자가 화면과 어긋나지
않게 하고, 결과는 요약 필드를 앞에 둔 짧은 모양으로 돌려준다. 쓰기는 `draft_appointment` 하나 — emr_bridge
초안만 만들고 승인은 사람(§5.2). 도구는 LLM을 부르지 않는다(Tool 규칙) — `ask_records`는 질문을 남기기만 하고
③ `chat.chat` 호출은 도구 단계 뒤 턴이 한다. 도구 이름·인자는 `TOOLS_VERSION`으로 고정(§7.1)."""

import datetime as dt
import json
from typing import Literal

from pydantic import BaseModel, Field

from backend import calls, claims, emr_bridge, missed, prep, recon
from backend.emr import _hours
from backend.harness.tools import Tool
from backend.query_tools import _procedure_codes, query_tools
from shared.clinic import Clinic

TOOLS_VERSION = "v5"  # v2: free_slots에 date_to(기간을 한 번에) · v3: draft_appointment에 window(시간대) · v4: prep_rows 칩 뜻, 초안 전 free_slots 불필요 · v5: 상담은 free_chairs 없음, wrong_role·bad_range·before_today
ROLES = {"원장": "doctor", "위생사": "hygienist", "상담실장": "consultant"}
ROLE_NAMES = {v: k for k, v in ROLES.items()}
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
    staff: str | None = None  # 역할(원장·위생사·상담실장)이나 담당 이름


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
    start: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    window: Literal["오전", "오후"] | None = (
        None  # start가 없을 때 — 이 시간대의 가장 이른 빈 시각을 코드가 고른다
    )
    staff: str | None = None  # 이름이나 역할, 없으면 그 시술 역할 중 자동
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


def _staff_ids(conn, staff: str | None) -> list | None:
    """역할이면 그 역할 담당들, 이름이면 그 사람, 없으면 [None](시술 역할 전부). 모르는 이름이면 None"""
    if not staff:
        return [None]
    role = ROLES.get(staff)
    ids = [
        s["id"]
        for s in emr_bridge._staff(conn)
        if (s["role"] == role if role else s["name"] == staff)
    ]
    return ids or None


def _day_slots(clinic: Clinic, conn, day: dt.date, code: str, ids: list) -> list | None:
    """그날 빈 시작 시각(시각 순) — 같은 시각은 한 줄로, 그 시각 빈 체어 수(번호는 초안 때 자동, 체어를 쓰지 않는 상담은
    없음). 담당은 초안 때 자동(이름을 주면 그 사람)이라 보이지 않는다. 휴진·공휴일이면 None"""
    if day_off(conn, day):
        return None
    chairs = emr_bridge.appointment_type(conn, code) != "consult"
    by_time: dict[str, dict] = {}
    for i in ids:
        for s in emr_bridge.free_slots(conn, clinic, day, code, i):
            row = {"start": s["start"], "end": s["end"]}
            by_time.setdefault(
                s["start"], row | {"free_chairs": len(s["chairs"])} if chairs else row
            )
    return [by_time[t] for t in sorted(by_time)]


def _wrong_role(conn, code: str, ids: list) -> bool:
    """이름·역할로 고른 담당 중 그 시술을 맡는 역할이 없음(스케일링을 원장으로 등) — 빈 시각 없음과 가른다"""
    need = emr_bridge.ROLE_FOR_TYPE[emr_bridge.appointment_type(conn, code)]
    roles = {s["id"]: s["role"] for s in emr_bridge._staff(conn)}
    return None not in ids and all(roles.get(i) != need for i in ids)


def _free_slots(clinic: Clinic, conn, args: dict) -> dict:
    """date 하루면 그날 빈 시각 전부. date_to가 있으면 그 기간(최대 FREE_SPAN_DAYS일)을 한 번에 — 휴진일은 건너뛰고
    날마다 빈 시각 수, 가까운 순 빈 시각 LIST_CAP개(날짜를 붙여). "다음 주에 잡아 줘"를 날마다 부르지 않게"""
    code, err = _procedure(conn, args["procedure"])
    if err:
        return err
    ids = _staff_ids(conn, args["staff"])
    if ids is None:
        return {"error": ["unknown_staff"]}
    if _wrong_role(conn, code, ids):
        return {"error": ["wrong_role"]}
    first = dt.date.fromisoformat(args["date"])
    if args.get("date_to") and args["date_to"] < args["date"]:
        return {"error": ["bad_range"]}
    if not args.get("date_to"):
        slots = _day_slots(clinic, conn, first, code, ids)
        return {"date": first.isoformat(), "closed": slots is None, "slots": slots or []}
    last = min(dt.date.fromisoformat(args["date_to"]), first + dt.timedelta(FREE_SPAN_DAYS - 1))
    days, found = [], []
    for n in range((last - first).days + 1):
        day = first + dt.timedelta(n)
        slots = _day_slots(clinic, conn, day, code, ids)
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
    want = (
        args["staff"]
        or ROLE_NAMES[emr_bridge.ROLE_FOR_TYPE[emr_bridge.appointment_type(conn, code)]]
    )
    role = ROLES.get(want)
    cands = [s for s in staff if (s["role"] == role if role else s["name"] == want)]
    if not cands:
        return {"error": ["unknown_staff"]}
    if _wrong_role(conn, code, [s["id"] for s in cands]):
        return {"error": ["wrong_role"]}
    day = dt.date.fromisoformat(args["date"])
    if args["start"]:
        tries = [(dt.time.fromisoformat(args["start"]), s) for s in cands]
    else:  # 시각을 말하지 않으면(시간대만·아무것도) 그 시간대 가장 이른 빈 시각부터 — 모델이 고르지 않는다
        if (
            day < clinic.today
        ):  # 빈 시각 없음(no_slot)과 가른다 — start가 있으면 create_draft가 낸다
            return {"error": ["before_today"]}
        if day_off(conn, day):
            return {"error": ["closed_day"]}
        tries = sorted(
            (
                (dt.time.fromisoformat(f["start"]), s)
                for s in cands
                for f in emr_bridge.free_slots(conn, clinic, day, code, s["id"])
                if _in_window(conn, f["start"], args["window"])
            ),
            key=lambda t: (t[0], t[1]["id"]),
        )
        if not tries:
            return {"error": ["no_slot"]}
    first = None
    for start, s in tries:  # 검사를 통과하는 첫 (시각, 담당) — 실패는 행을 남기지 않는다
        got = emr_bridge.create_draft(
            conn,
            clinic,
            patient_id=patient["id"],
            staff_id=s["id"],
            start=dt.datetime.combine(day, start),
            procedure_code=code,
            source_call_uid=args["source_call_uid"],
            conversation_id=turn["conversation_id"],
            turn=turn["turn"],
        )
        if "error" not in got:
            return got
        first = first or got
    return first


def day_off(conn, day: dt.date) -> str | None:
    """쉬는 날의 이름 — 공휴일(`bank_holidays`)이 휴진 요일보다 먼저, 진료일이면 None"""
    if day.isoformat() in prep.master_values(conn)["bank_holidays"]:
        return "공휴일"
    return "휴진일" if _hours(conn, day)["closed"] else None


def _in_window(conn, start: str, window: str | None) -> bool:
    """오전 = 점심 시작 전, 오후 = 점심 끝 뒤(병원 영업시간의 점심 — 토요일도 같은 경계), 없으면 하루 전부"""
    if window is None:
        return True
    lunch = json.loads(conn.execute("SELECT hours_json FROM emr_clinic").fetchone()[0])["lunch"]
    return start < lunch[0] if window == "오전" else start >= lunch[1]


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
            f"시술이 들어갈 빈 시작 시각(date YYYY-MM-DD, 오늘 {today}). 하루면 date만, '다음 주'처럼"
            " 기간이면 date~date_to를 한 번에(휴진일 건너뜀, 날마다 빈 시각 수 days, 가까운 순 slots)."
            " 시각마다 빈 체어 수 free_chairs(상담은 체어를 쓰지 않아 없음). staff는 역할이나 담당 이름 — 없으면"
            " 그 시술 역할 전부, 담당은 초안 때 자동. 그 시술을 맡지 않는 담당이면 {error: [wrong_role]}.",
            FreeSlotsArgs,
            lambda ctx, a: _free_slots(clinic, conn(ctx), a),
        ),
        Tool(
            "prep_rows",
            f"그날 준비표 행(시각·환자·시술·칩). date YYYY-MM-DD, 오늘 {today}. 칩: balance 미수 잔액 ·"
            " alert 그날 시술과 관련된 주의사항(병력·알레르기) · insurance 올해 스케일링 급여 미사용 ·"
            " family_recall 리콜이 지난 가족 · open_plan 남은 진료계획 · unconfirmed 예약 확인 안 됨(오늘만) ·"
            " appointment 예약. 미수·주의 질문은 이 칩으로 답한다(환자마다 다시 조회하지 않는다).",
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
            "예약 초안을 만든다(확정 아님, 사람이 승인). 직원이 시각을 말했으면 start(빈 시각인지 도구가 검사),"
            " 시각 없이 오전·오후만 말했으면 window, 둘 다 없으면 그날 가장 이른 빈 시각 — start가 없으면"
            " 코드가 고른다. staff는 담당 이름이나 역할(원장·위생사·상담실장), 없으면 그 시술 역할 중 자동."
            " 실패하면 {error: [코드]}(closed_day 휴진, no_slot 빈 시각 없음, wrong_role 그 시술을 맡지 않는 담당,"
            " before_today 지난 날짜).",
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
