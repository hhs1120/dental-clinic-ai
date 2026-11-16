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
from shared.llm import load_config

MAX_CHARS = 500
OVERLAP = 50  # 문서 — 통화는 대화 단위(split_dialog)
CARRY_MAX = 150  # 통화 조각 앞에 붙이는 직전 발화 상한(자) — 넘으면 그 발화의 끝부분만
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


def split_text(text: str, overlap: int = OVERLAP) -> list[str]:
    """줄 경계로 MAX_CHARS 안에 나눈다. 다음 조각은 앞 조각의 끝 overlap자로 시작한다(겹침 비교는 AI 실험 기록 §6)"""
    text = text.strip()
    if len(text) <= MAX_CHARS:
        return [text] if text else []
    room = MAX_CHARS - overlap - 1
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
        current = f"{current[-overlap:]}\n{unit}"
    pieces.append(current)
    return pieces


_SPEAKER = re.compile(r"^([^:：\n\d]{1,10})[:：]")  # 숫자 없는 표기만 — '10:30에'는 화자가 아니다


def _turns(text: str) -> list[str]:
    """같은 화자가 이어 말한 줄을 한 발화로. 표기 없는 줄은 앞 발화에 붙인다"""
    turns, who = [], None
    for line in (x.strip() for x in text.strip().split("\n")):
        if not line:
            continue
        m = _SPEAKER.match(line)
        if turns and (m is None or m[1] == who):
            turns[-1] += "\n" + line
        else:
            turns.append(line)
        who = m[1] if m else who
    return turns


def _carry(turn: str) -> str:
    """다음 조각 앞에 붙일 직전 발화 — CARRY_MAX를 넘으면 화자 표기 + 끝부분"""
    if len(turn) <= CARRY_MAX:
        return turn
    m = _SPEAKER.match(turn)
    head = f"{m[1]}: …" if m else "…"
    return head + turn[-(CARRY_MAX - len(head)) :]


def split_dialog(text: str) -> list[str]:
    """통화 녹취록을 발화 사이에서만 MAX_CHARS 안으로 나눈다. 다음 조각은 직전 발화로 시작해 이웃한 두 발화(질문→답)가
    늘 한 조각에 함께 있다(글자 겹침은 긴 발화에서 갈라진다 — AI 실험 기록 §6). 화자 표기가 없으면 split_text,
    한 발화가 MAX_CHARS를 넘으면 그 발화만 글자로 나눈다"""
    text = text.strip()
    if len(text) <= MAX_CHARS:
        return [text] if text else []
    if not any(_SPEAKER.match(x.strip()) for x in text.split("\n")):
        return split_text(text)
    pieces, current = [], []
    for turn in _turns(text):
        if len(turn) > MAX_CHARS:
            if current:
                pieces.append("\n".join(current))
            pieces += split_text(turn)
            current = [_carry(turn)]  # 다음 조각도 이 발화(끝부분)로 시작 — 다음 답과 갈리지 않게
            continue
        if len("\n".join([*current, turn])) <= MAX_CHARS:
            current.append(turn)
            continue
        pieces.append("\n".join(current))
        carry = _carry(current[-1])
        current = [carry, turn] if len(carry) + 1 + len(turn) <= MAX_CHARS else [turn]
    if current:
        pieces.append("\n".join(current))
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


def _documents(conn, overlap: int = OVERLAP) -> list[dict]:
    out = []
    for d in conn.execute("SELECT * FROM source.ax_documents ORDER BY id"):
        n = 0
        for section, text in _sections(d["title"], d["body"]):
            prefix = d["title"] if section == d["title"] else f"{d['title']} > {section}"
            for piece in split_text(text, overlap):
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
    """생성 통화 + 화면에서 붙여넣은 통화(ax_pasted_calls — 상대 이름이 없어 환자가 아니면 '미상')"""
    out = []
    for c in conn.execute(
        "SELECT c.call_uid, c.direction, c.started_at, c.transcript, c.caller_name,"
        " p.chart_no, p.name FROM source.ax_calls c"
        " LEFT JOIN source.emr_patients p ON p.id = c.patient_id"
        " WHERE c.has_transcript = 1"
        " UNION ALL SELECT c.call_uid, c.direction, c.started_at, c.transcript, '미상',"
        " p.chart_no, p.name FROM ax_pasted_calls c"
        " LEFT JOIN source.emr_patients p ON p.id = c.patient_id"
        " ORDER BY call_uid"
    ):
        day = c["started_at"][:10]
        who = (
            f"환자: {c['name']}({c['chart_no']})" if c["chart_no"] else f"상대: {c['caller_name']}"
        )
        prefix = f"[{who} | {day} | {DIRECTION[c['direction']]}]"
        for n, piece in enumerate(split_dialog(c["transcript"])):
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


def collect_chunks(conn: sqlite3.Connection, overlap: int = OVERLAP) -> list[dict]:
    return [*_documents(conn, overlap), *_calls(conn), *_threads(conn), *_charts(conn)]


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


def build_index(conn: sqlite3.Connection, embedder, overlap: int = OVERLAP) -> int:
    """청크·FTS를 다시 쓰고 이 모델의 벡터가 없는 청크만 계산한다(글이 바뀐 청크는 벡터를 지워 다시). 새로 계산한 수"""
    _write_chunks(conn, collect_chunks(conn, overlap))
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


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="검색 색인 (앱 설계 §3)")
    parser.add_argument("--embedding", required=True)
    parser.add_argument("--stats", action="store_true", help="청크 수만 보고 임베딩하지 않는다")
    parser.add_argument("--overlap", type=int, default=OVERLAP, help="청크 겹침(자) — 비교 실험용")
    parser.add_argument(
        "--db", type=Path, default=ROOT / "data" / "app.sqlite", help="색인할 app.sqlite(사본)"
    )
    args = parser.parse_args(argv)
    conn = db.connect(args.db, ROOT / "data" / "source.sqlite")
    db.init_schema(conn)
    if args.stats:
        chunks = collect_chunks(conn, args.overlap)
        print(f"청크 {len(chunks)}개, {sum(len(c['text']) for c in chunks)}자")
        return
    embedder = make_embedder(args.embedding)
    print(f"{args.embedding}: 새로 계산 {build_index(conn, embedder, args.overlap)}개")
    # 색인 임베딩은 하루 상한에 넣지 않고 비용만 알린다(설계 2026-11-09 챗 "Voyage 비용")
    tokens = getattr(embedder, "total_tokens", 0)
    if tokens:
        price = load_config()["models"].get(args.embedding, {}).get("input", 0)
        print(f"임베딩 {tokens:,}토큰 · ${tokens * price / 1_000_000:.4f}")


if __name__ == "__main__":
    main()
