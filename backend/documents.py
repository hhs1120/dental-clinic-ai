"""문서함 (기능 정의 §4 ③ 문서함, 설계 2026-11-09 검색 요약 "문서함 API"). ax_documents를 그대로 읽기만 한다 — 공지는
같은 제목의 최신 버전만 목록에 내고, 본문에는 그 제목의 모든 버전을 붙인다. 최신 규칙은 검색의 옛 공지 정렬과 같다.
schema: 앱 연결(source.sqlite ATTACH)은 "source", source.sqlite만 연 연결은 "main"."""

import sqlite3

COLUMNS = "id, type, title, version, effective_date"


def replacements(conn: sqlite3.Connection, schema: str = "source") -> dict[int, int]:
    """옛 공지 → 같은 제목의 최신 공지(시행일 내림차순, 같으면 id 큰 것). 옛 값이 답이 되지 않게 코드로 정한다(앱 설계 §3)"""
    latest, out = {}, {}
    rows = conn.execute(
        f"SELECT id, title FROM {schema}.ax_documents WHERE type = 'notice'"
        " ORDER BY effective_date DESC, id DESC"
    )
    for doc_id, title in rows:
        if title in latest:
            out[doc_id] = latest[title]
        else:
            latest[title] = doc_id
    return out


def _row(r) -> dict:
    return dict(zip(("id", "type", "title", "version", "effective_date"), r, strict=True))


def list_documents(conn: sqlite3.Connection, schema: str = "source") -> list[dict]:
    """옛 공지를 뺀 목록 — 공지는 시행일 최근 순, 나머지는 제목 순"""
    old = replacements(conn, schema)
    rows = conn.execute(
        f"SELECT {COLUMNS} FROM {schema}.ax_documents"
        " ORDER BY type = 'notice' DESC,"
        " CASE WHEN type = 'notice' THEN effective_date END DESC, title, id"
    )
    return [_row(r) for r in rows if r[0] not in old]


def get_document(conn: sqlite3.Connection, doc_id: int, schema: str = "source") -> dict | None:
    """본문 + versions(같은 제목 공지 전부, 최신 먼저 — 공지가 아니면 빈 목록) + latest_id"""
    r = conn.execute(
        f"SELECT {COLUMNS}, body FROM {schema}.ax_documents WHERE id = ?", (doc_id,)
    ).fetchone()
    if r is None:
        return None
    out = {**_row(r[:5]), "body": r[5], "versions": [], "latest_id": doc_id}
    if out["type"] == "notice":
        out["versions"] = [
            {"id": v[0], "version": v[1], "effective_date": v[2]}
            for v in conn.execute(
                f"SELECT id, version, effective_date FROM {schema}.ax_documents"
                " WHERE type = 'notice' AND title = ? ORDER BY effective_date DESC, id DESC",
                (out["title"],),
            )
        ]
        out["latest_id"] = out["versions"][0]["id"]
    return out
