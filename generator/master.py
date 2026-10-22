"""병원 마스터 — 생성기와 verify가 함께 쓰는 값의 원본. 의미·규칙은 docs/치과 AX - 가상 데이터 생성 설계.md §1·§2"""

from datetime import date, timedelta

CLINIC_NAME = "AX치과"  # 가상 병원

# 직원 구성 (§1). (role, 인원). 진료=원장, 위생=위생사(순환), 상담=상담실장
STAFF = [("doctor", 1), ("hygienist", 3), ("desk", 2), ("consultant", 1)]

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

# 당일 취소 시각 = 예약 1~SAMEDAY_CANCEL_LEAD_MIN분 전 (§2.6 (3))
SAMEDAY_CANCEL_LEAD_MIN = 120

# 예약 시점 (§2.6 (3)): 전화 예약 희망 리드 1~MAX_LEAD_DAYS일, 변경 새 예약은 원래 날짜 뒤 1~RESCHEDULE_MAX_DAYS일
MAX_LEAD_DAYS = 14
RESCHEDULE_MAX_DAYS = 14

# 스케일링 급여 나이 (§2.1: 19세↑ 연 1회)
SCALING_MIN_AGE = 19

# 임플란트 1차(conditional)는 시술일 기준 이 나이 이상이면 급여, 미만이면 비급여(본인부담 1.0·청구 없음) (§2.6 (4))
CONDITIONAL_INSURED_MIN_AGE = 65

# 내원 구성 목표 (§1). 목표 — 경고로만 확인
VISIT_MIX = {"treatment": 0.70, "hygiene": 0.25, "consult": 0.05}

# 연령 구간과 비중 (§2.2). (이름, 최소 나이, 최대 나이(포함, 노인은 None), 비중)
AGE_BANDS = [
    ("child", 0, 12, 0.15),
    ("teen", 13, 18, 0.08),
    ("adult", 19, 64, 0.62),
    ("senior", 65, None, 0.15),
]

# 나이 상한 (§2.2) — 노인 구간(최대 나이 None)을 뽑을 때만
MAX_AGE = 90


def add_years(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:  # 2월 29일
        return d.replace(year=d.year + years, day=28)


def age_on(birth: date, on: date) -> int:
    return on.year - birth.year - ((on.month, on.day) < (birth.month, birth.day))


def age_band(age: int) -> str | None:
    """AGE_BANDS 구간 이름. 음수(미래 출생)면 None"""
    return next((name for name, lo, hi, _ in AGE_BANDS if lo <= age and (hi is None or age <= hi)), None)


# 환자 (§2.2). 비중은 추첨 없이 비중 × 인원을 반올림한 인원으로 맞춘다(가족은 ±1명, 소아가 많으면 넘을 수 있음)
FAMILY_SHARE = 0.35            # 가족으로 묶인 환자 비중
FAMILY_SIZE = (2, 4)           # 가족 인원 (양 끝 포함)
GUARDIAN_MIN_AGE_GAP = 20      # 보호자는 가족의 소아·청소년보다 이 나이 이상 많다
PRIVATE_INSURANCE_SHARE = 0.50
NOTE_SHARE = 0.10
NOTES = ("치과 공포 있음", "고혈압 약 복용", "당뇨 있음", "오전 진료 선호", "항응고제 복용",
         "마취 시 어지러움 호소", "저녁 연락 선호")
REGISTERED_FROM = date(2015, 1, 1)  # 구환 등록일 하한

# 리콜 (§2.6 (1)): 다음 기한 = 마지막 위생 내원 + RECALL_CYCLE_MONTHS개월 ± RECALL_JITTER_DAYS일
RECALL_CYCLE_MONTHS = 6
RECALL_JITTER_DAYS = 30
RECALL_CYCLE = timedelta(days=round(RECALL_CYCLE_MONTHS * 365.25 / 12))

# 치료 수요의 에피소드 유형 비중 (§2.6 (2)). T1 = treatment 1회, Tn = treatment COURSE_VISITS회, consult = 상담 1회
_EPISODE_ADULT = {"T1": 0.50, "Tn": 0.40, "consult": 0.10}
_EPISODE_YOUNG = {"T1": 0.70, "Tn": 0.25, "consult": 0.05}
EPISODE_MIX = {"child": _EPISODE_YOUNG, "teen": _EPISODE_YOUNG, "adult": _EPISODE_ADULT, "senior": _EPISODE_ADULT}
COURSE_VISITS = (2, 4)  # 양 끝 포함
COURSE_INTERVAL_DAYS = (7, 14)  # 코스 다음 회차 희망 간격 (양 끝 포함)

# 신환 = 월 내원(completed)의 NEW_PATIENT_SHARE (§1). 생성 구조(schedule.new_patients)로 맞춘다
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


def open_slots(weekday: int) -> int:
    """그 요일의 진료 슬롯(SLOT_MIN분) 수 (월=0). 휴진 요일은 0. 공휴일은 호출하는 쪽에서 거른다."""
    if weekday in HOURS["closed_weekdays"]:
        return 0
    if weekday == SATURDAY:
        return (_minutes(HOURS["saturday_close"]) - _minutes(HOURS["open"])) // SLOT_MIN
    lunch = _minutes(HOURS["lunch"][1]) - _minutes(HOURS["lunch"][0])
    return (_minutes(HOURS["weekday_close"]) - _minutes(HOURS["open"]) - lunch) // SLOT_MIN


def daily_cap(visits_per_day: int, weekday: int) -> int:
    """그 요일의 하루 비취소 예약 상한 — config visits_per_day(가장 긴 날 기준)를 슬롯 수에 비례해 내림 (§1)"""
    return visits_per_day * open_slots(weekday) // max(open_slots(w) for w in range(7))


# 유형별 예정 시술 가중 (§2.6 (4)). 시술 이름 → 가중(%). 연령 구간은 예약일 기준.
# 성인·노인 hygiene은 표가 아니라 규칙: 그해 스케일링을 이미 했으면 구강검진, 아니면 스케일링 (HYGIENE_SCALING_BANDS)
PLANNED_WEIGHTS = {
    "hygiene": {
        "child": {"구강검진": 40, "불소도포": 60},
        "teen": {"구강검진": 60, "스케일링": 40},  # 19세 미만 스케일링은 비급여 (5단계)
    },
    "treatment": {
        "child": {"아말감/GI": 35, "실란트": 30, "레진": 20, "단순발치": 15},
        "teen": {"교정 월 체크": 50, "레진": 30, "아말감/GI": 10, "사랑니 발치": 10},
        "adult": {"레진": 27, "잇몸치료": 15, "아말감/GI": 10, "신경치료": 9, "단순발치": 8, "크라운": 7,
                  "교정 월 체크": 7, "사랑니 발치": 6, "인레이": 5, "임플란트 1차": 3, "미백": 2, "브릿지": 1},
        "senior": {"잇몸치료": 26, "단순발치": 18, "임플란트 1차": 14, "레진": 12, "아말감/GI": 10, "신경치료": 8,
                   "크라운": 8, "브릿지": 4},
    },
    "consult": {
        "child": {"상담": 100},
        "teen": {"교정 상담": 70, "상담": 30},
        "adult": {"상담": 85, "교정 상담": 15},
        "senior": {"상담": 100},
    },
}
HYGIENE_SCALING_BANDS = ("adult", "senior")
CHECKUP_CODE = next(p["code"] for p in PROCEDURES if p["name"] == "구강검진")
