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
VISIT_MIX = {"treatment": 0.66, "hygiene": 0.31, "consult": 0.03}

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
REGISTERED_FROM = date(2015, 1, 1)  # 구환 등록일 하한

# 리콜 (§2.6 (1)): 다음 기한 = 마지막 위생 내원 + RECALL_CYCLE_MONTHS개월 ± RECALL_JITTER_DAYS일
RECALL_CYCLE_MONTHS = 12  # 급여 스케일링 연 1회에 맞춤 — 6개월이면 검진만 하는 위생 내원이 늘어 위생 비중이 부푼다
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

# 수납 (§2.4). 결제 수단 가중(%), 미수 내원의 부분 수납 = amount_due × U(범위) — 10원 단위 내림
PAYMENT_METHOD_WEIGHTS = {"card": 80, "cash": 10, "transfer": 10}
UNPAID_PAID_RANGE = (0.0, 0.7)

# 9~10월처럼 전월 이전 N개월은 '제출만' — 심사 결과는 truth_claims에만 (§2.4)
SUBMITTED_MONTHS = 2

# 심사 결과 추첨 (§2.4). 급여 스케일링 줄은 먼저 이 비율로 타 기관 이력(연 1회 초과) 불능 — 우리 장부로는 오류가 아님
EXTERNAL_SCALING_DENY_RATE = 0.02
# 시술군(adjust_group)별 그 밖의 줄 결과 가중(%). 임상 시술(요건 사유가 있는 시술)이 주로 깎인다.
# 확정 월 전체로는 대략 paid 88 / adjusted 8 / denied 4
REVIEW_OUTCOME_WEIGHTS = {
    "clinical": {"paid": 80, "adjusted": 15, "denied": 5},
    "other": {"paid": 94, "adjusted": 4, "denied": 2},
}
# 깎인 줄(타 기관 이력 제외)의 시술군별 사유 유형 가중(%): 요건 / 장부 밖 / 상병. ADJUST_JUDGMENT_TARGET에서 역산,
# 판정 분포 리포트(report_adjust_judgment)로 맞춘 값
ADJUST_TYPE_WEIGHTS = {
    "clinical": {"requirement": 78, "documentation": 15, "diagnosis": 7},
    "other": {"requirement": 0, "documentation": 88, "diagnosis": 12},
}
# adjusted 지급액 = claim_amount × U(범위), 10원 단위 내림
ADJUSTED_PAID_RANGE = (0.5, 0.9)
# 5b: 요건 사유로 깎인 줄 중 임상 요건형 오류로 만드는 비율 (상병 사유로 깎인 줄은 전부 상병 착오)
REQUIREMENT_CUT_ERROR_SHARE = 0.6
# 확정 월 깎인 줄의 판정 목표(%): 정당 / 다툴 만함 / 보완 후 재청구 — 위 가중의 역산 기준. 5b 구현 후 verify 리포트로 출력
ADJUST_JUDGMENT_TARGET = {"justified": 50, "contestable": 20, "resubmit": 30}

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

# aliases는 비어 있다(스키마 기본값 '[]') — 채울지는 기능 정의 §9
PROCEDURES = [
    {**dict(zip(_KEYS, row)), "patient_share_rate": 1.0 if row[3] == "no" else 0.3}
    for row in _ROWS
]

SCALING_CODE = "U0001"
# 발치한 치아에는 이후 진료가 없다 — 임플란트 1차만 그 자리에 한다 (§2.1)
EXTRACTION_CODES = frozenset(p["code"] for p in PROCEDURES if p["name"] in ("단순발치", "사랑니 발치"))
IMPLANT_CODE = next(p["code"] for p in PROCEDURES if p["name"] == "임플란트 1차")

# 적용 연령(age_group)별 나이 범위 (양 끝 포함, None = 상한 없음) — 급여 조건 (§2.1·§2.6 (4))
_AGE_GROUP_RANGE = {
    "child": (0, next(hi for name, _, hi, _ in AGE_BANDS if name == "child")),
    "age19plus": (SCALING_MIN_AGE, None),
    "senior": (CONDITIONAL_INSURED_MIN_AGE, None),
}


def insured_on(procedure: dict, age: int) -> bool:
    """그 나이에 급여인지 — is_insured가 yes·conditional이고 적용 연령 안. 아니면 비급여(본인부담 1.0·청구 없음).
    연 횟수(스케일링 연 1회)는 4단계가 2회째를 구강검진으로 바꿔 여기서 보지 않는다"""
    if procedure["is_insured"] == "no":
        return False
    lo, hi = _AGE_GROUP_RANGE.get(procedure["age_group"], (0, None))
    return lo <= age and (hi is None or age <= hi)

# 주의사항 (§2.2, 4b단계). emr_alert_types에 code·label·category를 적재하고, 관련 시술·행동·대상·비율은 여기에만 둔다.
# 관련 시술 = 그날 예정 시술이 여기 있으면 ④ 주의 칩이 켜진다(평가 설계 §4 반영률). 행동은 ④ 요약·차트(5b)의 처리 문구.
# intake: rules = [(최소 나이, 최대 나이(포함, None = 상한 없음), 성별(None = 모두), 비율)] — 문진 기록일 나이로 판정
# visit(진료 중 발견): rate = 관련 시술을 창 안에서 받은 환자 중 비율, 그 내원 하나에 기록
_P = {p["name"]: p["code"] for p in PROCEDURES}
_ALL = frozenset(_P.values())
_SURGERY = frozenset(_P[n] for n in ("단순발치", "사랑니 발치", "임플란트 1차"))
_BLEEDING = _SURGERY | {_P["스케일링"], _P["잇몸치료"]}
_ANESTHESIA = _SURGERY | frozenset(_P[n] for n in ("레진", "아말감/GI", "인레이", "신경치료", "크라운", "브릿지", "잇몸치료"))
_LONG_OPEN = frozenset(_P[n] for n in ("인레이", "신경치료", "크라운", "브릿지", "사랑니 발치", "임플란트 1차"))
_IMPRESSION = frozenset(_P[n] for n in ("인레이", "크라운", "브릿지"))
_ADULT, _SENIOR = (19, 64), (65, None)
_ALERT_ROWS = [
    # (code, label, category, action, 관련 시술, intake rules 또는 visit 비율)
    ("HTN", "고혈압", "systemic", "진료 전 혈압 측정", _ANESTHESIA, [(*_ADULT, None, 0.08), (*_SENIOR, None, 0.40)]),
    ("DM", "당뇨", "systemic", "혈당 확인 후 진행, 감염 주의", _SURGERY | {_P["잇몸치료"]},
     [(*_ADULT, None, 0.04), (*_SENIOR, None, 0.25)]),
    ("ANTICOAG", "항응고제 복용", "systemic", "복용 여부 확인, 지혈 준비", _BLEEDING,
     [(*_ADULT, None, 0.01), (*_SENIOR, None, 0.15)]),
    ("OSTEO", "골다공증 약 복용", "systemic", "약 종류·복용 기간 확인 후 원장 판단", _SURGERY,
     [(*_SENIOR, "F", 0.15), (*_SENIOR, "M", 0.02)]),
    ("PROSTHESIS", "인공관절·인공판막", "systemic", "예방 항생제 필요 여부 확인", _BLEEDING, [(*_SENIOR, None, 0.06)]),
    ("PREGNANT", "임신 중", "systemic", "방사선·약 처방 최소화, 원장 확인", _ANESTHESIA, [(19, 44, "F", 0.03)]),  # 끝: PREGNANCY_LEFT_DAYS
    ("LATEX", "라텍스 알레르기", "allergy", "비라텍스 장갑 사용", _ALL, [(0, None, None, 0.01)]),
    ("PENICILLIN", "페니실린 알레르기", "allergy", "항생제 처방 시 대체 약", _SURGERY | {_P["신경치료"]},
     [(0, None, None, 0.05)]),
    ("EPI", "에피네프린 민감", "allergy", "무에피 마취제 준비", _ANESTHESIA, [(19, None, None, 0.03)]),
    ("CLAUSTRO", "폐소공포증", "procedure", "러버댐·장시간 진료 시 중간 휴식", _LONG_OPEN, [(13, None, None, 0.05)]),
    ("TRISMUS", "개구 제한", "procedure", "짧게 나눠 진행", _LONG_OPEN, [(19, None, None, 0.02)]),
    ("FEAR", "치과 공포", "procedure", "시술 전 충분히 설명하고 천천히 진행", _ALL, [(0, None, None, 0.06)]),
    ("CHILD_COOP", "협조 어려운 소아", "procedure", "보호자 동반, 짧게 진행", _ALL,  # 끝: 소아 구간을 벗어나는 생일
     [(0, next(hi for name, _, hi, _ in AGE_BANDS if name == "child"), None, 0.15)]),
    ("UPRIGHT", "체어 눕히지 말고 진료", "procedure", "체어를 세운 자세로 진료", _ALL,
     [(*_ADULT, None, 0.01), (*_SENIOR, None, 0.06)]),
    ("ANES_FAIL", "마취가 잘 안 됨", "procedure", "마취 추가 준비, 시간 여유", _ANESTHESIA, 0.05),
    ("GAG", "구역반사", "procedure", "상체를 세우고 구역반사 대비", _IMPRESSION | {_P["스케일링"]}, 0.04),
    ("SYNCOPE", "마취 후 실신 이력", "procedure", "눕힌 자세로 마취, 마취 후 관찰", _ANESTHESIA, 0.01),
]
ALERT_TYPES = {
    code: {"label": label, "category": category, "action": action, "procedures": frozenset(procs),
           **({"source": "visit", "rules": [], "rate": target} if isinstance(target, float)
              else {"source": "intake", "rules": target})}
    for code, label, category, action, procs, target in _ALERT_ROWS
}
MAX_ALERTS_PER_PATIENT = 3
# 기간이 있는 주의사항의 끝(ends_at — 이 날부터 안 보인다). 임신은 기록일 + 남은 기간 U(양 끝 포함, 일),
# 협조 어려운 소아는 소아 구간 최대 나이 + 1세 생일. 나머지는 끝이 없다
PREGNANCY_LEFT_DAYS = (1, 280)

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


MAX_OPEN_SLOTS = max(open_slots(w) for w in range(7))  # 가장 긴 날의 슬롯 수


def daily_cap(visits_per_day: int, weekday: int) -> int:
    """그 요일의 하루 비취소 예약 상한 — config visits_per_day(가장 긴 날 기준)를 슬롯 수에 비례해 내림 (§1)"""
    return visits_per_day * open_slots(weekday) // MAX_OPEN_SLOTS


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

# 심사 조정 상세 사유 (§2.4). category = 심평원 조정 사유 범주 문자(청구방법 매뉴얼 2020.7 별첨 3 — 대응은 추정),
# note = 일반사유 서술(adjust_note). 범주 문자는 여러 사유가 공유하므로 서술이 사유를 구별한다. 나이 사유는 두지 않는다
_INSURED_NAMES = ("스케일링", "구강검진", "실란트", "아말감/GI", "신경치료", "단순발치", "사랑니 발치", "임플란트 1차", "잇몸치료")
_REASON_ROWS = [
    # (key, category, type, note, 맞는 시술 이름 — None은 급여 시술 전부)
    ("R01", "C", "requirement", "동일 부위 재시행 간격 기준 미충족", ("아말감/GI", "신경치료")),
    ("R02", "C", "requirement", "치석제거 후 후속 치주치료 확인 불가", ("잇몸치료",)),
    ("R03", "C", "requirement", "치주치료 산정 기준 소견 미확인", ("잇몸치료",)),
    ("R04", "B", "requirement", "실란트 대상 치아 기준 적용착오", ("실란트",)),
    ("R05", "AK", "requirement", "발치 난이도 산정 근거 부족으로 하향 인정", ("사랑니 발치",)),
    ("R06", "B", "requirement", "임플란트 급여 대상 부위 기준 적용착오", ("임플란트 1차",)),
    ("D01", "C", "documentation", "내역설명 미기재로 인정 근거 없음", ("잇몸치료", "신경치료", "사랑니 발치", "임플란트 1차", "실란트")),
    ("D02", "M", "documentation", "방사선 등 증빙자료 미확인", ("신경치료", "단순발치", "사랑니 발치", "임플란트 1차")),
    ("D03", "K", "documentation", "부위·산정 내역 기재 누락", None),
    ("C01", "B", "diagnosis", "상병과 시행 행위 불일치", None),
    ("E01", "C", "external", "연 1회 산정 횟수 초과(타 기관 이력)", ("스케일링",)),
]
_CODE_BY_NAME = {p["name"]: p["code"] for p in PROCEDURES}
ADJUST_REASONS = {
    key: {"category": category, "type": adjust_type, "note": note,
          "procedures": frozenset(_CODE_BY_NAME[n] for n in (names or _INSURED_NAMES))}
    for key, category, adjust_type, note, names in _REASON_ROWS
}


# 차트 상병 (§4.3, 5b단계). 시술처럼 가상 코드 — 뜻은 이름이 갖는다. emr_diagnoses에 적재
DIAGNOSES = {
    "D01": "상아질 우식", "D02": "치수염", "D03": "만성 근단 치주염", "D04": "만성 치은염", "D05": "만성 치주염",
    "D06": "치아 침착물(치석)", "D07": "매복치", "D08": "치아 상실", "D09": "치아 위치 이상", "D10": "치아 변색",
    "D11": "치과 검사", "D12": "우식 예방 처치",
}
# 시술별 허용 상병 — 차트 상병이 이 밖이면 상병 착오(⑤-2 상병 확인 경고). 예방 시술(불소도포·실란트 → D12)은 확신이 낮다
_DX_BY_NAME = {
    "스케일링": ("D06", "D04"), "구강검진": ("D11",), "불소도포": ("D12",), "실란트": ("D12",), "레진": ("D01",),
    "아말감/GI": ("D01",), "인레이": ("D01",), "신경치료": ("D02", "D03"), "크라운": ("D01", "D03"), "브릿지": ("D08",),
    "단순발치": ("D03", "D05"), "사랑니 발치": ("D07",), "임플란트 1차": ("D08",), "임플란트 2차": ("D08",),
    "교정 상담": ("D09",), "교정 월 체크": ("D09",), "미백": ("D10",), "잇몸치료": ("D05",), "상담": ("D11",),
}
PROCEDURE_DIAGNOSES = {_CODE_BY_NAME[n]: frozenset(dx) for n, dx in _DX_BY_NAME.items()}

# 임상 요건 (§4.3, 5b단계) — 요건 사유(ADJUST_REASONS의 requirement 유형)와 1:1. 차트에 그 요건의 근거가 있는지가 ⑤-2·⑤-3의 기준.
# 요건 줄: 이 시술의 창 안 급여 줄은 항상 요건 줄(잇몸치료는 직전 PERIO_LINK_DAYS일 안에 치석제거가 있으면 R02, 아니면 R03)
CHART_REQUIREMENT = {_CODE_BY_NAME[n]: key for n, key in
                     (("잇몸치료", "R03"), ("실란트", "R04"), ("사랑니 발치", "R05"), ("임플란트 1차", "R06"))}
PERIO_LINK_DAYS = 90
# 아말감/GI·신경치료는 같은 치아에 같은 시술을 다시 할 때(장부에 이전 진료가 있거나 이 사유로 깎였을 때)만 요건 줄
RETREATMENT_REQUIREMENT = "R01"
# 제출 월·전월 오류 비율 — ⑤-2 평가가 성립하는 평가용 분포(확정 월은 심사 결과에서 정한다). 진료 1건당 오류 최대 1개
REQUIREMENT_ERROR_RATE = 0.25  # 요건 줄 중 임상 요건형 오류
DIAGNOSIS_ERROR_RATE = 0.03    # 요건 오류가 아닌 급여 줄 중 상병 착오
# 디코이 = 특이 케이스 수 × 이 배수만큼 평범한 창 안 줄을 서술(시술별 비례 층화)
CHART_DECOY_RATIO = 1.5
CHART_FORBIDDEN_WORDS = ("청구 오류", "삭감", "조정", "요건", "급여 기준")  # 차트 본문에 정답 어휘 금지 (§4.3 서술 규칙)


def adjust_group(procedure_code: str) -> str:
    """심사 결과 추첨의 시술군 — 요건 사유가 있는 시술이 clinical (REVIEW_OUTCOME_WEIGHTS·ADJUST_TYPE_WEIGHTS의 키)"""
    return "clinical" if any(r["type"] == "requirement" and procedure_code in r["procedures"]
                             for r in ADJUST_REASONS.values()) else "other"


# 통화 슬롯 (§4.1, 6단계). 목적·노이즈·방향 비중은 §4.1 표가 설계 목표 — 어긋나면 tests/generator/test_doc_sync.py가 실패.
# 목적·목적 안 방향·노이즈는 할당량(최대 잔여 반올림)으로 맞추고, 급함·플래그는 건마다 추첨한다
CALL_PURPOSE_SHARE = {
    "book": 0.18, "reschedule": 0.15, "confirm": 0.08, "cancel": 0.06, "recall": 0.08, "treatment_q": 0.10,
    "insurance_q": 0.06, "price_q": 0.08, "billing_q": 0.04, "document_req": 0.04, "emergency": 0.05,
    "complaint": 0.03, "other": 0.03, "unknown": 0.02,
}
# 예약 이벤트에서 역산하는 목적 — 이벤트 하나에 통화 최대 1건
BOOKING_PURPOSES = ("book", "reschedule", "cancel", "confirm")
# outbound 비중(전체 통화 대비 %p). 목적 안 방향 할당량 = 이 값 / 목적 비중. 나머지 목적은 전부 inbound
CALL_OUTBOUND_SHARE = {"recall": 0.08, "confirm": 0.04, "billing_q": 0.03, "document_req": 0.02, "treatment_q": 0.03}
OUTBOUND_MULTI_SHARE = 0.5  # outbound 중 구조상 복합 주제(noise_type 없음) — 노이즈 '주제 2개 섞임'과 별개
CALL_NOISE_SHARE = {"two_topics": 0.10, "interrupted": 0.08, "ambiguous_pain": 0.04, "guardian_confused": 0.03}
# 상대 분포(§4.1) — 목표가 아니라 근사(상대는 목적·환자에서 정해짐). 리포트로만 본다
CALL_COUNTERPART_SHARE = {"patient": 0.60, "guardian": 0.15, "new_caller": 0.15, "insurer": 0.03, "vendor": 0.04,
                          "other": 0.03}
URGENCIES = ("urgent", "normal", "low")
# 목적별 상대 가중(%) — 예약류 아닌 목적. side = 환자 측(통화일에 소아면 guardian, 아니면 patient). outbound는 항상 side
CALL_COUNTERPART_WEIGHTS = {
    "recall": {"side": 100},
    "treatment_q": {"side": 85, "new_caller": 15},
    "insurance_q": {"side": 50, "new_caller": 30, "insurer": 20},
    "price_q": {"side": 40, "new_caller": 60},
    "billing_q": {"side": 85, "insurer": 15},
    "document_req": {"side": 70, "insurer": 30},
    "emergency": {"side": 70, "new_caller": 30},
    "complaint": {"side": 100},
    "other": {"side": 20, "vendor": 50, "other": 30},
    "unknown": {"side": 30, "new_caller": 30, "other": 40},
}
# 플래그 6개. 주 목적이 함의하는 플래그는 항상 true
CALL_FLAGS = ("emergency", "complaint", "price_inquiry", "wants_appt", "appt_confirmed", "has_todo")
IMPLIED_FLAG = {"emergency": "emergency", "complaint": "complaint", "price_q": "price_inquiry", "book": "wants_appt"}
# 부 주제 플래그 → accepted_purposes에 들어가는 목적
SUBTOPIC_PURPOSE = {"price_inquiry": "price_q", "complaint": "complaint", "emergency": "emergency", "wants_appt": "book"}
# 목적별로 가능한 부 주제 플래그(주 목적이 함의하는 것 제외). 빈 목적은 복합 주제를 만들지 않는다
CALL_SUBTOPICS = {
    "book": ("price_inquiry",), "reschedule": ("price_inquiry", "complaint"), "cancel": ("complaint",),
    "confirm": ("price_inquiry",), "recall": ("wants_appt", "price_inquiry"),
    "treatment_q": ("price_inquiry", "wants_appt", "emergency"), "insurance_q": ("price_inquiry", "wants_appt"),
    "price_q": ("wants_appt",), "billing_q": ("complaint",), "document_req": ("wants_appt",),
    "emergency": ("wants_appt",), "complaint": ("wants_appt",), "other": (), "unknown": (),
}
CALL_PAIN_PURPOSES = ("book", "treatment_q")  # 노이즈 '급함 애매한 통증' 대상
PAIN_URGENT_SHARE = 0.5                       # 그중 urgent (나머지 normal)
CALL_URGENCY_WEIGHTS = {"normal": 85, "low": 15}  # emergency 플래그·애매한 통증이 아닌 통화
APPT_CONFIRMED_SHARE = 0.7                    # book·reschedule 중 appt_confirmed
HAS_TODO_SHARE = {"confirm": 0.3, "recall": 0.3}  # 나머지 목적은 HAS_TODO_DEFAULT
HAS_TODO_DEFAULT = 0.8
CALL_DURATION_SEC = (60, 180)  # 양 끝 포함 — 전사 규칙 1~3분 분량(§4.1 절차 5)
# 통화 전사 (§4.1 '7단계 구현 결정'). 발화 글자 수(화자 표시·공백 제외) ÷ 이 값 = 통화 길이(초)
TRANSCRIPT_CHARS_PER_SEC = 4
# 라벨 어휘 — 전사에 그대로 나오면 검증 실패(§4.1 절차 6(b)). 목적 이름(§4.1 목적 분포 표)과 그 조각
CALL_LABEL_WORDS = ("예약 잡기", "예약 변경", "예약 확인", "예약 취소", "리콜", "치료 문의", "보험 문의", "가격 문의",
                    "비용 문의", "수납 문의", "청구 문의", "서류 요청", "기록 요청", "응급", "불만")
# 환자 측(예약류 아님) 환자 조건 — (목적, 방향) → (규칙, 값). 없으면 'visited'(통화일 전 completed 내원이 있음)
# recall_due: 마지막 위생 내원 + RECALL_CYCLE < 통화일이고 그 시점 잡힌 예약 없음 / balance: 그 시점 잔액 > 0 /
# recent_treatment: 통화일 값[0]~값[1]일 전에 진료(treatment) 내원 / recent_visit: 통화일 값일 안에 내원
CALL_PATIENT_RULES = {
    ("recall", "outbound"): ("recall_due", None),
    ("billing_q", "outbound"): ("balance", None),
    ("treatment_q", "outbound"): ("recent_treatment", (1, 7)),
    ("complaint", "inbound"): ("recent_visit", 30),
}
# 판정 풀 층화 (평가 설계 §2.1): 희귀 카테고리는 각 전사의 이 비율, 나머지는 비례 + split당 최소 건수
EVAL_RARE_PURPOSES = ("complaint", "emergency", "unknown")
EVAL_RARE_SHARE = 0.6
EVAL_MIN_PER_SPLIT = 2


# 문자 (§2.5, 8단계). 리마인드는 리마인드 날 개원 시각(HOURS["open"])에 일괄
MESSAGE_CONFIRM_DELAY_MIN = (1, 5)  # 예약 확정 문자 = 예약을 잡은 뒤 (양 끝 포함, 분)
MESSAGE_DELAY_MIN = (1, 30)         # 노쇼 문자(진료가 끝났을 시각 뒤)·답장(out 뒤)
# 트리거별 본문. date = '11월 3일(화)', 리마인드도 '내일'이라 쓰지 않는다(직전 진료일이 전날이 아닐 수 있음)
MESSAGE_TEMPLATES = {
    "appt_confirm": "[{clinic}] {name}님, {date} {time} {procedure} 예약이 확정되었습니다.",
    "reminder": "[{clinic}] {name}님, {date} {time} {procedure} 예약 안내드립니다.",
    "noshow": "[{clinic}] {name}님, 오늘 {time} 예약에 내원하지 않으셔서 연락드립니다. 다시 예약하시려면 연락 주세요.",
    "balance": "[{clinic}] {name}님, {date} 진료비 중 {amount}원이 미납되어 안내드립니다.",
    "doc_ready": "[{clinic}] {name}님, 요청하신 서류가 준비되었습니다. 내원하실 때 받아 가세요.",
}
# 트리거별 답장 문구 (균등)
MESSAGE_REPLIES = {
    "appt_confirm": ("네", "확인했습니다", "시간 변경 가능할까요?"),
    "reminder": ("네", "확인했습니다", "시간 변경 가능할까요?"),
    "noshow": ("죄송합니다. 다시 연락드릴게요", "깜빡했어요"),
    "balance": ("다음에 갈 때 낼게요", "확인했습니다"),
    "doc_ready": ("감사합니다", "네, 들를게요"),
}
