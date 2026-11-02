"""색인 (`make index`, 앱 설계 §3). 생성 데이터를 청크로 나눠 ax_chunks·FTS5·벡터에 넣는다. 청크 id는 출처에서 결정적이고,
벡터는 없는 청크만 계산하며 배치마다 커밋한다 — 끊겨도 다시 돌리면 이어 간다."""

import argparse
import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

from backend import db
from backend.embeddings import BATCH, make_embedder
from backend.vector_store import VectorStore

MAX_CHARS = 500
OVERLAP = 50
ROOT = Path(__file__).resolve().parents[1]
DIRECTION = {"inbound": "수신", "outbound": "발신"}
_HEADER = re.compile(r"^#{1,6}\s+(.+)$", re.M)
_MONTHS = re.compile(r"(\d+)개월")
_DAYS_AGO = re.compile(r"(?<!월 )(?<!월)(?<!\d)(\d+)일 전")
QUADRANT = {"1": "오른쪽 위", "2": "왼쪽 위", "3": "왼쪽 아래", "4": "오른쪽 아래"}
TOOTH_KIND = {"1": "앞니", "2": "앞니", "3": "송곳니", "4": "작은어금니", "5": "작은어금니"}
TOOTH_KIND |= {"6": "어금니", "7": "어금니", "8": "사랑니"}
# 다른 시술을 뜻하는 말 중 차트에서 이미 시행한 일로만 쓰이는 것. 별칭을 넓히면 받지 않은 시술이 붙는다
NOTE_SYNONYMS = {"치석제거": "스케일링"}


def split_text(text: str) -> list[str]:
    """줄 경계로 MAX_CHARS 안에 나눈다. 다음 조각은 앞 조각의 끝 OVERLAP자로 시작한다"""
    text = text.strip()
    if len(text) <= MAX_CHARS:
        return [text] if text else []
    room = MAX_CHARS - OVERLAP - 1
    units = []
    for line in text.split("\n"):
        line = line.strip()
        units += [line[i : i + room] for i in range(0, len(line), room)]
    pieces, current = [], ""
    for unit in units:
        joined = f"{current}\n{unit}" if current else unit
        if len(joined) <= MAX_CHARS:
            current = joined
            continue
        pieces.append(current)
        current = f"{current[-OVERLAP:]}\n{unit}"
    pieces.append(current)
    return pieces


def _sections(title: str, body: str) -> list[tuple[str, str]]:
    """(절 제목, 본문). 헤더가 없으면 문서 제목 하나"""
    marks = list(_HEADER.finditer(body))
    if not marks:
        return [(title, body)]
    out = [(title, body[: marks[0].start()])]
    for m, end in zip(marks, [*(x.start() for x in marks[1:]), len(body)], strict=True):
        out.append((m.group(1).strip(), body[m.end() : end]))
    return out


def _documents(conn) -> list[dict]:
    out = []
    for d in conn.execute("SELECT * FROM source.ax_documents ORDER BY id"):
        n = 0
        for section, text in _sections(d["title"], d["body"]):
            prefix = d["title"] if section == d["title"] else f"{d['title']} > {section}"
            for piece in split_text(text):
                out.append(
                    {
                        "id": f"doc:{d['id']}:{n}",
                        "source_type": "document",
                        "text": f"{prefix}\n{piece}",
                        "chart_no": None,
                        "event_date": d["effective_date"],
                        "doc_type": d["type"],
                    }
                )
                n += 1
    return out


def _calls(conn) -> list[dict]:
    out = []
    for c in conn.execute(
        "SELECT c.*, p.chart_no, p.name FROM source.ax_calls c"
        " LEFT JOIN source.emr_patients p ON p.id = c.patient_id"
        " WHERE c.has_transcript = 1 ORDER BY c.id"
    ):
        day = c["started_at"][:10]
        who = (
            f"환자: {c['name']}({c['chart_no']})" if c["chart_no"] else f"상대: {c['caller_name']}"
        )
        prefix = f"[{who} | {day} | {DIRECTION[c['direction']]}]"
        for n, piece in enumerate(split_text(c["transcript"])):
            out.append(
                {
                    "id": f"call:{c['call_uid']}:{n}",
                    "source_type": "call",
                    "text": f"{prefix}\n{piece}",
                    "chart_no": c["chart_no"],
                    "event_date": day,
                    "doc_type": None,
                }
            )
    return out


def _threads(conn) -> list[dict]:
    months = defaultdict(list)
    names = {}
    for m in conn.execute(
        "SELECT m.direction, m.sent_at, m.body, p.chart_no, p.name FROM source.emr_messages m"
        " JOIN source.emr_patients p ON p.id = m.patient_id ORDER BY m.sent_at, m.id"
    ):
        months[m["chart_no"], m["sent_at"][:7]].append(m)
        names[m["chart_no"]] = m["name"]
    out = []
    for (chart_no, month), rows in sorted(months.items()):
        lines = " ".join(
            f"{m['sent_at'][5:10]} {'답장: ' if m['direction'] == 'in' else ''}{m['body']}"
            for m in rows
        )
        out.append(
            {
                "id": f"thread:{chart_no}:{month}",
                "source_type": "message_thread",
                "text": f"[환자: {names[chart_no]}({chart_no}) | {month}] {lines}",
                "chart_no": chart_no,
                "event_date": rows[-1]["sent_at"][:10],
                "doc_type": None,
            }
        )
    return out


def tooth_terms(tooth_no: str) -> list[str]:
    """치아 번호(FDI) → 부위 말. 차트에는 번호만 있어 말로 찾지 못한다"""
    if tooth_no[0] not in QUADRANT or tooth_no[1] not in TOOTH_KIND:
        return []
    return [QUADRANT[tooth_no[0]], TOOTH_KIND[tooth_no[1]]]


def _period_terms(note: str) -> list[str]:
    out = []
    for m in _MONTHS.findall(note):
        years, rest = divmod(int(m), 12)
        if years:
            out.append(
                f"{years}년 반" if rest == 6 else f"{years}년" + (f" {rest}개월" if rest else "")
            )
    for d in _DAYS_AGO.findall(note):
        weeks, rest = divmod(int(d), 7)
        if weeks and not rest:
            out += [f"{weeks}주 전", *(["일주일 전"] if weeks == 1 else [])]
    return out


def _name_terms(note: str, aliases: list[str]) -> list[str]:
    """그 차트 시술의 별칭과 정해 둔 동의어만 더한다. 서술에 나온 다른 시술 이름은 따라가지 않는다"""
    compact = note.replace(" ", "")
    words = [w.replace(" ", "") for w in aliases]
    words += [to for word, to in NOTE_SYNONYMS.items() if word in compact]
    return [w for w in words if w not in compact]


def chart_terms(note: str, tooth_no: str, aliases: list[str]) -> list[str]:
    """차트 서술과 다르게 말할 표현 (앱 설계 §3)"""
    return list(
        dict.fromkeys([*tooth_terms(tooth_no), *_period_terms(note), *_name_terms(note, aliases)])
    )


def _chart_text(r, aliases: dict) -> str:
    terms = chart_terms(r["note"], r["tooth_no"], aliases[r["procedure"]])
    tail = f" ({' '.join(terms)})" if terms else ""
    return f"[차트 {r['chart_no']} | {r['day']} | {r['procedure']}] {r['note']}{tail}"


def _charts(conn) -> list[dict]:
    aliases = {
        r["name"]: json.loads(r["aliases"])
        for r in conn.execute("SELECT name, aliases FROM source.emr_procedures")
    }
    return [
        {
            "id": f"chart:{r['treatment_id']}",
            "source_type": "chart_note",
            "text": _chart_text(r, aliases),
            "chart_no": r["chart_no"],
            "event_date": r["day"],
            "doc_type": None,
        }
        for r in conn.execute(
            "SELECT n.treatment_id, n.note, t.tooth_no, p.chart_no, date(t.performed_at) AS day,"
            " r.name AS procedure FROM source.emr_chart_notes n"
            " JOIN source.emr_treatments t ON t.id = n.treatment_id"
            " JOIN source.emr_patients p ON p.id = t.patient_id"
            " JOIN source.emr_procedures r ON r.code = t.procedure_code ORDER BY n.treatment_id"
        )
    ]


def collect_chunks(conn: sqlite3.Connection) -> list[dict]:
    return [*_documents(conn), *_calls(conn), *_threads(conn), *_charts(conn)]


def _write_chunks(conn: sqlite3.Connection, chunks: list[dict]) -> None:
    old = dict(conn.execute("SELECT id, text FROM ax_chunks").fetchall())
    new = {c["id"]: c["text"] for c in chunks}
    stale = [i for i, text in old.items() if new.get(i) != text]
    with conn:
        conn.executemany("DELETE FROM ax_chunk_vectors WHERE chunk_id = ?", [(i,) for i in stale])
        conn.executemany("DELETE FROM ax_chunks WHERE id = ?", [(i,) for i in old if i not in new])
        conn.executemany(
            "INSERT OR REPLACE INTO ax_chunks (id, source_type, text, chart_no, event_date,"
            " doc_type) VALUES (:id, :source_type, :text, :chart_no, :event_date, :doc_type)",
            chunks,
        )
        conn.execute("DELETE FROM ax_chunks_fts")
        conn.executemany(
            "INSERT INTO ax_chunks_fts VALUES (?, ?)", [(c["id"], c["text"]) for c in chunks]
        )


def build_index(conn: sqlite3.Connection, embedder) -> int:
    """청크·FTS를 다시 쓰고 이 모델의 벡터가 없는 청크만 계산한다. 새로 계산한 수를 돌려준다"""
    _write_chunks(conn, collect_chunks(conn))
    missing = conn.execute(
        "SELECT c.id, c.text FROM ax_chunks c LEFT JOIN ax_chunk_vectors v"
        " ON v.chunk_id = c.id AND v.model = ? WHERE v.chunk_id IS NULL ORDER BY c.id",
        (embedder.model,),
    ).fetchall()
    store = VectorStore(conn, embedder.model)
    for i in range(0, len(missing), BATCH):
        batch = missing[i : i + BATCH]
        with conn:
            store.upsert([r[0] for r in batch], embedder.embed([r[1] for r in batch], "document"))
    return len(missing)


def main() -> None:
    parser = argparse.ArgumentParser(description="검색 색인 (앱 설계 §3)")
    parser.add_argument("--embedding", required=True)
    parser.add_argument("--stats", action="store_true", help="청크 수만 보고 임베딩하지 않는다")
    args = parser.parse_args()
    conn = db.connect(ROOT / "data" / "app.sqlite", ROOT / "data" / "source.sqlite")
    db.init_schema(conn)
    if args.stats:
        chunks = collect_chunks(conn)
        print(f"청크 {len(chunks)}개, {sum(len(c['text']) for c in chunks)}자")
        return
    print(f"{args.embedding}: 새로 계산 {build_index(conn, make_embedder(args.embedding))}개")


if __name__ == "__main__":
    main()
