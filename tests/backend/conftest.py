"""앱 테스트 공용 — 임시 app.sqlite + 읽기 전용 source.sqlite, 움직이는 시계."""

import sqlite3
from datetime import datetime, timedelta

import pytest

from backend import db


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
    conn.commit()
    conn.close()
    return tmp_path / "app.sqlite", source


@pytest.fixture
def conn(paths):
    c = db.connect(*paths)
    db.init_schema(c)
    yield c
    c.close()
