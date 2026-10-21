"""병원 마스터 — 생성기와 verify가 함께 쓰는 값의 원본. 의미·규칙은 docs/치과 AX - 가상 데이터 생성 설계.md §1·§2"""

# 영업시간 (§1). emr_clinic.hours_json에 그대로 저장한다. 요일은 월=0
HOURS = {
    "open": "09:30",
    "weekday_close": "18:30",
    "saturday_close": "13:30",
    "lunch": ["13:00", "14:00"],  # 평일만
    "closed_weekdays": [3, 6],    # 목, 일 (+ 공휴일)
}

# 진료 슬롯 길이(분)와 토요일 (§1). 요일은 월=0
SLOT_MIN = 30
SATURDAY = 5

# 리마인드 발송 시각 (§2.5) — 직전 진료일의 이 시각에 그때 있던 예약에만 보낸다.
# 토요일은 마감(13:30)이 이르므로 마감 전 시각 — 확인 통화가 그날 영업시간 안에 들어오게
REMINDER_TIME = "17:00"
SATURDAY_REMINDER_TIME = "12:30"

# 운영 제약 (§1): 같은 시작 시각의 비취소 예약 상한. 하루 상한은 config의 visits_per_day(평일)
MAX_APPOINTMENTS_PER_SLOT = 3

# 스케일링 급여 나이 (§2.1: 19세↑ 연 1회)
SCALING_MIN_AGE = 19

# 임플란트 1차(conditional)는 시술일 기준 이 나이 이상이면 급여, 미만이면 비급여(본인부담 1.0·청구 없음) (§2.6 ④)
CONDITIONAL_INSURED_MIN_AGE = 65

# 내원 구성 목표 (§1). 목표 — 경고로만 확인
VISIT_MIX = {"treatment": 0.70, "hygiene": 0.25, "consult": 0.05}

# 피크 시간 (§1). [시작, 끝)
PEAK_HOURS = [("10:00", "11:00"), ("15:00", "17:00")]

# 토요일 하루 상한 = 평일 visits_per_day × 이 비율 (§1)
SATURDAY_VISIT_RATIO = 0.5

# 연령 구간과 비중 (§2.2). (이름, 최소 나이, 최대 나이(포함, 노인은 None), 비중)
AGE_BANDS = [
    ("child", 0, 12, 0.15),
    ("teen", 13, 18, 0.08),
    ("adult", 19, 64, 0.62),
    ("senior", 65, None, 0.15),
]

# 활동·휴면 (§2.2). 내원 = completed 예약. 활동 = TODAY 전 ACTIVE_MONTHS개월 안 내원 ≥ 1회. 목표 비중 — 경고로만 확인
ACTIVE_MONTHS = 6
ACTIVE_SHARE = 0.60  # 예정 신환을 뺀 환자 중. 초기 설계 가정 — 첫 large 실행 결과로 확정

# 신환 = 월 내원(completed)의 NEW_PATIENT_SHARE (§1). 목표 — 경고로만 확인
NEW_PATIENT_SHARE = 0.12

# 치아 번호 (§2.1). 치아 단위가 아닌 시술은 FULL_MOUTH 센티널
PERMANENT_TEETH = frozenset(f"{q}{n}" for q in (1, 2, 3, 4) for n in range(1, 9))
FULL_MOUTH = "00"

# 9~10월처럼 전월 이전 N개월은 '제출만' — 심사 결과는 truth_claims에만 (§2.4)
SUBMITTED_MONTHS = 2

# 시술 19개 (§2.1 표). 코드는 표 순번 기반 가상 코드
# (code, name, category, is_insured, fee_total, duration_min, insured_limit, limit_scope, age_group, is_tooth_level)
# age_group: child = 0–12세, age19plus = 19세 이상(노인 포함), senior = 65세 이상 — AGE_BANDS의 구간 이름과 별개
_ROWS = [
    ("U0001", "스케일링",      "preventive",  "yes",         60_000,    30, 1,    "year", "age19plus", 0),
    ("U0002", "구강검진",      "preventive",  "yes",         15_000,    30, None, None,   None,     0),
    ("U0003", "불소도포",      "preventive",  "no",          30_000,    30, None, None,   "child",  0),
    ("U0004", "실란트",        "preventive",  "yes",         40_000,    30, None, None,   "child",  1),
    ("U0005", "레진",          "restorative", "no",          80_000,    30, None, None,   None,     1),
    ("U0006", "아말감/GI",     "restorative", "yes",         25_000,    30, None, None,   None,     1),
    ("U0007", "인레이",        "restorative", "no",          350_000,   60, None, None,   None,     1),
    ("U0008", "신경치료",      "endo",        "yes",         120_000,   60, None, None,   None,     1),
    ("U0009", "크라운",        "prosth",      "no",          550_000,   60, None, None,   None,     1),
    ("U0010", "브릿지",        "prosth",      "no",          1_500_000, 60, None, None,   None,     0),
    ("U0011", "단순발치",      "surgery",     "yes",         30_000,    30, None, None,   None,     1),
    ("U0012", "사랑니 발치",   "surgery",     "yes",         90_000,    60, None, None,   None,     1),
    ("U0013", "임플란트 1차",  "implant",     "conditional", 1_300_000, 90, None, None,   "senior", 1),
    ("U0014", "임플란트 2차",  "implant",     "no",          0,         60, None, None,   None,     0),
    ("U0015", "교정 상담",     "ortho",       "no",          0,         30, None, None,   None,     0),
    ("U0016", "교정 월 체크",  "ortho",       "no",          80_000,    30, None, None,   None,     0),
    ("U0017", "미백",          "cosmetic",    "no",          300_000,   60, None, None,   None,     0),
    ("U0018", "잇몸치료",      "perio",       "yes",         35_000,    30, None, None,   None,     0),
    ("U0019", "상담",          "consult",     "no",          0,         30, None, None,   None,     0),
]
_KEYS = ("code", "name", "category", "is_insured", "fee_total", "duration_min",
         "insured_limit", "limit_scope", "age_group", "is_tooth_level")

# aliases는 생성기 전사 단계에서 채운다 (스키마 기본값 '[]')
PROCEDURES = [
    {**dict(zip(_KEYS, row)), "patient_share_rate": 1.0 if row[3] == "no" else 0.3}
    for row in _ROWS
]

SCALING_CODE = "U0001"

# 치아 단위 시술의 치아 후보 (§2.1). 뽑기만 한다 — 순서 고정을 위해 정렬된 튜플(frozenset 순회 순서는 실행마다 다름)
_WISDOM = tuple(f"{q}8" for q in (1, 2, 3, 4))
_MOLARS_6_7 = tuple(f"{q}{n}" for q in (1, 2, 3, 4) for n in (6, 7))
_DEFAULT_TEETH = tuple(sorted(PERMANENT_TEETH - set(_WISDOM)))
TOOTH_CHOICES = {
    p["code"]: {"사랑니 발치": _WISDOM, "실란트": _MOLARS_6_7}.get(p["name"], _DEFAULT_TEETH)
    for p in PROCEDURES if p["is_tooth_level"]
}


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def reminder_time(weekday: int) -> str:
    """그 요일(월=0)에 보내는 리마인드 시각"""
    return SATURDAY_REMINDER_TIME if weekday == SATURDAY else REMINDER_TIME


def open_slots(weekday: int) -> int:
    """그 요일의 진료 슬롯(SLOT_MIN분) 수 (월=0). 휴진 요일은 0. 공휴일은 호출하는 쪽에서 거른다."""
    if weekday in HOURS["closed_weekdays"]:
        return 0
    if weekday == SATURDAY:
        return (_minutes(HOURS["saturday_close"]) - _minutes(HOURS["open"])) // SLOT_MIN
    lunch = _minutes(HOURS["lunch"][1]) - _minutes(HOURS["lunch"][0])
    return (_minutes(HOURS["weekday_close"]) - _minutes(HOURS["open"]) - lunch) // SLOT_MIN
