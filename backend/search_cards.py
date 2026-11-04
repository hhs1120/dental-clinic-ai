"""검색 결과 카드 칸 (앱 설계 §3·§9). 색인 청크의 prefix를 떼어 화면에 낼 본문과 이름·방향을 만든다.
prefix 형식의 원본은 backend/rag_index.py이고, 어긋나면 tests/backend/test_search_cards.py가 깨진다."""

import re
import sqlite3

_CALL = re.compile(
    r"\[(?:환자: (?P<name>.+)\((?P<chart>[^()]+)\)|상대: (?P<caller>.*)) \| [\d-]+ \| (?P<dir>수신|발신)\]\n"
)
_THREAD = re.compile(r"\[환자: (?P<name>.+)\([^()]+\) \| [\d-]+\] ")


def _match(pattern: re.Pattern, chunk: dict) -> re.Match:
    m = pattern.match(chunk["text"])
    if not m:
        raise ValueError(f"색인 prefix 형식이 아니다: {chunk['id']}")
    return m


def card(conn: sqlite3.Connection, chunk: dict, replaced_by: dict[int, int]) -> dict:
    """청크 한 줄 → 화면 카드. 그 출처에 없는 칸은 None (앱 뼈대 설계안 '백엔드')"""
    out = {
        "id": chunk["id"],
        "source_type": chunk["source_type"],
        "title": None,
        "body": None,
        "event_date": chunk["event_date"],
        "chart_no": chunk["chart_no"],
        "patient_name": None,
        "caller_name": None,
        "direction": None,
        "doc_type": chunk["doc_type"],
        "version": None,
        "is_outdated": None,
    }
    kind, text = chunk["source_type"], chunk["text"]
    if kind == "document":
        doc_id = int(chunk["id"].split(":")[1])
        out["title"], _, out["body"] = text.partition("\n")
        out["version"] = conn.execute(
            "SELECT version FROM source.ax_documents WHERE id = ?", (doc_id,)
        ).fetchone()[0]
        out["is_outdated"] = doc_id in replaced_by
    elif kind == "call":
        m = _match(_CALL, chunk)
        out["patient_name"], out["caller_name"] = m["name"], m["caller"]
        out["direction"], out["body"] = m["dir"], text[m.end() :]
    elif kind == "message_thread":
        m = _match(_THREAD, chunk)
        out["patient_name"], out["body"] = m["name"], text[m.end() :]
    elif kind == "chart_note":
        # 차트는 서술 자체에 괄호가 있어 text에서 검색용 꼬리를 잘라내지 않고 원문을 읽는다
        treatment_id = int(chunk["id"].split(":")[1])
        out["body"] = conn.execute(
            "SELECT note FROM source.emr_chart_notes WHERE treatment_id = ?", (treatment_id,)
        ).fetchone()[0]
    else:
        raise ValueError(f"모르는 출처: {kind}")
    return out
