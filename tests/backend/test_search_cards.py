"""검색 카드 칸 (앱 설계 §3 prefix, 앱 뼈대 설계안 '백엔드')."""

import pytest

from backend import db
from backend.embeddings import FakeEmbedder
from backend.rag_index import build_index
from backend.rag_search import Searcher
from backend.search_cards import card
from shared.clinic import load_clinic

KEYS = {
    "id",
    "source_type",
    "title",
    "body",
    "event_date",
    "chart_no",
    "patient_name",
    "caller_name",
    "direction",
    "doc_type",
    "version",
    "is_outdated",
}


@pytest.fixture
def indexed(source_paths):
    conn = db.connect(*source_paths)
    db.init_schema(conn)
    build_index(conn, FakeEmbedder())
    yield conn
    conn.close()


@pytest.fixture
def cards(indexed):
    replaced = Searcher(indexed, load_clinic(), FakeEmbedder()).replaced_by
    rows = [dict(r) for r in indexed.execute("SELECT * FROM ax_chunks")]
    return {r["id"]: card(indexed, r, replaced) for r in rows}


def test_every_card_has_same_keys_and_no_prefix(cards):
    assert len(cards) == 7
    for c in cards.values():
        assert set(c) == KEYS
        assert not c["body"].startswith("[")


def test_document_title_and_version(cards):
    # 첫 헤더 앞 빈 절은 조각이 없어 번호를 쓰지 않는다(rag_index._documents)
    old, new, manual = cards["doc:1:0"], cards["doc:2:0"], cards["doc:3:0"]
    assert old["title"] == "미백 비용 > 변경 내용" and old["body"] == "미백 30만원"
    assert (old["version"], old["is_outdated"]) == ("v1", True)
    assert (new["version"], new["is_outdated"]) == ("v2", False)
    assert manual["title"] == "틀니 수리" and manual["body"] == "접수 후 기공소로 보낸다"
    assert manual["doc_type"] == "manual" and manual["event_date"] is None


def test_call_with_patient(cards):
    c = cards["call:c-0001:0"]
    assert (c["patient_name"], c["chart_no"], c["direction"]) == ("김철수", "C-00001", "수신")
    assert c["caller_name"] is None and c["body"] == "틀니 수리 문의"
    assert c["title"] is None and c["version"] is None and c["is_outdated"] is None


def test_call_without_patient(cards):
    c = cards["call:c-0002:0"]
    assert (c["caller_name"], c["patient_name"], c["direction"]) == ("김미영", None, "발신")
    assert c["chart_no"] is None


def test_thread(cards):
    c = cards["thread:C-00002:2026-07"]
    assert (c["patient_name"], c["chart_no"]) == ("박영희", "C-00002")
    assert c["body"] == "07-03 예약 확인 부탁드립니다 07-03 답장: 네"


def test_chart_body_is_note_without_search_tail(cards, indexed):
    text = indexed.execute("SELECT text FROM ax_chunks WHERE id = 'chart:7'").fetchone()[0]
    assert text.endswith(")")  # 검색용 꼬리가 실제로 붙어 있어야 이 테스트가 뜻이 있다
    c = cards["chart:7"]
    assert c["body"] == "통증 호소(좌측) 치석 제거 6개월 후 재내원"
    assert c["patient_name"] is None and c["chart_no"] == "C-00003"


def test_unknown_prefix_raises(source_paths):
    conn = db.connect(*source_paths)
    chunk = {
        "id": "call:x:0",
        "source_type": "call",
        "text": "형식 없음",
        "chart_no": None,
        "event_date": None,
        "doc_type": None,
    }
    with pytest.raises(ValueError):
        card(conn, chunk, {})


def test_document_body_drops_markdown_marks():
    chunk = {
        "id": "doc:9:0",
        "source_type": "document",
        "text": "비급여 안내\n## 레진\n- **레진**: 치아 색 `충전` __재료__\n| 구분 | 금액 |",
        "chart_no": None,
        "event_date": None,
        "doc_type": "manual",
    }

    class Conn:
        def execute(self, *_):
            return self

        def fetchone(self):
            return ("1",)

    c = card(Conn(), chunk, {})
    assert c["body"] == "레진\n- 레진: 치아 색 충전 재료\n| 구분 | 금액 |"
