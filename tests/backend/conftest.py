"""앱 테스트 공용 — 임시 app.sqlite + 읽기 전용 source.sqlite, 움직이는 시계."""

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from backend import db

SOURCE_SCHEMA = Path(__file__).resolve().parents[2] / "shared" / "source_schema.sql"


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 2, 3, 0, 0)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def paths(tmp_path):
    source = tmp_path / "source.sqlite"
    conn = sqlite3.connect(source)
    conn.execute("CREATE TABLE emr_patients (chart_no TEXT)")
    conn.execute("INSERT INTO emr_patients VALUES ('C-1')")
    conn.execute("CREATE TABLE ax_documents (id, type, title, effective_date)")
    conn.executemany(
        "INSERT INTO ax_documents VALUES (?, ?, ?, ?)",
        [
            (101, "notice", "휴진", "2024-01-01"),
            (102, "notice", "휴진", "2026-01-01"),
            (103, "notice", "주차", "2026-01-01"),
            (104, "manual", "휴진", "2020-01-01"),
        ],
    )
    conn.commit()
    conn.close()
    return tmp_path / "app.sqlite", source


@pytest.fixture
def conn(paths):
    c = db.connect(*paths)
    db.init_schema(c)
    yield c
    c.close()


@pytest.fixture
def source_paths(tmp_path):
    """실제 source 스키마에 출처마다 몇 줄 — 색인 prefix·카드 칸·API 테스트용. app.sqlite는 아직 없다"""
    source = tmp_path / "source.sqlite"
    conn = sqlite3.connect(source)
    conn.executescript(SOURCE_SCHEMA.read_text(encoding="utf-8"))
    conn.execute("INSERT INTO emr_clinic VALUES ('클로드치과', '{}')")
    conn.executemany(
        "INSERT INTO emr_patients (id, chart_no, name, birth_date, sex, phone, registered_at)"
        " VALUES (?, ?, ?, '1980-01-01', 'M', '010-0000-0000', '2024-01-01')",
        [(1, "C-00001", "김철수"), (2, "C-00002", "박영희"), (3, "C-00003", "이민수")],
    )
    conn.execute(
        "INSERT INTO emr_procedures (code, name, category, is_insured, fee_total,"
        " patient_share_rate, duration_min, is_tooth_level, aliases)"
        " VALUES ('scaling', '스케일링', 'preventive', 'yes', 0, 0.3, 30, 0, '[\"치석제거\"]')"
    )
    conn.execute("INSERT INTO emr_diagnoses VALUES ('K03', '치석')")
    conn.execute(
        "INSERT INTO emr_treatments VALUES (7, 1, 3, 'scaling', '16', '2026-07-03 10:00:00',"
        " 0, 0, 0)"
    )
    conn.execute(
        "INSERT INTO emr_chart_notes (treatment_id, dx_code, note)"
        " VALUES (7, 'K03', '통증 호소(좌측) 치석 제거 6개월 후 재내원')"
    )
    conn.executemany(
        "INSERT INTO ax_documents VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (1, "notice", "미백 비용", "v1", "2026-01-01", "n1.md", "# 변경 내용\n미백 30만원"),
            (2, "notice", "미백 비용", "v2", "2026-06-01", "n2.md", "# 변경 내용\n미백 35만원"),
            (3, "manual", "틀니 수리", "v1", None, "m1.md", "접수 후 기공소로 보낸다"),
        ],
    )
    conn.executemany(
        "INSERT INTO ax_calls (call_uid, source, patient_id, caller_name, direction, started_at,"
        " duration_sec, has_transcript, transcript) VALUES (?, 'generated', ?, ?, ?, ?, 60, 1, ?)",
        [
            ("c-0001", 1, None, "inbound", "2026-07-03 09:00:00", "틀니 수리 문의"),
            ("c-0002", None, "김미영", "outbound", "2026-07-04 09:00:00", "거래처 납품 확인"),
        ],
    )
    conn.executemany(
        'INSERT INTO emr_messages (patient_id, direction, sent_at, body, "trigger")'
        " VALUES (2, ?, ?, ?, ?)",
        [
            ("out", "2026-07-03 09:00:00", "예약 확인 부탁드립니다", "appt_confirm"),
            ("in", "2026-07-03 10:00:00", "네", "reply"),
        ],
    )
    conn.commit()
    conn.close()
    return tmp_path / "app.sqlite", source
