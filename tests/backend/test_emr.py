"""전환 전 EMR·예약표 조회 (앱 설계 §9, 전환 전 EMR·일별 예약표 설계안)."""

import sqlite3
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.api import app, get_db_paths
from backend.emr import (
    patient_record,
    patient_summary,
    schedule_day,
    search_patients,
    tour_patient,
)

TODAY, START = date(2026, 12, 1), date(2024, 12, 1)
HOURS = (
    '{"open":"09:30","weekday_close":"18:30","saturday_close":"13:30",'
    '"lunch":["13:00","14:00"],"closed_weekdays":[3,6]}'
)
APPT = (
    "INSERT INTO emr_appointments (id, patient_id, staff_id, scheduled_at, duration_min, type,"
    " planned_procedure_code, status, confirmed, booked_via, created_at, plan_item_id)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def rw(paths, *sql):
    conn = sqlite3.connect(paths[1])
    for s in sql:
        conn.execute(s) if isinstance(s, str) else conn.execute(*s)
    conn.commit()
    conn.close()


def ro(paths) -> sqlite3.Connection:
    conn = sqlite3.connect(f"{Path(paths[1]).resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


@pytest.fixture
def emr_paths(source_paths):
    """source_paths에 예약·수납·주의사항·치료계획·통화를 더한다"""
    conn = sqlite3.connect(source_paths[1])
    conn.execute("UPDATE emr_clinic SET hours_json = ?", (HOURS,))
    conn.executemany(
        "INSERT INTO emr_staff (id, name, role, active) VALUES (?, ?, ?, ?)",
        [
            (1, "강태현", "doctor", 1),
            (2, "임재윤", "hygienist", 1),
            (3, "오지수", "desk", 1),
            (4, "송민지", "consultant", 1),
            (5, "퇴사자", "hygienist", 0),
        ],
    )
    conn.executemany(
        "INSERT INTO emr_procedures (code, name, category, is_insured, fee_total,"
        " patient_share_rate, duration_min, is_tooth_level) VALUES (?, ?, ?, 'no', 0, 1, 30, 0)",
        [("CROWN", "크라운", "prosth"), ("CONSULT", "상담", "consult")],
    )
    conn.execute("UPDATE emr_patients SET registered_at = '2026-11-01' WHERE id = 3")
    conn.executemany(
        APPT,
        [
            (
                101,
                1,
                1,
                "2026-12-01 10:00:00",
                60,
                "treatment",
                "CROWN",
                "scheduled",
                0,
                "phone",
                "2026-11-17 14:20:00",
                212,
            ),
            (
                102,
                2,
                2,
                "2026-12-01 09:30:00",
                30,
                "hygiene",
                "scaling",
                "scheduled",
                1,
                "desk",
                "2026-06-01 10:00:00",
                None,
            ),
            (
                103,
                3,
                1,
                "2026-12-01 11:00:00",
                30,
                "treatment",
                "CONSULT",
                "cancelled",
                0,
                "phone",
                "2026-11-20 09:00:00",
                None,
            ),
            (
                104,
                3,
                1,
                "2026-12-01 11:00:00",
                30,
                "treatment",
                "CONSULT",
                "scheduled",
                0,
                "desk",
                "2026-11-30 09:00:00",
                None,
            ),
            (
                90,
                1,
                1,
                "2026-11-17 10:00:00",
                30,
                "treatment",
                "CROWN",
                "completed",
                1,
                "desk",
                "2026-11-03 10:00:00",
                211,
            ),
            (
                91,
                1,
                1,
                "2026-03-02 10:00:00",
                30,
                "treatment",
                "CROWN",
                "noshow",
                0,
                "phone",
                "2026-02-20 10:00:00",
                None,
            ),
            (
                92,
                1,
                1,
                "2025-11-20 10:00:00",
                30,
                "treatment",
                "CROWN",
                "noshow",
                0,
                "phone",
                "2025-11-10 10:00:00",
                None,
            ),
            (
                110,
                2,
                2,
                "2026-12-15 14:00:00",
                30,
                "hygiene",
                "scaling",
                "scheduled",
                0,
                "desk",
                "2026-12-01 09:00:00",
                None,
            ),
        ],
    )
    conn.execute(
        "INSERT INTO emr_treatment_plans (id, patient_id, created_at, created_appointment_id)"
        " VALUES (201, 1, '2026-10-20', 90)"
    )
    conn.executemany(
        "INSERT INTO emr_treatment_plan_items (id, plan_id, seq, procedure_code, tooth_no,"
        " est_fee, status) VALUES (?, 201, ?, 'CROWN', ?, 0, ?)",
        [(211, 1, "36", "done"), (212, 2, "36", "open"), (213, 3, "47", "open")],
    )
    conn.execute(
        "INSERT INTO ax_calls (call_uid, source, patient_id, direction, started_at,"
        " duration_sec, has_transcript, transcript)"
        " VALUES ('c-0003', 'generated', 1, 'inbound', '2026-11-17 14:20:00', 60, 1, '예약')"
    )
    conn.executemany(
        "INSERT INTO emr_payments (patient_id, kind, amount_due, amount_paid, paid_at, method)"
        " VALUES (?, 'visit', ?, ?, ?, 'card')",
        [
            (1, 100000, 15000, "2026-11-17 11:00:00"),
            (1, 50000, 0, "2026-12-01 09:00:00"),
            (2, 0, 10000, "2026-11-01 11:00:00"),
        ],
    )
    conn.executemany(
        "INSERT INTO emr_alert_types VALUES (?, ?, ?)",
        [
            ("ANTICOAG", "항응고제 복용", "systemic"),
            ("PREGNANT", "임신 중", "systemic"),
            ("PENICILLIN", "페니실린 알레르기", "allergy"),
        ],
    )
    conn.executemany(
        "INSERT INTO emr_patient_alerts (patient_id, code, source, recorded_at, ends_at)"
        " VALUES (?, ?, 'intake', ?, ?)",
        [
            (1, "ANTICOAG", "2026-01-01", None),
            (1, "PREGNANT", "2026-01-01", "2026-12-01"),
            (3, "PENICILLIN", "2026-12-02", None),
        ],
    )
    conn.execute(
        'INSERT INTO emr_messages (patient_id, direction, sent_at, body, "trigger",'
        " related_appointment_id) VALUES (1, 'out', '2026-11-30 10:00:00',"
        " '내일 10시 예약 확인 부탁드립니다', 'appt_confirm', 101)"
    )
    # 리뷰 반영: 101은 전날 리마인드에 답 없음, 102는 확인됨, 104는 문자 없음
    conn.execute(
        'INSERT INTO emr_messages (patient_id, direction, sent_at, body, "trigger",'
        " related_appointment_id) VALUES (1, 'out', '2026-11-30 09:30:00',"
        " '내일 10시 예약입니다', 'reminder', 101)"
    )
    conn.execute(
        "INSERT INTO emr_treatment_plan_items (id, plan_id, seq, procedure_code, tooth_no,"
        " est_fee, status) VALUES (214, 201, 4, 'CROWN', '46', 0, 'replaced')"
    )
    conn.executemany(
        APPT,
        [
            # 365일 경계 — 2025-12-01은 포함, 2025-11-30은 제외
            (
                93,
                1,
                1,
                "2025-12-01 10:00:00",
                30,
                "treatment",
                "CROWN",
                "noshow",
                0,
                "phone",
                "2025-11-25 10:00:00",
                None,
            ),
            (
                94,
                1,
                1,
                "2025-11-30 10:00:00",
                30,
                "treatment",
                "CROWN",
                "noshow",
                0,
                "phone",
                "2025-11-25 10:00:00",
                None,
            ),
        ],
    )
    conn.commit()
    conn.close()
    return source_paths


@pytest.fixture
def conn(emr_paths):
    c = ro(emr_paths)
    yield c
    c.close()


def _client(paths):
    app.dependency_overrides[get_db_paths] = lambda: paths
    return TestClient(app)


@pytest.fixture
def client(emr_paths):
    yield _client(emr_paths)
    app.dependency_overrides.clear()


@pytest.fixture
def client_source_only(emr_paths, tmp_path):
    yield _client((tmp_path / "none" / "app.sqlite", emr_paths[1]))
    app.dependency_overrides.clear()


def test_schedule_staff_order_excludes_desk_and_inactive(conn):
    d = schedule_day(conn, TODAY, TODAY, START)
    assert [(s["name"], s["role"]) for s in d["staff"]] == [
        ("강태현", "원장"),
        ("임재윤", "위생사"),
        ("송민지", "상담실장"),
    ]


def test_schedule_appointment_fields(conn):
    a = {x["id"]: x for x in schedule_day(conn, TODAY, TODAY, START)["appointments"]}
    assert (a[101]["time"], a[101]["duration"], a[101]["unconfirmed"]) == ("10:00", 60, True)
    assert (a[101]["staff_name"], a[101]["staff_role"]) == ("강태현", "원장")
    assert a[101]["plan_step"] == {"seq": 2, "total": 3} and a[101]["tooth"] == "36"
    assert a[101]["call"] == {"uid": "c-0003", "started_at": "2026-11-17 14:20", "pool": False}
    assert a[101]["booked_at"] == "2026-11-17 14:20" and a[101]["booked_via"] == "phone"
    assert a[102]["unconfirmed"] is False and a[102]["plan_step"] is None
    assert a[102]["call"] is None and a[102]["tooth"] is None
    assert a[104]["procedure"] == "진찰" and a[104]["new_patient"] is True
    assert a[101]["new_patient"] is False


def test_schedule_includes_cancelled_overlap(conn):
    day = schedule_day(conn, TODAY, TODAY, START)["appointments"]
    assert sorted(x["status"] for x in day if x["time"] == "11:00") == ["cancelled", "scheduled"]


def test_schedule_counts_skip_cancelled(conn):
    assert schedule_day(conn, TODAY, TODAY, START)["counts"] == {
        "total": 3,
        "treatment": 2,
        "hygiene": 1,
        "consult": 0,
        "unconfirmed": 2,
    }


def test_unconfirmed_only_on_today(conn):
    d = schedule_day(conn, date(2026, 12, 15), TODAY, START)
    assert [x["unconfirmed"] for x in d["appointments"]] == [False]
    assert d["counts"]["unconfirmed"] == 0


def test_schedule_closed_day(conn):
    assert schedule_day(conn, date(2026, 12, 3), TODAY, START)["hours"]["closed"] is True


def test_schedule_saturday_close(conn):
    h = schedule_day(conn, date(2026, 12, 5), TODAY, START)["hours"]
    assert (h["close"], h["closed"], h["open"], h["lunch"]) == ("13:30", False, "09:30", None)


def test_schedule_weekday_hours(conn):
    h = schedule_day(conn, TODAY, TODAY, START)["hours"]
    assert (h["open"], h["close"], h["lunch"], h["closed"]) == (
        "09:30",
        "18:30",
        ["13:00", "14:00"],
        False,
    )


def test_schedule_range(conn):
    assert schedule_day(conn, TODAY, TODAY, START)["range"] == {
        "from": "2025-11-20",  # 가장 이른 예약(92)
        "to": "2026-12-15",
    }


def test_schedule_route_default_and_bad_date(client):
    assert client.get("/emr/schedule").json()["date"] == "2026-12-01"
    assert (
        client.get("/emr/schedule", params={"date": "2026-12-15"}).json()["date"] == "2026-12-15"
    )
    assert client.get("/emr/schedule", params={"date": "2026-13-01"}).status_code == 422


def test_schedule_route_without_index(client_source_only):
    assert client_source_only.get("/emr/schedule").status_code == 200  # app.sqlite가 없어도 된다


def test_summary_today_boundaries(conn):
    s = patient_summary(conn, 1, TODAY)
    assert s["balance"] == 85000  # 기준일 당일 수납분 제외
    assert [a["code"] for a in s["alerts"]] == ["ANTICOAG"]  # ends_at 당일부터 안 보임
    assert s["last_visit"] == "2026-11-17"
    assert (s["noshow_1y"], s["cancelled_1y"]) == (2, 0)  # 91·93(경계 당일), 94·92는 밖
    assert [u["id"] for u in s["upcoming"]] == [101]
    assert s["upcoming"][0]["staff"] == "강태현"


def test_summary_overpay_is_zero_and_future_alert_hidden(conn):
    assert patient_summary(conn, 2, TODAY)["balance"] == 0
    assert patient_summary(conn, 3, TODAY)["alerts"] == []


def test_search_patients(conn):
    found = search_patients(conn, "c-0000", TODAY)
    assert [p["chart_no"] for p in found] == ["C-00001", "C-00002", "C-00003"]
    assert search_patients(conn, "영희", TODAY)[0] == {
        "chart_no": "C-00002",
        "name": "박영희",
        "age": 46,
        "sex": "남",
    }


def test_patient_record_lists(conn):
    r = patient_record(conn, "c-00001", TODAY)
    assert r["patient"]["chart_no"] == "C-00001" and r["summary"]["balance"] == 85000
    confirms = [m for m in r["messages"] if m["trigger"] == "appt_confirm"]
    assert [m["related_appointment_id"] for m in confirms] == [101]
    assert {p["status"] for p in r["plans"]} == {"done", "open", "replaced"}
    assert [a["id"] for a in r["appointments"]] == [92, 94, 93, 91, 90, 101]
    assert patient_record(conn, "C-99999", TODAY) is None


def test_patient_routes(client):
    assert client.get("/emr/patients", params={"q": " "}).status_code == 422
    assert client.get("/emr/patients", params={"q": "철수"}).json()[0]["chart_no"] == "C-00001"
    assert client.get("/emr/patients/C-99999").status_code == 404
    assert client.get("/emr/patients/c-00001").json()["summary"]["balance"] == 85000


def test_tour_picks_most_items(conn):
    t = tour_patient(conn, TODAY)
    assert (t["chart_no"], t["name"], t["appointment_id"], t["time"]) == (
        "C-00001",
        "김철수",
        101,
        "10:00",
    )
    # 212는 오늘 예약이 이행하는 항목이라 빠지고 213이 남는다
    assert t["has"] == {"balance": True, "alert": True, "remaining": True}
    assert t["appointments_today"] == 3


def test_tour_counts_missing(emr_paths):
    rw(emr_paths, "UPDATE emr_treatment_plan_items SET status = 'done' WHERE id = 213")
    t = tour_patient(ro(emr_paths), TODAY)
    assert t["chart_no"] == "C-00001" and t["has"]["remaining"] is False


def test_tour_tie_prefers_unconfirmed_then_time(emr_paths):
    # 모두 0개면 미확인인 101(10:00)·104(11:00) 중 이른 101. 102는 확인돼 뒤로
    rw(
        emr_paths,
        "DELETE FROM emr_patient_alerts WHERE patient_id = 1",
        "DELETE FROM emr_payments WHERE patient_id = 1",
        "UPDATE emr_treatment_plan_items SET status = 'done' WHERE id = 213",
    )
    assert tour_patient(ro(emr_paths), TODAY)["chart_no"] == "C-00001"


def test_tour_tie_unconfirmed_beats_earlier_confirmed(emr_paths):
    # 102(09:30)는 확인됐고 104(11:00)는 미확인 — 101을 확인 처리하면 104가 뽑힌다
    rw(
        emr_paths,
        "DELETE FROM emr_patient_alerts WHERE patient_id = 1",
        "DELETE FROM emr_payments WHERE patient_id = 1",
        "UPDATE emr_treatment_plan_items SET status = 'done' WHERE id = 213",
        "UPDATE emr_appointments SET confirmed = 1 WHERE id = 101",
    )
    assert tour_patient(ro(emr_paths), TODAY)["chart_no"] == "C-00003"


def test_tour_route(client, emr_paths):
    assert client.get("/emr/tour").json()["chart_no"] == "C-00001"
    rw(emr_paths, "DELETE FROM emr_appointments WHERE date(scheduled_at) = '2026-12-01'")
    assert client.get("/emr/tour").status_code == 404


def test_confirm_status_from_reminder(conn):
    a = {x["id"]: x for x in schedule_day(conn, TODAY, TODAY, START)["appointments"]}
    assert a[101]["confirm"] == "no_reply"  # 리마인드에 답 없음
    assert a[102]["confirm"] == "replied"  # 확인됨
    assert a[104]["confirm"] == "no_message"  # 리마인드 자체가 없음(전날 잡은 응급 등)
    r = patient_record(conn, "C-00001", TODAY)
    assert {x["id"]: x["confirm"] for x in r["appointments"]}[101] == "no_reply"


def test_reply_without_confirmed_is_not_replied(emr_paths):
    # 답장이 있어도 확인 여부(confirmed — 전날 리마인드 응답)가 0이면 확인됨이 아니다.
    # 예약을 잡을 때 보낸 확정 알림에 대한 답장일 수 있다 — 격자 '내원 확인 전'과 어긋나지 않게
    rw(
        emr_paths,
        'INSERT INTO emr_messages (patient_id, direction, sent_at, body, "trigger",'
        " related_appointment_id) VALUES (1, 'in', '2026-11-17 15:00:00', '네', 'reply', 101)",
    )
    a = {x["id"]: x for x in schedule_day(ro(emr_paths), TODAY, TODAY, START)["appointments"]}
    assert (a[101]["confirm"], a[101]["unconfirmed"]) == ("no_reply", True)


def test_plan_step_skips_replaced_and_open_items(conn):
    a = {x["id"]: x for x in schedule_day(conn, TODAY, TODAY, START)["appointments"]}
    assert a[101]["plan_step"] == {"seq": 2, "total": 3} and a[101]["plan_item_id"] == 212
    items = patient_summary(conn, 1, TODAY)["open_items"]
    assert [(i["id"], i["procedure"], i["tooth"]) for i in items] == [
        (212, "크라운", "36"),
        (213, "크라운", "47"),
    ]


def test_search_escapes_like_wildcards(conn):
    assert search_patients(conn, "%", TODAY) == []
    assert search_patients(conn, "_", TODAY) == []


def test_schedule_booking_call_in_pool(conn, emr_paths):
    """판정 풀 통화는 통화 화면에서 열 수 없다 — 화면이 링크를 걸지 않게 표시만"""
    with sqlite3.connect(emr_paths[1]) as w:
        w.execute("UPDATE ax_calls SET in_eval_pool = 1 WHERE call_uid = 'c-0003'")
    a = {x["id"]: x for x in schedule_day(conn, TODAY, TODAY, START)["appointments"]}
    assert a[101]["call"]["pool"] is True


def test_schedule_route_booking_call_emergency(emr_paths, client):
    """잡은 통화의 응급 여부는 앱 DB의 활성 분류에서 — 예약표 패널이 AI 표시와 함께 보인다"""
    from backend import db
    from tests.backend.call_db import LABELS, classify_row

    c = db.connect(emr_paths[0], emr_paths[1])
    db.init_schema(c)
    db.set_setting(c, "active_prompt_version", "v2")
    classify_row(c, "c-0003", labels=LABELS | {"emergency": 1})
    c.close()
    a = {x["id"]: x for x in client.get("/emr/schedule").json()["appointments"]}
    assert a[101]["call"]["emergency"] is True


def test_schedule_route_booking_call_unclassified(client_source_only):
    """앱 DB가 없거나 분류 전이면 None — 화면은 분류 전 표시"""
    a = {x["id"]: x for x in client_source_only.get("/emr/schedule").json()["appointments"]}
    assert a[101]["call"]["emergency"] is None
