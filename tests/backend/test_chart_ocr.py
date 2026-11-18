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
    assert chart_ocr.suggest("Pulp ext. st, caoH") == []  # 1st의 1이 빠진 st
    assert (
        chart_ocr.suggest("Dulp ext. 1st, Lido 2@") == []
    )  # 숫자로 시작하는 낱말은 약어가 아니다
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
    # 2.3.14(앞자리 1 빠짐)는 1을 채운 2012-03-14가 수납 12.3.14와 맞아 자동 확인
    assert (v[5]["date"], v[5]["date_check"], v[5]["date_note"]) == (
        "2012-03-14",
        False,
        "수납 장부와 일치",
    )
    assert v[0]["teeth"] == "전악" and v[0]["quadrant"] is None and v[0]["fdi"] is None
    assert {"word": "DHI", "abbr": "OHI"} in v[11]["suggestions"]
    assert f["payments"][4]["balance"] == 300000 and f["payments"][4]["date"] == "2012-04-18"


# 날짜 교차검증(차트 OCR 설계 §설계 2) — 정보 추출이 빠뜨린 날짜는 OCR 글자로, 연도는 수납 장부로
def _v(raw):
    return {"date_raw": raw, "date": None, "date_check": False, "date_note": None}


def _p(raw):
    return {"date_raw": raw, "date": None, "date_check": False}


def _w(text, x0, y0):
    return {"text": text, "confidence": 0.99, "box": [x0, y0, x0 + 0.03, y0 + 0.015]}


HEADERS = [_w("Date", 0.533, 0.05), _w("Location", 0.58, 0.05)]


def test_settle_fills_missing_date_from_ocr_then_ledger():
    # C-00017 실제 실패: 정보 추출은 14.1.22를 "."로, 〃를 ","로 — OCR은 Date 칸에서 14.1.22를 읽었다
    f = {
        "visits": [_v("13.3.5"), _v("."), _v(","), _v("9/30")],
        "payments": [_p("13.3.5"), _p("14.1.22"), _p("14.9.30")],
    }
    words = HEADERS + [
        _w("13.3.5", 0.523, 0.25),
        _w("14.1.22", 0.526, 0.28),
        _w(",", 0.528, 0.30),
        _w("9/30", 0.525, 0.32),
        _w("6", 0.592, 0.28),  # Location 칸 — Date 칸이 아니다
    ]
    v = chart_ocr.settle(f, words)["visits"]
    assert (v[1]["date"], v[1]["date_check"]) == ("2014-01-22", False)
    assert v[1]["date_note"] == "OCR 글자로 채움 · 수납 장부와 일치"
    # 〃로 본 줄은 맞아도 확인 필요 그대로
    assert (v[2]["date"], v[2]["date_check"], v[2]["date_note"]) == (
        "2014-01-22",
        True,
        "〃(윗줄과 같은 날)로 봄",
    )
    assert (v[3]["date"], v[3]["date_check"]) == ("2014-09-30", False)


def test_settle_ocr_fill_without_ledger_notes_source():
    f = {"visits": [_v("13.3.5"), _v(".")], "payments": []}
    words = HEADERS + [_w("13.3.5", 0.523, 0.25), _w("14.1.22", 0.526, 0.28)]
    v = chart_ocr.settle(f, words)["visits"]
    assert (v[1]["date"], v[1]["date_check"], v[1]["date_note"]) == (
        "2014-01-22",
        False,
        "OCR 글자로 채움",
    )


def test_settle_fixes_year_from_ledger():
    # 앞 줄을 못 읽어 9/30이 2013년을 이어받음 — 수납 장부에 같은 월·일이 한 번이면 그 연도로
    f = {"visits": [_v("13.3.5"), _v("9/30")], "payments": [_p("14.9.30")]}
    v = chart_ocr.settle(f, [])["visits"]
    assert (v[1]["date"], v[1]["date_check"], v[1]["date_note"]) == (
        "2014-09-30",
        True,
        "수납 장부 연도로 고침",
    )


def test_refresh_recomputes_suggestions():
    f = {
        "visits": [
            _v("13.3.5")
            | {"treatment": "Pulp ext. 1st, DHI", "suggestions": [{"word": "1st", "abbr": "set"}]}
        ],
        "payments": [],
    }
    assert chart_ocr.refresh(f, [])["visits"][0]["suggestions"] == [{"word": "DHI", "abbr": "OHI"}]
