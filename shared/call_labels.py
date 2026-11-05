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
