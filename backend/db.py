"""앱 DB 연결 (앱 설계 머리말·§6). app.sqlite에 쓰고 source.sqlite는 읽기 전용으로 붙인다 — emr_·fin_을 쓰지 않는다는
규칙을 연결 방식으로 지킨다. 다른 DB는 붙이지 않는다."""

import json
import sqlite3
from pathlib import Path

import sqlite_vec

SCHEMA = Path(__file__).resolve().parents[1] / "shared" / "app_schema.sql"


def connect(app_path: Path, source_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(app_path).resolve().as_uri(), uri=True)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "ATTACH DATABASE ? AS source", (f"{Path(source_path).resolve().as_uri()}?mode=ro",)
    )
    return conn


def init_schema(conn: sqlite3.Connection, schema: Path = SCHEMA) -> None:
    """빈 DB면 스키마 전체, 스키마보다 앞선 DB면 없는 테이블·인덱스·칸만 더한다(있던 행은 그대로).
    더한 칸은 타입·NOT NULL·기본값만 옮긴다. NOT NULL에 기본값 없는 칸은 기존 행을 채울 수 없어 RuntimeError"""
    sql = schema.read_text(encoding="utf-8")
    if not conn.execute("SELECT 1 FROM main.sqlite_master").fetchone():
        conn.executescript(sql)
        return
    ref = sqlite3.connect(":memory:")
    ref.executescript(sql)
    for name, kind, create in ref.execute(
        "SELECT name, type, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY rowid"
    ).fetchall():
        exists = conn.execute(
            "SELECT 1 FROM main.sqlite_master WHERE name = ?", (name,)
        ).fetchone()
        if not exists:
            conn.execute(create)  # 가상 테이블의 그림자 테이블은 이때 같이 생겨 다음 차례에 있다
        elif kind == "table":
            _add_columns(conn, ref, name)
    ref.close()
    conn.commit()


def _add_columns(conn: sqlite3.Connection, ref: sqlite3.Connection, table: str) -> None:
    have = {r[1] for r in conn.execute(f"PRAGMA main.table_info({table})")}
    for _cid, col, type_, notnull, default, _pk in ref.execute(f"PRAGMA table_info({table})"):
        if col in have:
            continue
        if notnull and default is None:
            raise RuntimeError(
                f"{table}.{col}: NOT NULL에 기본값 없는 칸은 기존 DB에 더할 수 없음"
            )
        conn.execute(
            f"ALTER TABLE {table} ADD COLUMN {col} {type_}"
            + (" NOT NULL" if notnull else "")
            + (f" DEFAULT {default}" if default is not None else "")
        )


def get_setting(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM ax_settings WHERE key = ?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def set_setting(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT INTO ax_settings VALUES (?, ?)"
        " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value, ensure_ascii=False)),
    )
