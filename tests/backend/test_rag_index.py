"""색인 (앱 설계 §3) — 청크 id·prefix·크기, 재실행 안전."""

import sqlite3
from datetime import date
from pathlib import Path

import numpy as np
import pytest

from backend import db
from backend.embeddings import FakeEmbedder
from backend.rag_index import (
    MAX_CHARS,
    OVERLAP,
    _sections,
    build_index,
    chart_terms,
    split_text,
    tooth_terms,
)
from backend.vector_store import VectorStore
from shared.clinic import Clinic

SOURCE_SCHEMA = Path(__file__).parents[2] / "shared" / "source_schema.sql"
CLINIC = Clinic(today=date(2026, 12, 1), range_start=date(2026, 1, 1))
LONG = "\n\n".join(f"문단 {i} " + "가나다라마바사 " * 12 for i in range(6))


@pytest.fixture
def conn(tmp_path):
    source = tmp_path / "s.sqlite"
    s = sqlite3.connect(source)
    s.executescript(SOURCE_SCHEMA.read_text(encoding="utf-8"))
    s.executemany(
        "INSERT INTO ax_documents (id, type, title, version, effective_date, path, body)"
        " VALUES (?, ?, ?, 'v1', ?, 'p', ?)",
        [
            (
                1,
                "manual",
                "틀니 수리",
                None,
                f"# 틀니 수리\n\n머리말입니다.\n\n## 접수\n\n접수 절차입니다.\n\n## 긴 절\n\n{LONG}",
            ),
            (2, "notice", "휴진", "2026-05-01", "# 휴진\n\n## 안내\n\n휴진합니다."),
        ],
    )
    s.executescript(
        """
        INSERT INTO emr_procedures (code, name, category, is_insured, fee_total,
            patient_share_rate, duration_min, is_tooth_level) VALUES
            ('U0001', '스케일링', 'preventive', 'yes', 1, 0.3, 30, 0);
        INSERT INTO emr_patients (id, chart_no, name, birth_date, sex, phone, registered_at) VALUES
            (1, 'C-00001', '김철수', '1980-01-01', 'M', '010', '2020-01-01');
        INSERT INTO emr_staff (id, name, role) VALUES (1, '원장', 'doctor');
        INSERT INTO emr_appointments (id, patient_id, staff_id, scheduled_at, duration_min, type,
            planned_procedure_code, status, booked_via, created_at) VALUES
            (1, 1, 1, '2026-07-03 10:00:00', 30, 'hygiene', 'U0001', 'completed', 'phone', 'x');
        INSERT INTO emr_treatments (id, appointment_id, patient_id, procedure_code, tooth_no,
            performed_at, fee_total, insured_amount, patient_amount) VALUES
            (7, 1, 1, 'U0001', '00', '2026-07-03 10:30:00', 1, 0, 1);
        INSERT INTO emr_diagnoses (code, name) VALUES ('K05', '치은염');
        INSERT INTO emr_chart_notes (id, treatment_id, dx_code, note) VALUES (1, 7, 'K05', '치석 제거함');
        INSERT INTO ax_calls (id, call_uid, source, patient_id, caller_name, direction, started_at,
            duration_sec, has_transcript, transcript) VALUES
            (1, 'c-0001', 'generated', 1, '김철수', 'inbound', '2026-07-03 09:00:00', 60, 1, '환자: 예약 문의요'),
            (2, 'c-0002', 'generated', NULL, '이영자', 'outbound', '2026-07-04 09:00:00', 60, 1, '직원: 보험사죠'),
            (3, 'c-0003', 'generated', 1, '김철수', 'inbound', '2026-07-05 09:00:00', 60, 0, NULL);
        INSERT INTO emr_messages (id, patient_id, direction, sent_at, body, "trigger") VALUES
            (1, 1, 'out', '2026-07-03 08:00:00', '예약 확인', 'appt_confirm'),
            (2, 1, 'in', '2026-07-10 08:00:00', '네', 'reply'),
            (3, 1, 'out', '2026-08-01 08:00:00', '리콜 안내', 'recall');
        """
    )
    s.commit()
    s.close()
    c = db.connect(tmp_path / "a.sqlite", source)
    db.init_schema(c)
    yield c
    c.close()


def chunk(conn, id_):
    return conn.execute("SELECT * FROM ax_chunks WHERE id = ?", (id_,)).fetchone()


def test_short_text_is_one_piece():
    assert split_text("짧은 글") == ["짧은 글"]


def test_long_text_splits_with_overlap_and_limit():
    text = "\n\n".join(f"문단{i} " + "".join(f"{i}-{n} " for n in range(50)) for i in range(6))
    pieces = split_text(text)
    assert len(pieces) > 1 and all(len(p) <= MAX_CHARS for p in pieces)
    assert all(b.startswith(a[-OVERLAP:]) for a, b in zip(pieces, pieces[1:], strict=False))


def test_single_huge_line_is_cut():
    assert all(len(p) <= MAX_CHARS for p in split_text("가" * 1500))


def test_document_chunks_follow_headers(conn):
    build_index(conn, FakeEmbedder())
    first = chunk(conn, "doc:1:0")
    assert first["text"].startswith("틀니 수리\n") and first["source_type"] == "document"
    assert "접수 절차" in chunk(conn, "doc:1:1")["text"]
    assert chunk(conn, "doc:1:1")["text"].startswith("틀니 수리 > 접수\n")
    assert chunk(conn, "doc:1:0")["doc_type"] == "manual"


def test_long_section_keeps_header_prefix_on_every_piece(conn):
    build_index(conn, FakeEmbedder())
    rows = conn.execute("SELECT text FROM ax_chunks WHERE id LIKE 'doc:1:%'").fetchall()
    long_rows = [r[0] for r in rows if "문단" in r[0]]
    assert len(long_rows) > 1 and all(t.startswith("틀니 수리 > 긴 절\n") for t in long_rows)


def test_notice_keeps_effective_date(conn):
    build_index(conn, FakeEmbedder())
    assert chunk(conn, "doc:2:0")["event_date"] == "2026-05-01"


def test_call_prefix_with_patient(conn):
    build_index(conn, FakeEmbedder())
    c = chunk(conn, "call:c-0001:0")
    assert c["text"].startswith("[환자: 김철수(C-00001) | 2026-07-03 | 수신]\n")
    assert c["chart_no"] == "C-00001" and c["event_date"] == "2026-07-03"


def test_call_prefix_without_patient(conn):
    build_index(conn, FakeEmbedder())
    c = chunk(conn, "call:c-0002:0")
    assert c["text"].startswith("[상대: 이영자 | 2026-07-04 | 발신]\n") and c["chart_no"] is None


def test_call_without_transcript_is_skipped(conn):
    build_index(conn, FakeEmbedder())
    assert chunk(conn, "call:c-0003:0") is None


def test_thread_is_one_chunk_per_patient_month(conn):
    build_index(conn, FakeEmbedder())
    c = chunk(conn, "thread:C-00001:2026-07")
    assert c["text"] == "[환자: 김철수(C-00001) | 2026-07] 07-03 예약 확인 07-10 답장: 네"
    assert c["source_type"] == "message_thread" and c["event_date"] == "2026-07-10"
    assert chunk(conn, "thread:C-00001:2026-08") is not None


def test_chart_has_no_patient_name(conn):
    build_index(conn, FakeEmbedder())
    c = chunk(conn, "chart:7")
    assert c["text"] == "[차트 C-00001 | 2026-07-03 | 스케일링] 치석 제거함 (스케일링)"
    assert "김철수" not in c["text"] and c["chart_no"] == "C-00001"


def test_fts_row_for_every_chunk(conn):
    build_index(conn, FakeEmbedder())
    chunks = conn.execute("SELECT COUNT(*) FROM ax_chunks").fetchone()[0]
    assert conn.execute("SELECT COUNT(*) FROM ax_chunks_fts").fetchone()[0] == chunks


def test_rebuild_embeds_only_missing(conn):
    e = FakeEmbedder()
    build_index(conn, e)
    first = e.calls
    build_index(conn, e)
    assert first and e.calls == first
    assert (
        conn.execute("SELECT COUNT(*) FROM ax_chunks_fts").fetchone()[0]
        == conn.execute("SELECT COUNT(*) FROM ax_chunks").fetchone()[0]
    )


def test_second_model_gets_own_vectors(conn):
    build_index(conn, FakeEmbedder("a"))
    build_index(conn, FakeEmbedder("b"))
    models = {r[0] for r in conn.execute("SELECT DISTINCT model FROM ax_chunk_vectors")}
    assert models == {"a", "b"}


def test_changed_text_is_embedded_again(conn, tmp_path):
    e = FakeEmbedder()
    build_index(conn, e)
    conn.execute("UPDATE ax_chunks SET text = '옛 글' WHERE id = 'chart:7'")
    conn.commit()
    before = e.texts
    build_index(conn, e)
    assert e.texts == before + 1


def test_vector_store_finds_nearest_with_filter(conn):
    build_index(conn, FakeEmbedder())
    store = VectorStore(conn, "fake")
    wanted = conn.execute("SELECT text FROM ax_chunks WHERE id = 'chart:7'").fetchone()[0]
    vector = FakeEmbedder().embed([wanted], "query")[0]
    assert store.query(vector, None, 1)[0] == "chart:7"
    assert "chart:7" not in store.query(vector, ("c.source_type = ?", ["document"]), 5)


def test_vector_store_sees_upsert_after_cache(conn):
    build_index(conn, FakeEmbedder())
    store = VectorStore(conn, "fake")
    store.query(np.ones(8, dtype="float32"), None, 1)
    store.upsert(["chart:7"], [np.ones(8, dtype="float32")])
    assert store.query(np.ones(8, dtype="float32"), None, 1) == ["chart:7"]


@pytest.mark.parametrize(
    "tooth, want",
    [
        ("43", ["오른쪽 아래", "송곳니"]),
        ("11", ["오른쪽 위", "앞니"]),
        ("26", ["왼쪽 위", "어금니"]),
        ("35", ["왼쪽 아래", "작은어금니"]),
        ("48", ["오른쪽 아래", "사랑니"]),
        ("00", []),
    ],
)
def test_tooth_number_becomes_position(tooth, want):
    assert tooth_terms(tooth) == want


@pytest.mark.parametrize(
    "note, aliases, want",
    [
        ("타 기관에서 발치 후 12개월 지난 부위", [], ["1년"]),
        ("약 18개월 전 치료 이력", [], ["1년 반"]),
        ("약 20개월 전 치료 이력", [], ["1년 8개월"]),
        ("약 3개월 전 치료", [], []),
        ("7일 전 발치한 부위", [], ["1주 전", "일주일 전"]),
        ("14일 전 발치", [], ["2주 전"]),
        ("3월 7일 전까지 내원", [], []),
        ("3월7일 전까지 내원", [], []),
        ("3월 17일 전까지 내원", [], []),
        ("스케일링 시행함", ["치석 제거"], ["치석제거"]),
        ("치석 제거 시행함", ["치석 제거"], ["스케일링"]),
        ("4일 전 치석제거 시행", [], ["스케일링"]),
        ("12개월 후 정기 검진 안내함", [], ["1년"]),  # 받지 않은 시술이 붙으면 안 된다
        ("남은 치료는 미백", [], []),
    ],
)
def test_note_gets_other_expressions(note, aliases, want):
    assert chart_terms(note, "00", aliases) == want


def test_terms_are_deduplicated():
    assert chart_terms("12개월 전과 12개월 뒤", "00", []) == ["1년"]


def test_chart_chunk_ends_with_terms(conn, tmp_path):
    s = sqlite3.connect(tmp_path / "s.sqlite")
    s.execute("UPDATE emr_chart_notes SET note = '12개월 지난 부위'")
    s.execute("UPDATE emr_treatments SET tooth_no = '43'")
    s.commit()
    s.close()
    build_index(conn, FakeEmbedder())
    c = chunk(conn, "chart:7")
    assert (
        c["text"]
        == "[차트 C-00001 | 2026-07-03 | 스케일링] 12개월 지난 부위 (오른쪽 아래 송곳니 1년)"
    )


def test_text_before_first_header_is_kept():
    assert _sections("제목", "머리글입니다\n\n## 절\n본문")[0] == ("제목", "머리글입니다\n\n")


def test_removed_source_removes_chunks_and_vectors(conn, tmp_path):
    build_index(conn, FakeEmbedder())
    s = sqlite3.connect(tmp_path / "s.sqlite")
    s.execute("DELETE FROM emr_chart_notes")
    s.commit()
    s.close()
    build_index(conn, FakeEmbedder())
    assert chunk(conn, "chart:7") is None
    gone = conn.execute("SELECT COUNT(*) FROM ax_chunk_vectors WHERE chunk_id = 'chart:7'")
    assert gone.fetchone()[0] == 0
