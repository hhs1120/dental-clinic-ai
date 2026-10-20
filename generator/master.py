"""병원 마스터 — 생성기와 verify가 함께 쓰는 단일 원본. 출처: docs/치과 AX - 가상 데이터 생성 설계.md §1·§2.1"""

# 영업시간 (§1). emr_clinic.hours_json에 그대로 저장한다. 요일은 월=0
HOURS = {
    "open": "09:30",
    "weekday_close": "18:30",
    "saturday_close": "13:30",
    "lunch": ["13:00", "14:00"],  # 평일만
    "closed_weekdays": [3, 6],    # 목, 일 (+ 공휴일)
}

# 동시성 (§1): 원장은 동시 2체어, 체어 작업(hygiene + treatment)은 동시 3 (위생사 3명)
DOCTOR_CHAIRS = 2
MAX_CHAIR_WORK = 3

# 스케일링 급여 나이 (§2.1: 19세↑ 연 1회)
SCALING_MIN_AGE = 19

# 피크 시간 (§1). [시작, 끝)
PEAK_HOURS = [("10:00", "11:00"), ("15:00", "17:00")]

# 토요일 내원 수 = 평일 visits_per_day × 이 비율 (§0)
SATURDAY_VISIT_RATIO = 0.5

# 연령 구간과 비중 (§2.2). (이름, 최소 나이, 최대 나이(포함, 노인은 None), 비중)
AGE_BANDS = [
    ("child", 0, 12, 0.15),
    ("teen", 13, 18, 0.08),
    ("adult", 19, 64, 0.62),
    ("senior", 65, None, 0.15),
]

# FDI 치아 번호 (§2.6 ⑦). 전악 시술은 FULL_MOUTH 센티널
PERMANENT_TEETH = frozenset(f"{q}{n}" for q in (1, 2, 3, 4) for n in range(1, 9))
PRIMARY_TEETH = frozenset(f"{q}{n}" for q in (5, 6, 7, 8) for n in range(1, 6))
FULL_MOUTH = "00"

# 9~10월처럼 전월 이전 N개월은 '제출만' — 심사 결과는 truth_claims에만 (§2.4)
SUBMITTED_MONTHS = 2

# 시술 19개 (§2.1 표). 코드는 표 순번 기반 가상 코드
# (code, name, category, is_insured, fee_total, duration_min, insured_limit, limit_scope, age_group, is_tooth_level)
_ROWS = [
    ("U0001", "스케일링",      "preventive",  "yes",         60_000,    30, 1,    "year", "adult",  0),
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

# typical_sessions·aliases는 생성기 전사 단계에서 채운다 (스키마 기본값 1, '[]')
PROCEDURES = [
    {**dict(zip(_KEYS, row)), "patient_share_rate": 1.0 if row[3] == "no" else 0.3}
    for row in _ROWS
]

SCALING_CODE = "U0001"


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def open_slots(weekday: int) -> int:
    """그 요일의 30분 진료 슬롯 수 (월=0). 휴진 요일은 0. 공휴일은 호출하는 쪽에서 거른다."""
    if weekday in HOURS["closed_weekdays"]:
        return 0
    if weekday == 5:
        return (_minutes(HOURS["saturday_close"]) - _minutes(HOURS["open"])) // 30
    lunch = _minutes(HOURS["lunch"][1]) - _minutes(HOURS["lunch"][0])
    return (_minutes(HOURS["weekday_close"]) - _minutes(HOURS["open"]) - lunch) // 30
