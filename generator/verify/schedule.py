"""예약 검사 — 충돌·체어·동시 진료·예약 시각·변경·리마인드·창 이전 내원, 내원 구성 경고·하루 채움 리포트
(가상 데이터 생성 설계 §1·§2.6·§5.3 충돌·운영 제약·예약)"""

from collections import defaultdict
from datetime import date, timedelta

from generator.master import (
    CHAIRS,
    CHURN_MIN_DAYS,
    DOCTOR_CONCURRENCY,
    MAX_LEAD_DAYS,
    VISIT_MIX,
    daily_cap,
    open_slots,
)
from generator.verify.common import _dt, _rows

# 경고 허용치 (§5.3 경고 표)
VISIT_MIX_TOLERANCE = 0.04


# ── 충돌·운영 제약 ──────────────────────────────────────────────


def check_patient_same_day(conn, ctx):
    """같은 환자 같은 날 예약 중복 없음 (취소 제외)"""
    return _rows(
        conn,
        "SELECT patient_id, date(scheduled_at) AS day, group_concat(id) AS ids"
        " FROM emr_appointments WHERE status != 'cancelled'"
        " GROUP BY patient_id, day HAVING count(*) > 1",
    )


def _window_busy(conn, ctx, role=None) -> list[dict]:
    """창 안 비취소 예약의 시작·끝 — 창 이전 내원은 체어·동시 진료 검사 대상 아님 (생성 설계 §5.3)"""
    rows = _rows(
        conn,
        "SELECT a.id, a.staff_id, a.chair_no, a.scheduled_at, a.duration_min, s.role"
        " FROM emr_appointments a JOIN emr_staff s ON s.id = a.staff_id"
        " WHERE a.status != 'cancelled' AND a.scheduled_at >= ?",
        ctx.range_start.isoformat(),
    )
    for r in rows:
        r["start"] = _dt(r["scheduled_at"])
        r["end"] = r["start"] + timedelta(minutes=r["duration_min"])
    return [r for r in rows if role is None or r["role"] == role]


def _over_capacity(rows: list[dict], key: str, cap: int) -> list[dict]:
    """같은 key(체어·담당)에서 어떤 예약의 시작 시각에 진행 중인 예약이 cap건을 넘는 예약 — 동시 건수의 최대는 어떤 시작 시각에 나온다"""
    groups: dict = {}
    for r in rows:
        if r[key] is not None:
            groups.setdefault(r[key], []).append(r)
    bad = []
    for group in groups.values():
        for r in group:
            n = sum(1 for o in group if o["start"] <= r["start"] < o["end"])
            if n > cap:
                bad.append({"id": r["id"], key: r[key], "scheduled_at": r["scheduled_at"], "n": n})
    return bad


def check_chair_overlap(conn, ctx):
    """같은 체어는 소요시간 동안 예약 1건 — 창 안 비취소 예약끼리 시간 겹침 없음"""
    return _over_capacity(_window_busy(conn, ctx), "chair_no", 1)


def check_hygienist_concurrency(conn, ctx):
    """위생사는 동시에 1건 — 창 안 비취소 예약"""
    return _over_capacity(_window_busy(conn, ctx, "hygienist"), "staff_id", 1)


def check_doctor_concurrency(conn, ctx):
    """원장은 동시에 master.DOCTOR_CONCURRENCY건까지 — 창 안 비취소 예약"""
    return _over_capacity(_window_busy(conn, ctx, "doctor"), "staff_id", DOCTOR_CONCURRENCY)


def check_consult_no_chair(conn, ctx):
    """상담은 체어 없음(chair_no NULL), 그 밖의 예약은 체어 1~master.CHAIRS"""
    return _rows(
        conn,
        "SELECT id, type, chair_no FROM emr_appointments WHERE"
        " (type = 'consult' AND chair_no IS NOT NULL)"
        " OR (type != 'consult' AND (chair_no IS NULL OR chair_no NOT BETWEEN 1 AND ?))",
        CHAIRS,
    )


# ── 예약 ────────────────────────────────────────────────────


def _prev_open_day(d: date, holidays) -> date:
    """d 직전의 진료일 (휴진 요일·공휴일 건너뜀)"""
    d -= timedelta(days=1)
    while not open_slots(d.weekday()) or d in holidays:
        d -= timedelta(days=1)
    return d


def check_confirmed_not_future(conn, ctx):
    """confirmed는 직전 진료일 리마인드 응답 — 직전 진료일이 TODAY 이후인 예약은 0"""
    return [
        r
        for r in _rows(
            conn, "SELECT id, scheduled_at, confirmed FROM emr_appointments WHERE confirmed = 1"
        )
        if _prev_open_day(_dt(r["scheduled_at"]).date(), ctx.holidays) > ctx.today
    ]


def check_confirmed_reminded(conn, ctx):
    """confirmed는 리마인드 대상만 — 직전 진료일 전에 잡혔고(그날 잡은 예약은 대상 아님) 그날까지 취소되지 않은 예약"""
    bad = []
    for r in _rows(
        conn,
        "SELECT id, scheduled_at, created_at, cancelled_at FROM emr_appointments WHERE confirmed = 1",
    ):
        prev = _prev_open_day(_dt(r["scheduled_at"]).date(), ctx.holidays)
        if _dt(r["created_at"]).date() >= prev or (
            r["cancelled_at"] and _dt(r["cancelled_at"]).date() <= prev
        ):
            bad.append(r)
    return bad


def check_pre_window_visit(conn, ctx):
    """창 이전 내원(scheduled_at < range_start)은 환자당 completed 위생 1행, 등록일 < 내원일"""
    rows = _rows(
        conn,
        "SELECT a.id, a.patient_id, a.scheduled_at, a.type, a.status, p.registered_at"
        " FROM emr_appointments a JOIN emr_patients p ON p.id = a.patient_id"
        " WHERE date(a.scheduled_at) < ? ORDER BY a.patient_id, a.id",
        ctx.range_start.isoformat(),
    )
    bad, seen = [], set()
    for r in rows:
        if (
            (r["type"], r["status"]) != ("hygiene", "completed")
            or r["patient_id"] in seen
            or date.fromisoformat(r["registered_at"]) >= _dt(r["scheduled_at"]).date()
        ):
            bad.append(r)
        seen.add(r["patient_id"])
    return bad


def check_created_before_scheduled(conn, ctx):
    """예약은 잡은 뒤에 열린다 — created_at < scheduled_at"""
    return _rows(
        conn,
        "SELECT id, created_at, scheduled_at FROM emr_appointments"
        " WHERE datetime(created_at) >= datetime(scheduled_at)",
    )


def check_cancel_time(conn, ctx):
    """취소 시각은 예약을 잡은 뒤, 예약 시각 전 — created_at ≤ cancelled_at < scheduled_at"""
    return _rows(
        conn,
        "SELECT id, created_at, cancelled_at, scheduled_at FROM emr_appointments"
        " WHERE cancelled_at IS NOT NULL AND (datetime(cancelled_at) < datetime(created_at)"
        " OR datetime(cancelled_at) >= datetime(scheduled_at))",
    )


def check_cancelled_at_status(conn, ctx):
    """cancelled_at은 cancelled 예약에만, cancelled 예약은 cancelled_at 필수"""
    return _rows(
        conn,
        "SELECT id, status, cancelled_at FROM emr_appointments"
        " WHERE (status = 'cancelled') != (cancelled_at IS NOT NULL)",
    )


def check_reschedule_pair(conn, ctx):
    """변경 쌍: 원래 예약은 cancelled, 새 예약 created_at = 원래 cancelled_at (변경 통화 시각의 근거)"""
    return _rows(
        conn,
        "SELECT n.id, n.rescheduled_from_id, o.status, o.cancelled_at, n.created_at"
        " FROM emr_appointments n JOIN emr_appointments o ON o.id = n.rescheduled_from_id"
        " WHERE o.status != 'cancelled' OR n.created_at IS NOT o.cancelled_at",
    )


def check_cancellation_kind(conn, ctx):
    """취소는 변경 쌍의 원래 예약 또는 당일 취소(TODAY 이전 날짜)뿐"""
    return _rows(
        conn,
        "SELECT a.id, a.scheduled_at, a.cancelled_at FROM emr_appointments a"
        " WHERE a.status = 'cancelled'"
        " AND NOT EXISTS (SELECT 1 FROM emr_appointments n WHERE n.rescheduled_from_id = a.id)"
        " AND NOT (date(a.cancelled_at) = date(a.scheduled_at) AND date(a.scheduled_at) < ?)",
        ctx.today.isoformat(),
    )


# ── 이탈·병행 (§2.2·§2.6 (1)) ──────────────────────────────


def check_churn_date(conn, ctx):
    """이탈 환자는 구환(등록일 < 창 시작), 이탈일은 창 시작 ~ TODAY − master.CHURN_MIN_DAYS (흔적이 쌓일 시간)"""
    last = (ctx.today - timedelta(days=CHURN_MIN_DAYS)).isoformat()
    return _rows(
        conn,
        "SELECT c.patient_id, c.churned_on, p.registered_at FROM truth.truth_patient_churn c"
        " JOIN emr_patients p ON p.id = c.patient_id WHERE c.kind = 'churn'"
        " AND (p.registered_at >= ? OR c.churned_on < ? OR c.churned_on > ?)",
        ctx.range_start.isoformat(),
        ctx.range_start.isoformat(),
        last,
    )


def check_churn_after(conn, ctx):
    """이탈 환자는 이탈일부터 새 예약(created_at)·completed 내원·리마인드 확인 없음 — 이미 잡힌 예약은
    노쇼 또는 당일 취소(TODAY 전)"""
    bad = []
    for r in _rows(
        conn,
        "SELECT a.id, a.patient_id, a.scheduled_at, a.created_at, a.status, a.confirmed, c.churned_on"
        " FROM emr_appointments a JOIN truth.truth_patient_churn c ON c.patient_id = a.patient_id"
        " WHERE c.kind = 'churn'",
    ):
        on = date.fromisoformat(r["churned_on"])
        day = _dt(r["scheduled_at"]).date()
        if (
            _dt(r["created_at"]).date() >= on
            or (on <= day < ctx.today and r["status"] not in ("noshow", "cancelled"))
            or (r["confirmed"] and _prev_open_day(day, ctx.holidays) >= on)
        ):
            bad.append(r)
    return bad


def report_churn(conn, ctx):
    """다른 치과 이용 환자 수 — 이탈·병행 (⑨ 다른 치과 추정의 양성)"""
    return dict(conn.execute("SELECT kind, count(*) FROM truth.truth_patient_churn GROUP BY kind"))


# ── 분포 ────────────────────────────────────────────────────


def report_daily_fill(conn, ctx):
    """하루 채움(비취소 예약 / 하루 상한) — range_start + MAX_LEAD_DAYS부터 TODAY 전날까지의 진료일
    (시작 직후는 미리 잡힌 예약이 없고, TODAY 이후는 덜 찬 게 정상)"""
    counts = defaultdict(int)
    for r in _rows(
        conn, "SELECT date(scheduled_at) AS d FROM emr_appointments WHERE status != 'cancelled'"
    ):
        counts[date.fromisoformat(r["d"])] += 1
    fills, d = [], ctx.range_start + timedelta(days=MAX_LEAD_DAYS)
    while d <= min(ctx.range_end, ctx.today - timedelta(days=1)):
        if open_slots(d.weekday()) and d not in ctx.holidays:
            fills.append(counts[d] / daily_cap(ctx.visits_per_day, d.weekday()))
        d += timedelta(days=1)
    if not fills:
        return {"days": 0}
    return {
        "days": len(fills),
        "mean": round(sum(fills) / len(fills), 3),
        "min": round(min(fills), 3),
    }


def check_visit_mix(conn, ctx):
    """창 안 내원(completed) 유형 구성 master.VISIT_MIX ± VISIT_MIX_TOLERANCE — 창 이전 내원은 빼고"""
    counts = {
        r["type"]: r["n"]
        for r in _rows(
            conn,
            "SELECT type, count(*) AS n FROM emr_appointments"
            " WHERE status = 'completed' AND date(scheduled_at) >= ?"
            " GROUP BY type",
            ctx.range_start.isoformat(),
        )
    }
    total = sum(counts.values())
    if not total:
        return []
    return [
        {"type": t, "share": round(counts.get(t, 0) / total, 3), "target": target}
        for t, target in VISIT_MIX.items()
        if abs(counts.get(t, 0) / total - target) > VISIT_MIX_TOLERANCE
    ]
