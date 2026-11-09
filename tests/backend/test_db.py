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


def test_init_schema_adds_missing_table_and_column(conn):
    """스키마보다 앞선 DB — 없는 테이블·칸만 더하고 있던 행은 그대로(앱 설계 §6)"""
    db.set_setting(conn, "auto_policy", {"x": True})
    conn.execute("DROP TABLE ax_call_disagreements")
    conn.execute("ALTER TABLE ax_call_classifications DROP COLUMN summary_llm_call_id")
    conn.commit()
    db.init_schema(conn)
    assert db.get_setting(conn, "auto_policy") == {"x": True}
    conn.execute("SELECT is_core FROM ax_call_disagreements").fetchall()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(ax_call_classifications)")}
    assert "summary_llm_call_id" in cols


def test_init_schema_refuses_not_null_column_without_default(conn, tmp_path):
    """NOT NULL이고 기본값 없는 칸은 기존 행에 채울 값이 없어 더하지 못한다 — 조용히 넘기지 않는다"""
    schema = tmp_path / "s.sql"
    schema.write_text(
        db.SCHEMA.read_text(encoding="utf-8").replace(
            "CREATE TABLE ax_settings (", "CREATE TABLE ax_settings (\n    extra TEXT NOT NULL,"
        ),
        "utf-8",
    )
    conn.execute("DROP TABLE ax_call_disagreements")
    conn.commit()
    with pytest.raises(RuntimeError, match="ax_settings.extra"):
        db.init_schema(conn, schema)
    # 한 트랜잭션 — 앞서 만든 테이블도 되돌린다(동시에 연 프로세스와 겹치지 않게)
    gone = "SELECT 1 FROM sqlite_master WHERE name = 'ax_call_disagreements'"
    assert conn.execute(gone).fetchone() is None
    assert not conn.in_transaction


def test_new_tables_exist(conn):
    """통화 화면 앱 테이블(통화·② 판정 화면 설계 '앱 테이블')"""
    for t in ("ax_pasted_calls", "ax_judge_state", "ax_todo_checks"):
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (t,)).fetchone()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO ax_pasted_calls (call_uid, direction, started_at, transcript, added_by,"
            " added_at) VALUES ('p-0001', 'sideways', '2026-12-01 09:00:00', '내용', '데스크',"
            " '2026-11-05 09:00:00')"
        )


def test_get_setting_default(conn):
    assert db.get_setting(conn, "없음", {}) == {}


def test_sqlite_vec_is_loaded(conn):
    assert conn.execute("SELECT vec_version()").fetchone()[0]


def test_extension_loading_is_closed_after_connect(conn):
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("SELECT load_extension('nothing')")


def test_app_schema_has_disagreements(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(ax_call_disagreements)")}
    assert cols == {
        "call_uid",
        "a_version",
        "b_version",
        "is_core",
        "fallback",
        "todo_mismatch",
        "items",
    }
    cls = {r[1] for r in conn.execute("PRAGMA table_info(ax_call_classifications)")}
    assert {"summary_model_actual", "summary_llm_call_id"} <= cls


def test_connect_uses_wal_and_busy_timeout(conn):
    """SSE 챗이 쓰는 동안 다른 화면의 쓰기가 막히지 않게 (설계 2026-11-09 챗 "동시 쓰기")"""
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == db.BUSY_TIMEOUT_MS


def test_search_logs_status_is_checked(conn):
    sql = "INSERT INTO ax_search_logs (kind, question, status, created_at) VALUES ('chat', 'q', ?, 'x')"
    conn.execute(sql, ("running",))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, ("weird",))
