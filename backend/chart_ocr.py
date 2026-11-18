"""옛 종이 차트 OCR — 업스테이지 Document OCR(글자·상자·신뢰도)과 Information Extraction(스키마로 항목)을 부른다
(차트 OCR 설계 §설계 3). 응답은 그대로 돌려주고 저장·승인은 emr_bridge가 한다.

- OCR: POST https://api.upstage.ai/v1/document-digitization, multipart `document`·`model=ocr`,
  응답 pages[].words[] = {text, confidence, boundingBox.vertices[4]}
- 정보 추출: POST https://api.upstage.ai/v1/information-extraction/chat/completions(OpenAI 호환),
  model `information-extract`, 이미지는 base64 data URI, 스키마는 response_format json_schema,
  결과는 choices[0].message.content(JSON 문자열). 스키마 맨 위 칸은 string·number·integer·array만(객체 X),
  배열 안 배열 X"""

import base64

import httpx

OCR_URL = "https://api.upstage.ai/v1/document-digitization"
EXTRACT_URL = "https://api.upstage.ai/v1/information-extraction/chat/completions"
TIMEOUT_S = 120

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
