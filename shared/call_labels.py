"""통화 라벨 9항목의 enum과 구조화 출력 스키마 — 분류기·판정기가 쓴다(분류기 설계 "분류기", 앱 설계 §1).
enum 원본은 generator/master.py(CALL_PURPOSE_SHARE 키·CALL_COUNTERPARTS·URGENCIES·CALL_FLAGS). backend가 그 모듈을
읽을 수 없어 값을 옮겨 두고, 같은지는 tests/generator/test_call_labels_sync.py가 비교한다.
"""

PURPOSES = (
    "book",
    "reschedule",
    "confirm",
    "cancel",
    "recall_q",
    "treatment_q",
    "insurance_q",
    "price_q",
    "billing_q",
    "document_req",
    "emergency",
    "complaint",
    "other",
    "unknown",
)
COUNTERPARTS = ("patient", "guardian", "new_caller", "insurer", "vendor", "other")
URGENCIES = ("urgent", "normal", "low")
FLAGS = (
    "emergency",
    "complaint",
    "price_inquiry",
    "wants_appt",
    "appt_confirmed",
    "has_todo",
)
ITEMS = ("primary_purpose", "counterpart", "urgency") + FLAGS
CORE_ITEMS = ("primary_purpose", "urgency", "emergency")  # 다르면 엇갈림 is_core (앱 설계 §1)

# 화면에 보이는 우리말 이름(통화·② 판정 화면 설계 "화면 공통 원칙") — API GET /labels가 내준다
PURPOSE_KO = {
    "book": "예약 잡기",
    "reschedule": "예약 변경",
    "confirm": "예약 확인",
    "cancel": "예약 취소",
    "recall_q": "리콜 문의",
    "treatment_q": "치료 문의",
    "insurance_q": "보험 문의",
    "price_q": "가격·비용 문의",
    "billing_q": "수납·청구 문의",
    "document_req": "서류·기록 요청",
    "emergency": "응급·통증",
    "complaint": "불만",
    "other": "기타",
    "unknown": "모르겠음·애매",
}
COUNTERPART_KO = {
    "patient": "환자",
    "guardian": "보호자",
    "new_caller": "처음 연락",
    "insurer": "보험사",
    "vendor": "업체",
    "other": "기타",
}
URGENCY_KO = {"urgent": "급함", "normal": "보통", "low": "낮음"}
FLAG_KO = {
    "emergency": "응급",
    "complaint": "불만",
    "price_inquiry": "비용 문의",
    "wants_appt": "예약 희망",
    "appt_confirmed": "예약 잡힘",
    "has_todo": "할 일 있음",
}


def differing_items(a: dict, b: dict) -> list[str]:
    """두 라벨에서 다른 항목(평가 설계 §2.5) — 급함은 urgent 여부만 본다(normal/low는 골드가 추첨이라 같은 것으로).
    엇갈림·교차검증의 모델 오류가 같이 쓴다"""
    return [
        i
        for i in ITEMS
        if ((a[i] == "urgent") != (b[i] == "urgent") if i == "urgency" else a[i] != b[i])
    ]


LABEL_SCHEMA = {
    "type": "object",
    "properties": {
        "primary_purpose": {"type": "string", "enum": list(PURPOSES)},
        "counterpart": {"type": "string", "enum": list(COUNTERPARTS)},
        "urgency": {"type": "string", "enum": list(URGENCIES)},
        **{f: {"type": "boolean"} for f in FLAGS},
    },
    "required": list(ITEMS),
    "additionalProperties": False,
}
SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "todo": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "todo"],
    "additionalProperties": False,
}
