"""앱 DB 연결 (앱 설계 머리말·§6) — 쓰지 못하는 것을 연결 방식으로 지킨다."""

import sqlite3

import pytest

from backend import db


def test_source_is_readable(conn):
    assert conn.execute("SELECT chart_no FROM source.emr_patients").fetchone()[0] == "C-1"


def test_source_write_is_refused_by_connection(conn):
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("INSERT INTO source.emr_patients VALUES ('C-2')")


def test_only_app_and_source_are_attached(conn):
    names = {r[1] for r in conn.execute("PRAGMA database_list")}
    assert names == {"main", "source"}


def test_init_schema_twice_keeps_rows(conn):
    db.set_setting(conn, "auto_policy", {"x": True})
    conn.commit()
    db.init_schema(conn)
    assert db.get_setting(conn, "auto_policy") == {"x": True}


def test_get_setting_default(conn):
    assert db.get_setting(conn, "없음", {}) == {}
