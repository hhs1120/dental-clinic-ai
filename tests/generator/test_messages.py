"""8단계 문자 — 실제로 생성해 시각·문구 규칙을 본다 (가상 데이터 생성 설계 §2.5). 트리거 대응·순서 불변식은
verify 필수 검사(tests/generator/test_records.py가 전부 돌림)."""

import json
import sqlite3
from datetime import date, datetime, timedelta

import pytest
import yaml

from generator import build
from generator.calls import next_open_day
from generator.master import (
    CLINIC_NAME,
    HOURS,
    MESSAGE_CONFIRM_DELAY_MIN,
    MESSAGE_DELAY_MIN,
    MESSAGE_REPLIES,
    MESSAGE_REPLIES_PAID,
    MESSAGE_REPLIES_PAID_METHOD,
    MESSAGE_REPLY_CANCEL,
)
from generator.paths import ROOT

CONFIG = ROOT / "config" / "generator.yaml"
FMT = "%Y-%m-%d %H:%M:%S"
CFG = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
HOLIDAYS = frozenset(
    date.fromisoformat(d) for d in json.loads((ROOT / CFG["holidays"]).read_text(encoding="utf-8"))
)


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    out = tmp_path_factory.mktemp("messages")
    build.generate(out / "source.sqlite", out / "truth.sqlite", CONFIG, fake_llm=True)
    conn = sqlite3.connect(out / "source.sqlite")
    conn.row_factory = sqlite3.Row
    conn.execute("ATTACH DATABASE ? AS truth", (str(out / "truth.sqlite"),))
    return conn


def _minutes(a: str, b: str) -> float:
    return (datetime.strptime(b, FMT) - datetime.strptime(a, FMT)).total_seconds() / 60


def test_reminder_sent_at_opening(db):
    times = {
        r[0][11:16]
        for r in db.execute("SELECT sent_at FROM emr_messages WHERE \"trigger\" = 'reminder'")
    }
    assert times == {HOURS["open"]}


def test_appt_confirm_delay(db):
    lo, hi = MESSAGE_CONFIRM_DELAY_MIN
    rows = db.execute(
        "SELECT m.sent_at, a.created_at FROM emr_messages m JOIN emr_appointments a"
        " ON a.id = m.related_appointment_id WHERE m.\"trigger\" = 'appt_confirm'"
    ).fetchall()
    assert rows and all(lo <= _minutes(created, sent) <= hi for sent, created in rows)


def test_reply_delay_and_phrases(db):
    lo, hi = MESSAGE_DELAY_MIN
    replies = db.execute(
        "SELECT i.sent_at, i.body, (SELECT o.\"trigger\" FROM emr_messages o WHERE o.direction = 'out'"
        " AND o.patient_id = i.patient_id AND o.related_appointment_id IS i.related_appointment_id"
        " AND o.sent_at < i.sent_at ORDER BY o.sent_at DESC LIMIT 1) AS trig,"
        " (SELECT max(o.sent_at) FROM emr_messages o WHERE o.direction = 'out'"
        " AND o.patient_id = i.patient_id AND o.related_appointment_id IS i.related_appointment_id"
        " AND o.sent_at < i.sent_at) AS out_at FROM emr_messages i WHERE i.direction = 'in'"
        " AND i.body != ?",  # 취소 답장(8a단계)은 test_emergency
        (MESSAGE_REPLY_CANCEL,),
    ).fetchall()
    assert replies
    assert all(
        r["body"] in MESSAGE_REPLIES[r["trig"]]
        or (
            r["trig"] == "balance"
            and (
                r["body"] in MESSAGE_REPLIES_PAID
                or r["body"] in MESSAGE_REPLIES_PAID_METHOD.values()
            )
        )  # 이미 냈다는 답(§2.5)
        for r in replies
    )
    assert {r["trig"] for r in replies} >= {"recall", "balance"}
    assert all(lo <= _minutes(r["out_at"], r["sent_at"]) <= hi for r in replies)


def test_noshow_delay_after_visit_end(db):
    lo, hi = MESSAGE_DELAY_MIN
    rows = db.execute(
        "SELECT m.sent_at, a.scheduled_at, a.duration_min FROM emr_messages m JOIN emr_appointments a"
        " ON a.id = m.related_appointment_id WHERE m.\"trigger\" = 'noshow'"
    ).fetchall()

    def end(at, d):
        return (datetime.strptime(at, FMT) + timedelta(minutes=d)).strftime(FMT)

    assert rows and all(lo <= _minutes(end(at, d), sent) <= hi for sent, at, d in rows)


def test_balance_and_doc_ready_on_next_clinic_day(db):
    """미수 안내 = 미수가 생긴 내원 수납일의 다음 진료일, 서류 준비 = 서류 요청 inbound 통화일의 다음 진료일"""
    balance = db.execute(
        "SELECT m.sent_at, p.paid_at FROM emr_messages m JOIN emr_payments p"
        " ON p.appointment_id = m.related_appointment_id AND p.kind = 'visit'"
        " WHERE m.\"trigger\" = 'balance'"
    ).fetchall()
    assert balance and all(
        sent[:10] == next_open_day(date.fromisoformat(paid[:10]), HOLIDAYS).isoformat()
        for sent, paid in balance
    )
    calls = db.execute(
        "SELECT c.patient_id, c.started_at, c.direction FROM ax_calls c JOIN truth.truth_call_labels l"
        " USING (call_uid) WHERE l.primary_purpose = 'document_req' AND c.patient_id IS NOT NULL"
    ).fetchall()
    want = sorted(
        (pid, next_open_day(date.fromisoformat(t[:10]), HOLIDAYS).isoformat())
        for pid, t, d in calls
        if d == "inbound"
    )
    got = sorted(
        (r[0], r[1][:10])
        for r in db.execute(
            "SELECT patient_id, sent_at FROM emr_messages WHERE \"trigger\" = 'doc_ready'"
        )
    )
    assert got and set(got) <= set(want) and len(got) <= len(want)


def test_out_bodies_name_clinic_and_patient_without_tomorrow(db):
    rows = db.execute(
        "SELECT m.body, p.name FROM emr_messages m JOIN emr_patients p ON p.id = m.patient_id"
        " WHERE m.direction = 'out'"
    ).fetchall()
    assert all(
        body.startswith(f"[{CLINIC_NAME}] {name}님,") and "내일" not in body for body, name in rows
    )
