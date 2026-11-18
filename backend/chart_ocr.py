"""옛 종이 차트 OCR — 업스테이지 Document OCR(글자·상자·신뢰도)과 Information Extraction(스키마로 항목)을 부른다
(차트 OCR 설계 §설계 3). 응답은 그대로 돌려주고 저장·승인은 emr_bridge가 한다. 화면용 정리(상자 비율·날짜 이어받기·
치아 번호·약어 제안)도 여기서 — 확정은 사람이 한다(설계 §설계 2).

- OCR: POST https://api.upstage.ai/v1/document-digitization, multipart `document`·`model=ocr`,
  응답 pages[].words[] = {text, confidence, boundingBox.vertices[4]}
- 정보 추출: POST https://api.upstage.ai/v1/information-extraction/chat/completions(OpenAI 호환),
  model `information-extract`, 이미지는 base64 data URI, 스키마는 response_format json_schema,
  결과는 choices[0].message.content(JSON 문자열). 스키마 맨 위 칸은 string·number·integer·array만(객체 X),
  배열 안 배열 X"""

import base64
import difflib
import json
import re
from datetime import date

import httpx

OCR_URL = "https://api.upstage.ai/v1/document-digitization"
EXTRACT_URL = "https://api.upstage.ai/v1/information-extraction/chat/completions"
TIMEOUT_S = 120
LOW_CONFIDENCE = 0.8  # 이보다 낮은 OCR 단어는 화면에서 강조
DITTO = ("〃", '"', "''", "״")
# 손글씨 〃를 OCR·정보 추출이 이렇게 읽는다(스파이크) — 〃로 보되 확인 필요
DITTO_LIKE = (",", ",,", "'", "11", "//", "„")

# 치과 약어 → 뜻(화면 툴팁·제안). 한 낱말 약어만 제안에 쓴다
ABBREVIATIONS = {
    "Pano": "파노라마 X-ray",
    "PA": "치근단 X-ray",
    "OHI": "구강 위생 교육",
    "SC": "스케일링",
    "RP": "치근 활택술",
    "Ext.": "발치",
    "Lido": "리도카인 마취(@ = 앰플)",
    "S/O": "실밥 제거",
    "C1": "법랑질 충치",
    "C2": "상아질 충치",
    "C3": "신경까지 닿은 충치",
    "Pulp ext.": "신경 제거(발수)",
    "C&S": "근관 세척·성형",
    "CaOH": "수산화칼슘(근관 약제)",
    "CF": "근관 충전",
    "GP": "거타퍼차(근관 충전재)",
    "Core": "코어(기둥) 쌓기",
    "PFM": "도재 금속 크라운",
    "Cr.": "크라운",
    "prep": "치아 삭제",
    "imp.": "인상 채득(본뜨기) — 계획·상담 줄에서는 임플란트",
    "Temp.": "임시",
    "set": "접착(붙이기)",
    "pain": "통증",
    "Fuji": "글래스아이오노머 시멘트(접착)",
    "RCT": "신경 치료",
    "Inlay": "인레이(본떠 만든 때움)",
    "Resin": "레진 충전",
    "GI": "글래스아이오노머 충전",
    "EPT": "전기 치수 검사",
    "Tx.": "치료",
    "Rx)": "처방",
    "Fx": "파절",
    "DM": "당뇨",
    "HTN": "고혈압",
    "NKDA": "알려진 약물 알레르기 없음",
    "BP": "혈압",
}
# OCR이 헷갈리는 글자 — S10 → S/O
_LOOKALIKE = str.maketrans({"1": "/", "0": "O", "5": "S"})

SCHEMA = {
    "type": "object",
    "properties": {
        "patient_name": {"type": "string", "description": "성명 칸에 쓴 환자 이름"},
        "birth_date": {"type": "string", "description": "생년월일, YYYY-MM-DD"},
        "old_chart_no": {"type": "string", "description": "차트번호 칸의 번호"},
        "first_visit": {"type": "string", "description": "초진일, YYYY-MM-DD"},
        "chief_complaint": {"type": "string", "description": "C.C 칸 내용(주소)"},
        "present_illness": {"type": "string", "description": "P.I 칸 내용(병력·복용약)"},
        "treatment_plan": {"type": "string", "description": "Tx. Plan 칸 내용"},
        "memo": {"type": "string", "description": "Tx. Plan 옆 빈 칸에 쓴 메모·경고"},
        "visits": {
            "type": "array",
            "description": "진료 경과(Date·Location·Treatment·Signature) — 진료 한 번이 여러 줄이면 한 항목",
            "items": {
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": "Date 칸 그대로(예: 12.3.14, 3/21, 〃)",
                    },
                    "location": {
                        "type": "string",
                        "description": "Location 칸 — 십자 안 위치를 기호로(예: 6┐, └5) 또는 전악",
                    },
                    "treatment": {
                        "type": "string",
                        "description": "Treatment 칸 내용, 여러 줄은 이어서",
                    },
                    "signature": {"type": "string", "description": "Signature 칸 서명 글자"},
                },
            },
        },
        "payments": {
            "type": "array",
            "description": "수납 장부(날짜·내용·금액·잔액·서명)",
            "items": {
                "type": "object",
                "properties": {
                    "date": {"type": "string"},
                    "item": {"type": "string", "description": "내용"},
                    "amount": {"type": "integer", "description": "금액(원)"},
                    "balance": {"type": "integer", "description": "잔액(원)"},
                },
            },
        },
    },
}


class UpstageError(Exception):
    """업스테이지 호출 실패 — 키 없음·HTTP 오류·시간 초과(메시지는 화면에 그대로)"""


def _post(url: str, key: str, **kw) -> dict:
    if not key:
        raise UpstageError("업스테이지 API 키(UPSTAGE_API_KEY)가 없습니다")
    try:
        r = httpx.post(url, headers={"Authorization": f"Bearer {key}"}, timeout=TIMEOUT_S, **kw)
    except httpx.HTTPError as e:
        raise UpstageError(f"업스테이지 연결 실패: {e}") from e
    if r.status_code != 200:
        raise UpstageError(f"업스테이지 오류 {r.status_code}: {r.text[:300]}")
    return r.json()


def ocr(image: bytes, filename: str, key: str) -> dict:
    """글자·상자·신뢰도 — 응답 JSON 그대로"""
    return _post(OCR_URL, key, files={"document": (filename, image)}, data={"model": "ocr"})


def extract(image: bytes, key: str) -> dict:
    """SCHEMA 항목 — 응답 JSON 그대로(항목은 choices[0].message.content)"""
    data = base64.b64encode(image).decode()
    body = {
        "model": "information-extract",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:application/octet-stream;base64,{data}"},
                    }
                ],
            }
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "paper_dental_chart", "schema": SCHEMA},
        },
    }
    return _post(EXTRACT_URL, key, json=body)


def _norm(word: str) -> str:
    return word.upper().rstrip(".)")


_KEYS = {_norm(k): k for k in ABBREVIATIONS if " " not in k}


def words(ocr_resp: dict) -> list[dict]:
    """첫 쪽 단어 — 상자는 이미지 비율 [x0, y0, x1, y1](0~1). 빈 글자 단어(실제 응답에 있음)는 뺀다"""
    page = ocr_resp["pages"][0]
    w, h = page["width"], page["height"]
    out = []
    for word in page["words"]:
        if not word["text"].strip():
            continue
        xs = [v["x"] for v in word["boundingBox"]["vertices"]]
        ys = [v["y"] for v in word["boundingBox"]["vertices"]]
        box = [min(xs) / w, min(ys) / h, max(xs) / w, max(ys) / h]
        out.append(
            {
                "text": word["text"],
                "confidence": word["confidence"],
                "box": [min(max(v, 0.0), 1.0) for v in box],
            }
        )
    return out


def _parse(raw: str) -> date | None:
    m = re.fullmatch(r"(\d{1,2}|\d{4})[./](\d{1,2})[./](\d{1,2})", raw)
    if not m:
        return None
    y = int(m[1])
    try:
        return date(y if y >= 100 else 2000 + y, int(m[2]), int(m[3]))
    except ValueError:
        return None


def written_date(raw: str, prev: str | None) -> tuple[str | None, bool]:
    """손으로 쓴 날짜 → (YYYY-MM-DD, 확인 필요). 같은 해 `3/21`·`〃`는 prev에서 이어받는다. 앞 날짜보다
    거꾸로 가면 확인 필요 — 손글씨 1이 자주 빠져(스파이크) 앞에 1을 붙여 맞으면 그 값을 채워 둔다"""
    raw = raw.strip()
    before = date.fromisoformat(prev) if prev else None
    if raw in DITTO:
        return (prev, prev is None)
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})", raw)
    if m:
        if before is None:
            return (None, True)
        try:
            d = date(before.year, int(m[1]), int(m[2]))
        except ValueError:
            return (None, True)
        return (d.isoformat(), d < before)
    d = _parse(raw)
    if d is None:
        return (None, True)
    if before and d < before:
        guess = _parse("1" + raw)
        if guess and guess >= before:
            return (guess.isoformat(), True)
        return (d.isoformat(), True)
    return (d.isoformat(), False)


def fdi(quadrant: int | None, teeth: str) -> str | None:
    """사분면(사람이 십자에서 고름) + 치아 숫자 → FDI 번호, 여럿은 쉼표로"""
    nums = re.findall(r"[1-8]", teeth)
    if quadrant not in (1, 2, 3, 4) or not nums or "전악" in teeth:
        return None
    return ", ".join(f"{quadrant}{n}" for n in nums)


def suggest(text: str) -> list[dict]:
    """잘못 읽혔을 법한 낱말 → 비슷한 약어 제안(같으면 제안 안 함). 확정은 사람"""
    out = []
    for word in re.findall(r"[A-Za-z0-9&/]+[.)]?", text):
        n = _norm(word)
        # 숫자로 시작(1st·2@)은 약어가 아니고, 두 글자는 엇비슷한 약어가 너무 많다(st → set)
        if n in _KEYS or n[0].isdigit() or len(n) < 3:
            continue
        hit = _KEYS.get(n.translate(_LOOKALIKE)) or next(
            (_KEYS[k] for k in difflib.get_close_matches(n, _KEYS, n=1, cutoff=0.6)), None
        )
        if hit:
            out.append({"word": word, "abbr": hit})
    return out


def fields(extract_resp: dict) -> dict:
    """정보 추출 결과 → 화면 폼 — 머리 칸 그대로, 진료·수납은 날짜 정리, 진료는 치아 숫자·약어 제안"""
    raw = json.loads(extract_resp["choices"][0]["message"]["content"])
    head = {k: raw.get(k, "") for k, v in SCHEMA["properties"].items() if v["type"] != "array"}
    visits = []
    for v in raw.get("visits", []):
        loc = v.get("location", "")
        teeth = "전악" if "전악" in loc else " ".join(re.findall(r"\d", loc))
        visits.append(
            {
                "date_raw": v.get("date", ""),
                "location_raw": loc,
                "teeth": teeth,
                "quadrant": None,
                "fdi": None,
                "treatment": v.get("treatment", ""),
                "signature": v.get("signature", ""),
                "suggestions": suggest(v.get("treatment", "")),
            }
        )
    payments = []
    for p in raw.get("payments", []):
        payments.append(
            {
                "date_raw": p.get("date", ""),
                "item": p.get("item", ""),
                "amount": p.get("amount"),
                "balance": p.get("balance"),
            }
        )
    return settle(head | {"visits": visits, "payments": payments}, [])


def _date_column(words: list[dict]) -> list[dict]:
    """진료 경과 Date 칸의 OCR 단어(위에서 아래로) — 머리 글자 Date·Location 가운데 사이 거리를 칸 폭으로"""
    head = {
        w["text"]: w
        for w in words
        if w["text"] in ("Date", "Location") and w["box"][0] > 0.5 and w["box"][1] < 0.1
    }
    if len(head) < 2:
        return []
    cx = {k: (w["box"][0] + w["box"][2]) / 2 for k, w in head.items()}
    width = cx["Location"] - cx["Date"]
    top = head["Date"]["box"][3]
    col = [
        w
        for w in words
        if abs((w["box"][0] + w["box"][2]) / 2 - cx["Date"]) < width / 2 and w["box"][1] > top
    ]
    return sorted(col, key=lambda w: w["box"][1])


def _fill_from_ocr(visits: list[dict], words: list[dict]) -> list[str | None]:
    """정보 추출이 날짜를 못 읽은 진료(날짜도 〃도 아님)에 OCR이 Date 칸에서 읽은 글자를 채운다 — 양쪽이 같은
    글자로 맞는 진료 사이 틈에서, 못 읽은 진료 수와 남는 OCR 단어 수가 같을 때만 차례로 짝짓는다"""
    col = [w["text"] for w in _date_column(words)]
    out: list[str | None] = [None] * len(visits)
    anchors, j = [], 0
    for i, v in enumerate(visits):
        k = next((k for k in range(j, len(col)) if col[k] == v["date_raw"]), None)
        if k is not None:
            anchors.append((i, k))
            j = k + 1
    bounds = [(-1, -1), *anchors, (len(visits), len(col))]
    for (i0, k0), (i1, k1) in zip(bounds, bounds[1:], strict=False):
        missing = [i for i in range(i0 + 1, i1) if _unreadable(visits[i]["date_raw"])]
        spare = col[k0 + 1 : k1]
        if missing and len(missing) == len(spare):
            for i, text in zip(missing, spare, strict=True):
                out[i] = text
    return out


def _unreadable(raw: str) -> bool:
    raw = raw.strip()
    return raw not in DITTO + DITTO_LIKE and _parse(raw) is None and "/" not in raw


def settle(f: dict, words: list[dict]) -> dict:
    """날짜 정리(설계 §설계 2) — ① 정보 추출이 빠뜨린 진료 날짜는 OCR 글자로 채우고 ② 윗줄에서 연도·날짜를
    이어받은 뒤 ③ 수납 장부와 맞춰 본다: 같은 날 수납이 있으면 확인됨, 연도를 이어받은 날짜가 장부에 없고 같은
    월·일 수납이 한 번이면 그 연도로 고친다(확인 필요). 원문(date_raw)에서 다시 계산해 몇 번 불러도 같다"""
    payments, prev = [], None
    for p in f["payments"]:
        d, check = written_date(p["date_raw"], prev)
        prev = d or prev
        payments.append(p | {"date": d, "date_check": check})
    paid = {p["date"] for p in payments if p["date"] and not p["date_check"]}

    filled = _fill_from_ocr(f["visits"], words)
    visits, prev = [], None
    for v, ocr_text in zip(f["visits"], filled, strict=True):
        raw = ocr_text or v["date_raw"].strip()
        note = "OCR 글자로 채움" if ocr_text else None
        if raw in DITTO_LIKE:
            d, check, note = prev, True, "〃(윗줄과 같은 날)로 봄"
        else:
            d, check = written_date(raw, prev)
        read = raw not in DITTO + DITTO_LIKE  # 그 줄에 날짜를 실제로 썼다
        inherited = read and "/" in raw  # 연도를 윗줄에서 이어받음
        if d and read and d in paid:
            check = False
            note = " · ".join(x for x in (note, "수납 장부와 일치") if x)
        elif d and (inherited or check) and d not in paid:
            same = {x for x in paid if x[5:] == d[5:]}
            if len(same) == 1:
                d, check, note = same.pop(), True, "수납 장부 연도로 고침"
        prev = d or prev
        visits.append(v | {"date": d, "date_check": check, "date_note": note})
    return f | {"visits": visits, "payments": payments}
