"""차트 OCR 응답 정리 — 상자·날짜 이어받기·치아 번호·약어 제안(backend/chart_ocr.py, 차트 OCR 설계 §설계 2).
고정값은 샘플 C-00016으로 업스테이지를 한 번 부른 실제 응답(tests/backend/fixtures/)."""

import json
from pathlib import Path

import pytest

from backend import chart_ocr

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_written_date_carries_year():
    assert chart_ocr.written_date("12.3.14", "2011-07-04") == ("2012-03-14", False)
    assert chart_ocr.written_date("3/21", "2012-03-14") == ("2012-03-21", False)
    assert chart_ocr.written_date("〃", "2012-03-14") == ("2012-03-14", False)
    assert chart_ocr.written_date("10.2.8", None) == ("2010-02-08", False)


def test_written_date_flags_backwards_and_garbage():
    # 손글씨 1이 자주 빠진다(스파이크) — 거꾸로 가면 앞에 1을 붙여 보고 맞으면 그 값, 확인 필요는 그대로
    assert chart_ocr.written_date("1.6.13", "2010-02-22") == ("2011-06-13", True)
    assert chart_ocr.written_date("??", "2010-02-22") == (None, True)
    assert chart_ocr.written_date("3/21", None) == (None, True)


@pytest.mark.parametrize(
    "q, teeth, want",
    [(4, "6", "46"), (1, "7 6", "17, 16"), (3, "7", "37"), (None, "6", None), (2, "전악", None)],
)
def test_fdi(q, teeth, want):
    assert chart_ocr.fdi(q, teeth) == want


def test_suggest_near_abbreviation():
    assert {"word": "DHI", "abbr": "OHI"} in chart_ocr.suggest("Scaling, 출혈 多, DHI")
    assert chart_ocr.suggest("Scaling 2/2, OHI") == []
    # 흔한 낱말을 약어로 바꾸지 않는다
    assert chart_ocr.suggest("C&S, CaOH 교체, Tx. pain(-), Cr. set") == []
    assert chart_ocr.suggest("S10, 치유 양호") == [{"word": "S10", "abbr": "S/O"}]


def test_words_from_fixture():
    words = chart_ocr.words(_load("upstage_ocr.json"))
    assert len(words) == 304  # 응답 305개 중 빈 글자 1개
    assert all(0 <= v <= 1 for w in words for v in w["box"])
    assert all(w["text"] and 0 <= w["confidence"] <= 1 for w in words)


def test_fields_from_fixture():
    f = chart_ocr.fields(_load("upstage_extract.json"))
    assert f["patient_name"] == "구상철" and f["birth_date"] == "1940-04-05"
    v = f["visits"]
    assert len(v) == 12
    assert v[2]["date_raw"] == "1.6.13" and v[2]["date_check"] is True
    assert v[2]["date"] == "2011-06-13"  # 앞자리 1을 채운 추정 — 확인 필요는 그대로
    assert v[3]["date"] == "2011-06-27" and v[3]["date_check"] is False and v[3]["teeth"] == "6 7"
    assert v[6]["date"] == "2012-03-21" and v[6]["date_check"] is False
    assert v[0]["teeth"] == "전악" and v[0]["quadrant"] is None and v[0]["fdi"] is None
    assert {"word": "DHI", "abbr": "OHI"} in v[11]["suggestions"]
    assert f["payments"][4]["balance"] == 300000 and f["payments"][4]["date"] == "2012-04-18"
