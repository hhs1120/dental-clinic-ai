"""앱 DB 연결 (앱 설계 머리말·§6). app.sqlite에 쓰고 source.sqlite는 읽기 전용으로 붙인다 — emr_·fin_을 쓰지 않는다는
규칙을 연결 방식으로 지킨다. 다른 DB는 붙이지 않는다."""

import json
import sqlite3
from pathlib import Path

SCHEMA = Path(__file__).resolve().parents[1] / "shared" / "app_schema.sql"


def connect(app_path: Path, source_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(app_path).resolve().as_uri(), uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "ATTACH DATABASE ? AS source", (f"{Path(source_path).resolve().as_uri()}?mode=ro",)
    )
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """빈 DB에만 스키마를 만든다"""
    exists = conn.execute("SELECT 1 FROM main.sqlite_master WHERE name = 'ax_settings'").fetchone()
    if not exists:
        conn.executescript(SCHEMA.read_text(encoding="utf-8"))


def get_setting(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM ax_settings WHERE key = ?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def set_setting(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT INTO ax_settings VALUES (?, ?)"
        " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value, ensure_ascii=False)),
    )
