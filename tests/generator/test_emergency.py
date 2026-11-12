"""6b단계 응급 내원·8a단계 취소 연락 — 실제로 생성해 규칙을 본다 (가상 데이터 생성 설계 §2.6 (5)·§2.5 취소 답장).
불변식(예약·통화·문자 대응)은 verify 필수 검사(check_emergency_visits·check_cancel_replies — tests/generator/test_records.py가 전부 돌림)"""

import json
import sqlite3
from datetime import date, datetime, timedelta

import pytest

from generator import emergency
from generator.calls import FMT, prev_open_day
from generator.master import (
    EMERGENCY_AFTER_VISIT_SCENE,
    EMERGENCY_HISTORY_SCENES,
    EMERGENCY_LEAD_MIN,
    EMERGENCY_RESTORED_SCENES,
    EMERGENCY_SCENES,
    EMERGENCY_TREATMENT,
    MESSAGE_REPLY_CANCEL,
    NOSHOW_TO_CANCEL,
    PROCEDURES,
    SLOT_MIN,
    age_on,
    close_time,
)
from generator.paths import ROOT
from tests.generator.test_messages import HOLIDAYS

CONFIG = ROOT / "config" / "generator.yaml"
NAME = {p["code"]: p["name"] for p in PROCEDURES}
PROCEDURE_BY_CODE = {p["code"]: p for p in PROCEDURES}
TODAY = "2026-12-01"


@pytest.fixture(scope="module")
def db(generated):
    out, _ = generated("emergency")
    conn = sqlite3.connect(out / "source.sqlite")
    conn.row_factory = sqlite3.Row
    conn.execute("ATTACH DATABASE ? AS truth", (str(out / "truth.sqlite"),))
    return conn


def _visits(db):
    rows = []
    for c in emergency.emergency_calls(db):
        visit = json.loads(c["used_fact"])["emergency_visit"]
        a = dict(
            db.execute(
                "SELECT * FROM emr_appointments WHERE id = ?", (visit["appointment_id"],)
            ).fetchone()
        )
        rows.append((c, a))
    return rows


def test_every_patient_emergency_call_has_visit(db):
    rows = _visits(db)
    assert rows and len({a["id"] for _, a in rows}) == len(rows)


def test_visit_time_after_call_on_slot(db):
    """통화 뒤 EMERGENCY_LEAD_MIN분이 지난 SLOT_MIN분 격자 — 대부분 그날"""
    rows = _visits(db)
    for c, a in rows:
        call, start = (datetime.strptime(x, FMT) for x in (c["started_at"], a["scheduled_at"]))
        assert start >= call + timedelta(minutes=EMERGENCY_LEAD_MIN)
        assert start.minute % SLOT_MIN == 0
    same_day = sum(c["started_at"][:10] == a["scheduled_at"][:10] for c, a in rows)
    assert same_day > len(rows) / 2


def test_procedure_follows_scene(db):
    """처치는 상황의 시술(치아는 후보 안 — 장부에 없는 치아, 때운 것이 빠진 상황은 전에 다른 수복 시술로 때운 치아) 또는 상담"""
    for c, a in _visits(db):
        visit = json.loads(c["used_fact"])["emergency_visit"]
        name, teeth = EMERGENCY_TREATMENT[visit.get("scene") or emergency.scene(c["call_uid"])]
        got = NAME[a["planned_procedure_code"]]
        assert got in (name, "상담")
        t = db.execute(
            "SELECT * FROM emr_treatments WHERE appointment_id = ?", (a["id"],)
        ).fetchone()
        if t is None or got == "상담":
            continue
        assert t["tooth_no"] in teeth
        others = db.execute(
            "SELECT procedure_code, performed_at FROM emr_treatments WHERE patient_id = ? AND tooth_no = ?"
            " AND id != ?",
            (t["patient_id"], t["tooth_no"], t["id"]),
        ).fetchall()
        assert all(code != t["procedure_code"] for code, _ in others)  # 재시행 아님
        if others:  # 때운 것이 빠진 상황만 — 통화 전에 다른 수복 시술로 때운 치아
            assert emergency.scene(c["call_uid"]) in EMERGENCY_RESTORED_SCENES
            assert any(at < c["started_at"] for _, at in others)


def test_today_scene_call(db):
    """TODAY 장면 — 전 진료일 진료 끝 무렵(마감 − SCENE_CALL_BEFORE_CLOSE_MIN)의 응급 통화 1건이 TODAY 오전 빈 시간
    scheduled 내원을 잡는다(풀 밖 통화)"""
    scene = [(c, a) for c, a in _visits(db) if a["scheduled_at"][:10] == TODAY]
    assert len(scene) == 1
    c, a = scene[0]
    day = prev_open_day(datetime.strptime(TODAY, "%Y-%m-%d").date(), HOLIDAYS)
    close = datetime.combine(day, datetime.strptime(close_time(day.weekday()), "%H:%M").time())
    want = close - timedelta(minutes=emergency.SCENE_CALL_BEFORE_CLOSE_MIN)
    assert c["started_at"] == want.strftime(FMT)
    assert a["status"] == "scheduled" and not c["in_eval_pool"] and not c["in_blind_pool"]


def test_call_ids_follow_time(db):
    times = [
        r[0]
        for r in db.execute(
            "SELECT started_at FROM ax_calls WHERE call_uid LIKE 'c-%' ORDER BY id"
        )
    ]
    assert times == sorted(times)


def test_today_cancel_scene(db):
    """TODAY 예약 1건 — 전날 리마인드에 취소 답장, 그 뒤 취소(그날 영업시간 안)"""
    rows = db.execute(
        "SELECT a.cancelled_at, m.sent_at FROM emr_appointments a JOIN emr_messages m"
        " ON m.related_appointment_id = a.id AND m.body = ?"
        " WHERE date(a.scheduled_at) = ? AND a.status = 'cancelled'",
        (MESSAGE_REPLY_CANCEL, TODAY),
    ).fetchall()
    assert len(rows) == 1
    cancelled, sent = rows[0]
    assert sent < cancelled < f"{TODAY} 00:00:00"


def test_preopen_cancels_in_last_month(db):
    """TODAY 전 달에 개원 전 문자 당일 취소가 NOSHOW_TO_CANCEL건 이상(장면) — 모두 취소 답장 뒤"""
    rows = db.execute(
        "SELECT a.id, a.cancelled_at, (SELECT max(sent_at) FROM emr_messages m WHERE"
        " m.related_appointment_id = a.id AND m.body = ?) AS reply FROM emr_appointments a"
        " WHERE a.status = 'cancelled' AND substr(a.scheduled_at, 1, 7) = '2026-11'"
        " AND date(a.cancelled_at) = date(a.scheduled_at) AND time(a.cancelled_at) < '09:30:00'",
        (MESSAGE_REPLY_CANCEL,),
    ).fetchall()
    assert len(rows) >= NOSHOW_TO_CANCEL
    assert all(r["reply"] and r["reply"] <= r["cancelled_at"] for r in rows)
    noshow_msgs = db.execute(
        "SELECT count(*) FROM emr_messages m JOIN emr_appointments a ON a.id = m.related_appointment_id"
        " WHERE m.\"trigger\" = 'noshow' AND a.status != 'noshow'"
    ).fetchone()[0]
    assert noshow_msgs == 0


def test_message_ids_follow_time(db):
    times = [r[0] for r in db.execute("SELECT sent_at FROM emr_messages ORDER BY id")]
    assert times == sorted(times)


def test_visit_area_matches_tooth(db):
    """치아 단위 처치면 통화 사실에 그 치아의 부위 말 — 사분면(1 오른쪽 위 · 2 왼쪽 위 · 3 왼쪽 아래 · 4 오른쪽 아래)"""
    for c, a in _visits(db):
        visit = json.loads(c["used_fact"])["emergency_visit"]
        t = db.execute(
            "SELECT tooth_no FROM emr_treatments WHERE appointment_id = ?", (a["id"],)
        ).fetchone()
        if t is None or t[0] == "00":
            assert "area" not in visit
        else:
            assert visit["area"] == emergency.tooth_area(t[0])
    assert emergency.tooth_area("26") == "왼쪽 위 어금니"
    assert emergency.tooth_area("44") == "오른쪽 아래 작은어금니"


def test_scene_follows_same_day_visit_and_age(db):
    """그날 먼저 치료받고 간 뒤 건 응급 통화는 그 치료와 이어진 통증(상담), 때운·씌운 이력이 없으면 대신 상황, 소아는 소아에게
    맞는 상황 — 바꾼 상황은 사실에(emergency.visit_scene)"""
    changed = 0
    for c, a in _visits(db):
        visit = json.loads(c["used_fact"])["emergency_visit"]
        before = db.execute(
            "SELECT 1 FROM emr_appointments WHERE patient_id = ? AND status = 'completed'"
            " AND type = 'treatment' AND date(scheduled_at) = ? AND scheduled_at < ? AND id != ?",
            (c["patient_id"], c["started_at"][:10], c["started_at"], a["id"]),
        ).fetchone()
        done = {
            PROCEDURE_BY_CODE[r[0]]["category"]
            for r in db.execute(
                "SELECT procedure_code FROM emr_treatments WHERE patient_id = ? AND performed_at < ?",
                (c["patient_id"], c["started_at"]),
            )
        }
        p = db.execute(
            "SELECT birth_date FROM emr_patients WHERE id = ?", (c["patient_id"],)
        ).fetchone()
        age = age_on(date.fromisoformat(p[0]), date.fromisoformat(c["started_at"][:10]))
        want, did = emergency.visit_scene(c["call_uid"], age, before is not None, done)
        assert (
            visit.get("scene", emergency.scene(c["call_uid"])) == want
            and ("scene" in visit) == did
        )
        if before:
            assert (
                want == EMERGENCY_AFTER_VISIT_SCENE and NAME[a["planned_procedure_code"]] == "상담"
            )
        if want in EMERGENCY_HISTORY_SCENES:  # 이력이 있어야 하는 상황이 남았으면 그 이력이 있다
            assert done & set(EMERGENCY_HISTORY_SCENES[want][0])
        changed += did
    assert changed


def test_recent_visits_mark_emergency(db):
    """뒤 통화의 최근 진료 3건에 응급 내원이 있으면 응급 표시 — 일반 상담으로 이어 쓰지 않게"""
    marks = [
        v
        for (raw,) in db.execute("SELECT used_fact FROM truth.truth_call_labels")
        for v in json.loads(raw).get("recent_visits", [])
        if v.get("emergency")
    ]
    assert marks


def test_sudden_scene_never_gradual():
    """갑자기 빠진 상황엔 시작 시점 '점점'을 쓰지 않는다 — 다른 상황은 해시 그대로"""
    from generator import transcripts
    from generator.master import EMERGENCY_GRADUAL_ONSETS, EMERGENCY_SUDDEN_SCENES

    for i in range(1, 400):
        uid = f"c-{i:04d}"
        text = transcripts.situation(uid, "emergency")
        scene = emergency.scene(uid)
        if scene in EMERGENCY_SUDDEN_SCENES:
            assert not any(text.startswith(o) for o in EMERGENCY_GRADUAL_ONSETS)


def _uid_with(scene):
    return next(f"c-{n:04d}" for n in range(1, 2000) if emergency.scene(f"c-{n:04d}") == scene)


@pytest.mark.parametrize(
    ("base", "age", "visited", "done", "want"),
    [
        (EMERGENCY_SCENES[0], 40, False, set(), EMERGENCY_SCENES[0]),  # 이력이 필요 없는 상황
        (EMERGENCY_SCENES[0], 40, True, set(), EMERGENCY_AFTER_VISIT_SCENE),  # 그날 진료 뒤
        (EMERGENCY_SCENES[3], 40, False, set(), EMERGENCY_SCENES[1]),  # 때운 적 없음 → 깨진 이
        (EMERGENCY_SCENES[3], 40, False, {"restorative"}, EMERGENCY_SCENES[3]),
        (EMERGENCY_SCENES[6], 40, False, {"restorative"}, EMERGENCY_SCENES[2]),  # 씌운 적 없음
        (EMERGENCY_SCENES[6], 40, False, {"prosth"}, EMERGENCY_SCENES[6]),
        (EMERGENCY_SCENES[6], 7, False, {"prosth"}, EMERGENCY_SCENES[2]),  # 소아
    ],
)
def test_visit_scene_table(base, age, visited, done, want):
    got, changed = emergency.visit_scene(_uid_with(base), age, visited, done)
    assert got == want and changed == (want != base)
