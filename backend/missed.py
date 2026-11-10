"""⑨ 미예약 환자 (기능 정의 §5 ⑨, 설계 docs/superpowers/specs/2026-11-10-missed-patients-design.md).
다시 올 이유(칩)가 있는데 다음 예약이 없는 우리 환자를 코드가 모으고 "연락하면 결과(예약·수납)가 날 가능성" 원칙
점수로 줄 세운다. 사실은 그날 아침 기준(그날 전 사건만 — ④ prep_facts처럼, 지난 날짜도 다시 셀 수 있게).
규칙은 ④ 준비표 도우미(backend/prep.py `_scaling_unused`·`_open_plan`·`_family_recall`·잔액)와 같고, 예약 한 건
단위인 그 도우미 대신 표마다 한 번 훑는 집합 쿼리로 다시 쓴다 — 환자 수와 무관하게 쿼리 수가 같다(설계 Review Focus 1).
master 값은 source.ax_master_values로만 읽는다(CLAUDE.md)."""

import json
import math
import sqlite3
from datetime import date, datetime

from backend import db
from backend.prep import DAYS_PER_MONTH, FULL_MOUTH, _age, master_values

# 칩 순서 = 화면 순서(설계 §2). 화면 문구는 frontend/lib/missed-api.ts
CHIPS = ("recall", "scaling_unused", "noshow", "balance", "open_plan", "other_clinic")
# 다른 치과 추정의 문자 무응답 — 그날 전 마지막 답장 뒤 연달아 답 없는 안내 문자 수(설계 §2). 예약 한 건의 확정·
# 리마인드 둘은 답 없이 지나는 게 흔해(미예약 환자의 무응답 연속 1~2가 대부분) 그 뒤 안내까지 답이 없을 때부터 —
# 실데이터 기준일에 미예약 환자 약 5%. 정답(이탈·병행 라벨)은 보지 않고 정했다
NO_REPLY_RUN = 3
NO_REPLY_TRIGGERS = ("recall", "reminder", "appt_confirm")  # 답장을 기대하는 안내 문자(out)
PAGE = 10  # 화면 처음 몇 명·"더보기" 한 번에 더 보이는 수(기능 정의 §5 ⑨ 화면) — GET /missed가 같이 보낸다

# 점수 기본값(설계 §3·§5 — 원칙으로 정하고 값마다 이유를 REASONS에). 바꾼 값만 ax_settings.missed_weights
WEIGHTS: dict[str, float] = {
    "recent": 40,  # 마지막 방문 직후 점수 — 오래될수록 줄어든다
    "recent_half_life_months": 12,
    "open_plan": 30,
    "scaling_unused": 20,
    "recall": 15,
    "balance_won_per_point": 10000,
    "balance_cap": 15,
    "noshow": -15,
    "no_reply": -15,
    "no_answer_repeat": 2,  # 직원 연락 "부재중·안 받음"이 이만큼 쌓이면 무응답으로 본다(설계 §4)
    "external_claim": -30,
    "no_answer": -25,
    "no_answer_days": 7,
    "later": -25,
    "later_days": 14,
    "declined_days": 180,
}
REASONS: dict[str, str] = {
    "recent": "최근에 온 환자일수록 연락이 닿고 다시 오기 쉽다",
    "recent_half_life_months": "오래 안 온 환자는 이미 다른 곳에 다니거나 연락이 닿기 어려울 가능성이 커진다",
    "open_plan": "진단과 계획까지 받은 치료라 필요성을 이미 알고 있어 예약으로 이어지기 쉽다",
    "scaling_unused": "본인 부담이 적은 연 1회 급여라 권하기 쉽고, 해가 바뀌면 사라진다",
    "recall": "정기 검진 안내는 환자에게 부담이 적은 연락이다",
    "balance_won_per_point": "연락하면 수납으로 이어지므로 미수 금액에 비례해 올린다",
    "balance_cap": "미수 하나로 맨 위를 차지하지 않게 상한을 둔다",
    "noshow": "약속에 오지 않고 다시 잡지 않은 환자는 연락해도 오지 않을 가능성이 크다",
    "no_reply": "안내 문자에 연달아 답이 없으면 연락이 닿기 어렵다",
    "no_answer_repeat": "직원 연락을 여러 번 안 받은 것도 같은 신호로 본다",
    "external_claim": "다른 기관에서 스케일링을 받은 기록이 있어 이미 다른 치과에 다닐 가능성이 크다",
    "no_answer": "방금 연락이 안 닿은 환자를 같은 날 여러 사람이 거듭 연락하지 않게 내린다",
    "no_answer_days": "며칠 뒤 다시 걸어 볼 만한 기간",
    "later": "환자가 나중에 하자고 한 기간은 기다린다",
    "later_days": "환자가 미룬 뒤 다시 연락하기까지 두 주",
    "declined_days": "안 오겠다고 한 환자는 반년 동안 목록에서 뺀다",
}


def weights(conn: sqlite3.Connection) -> dict:
    """지금 점수 값 — 기본값에 ax_settings.missed_weights의 바꾼 값(아는 키만)을 덮는다(설계 §3)"""
    over = db.get_setting(conn, "missed_weights", {}) or {}
    return WEIGHTS | {k: v for k, v in over.items() if k in WEIGHTS}


def _n(v: float) -> str:
    return f"{v:+g}점"


def rules(w: dict) -> list[dict]:
    """화면 "순위는 어떻게 정해지나요?" — 지금 값으로 규칙을 사람 말로(와이어프레임 howOpen). 이유는 REASONS 그대로"""
    r = REASONS
    return [
        {
            "name": "마지막 방문",
            "value": f"방문 직후 {_n(w['recent'])}, {w['recent_half_life_months']:g}개월마다 절반",
            "reasons": [r["recent"], r["recent_half_life_months"]],
        },
        {"name": "진료계획 미예약", "value": _n(w["open_plan"]), "reasons": [r["open_plan"]]},
        {
            "name": "올해 스케일링 급여 미사용",
            "value": _n(w["scaling_unused"]),
            "reasons": [r["scaling_unused"]],
        },
        {"name": "리콜 연체", "value": _n(w["recall"]), "reasons": [r["recall"]]},
        {
            "name": "미수 잔액",
            "value": f"{w['balance_won_per_point']:,.0f}원마다 +1점, 최대 {_n(w['balance_cap'])}",
            "reasons": [r["balance_won_per_point"], r["balance_cap"]],
        },
        {"name": "노쇼 뒤 재예약 없음", "value": _n(w["noshow"]), "reasons": [r["noshow"]]},
        {
            "name": "문자 무응답",
            "value": f"{_n(w['no_reply'])} (안내 문자 {NO_REPLY_RUN}번 연달아, 또는 직원 연락"
            f" 부재중 {w['no_answer_repeat']:g}번)",
            "reasons": [r["no_reply"], r["no_answer_repeat"]],
        },
        {
            "name": "다른 기관 스케일링 기록",
            "value": _n(w["external_claim"]),
            "reasons": [r["external_claim"]],
        },
        {
            "name": "최근 연락 — 부재중·안 받음",
            "value": f"{w['no_answer_days']:g}일 동안 {_n(w['no_answer'])}",
            "reasons": [r["no_answer"], r["no_answer_days"]],
        },
        {
            "name": "최근 연락 — 나중에 다시",
            "value": f"{w['later_days']:g}일 동안 {_n(w['later'])}",
            "reasons": [r["later"], r["later_days"]],
        },
        {
            "name": "최근 연락 — 안 오겠다고 함",
            "value": f"{w['declined_days']:g}일 동안 목록에서 빠짐",
            "reasons": [r["declined_days"]],
        },
    ]


# ── 사실 — 표마다 한 번 훑는 집합 쿼리(환자별 루프 안에서 DB를 부르지 않는다) ─────────


def _visits(conn, d: str) -> dict[int, dict]:
    """환자 + 예약 표 한 번 — 마지막 내원·마지막 위생 내원·마지막 예약(내원·노쇼)·그날 아침 예약 여부.
    그날 아침 예약 = 그날(포함) 이후 날짜에 그날 전에 잡았고 그날 전에 취소되지 않은 예약(④ `_family_recall`과 같은
    기준 — 그 뒤 노쇼·취소된 것도 그날 아침엔 잡혀 있던 것)"""
    return {
        r["id"]: dict(r)
        for r in conn.execute(
            "SELECT p.id, p.chart_no, p.name, p.birth_date, p.phone,"
            " p.recall_cycle_months AS months,"
            " MAX(CASE WHEN a.status = 'completed' AND date(a.scheduled_at) < :d"
            "  THEN date(a.scheduled_at) END) AS last_visit,"
            " MAX(CASE WHEN a.status = 'completed' AND a.type = 'hygiene'"
            "  AND date(a.scheduled_at) < :d THEN date(a.scheduled_at) END) AS last_hygiene,"
            " MAX(CASE WHEN a.status IN ('completed', 'noshow') AND date(a.scheduled_at) < :d"
            "  THEN a.scheduled_at END) AS last_appt,"
            " MAX(CASE WHEN a.status = 'noshow' AND date(a.scheduled_at) < :d"
            "  THEN a.scheduled_at END) AS last_noshow,"
            " COALESCE(MAX(date(a.scheduled_at) >= :d AND a.created_at < :d"
            "  AND (a.cancelled_at IS NULL OR date(a.cancelled_at) >= :d)), 0) AS booked"
            " FROM source.emr_patients p JOIN source.emr_appointments a ON a.patient_id = p.id"
            " GROUP BY p.id",
            {"d": d},
        )
    }


def _claims(conn, d: str, mv: dict) -> dict[int, dict]:
    """청구 표 한 번 — 올해 급여 스케일링(그날 전 진료일, ④ `_scaling_unused`), 외부 이력 사유로 거절·조정된 청구
    (그날 전 통보 — 병행, 가상 데이터 생성 설계 §2.2)"""
    return {
        r["patient_id"]: {"scaled": bool(r["scaled"]), "external": bool(r["external"])}
        for r in conn.execute(
            "SELECT patient_id,"
            " MAX(procedure_code = :code AND substr(service_date, 1, 4) = :y"
            "  AND service_date < :d) AS scaled,"
            " MAX(status IN ('denied', 'adjusted') AND reviewed_at < :d"
            "  AND adjust_note IN (SELECT value FROM json_each(:notes))) AS external"
            " FROM source.emr_claims GROUP BY patient_id",
            {
                "code": mv["scaling_code"],
                "y": d[:4],
                "d": d,
                "notes": json.dumps(mv["external_adjust_notes"], ensure_ascii=False),
            },
        )
    }


def _balances(conn, d: str) -> dict[int, int]:
    """수납 표 한 번 — 그날 전 수납의 (받을 돈 − 받은 돈) 합(④ 준비표 잔액과 같은 계산, 음수 그대로)"""
    return dict(
        conn.execute(
            "SELECT patient_id, SUM(amount_due - amount_paid) FROM source.emr_payments"
            " WHERE date(paid_at) < ? GROUP BY patient_id",
            (d,),
        ).fetchall()
    )


def _no_reply(conn, d: str) -> dict[int, int]:
    """문자 표 한 번 — 그날 전 마지막 답장 뒤 연달아 답 없는 안내 문자 수(NO_REPLY_TRIGGERS)"""
    marks = ", ".join("?" * len(NO_REPLY_TRIGGERS))
    return dict(
        conn.execute(
            "WITH lr AS (SELECT patient_id, MAX(sent_at) AS at FROM source.emr_messages"
            "  WHERE \"trigger\" = 'reply' AND sent_at < ? GROUP BY patient_id)"
            " SELECT m.patient_id, COUNT(*) FROM source.emr_messages m"
            " LEFT JOIN lr ON lr.patient_id = m.patient_id"
            f" WHERE m.direction = 'out' AND m.\"trigger\" IN ({marks}) AND m.sent_at < ?"
            " AND m.sent_at > COALESCE(lr.at, '') GROUP BY m.patient_id",
            (d, *NO_REPLY_TRIGGERS, d),
        ).fetchall()
    )


def _open_plans(conn, d: str) -> dict[int, int]:
    """치료계획 한 번(CTE) — 그날 아침 열린 항목 수. ④ `_open_plan`과 같은 규칙: 그날 전에 만든 계획의 항목 중
    끝나지 않음(그날 전 completed 연결 예약 없음)·대체되지 않음(계획을 만든 뒤·그날 전에 다른 계획 또는 계획 밖의
    진료가 같은 치아에 — 전체 구강 시술은 같은 시술코드). 예약이 없는 환자라 그날 예정 항목은 없다"""
    return dict(
        conn.execute(
            "WITH done AS (SELECT DISTINCT plan_item_id AS id FROM source.emr_appointments"
            "  WHERE plan_item_id IS NOT NULL AND status = 'completed'"
            "  AND date(scheduled_at) < :d)"
            " SELECT pl.patient_id, COUNT(*) FROM source.emr_treatment_plan_items i"
            " JOIN source.emr_treatment_plans pl ON pl.id = i.plan_id"
            " WHERE date(pl.created_at) < :d AND i.id NOT IN (SELECT id FROM done)"
            " AND NOT EXISTS (SELECT 1 FROM source.emr_treatments t"
            "  JOIN source.emr_appointments a ON a.id = t.appointment_id"
            "  LEFT JOIN source.emr_treatment_plan_items o ON o.id = a.plan_item_id"
            "  WHERE t.patient_id = pl.patient_id"
            "  AND CASE WHEN i.tooth_no = :fm THEN t.procedure_code = i.procedure_code"
            "   ELSE t.tooth_no = i.tooth_no END"
            "  AND t.performed_at > pl.created_at AND date(t.performed_at) < :d"
            "  AND (o.plan_id IS NULL OR o.plan_id != pl.id))"
            " GROUP BY pl.patient_id",
            {"d": d, "fm": FULL_MOUTH},
        ).fetchall()
    )


def _recall_overdue(last_hygiene: str, months: int, day: date) -> bool:
    """마지막 위생 내원 + 리콜 주기 < 그날(④ `_family_recall`과 같은 계산)"""
    due = date.fromisoformat(last_hygiene).toordinal() + round(months * DAYS_PER_MONTH)
    return due < day.toordinal()


def _scaling_unused(birth: str, scaled: bool, day: date, mv: dict) -> bool:
    """올해 스케일링 급여 미사용 성인(④ `_scaling_unused`, 예정 시술 없음)"""
    return _age(birth, day) >= mv["scaling_min_age"] and not scaled


def facts(conn: sqlite3.Connection, day: date) -> list[dict]:
    """그날 아침 다음 예약이 없는 우리 환자(completed 내원 있음) 중 칩이 하나 이상인 환자 — 설계 §1·§2.
    signals는 점수 입력(score)"""
    d, mv = day.isoformat(), master_values(conn)
    claims = _claims(conn, d, mv)
    balances = _balances(conn, d)
    no_reply = _no_reply(conn, d)
    plans = _open_plans(conn, d)
    out = []
    for pid, v in _visits(conn, d).items():
        if v["last_visit"] is None or v["booked"]:
            continue
        c = claims.get(pid, {"scaled": False, "external": False})
        balance = max(balances.get(pid, 0), 0)  # 더 낸 금액은 미수가 아니다
        noshow = v["last_noshow"] is not None and v["last_noshow"] == v["last_appt"]
        run = no_reply.get(pid, 0)
        on = {
            "recall": v["last_hygiene"] is not None
            and _recall_overdue(v["last_hygiene"], v["months"], day),
            "scaling_unused": _scaling_unused(v["birth_date"], c["scaled"], day, mv),
            "noshow": noshow,
            "balance": balance > 0,
            "open_plan": plans.get(pid, 0) > 0,
            "other_clinic": c["external"] or run >= NO_REPLY_RUN or noshow,
        }
        chips = [k for k in CHIPS if on[k]]
        if not chips:
            continue
        months = (day - date.fromisoformat(v["last_visit"])).days / DAYS_PER_MONTH
        out.append(
            {
                "patient_id": pid,
                "chart_no": v["chart_no"],
                "name": v["name"],
                "age": _age(v["birth_date"], day),
                "phone": v["phone"],
                "last_visit": v["last_visit"],
                "last_hygiene": v["last_hygiene"],
                "balance": balance,
                "chips": chips,
                "signals": {
                    "months_since_visit": round(months, 1),
                    "no_reply_run": run,
                    "external_claim": c["external"],
                    "open_plan_items": plans.get(pid, 0),
                },
            }
        )
    return out


# ── 점수 (설계 §3) ─────────────────────────────────────────────


def score(row: dict, contact: dict | None, now: datetime, w: dict) -> int:
    """가중 덧셈 + 마지막 방문 최근성 감쇠 + 미수(금액 비례, 상한). contact = 이 환자의 가장 최근 연락 기록
    ({result, at, no_answer — 가장 최근의 다른 결과 뒤 연달은 부재중 수})이고 기간은 실제 시계(now) 기준 — 연락
    기록이 실제 시계로 남는다"""
    s, chips = row["signals"], set(row["chips"])
    total = w["recent"] * 0.5 ** (s["months_since_visit"] / w["recent_half_life_months"])
    for k in ("open_plan", "scaling_unused", "recall", "noshow"):
        if k in chips:
            total += w[k]
    total += min(row["balance"] / w["balance_won_per_point"], w["balance_cap"])
    no_answer = contact["no_answer"] if contact else 0
    if s["no_reply_run"] >= NO_REPLY_RUN or no_answer >= w["no_answer_repeat"]:
        total += w["no_reply"]
    if s["external_claim"]:
        total += w["external_claim"]
    if _active(contact, now, w):
        total += w[contact["result"]]
    return math.floor(total + 0.5)


def _days(contact: dict, now: datetime) -> float:
    return (now - datetime.fromisoformat(contact["at"])).total_seconds() / 86400


def _active(contact: dict | None, now: datetime, w: dict) -> bool:
    """최근 연락의 감점 기간이 아직 있음 — 부재중·안 받음은 no_answer_days, 나중에 다시는 later_days 동안. 점수
    감점과 화면 "연락함 숨기기"(contact.active)가 같이 쓴다(빠지는 결과는 이미 목록에 없다)"""
    if contact is None or contact["result"] not in ("no_answer", "later"):
        return False
    return _days(contact, now) < w[f"{contact['result']}_days"]


# ── 연락 기록과 목록 (설계 §4) ─────────────────────────────────

# 연락 결과·수단 — 화면 문구는 frontend/lib/missed-api.ts(예약함·부재중·안 받음·나중에 다시·안 오겠다고 함·다른 치과 다님 / 전화·문자)
RESULTS = ("booked", "no_answer", "later", "declined", "other_clinic")
CHANNELS = ("call", "text")


def record_contact(
    conn: sqlite3.Connection,
    patient_id: int,
    result: str,
    channel: str,
    by: str,
    at: str,
    day: date,
) -> None:
    """연락 결과 한 줄 — 모르는 결과·수단이나 그날 미예약 환자(facts)가 아닌 환자면 ValueError. 커밋은 부르는 쪽.
    목록에서 빠진 환자도 facts에는 있어 결과를 고쳐 남길 수 있다(가장 최근 기록이 이긴다)"""
    if result not in RESULTS or channel not in CHANNELS:
        raise ValueError(f"모르는 결과·수단: {result}, {channel}")
    if all(r["patient_id"] != patient_id for r in facts(conn, day)):
        raise ValueError(f"미예약 환자가 아님: {patient_id}")
    conn.execute(
        "INSERT INTO ax_missed_contacts (patient_id, result, channel, contacted_by, contacted_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (patient_id, result, channel, by, at),
    )


def _contacts(conn) -> dict[int, dict]:
    """연락 기록 한 번 — 환자마다 가장 최근 기록(시각, 같으면 나중 줄)과 연달은 부재중 수(가장 최근의 다른 결과
    뒤 기록 수 — 그 뒤는 모두 부재중)"""
    return {
        r["patient_id"]: {k: r[k] for k in ("result", "channel", "by", "at", "no_answer")}
        for r in conn.execute(
            "SELECT * FROM (SELECT patient_id, result, channel, by, at, n,"
            " COUNT(*) OVER p - COALESCE(MAX(CASE WHEN result != 'no_answer' THEN n END) OVER p, 0)"
            "  AS no_answer,"
            " COUNT(*) OVER p AS total"
            " FROM (SELECT patient_id, result, channel, contacted_by AS by, contacted_at AS at,"
            "  ROW_NUMBER() OVER (PARTITION BY patient_id ORDER BY contacted_at, id) AS n"
            "  FROM ax_missed_contacts)"
            " WINDOW p AS (PARTITION BY patient_id)) WHERE n = total"
        )
    }


def _removed(contact: dict | None, now: datetime, w: dict) -> bool:
    """목록에서 빠짐 — 예약함·다른 치과 다님은 기간 없이, 안 오겠다고 함은 declined_days 동안"""
    if contact is None:
        return False
    if contact["result"] in ("booked", "other_clinic"):
        return True
    return contact["result"] == "declined" and _days(contact, now) < w["declined_days"]


def sheet(conn: sqlite3.Connection, day: date, now: datetime) -> dict:
    """GET /missed — day = 앱 기준일(사실), now = 실제 시계(연락 기록 기간). 칩별 인원수는 목록 전체(화면의 칩 필터·
    연락함 숨기기 전). 순서 = 점수 내림차순 → 마지막 방문 최근 → 차트번호"""
    w = weights(conn)
    contacts = _contacts(conn)
    rows = []
    for r in facts(conn, day):
        c = contacts.get(r["patient_id"])
        if _removed(c, now, w):
            continue
        shown = (
            None
            if c is None
            else {k: c[k] for k in ("result", "channel", "by", "at")}
            | {"active": _active(c, now, w)}
        )
        rows.append(r | {"score": score(r, c, now, w), "contact": shown})
    rows.sort(key=lambda r: r["chart_no"])
    rows.sort(key=lambda r: (r["score"], r["last_visit"]), reverse=True)
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return {
        "date": day.isoformat(),
        "page": PAGE,
        "chip_counts": {k: sum(k in r["chips"] for r in rows) for k in CHIPS},
        "rows": rows,
        "rules": rules(w),
    }
