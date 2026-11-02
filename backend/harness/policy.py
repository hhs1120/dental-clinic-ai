"""소스 지문 (앱 설계 §7.3). 생성 데이터가 바뀌면 자동 처리 근거가 사라지므로 auto_policy를 전부 끈다.
앱 기동과 야간 실행 시작 때 부른다."""

import hashlib
import sqlite3
from pathlib import Path

from backend import db


def fingerprint(path: Path) -> str:
    with Path(path).open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def check_source(conn: sqlite3.Connection, source_path: Path) -> bool:
    """지문이 바뀌어 정책을 껐으면 True. 처음이면 지문만 저장한다. 커밋은 부르는 쪽"""
    now = fingerprint(source_path)
    old = db.get_setting(conn, "source_fingerprint")
    changed = old is not None and old != now
    if changed:
        db.set_setting(conn, "auto_policy", {})
    db.set_setting(conn, "source_fingerprint", now)
    return changed
