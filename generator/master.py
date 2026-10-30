"""병원 마스터 — 생성기와 verify가 함께 쓰는 값의 원본. 의미·규칙은 docs/치과 AX - 가상 데이터 생성 설계.md §1·§2"""

import json
from datetime import date, timedelta

CLINIC_NAME = "클로드치과"  # 가상 병원

# 직원 구성 (§1). (role, 인원). 진료=원장, 위생=위생사(순환), 상담=상담실장
STAFF = [("doctor", 1), ("hygienist", 3), ("desk", 2), ("consultant", 1)]

# 체어·동시 진료 운영 제약 (§1 (3)·(4)). 한 체어는 소요시간 동안 예약 1건, 위생사는 동시에 1건, 상담은 체어 없음.
# 하루 상한을 막지 않을 만큼(체어 × 슬롯 ≥ 상한 × 평균 슬롯) — 넣은 뒤 내원 구성 경고·하루 채움 리포트(§5.3)로 확인
CHAIRS = 5
DOCTOR_CONCURRENCY = 3

# 영업시간 (§1). emr_clinic.hours_json에 그대로 저장한다. 요일은 월=0
HOURS = {
    "open": "09:30",
    "weekday_close": "18:30",
    "saturday_close": "13:30",
    "lunch": ["13:00", "14:00"],  # 평일만
    "closed_weekdays": [3, 6],  # 목, 일 (+ 공휴일)
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
    return next(
        (name for name, lo, hi, _ in AGE_BANDS if lo <= age and (hi is None or age <= hi)), None
    )


# 환자 (§2.2). 비중은 추첨 없이 비중 × 인원을 반올림한 인원으로 맞춘다(가족은 ±1명, 소아가 많으면 넘을 수 있음)
FAMILY_SHARE = 0.35  # 가족으로 묶인 환자 비중
FAMILY_SIZE = (2, 4)  # 가족 인원 (양 끝 포함)
GUARDIAN_MIN_AGE_GAP = 20  # 보호자는 가족의 소아·청소년보다 이 나이 이상 많다
PRIVATE_INSURANCE_SHARE = 0.50
REGISTERED_FROM = date(2015, 1, 1)  # 구환 등록일 하한

# 다른 치과 이용 (§2.2, ⑨ 다른 치과 추정의 정답 — truth_patient_churn). 추정값 — 리포트로 확인
CHURN_SHARE = 0.03  # 구환 중 이탈 비율 (병행은 PARALLEL_SHARE)
CHURN_MIN_DAYS = (
    120  # 이탈일은 TODAY − 이 일수 이전 (리콜 무응답·노쇼·재예약 없음 흔적이 쌓일 시간)
)

# 리콜 (§2.6 (1)): 다음 기한 = 마지막 위생 내원 + RECALL_CYCLE_MONTHS개월 ± RECALL_JITTER_DAYS일
RECALL_CYCLE_MONTHS = (
    12  # 급여 스케일링 연 1회에 맞춤 — 6개월이면 검진만 하는 위생 내원이 늘어 위생 비중이 부푼다
)
RECALL_JITTER_DAYS = 30
# 휴면 (§2.6 (1)): 환자마다 월 이 확률로(지수분포 — 구환은 창 시작부터, 신환은 등록 다음 날부터) 휴면일이 오고, 그날부터
# 리콜·새 치료 수요가 없다(진행 중 에피소드는 이어 감). 다른 치과로 옮긴 건 아님(⑨ 정답 아님). 신환이 계속 들어오는 만큼 빠져야
# 긴 창에서 하루 상한에 붙지 않는다 — 하루 채움 리포트로 맞춘 값
DORMANT_MONTHLY_RATE = 0.03
RECALL_CYCLE = timedelta(days=round(RECALL_CYCLE_MONTHS * 365.25 / 12))

# 치료 수요의 에피소드 유형 비중 (§2.6 (2)). T1 = treatment 1회, Tn = 치료계획 항목 수만큼 treatment(§2.3), consult = 상담 1회
_EPISODE_ADULT = {"T1": 0.50, "Tn": 0.40, "consult": 0.10}
_EPISODE_YOUNG = {"T1": 0.70, "Tn": 0.25, "consult": 0.05}
EPISODE_MIX = {
    "child": _EPISODE_YOUNG,
    "teen": _EPISODE_YOUNG,
    "adult": _EPISODE_ADULT,
    "senior": _EPISODE_ADULT,
}
COURSE_VISITS = (
    2,
    4,
)  # 양 끝 포함 — 3단계 수요 추첨에서 Tn 여부(2회 이상)로만 쓰인다. 실제 회차는 치료계획 항목 수
# 치아 번호가 영구치(PERMANENT_TEETH)뿐이라 영구치가 나기 전 나이에는 치료 수요가 없다 — 이 나이 미만은 위생(신환은 상담도)만
TOOTH_TREATMENT_MIN_AGE = 6
MIN_VISIT_AGE = 1  # 첫 등록(첫 내원) 나이 하한 — 젖니가 나기 전 영아는 등록하지 않는다
COURSE_INTERVAL_DAYS = (7, 14)  # 코스 다음 회차 희망 간격 (양 끝 포함)

# 치료계획 (§2.3·§2.6 (3)). 항목 순서 = 시술 이름 튜플(항목 2~4개). 추정값 — 리포트로 확인
# treatment: 코스 첫 내원의 예정 시술 → 항목(항목 1 = 그 내원) / consult: 상담 내원이 만드는 계획 후보(균등) /
# hygiene: 치주 계획을 만드는 위생 내원의 항목. 연령에 따른 급여·치아 조건은 쓰는 쪽이 본다
PLAN_TEMPLATES = {
    "treatment": {
        "실란트": ("실란트", "실란트"),
        "레진": ("레진", "레진"),
        "아말감/GI": ("아말감/GI", "아말감/GI"),
        "인레이": ("인레이", "인레이"),
        "신경치료": ("신경치료", "신경치료", "크라운"),
        "크라운": ("크라운", "크라운"),
        "브릿지": ("브릿지", "브릿지", "브릿지"),
        "단순발치": ("단순발치", "단순발치"),
        "사랑니 발치": ("사랑니 발치", "사랑니 발치"),
        "임플란트 1차": ("임플란트 1차", "임플란트 1차"),
        "교정 월 체크": ("교정 월 체크", "교정 월 체크", "교정 월 체크", "교정 월 체크"),
        "미백": ("미백", "미백"),
        "잇몸치료": ("잇몸치료", "잇몸치료", "잇몸치료"),
    },
    "consult": {
        "상담": (
            ("신경치료", "크라운"),
            ("인레이", "인레이"),
            ("단순발치", "임플란트 1차"),
            ("미백", "미백"),
        ),
        "교정 상담": (("교정 월 체크", "교정 월 체크", "교정 월 체크"),),
    },
    "hygiene": ("잇몸치료", "잇몸치료"),
}
# 수락한 계획 중 중간에 그만두는 비율(남은 항목은 열린 채) / 상담·치주 위생 계획 중 처음부터 예약하지 않는 비율.
# 열린 계획이 ⑨ 진료계획 미예약 칩에 맞는 규모가 되게 맞춘다. 추정값 — 리포트로 확인
PLAN_DROP_RATE = 0.10
PLAN_UNBOOKED_RATE = 0.30
# 성인·노인 스케일링 내원 중 치주 계획(PLAN_TEMPLATES hygiene)을 만드는 비율. 추정값 — 리포트로 확인
PERIO_PLAN_RATE = 0.10

# 신환 = 월 내원(completed)의 NEW_PATIENT_SHARE (§1). 생성 구조(schedule.new_patients)로 맞춘다
NEW_PATIENT_SHARE = 0.12

# 치아 번호 (§2.1). 치아 단위가 아닌 시술은 FULL_MOUTH 센티널
PERMANENT_TEETH = frozenset(f"{q}{n}" for q in (1, 2, 3, 4) for n in range(1, 9))
FULL_MOUTH = "00"

# 수납 (§2.4). 결제 수단 가중(%), 미수 내원의 부분 수납 = amount_due × U(범위) — 10원 단위 내림
PAYMENT_METHOD_WEIGHTS = {"card": 80, "cash": 10, "transfer": 10}
UNPAID_PAID_RANGE = (0.0, 0.7)

# 9~10월처럼 전월 이전 N개월은 '제출만' — 심사 결과는 아직 없음 (§2.4)
SUBMITTED_MONTHS = 2

# 재심 기한 = 확정 월 청구의 심사결과 통보일(emr_claims.reviewed_at) + 이 일수 (§2.4 통보일, ⑤-3 조정 목록)
APPEAL_DAYS = 90
# 심사결과 통보일 = 청구 제출일(익월 1일) + 이 일수 — 청구 월마다 하루. 제출 월(SUBMITTED_MONTHS)은 TODAY에 아직
# 결과가 없고 그 앞 달은 있도록 (verify check_reviewed_at이 범위를 본다)
REVIEW_NOTICE_DAYS = 70

# 병행(§2.2): 확정 월 창 안 급여 스케일링 줄이 있는 환자 중 이 비율 — 그 줄이 타 기관 이력(연 1회 초과)으로 불능.
# 우리 장부로는 오류가 아님. 추정값 — 리포트로 확인
PARALLEL_SHARE = 0.02
# 시술군(adjust_group)별 그 밖의 줄 결과 가중(%). 임상 시술(요건 사유가 있는 시술)이 주로 깎인다.
# 확정 월 전체로는 대략 paid 88 / adjusted 8 / denied 4
REVIEW_OUTCOME_WEIGHTS = {
    "clinical": {"paid": 80, "adjusted": 15, "denied": 5},
    "other": {"paid": 94, "adjusted": 4, "denied": 2},
}
# 깎인 줄(타 기관 이력 제외)의 시술군별 사유 유형 가중(%): 요건 / 장부 밖 / 상병. ADJUST_JUDGMENT_TARGET에서 역산,
# 판정 분포 리포트(report_adjust_judgment)로 맞춘 값
ADJUST_TYPE_WEIGHTS = {
    "clinical": {"requirement": 78, "documentation": 8, "diagnosis": 14},
    "other": {"requirement": 0, "documentation": 88, "diagnosis": 12},
}
# adjusted 지급액 = claim_amount × U(범위), 10원 단위 내림
ADJUSTED_PAID_RANGE = (0.5, 0.9)
# 5b: 요건 사유로 깎인 줄 중 임상 요건형 오류로 만드는 비율 (상병 사유로 깎인 줄은 전부 상병 착오)
REQUIREMENT_CUT_ERROR_SHARE = 0.6
# 확정 월 깎인 줄의 판정 목표(%): 정당 / 다툴 만함 / 보완 후 재청구 — 위 가중의 역산 기준. 실제 분포는 verify 리포트(report_adjust_judgment)
ADJUST_JUDGMENT_TARGET = {"justified": 50, "contestable": 20, "resubmit": 30}

# 시술 19개 (§2.1 표). 코드는 표 순번 기반 가상 코드
# (code, name, category, is_insured, fee_total, duration_min, insured_limit, limit_scope, age_group, is_tooth_level)
# age_group: child = 0–12세, age19plus = 19세 이상(노인 포함), senior = 65세 이상 — AGE_BANDS의 구간 이름과 별개
_ROWS = [
    ("U0001", "스케일링", "preventive", "yes", 60_000, 30, 1, "year", "age19plus", 0),
    ("U0002", "구강검진", "preventive", "yes", 15_000, 30, None, None, None, 0),
    ("U0003", "불소도포", "preventive", "no", 30_000, 30, None, None, "child", 0),
    ("U0004", "실란트", "preventive", "yes", 40_000, 30, None, None, "child", 1),
    ("U0005", "레진", "restorative", "no", 80_000, 30, None, None, None, 1),
    ("U0006", "아말감/GI", "restorative", "yes", 25_000, 30, None, None, None, 1),
    ("U0007", "인레이", "restorative", "no", 350_000, 60, None, None, None, 1),
    ("U0008", "신경치료", "endo", "yes", 120_000, 60, None, None, None, 1),
    ("U0009", "크라운", "prosth", "no", 550_000, 60, None, None, None, 1),
    ("U0010", "브릿지", "prosth", "no", 1_500_000, 60, None, None, None, 0),
    ("U0011", "단순발치", "surgery", "yes", 30_000, 30, None, None, None, 1),
    ("U0012", "사랑니 발치", "surgery", "yes", 90_000, 60, None, None, None, 1),
    ("U0013", "임플란트 1차", "implant", "conditional", 1_300_000, 90, None, None, "senior", 1),
    ("U0014", "임플란트 2차", "implant", "no", 0, 60, None, None, None, 0),
    ("U0015", "교정 상담", "ortho", "no", 0, 30, None, None, None, 0),
    ("U0016", "교정 월 체크", "ortho", "no", 80_000, 30, None, None, None, 0),
    ("U0017", "미백", "cosmetic", "no", 300_000, 60, None, None, None, 0),
    ("U0018", "잇몸치료", "perio", "yes", 35_000, 30, None, None, None, 0),
    ("U0019", "상담", "consult", "no", 0, 30, None, None, None, 0),
]
_KEYS = (
    "code",
    "name",
    "category",
    "is_insured",
    "fee_total",
    "duration_min",
    "insured_limit",
    "limit_scope",
    "age_group",
    "is_tooth_level",
)

# 구어 별칭 (§2.1 aliases) — 전사의 꼭 넣을 시술 검사(글자 포함)·검색 동의어(차트는 표준 이름만). 다른 시술과 겹치는 말·
# 낱말이 아닌 조각은 넣지 않는다(때우는 — 레진과, 발치 — 사랑니 발치와, 교정 — 교정 상담과, '이 뽑' — '아이 뽑'에도 맞음)
PROCEDURE_ALIASES = {
    "스케일링": ["치석 제거"],
    "구강검진": ["검진"],
    "불소도포": ["불소"],
    "실란트": ["홈 메우기", "홈메우기"],
    "아말감/GI": ["아말감"],
    "신경치료": ["신경 치료"],
    "브릿지": ["브리지"],
    "단순발치": ["단순 발치"],
    "사랑니 발치": ["사랑니"],
    "임플란트 1차": ["임플란트"],
    "임플란트 2차": ["임플란트"],
    "교정 월 체크": ["교정 체크", "교정 점검"],
    "미백": ["화이트닝"],
    "잇몸치료": ["잇몸 치료"],
}
PROCEDURES = [
    {
        **dict(zip(_KEYS, row, strict=True)),
        "patient_share_rate": 1.0 if row[3] == "no" else 0.3,
        "aliases": json.dumps(PROCEDURE_ALIASES.get(row[1], []), ensure_ascii=False),
    }
    for row in _ROWS
]

SCALING_CODE = "U0001"
# 발치한 치아에는 이후 진료가 없다 — 임플란트 1차만 그 자리에 한다 (§2.1)
EXTRACTION_CODES = frozenset(
    p["code"] for p in PROCEDURES if p["name"] in ("단순발치", "사랑니 발치")
)
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
_ANESTHESIA = _SURGERY | frozenset(
    _P[n] for n in ("레진", "아말감/GI", "인레이", "신경치료", "크라운", "브릿지", "잇몸치료")
)
_LONG_OPEN = frozenset(
    _P[n] for n in ("인레이", "신경치료", "크라운", "브릿지", "사랑니 발치", "임플란트 1차")
)
_IMPRESSION = frozenset(_P[n] for n in ("인레이", "크라운", "브릿지"))
_ADULT, _SENIOR = (19, 64), (65, None)
_ALERT_ROWS = [
    # (code, label, category, action, 관련 시술, intake rules 또는 visit 비율)
    (
        "HTN",
        "고혈압",
        "systemic",
        "진료 전 혈압 측정",
        _ANESTHESIA,
        [(*_ADULT, None, 0.08), (*_SENIOR, None, 0.40)],
    ),
    (
        "DM",
        "당뇨",
        "systemic",
        "혈당 확인 후 진행, 감염 주의",
        _SURGERY | {_P["잇몸치료"]},
        [(*_ADULT, None, 0.04), (*_SENIOR, None, 0.25)],
    ),
    (
        "ANTICOAG",
        "항응고제 복용",
        "systemic",
        "복용 여부 확인, 지혈 준비",
        _BLEEDING,
        [(*_ADULT, None, 0.01), (*_SENIOR, None, 0.15)],
    ),
    (
        "OSTEO",
        "골다공증 약 복용",
        "systemic",
        "약 종류·복용 기간 확인 후 원장 판단",
        _SURGERY,
        [(*_SENIOR, "F", 0.15), (*_SENIOR, "M", 0.02)],
    ),
    (
        "PROSTHESIS",
        "인공관절·인공판막",
        "systemic",
        "예방 항생제 필요 여부 확인",
        _BLEEDING,
        [(*_SENIOR, None, 0.06)],
    ),
    (
        "PREGNANT",
        "임신 중",
        "systemic",
        "방사선·약 처방 최소화, 원장 확인",
        _ANESTHESIA,
        [(19, 44, "F", 0.03)],
    ),  # 끝: PREGNANCY_LEFT_DAYS
    ("LATEX", "라텍스 알레르기", "allergy", "비라텍스 장갑 사용", _ALL, [(0, None, None, 0.01)]),
    (
        "PENICILLIN",
        "페니실린 알레르기",
        "allergy",
        "항생제 처방 시 대체 약",
        _SURGERY | {_P["신경치료"]},
        [(0, None, None, 0.05)],
    ),
    (
        "EPI",
        "에피네프린 민감",
        "allergy",
        "무에피 마취제 준비",
        _ANESTHESIA,
        [(19, None, None, 0.03)],
    ),
    (
        "CLAUSTRO",
        "폐소공포증",
        "procedure",
        "러버댐·장시간 진료 시 중간 휴식",
        _LONG_OPEN,
        [(13, None, None, 0.05)],
    ),
    ("TRISMUS", "개구 제한", "procedure", "짧게 나눠 진행", _LONG_OPEN, [(19, None, None, 0.02)]),
    (
        "FEAR",
        "치과 공포",
        "procedure",
        "시술 전 충분히 설명하고 천천히 진행",
        _ALL,
        [(0, None, None, 0.06)],
    ),
    (
        "CHILD_COOP",
        "협조 어려운 소아",
        "procedure",
        "보호자 동반, 짧게 진행",
        _ALL,  # 끝: 소아 구간을 벗어나는 생일
        [(0, next(hi for name, _, hi, _ in AGE_BANDS if name == "child"), None, 0.15)],
    ),
    (
        "UPRIGHT",
        "체어 눕히지 말고 진료",
        "procedure",
        "체어를 세운 자세로 진료",
        _ALL,
        [(*_ADULT, None, 0.01), (*_SENIOR, None, 0.06)],
    ),
    ("ANES_FAIL", "마취가 잘 안 됨", "procedure", "마취 추가 준비, 시간 여유", _ANESTHESIA, 0.05),
    (
        "GAG",
        "구역반사",
        "procedure",
        "상체를 세우고 구역반사 대비",
        _IMPRESSION | {_P["스케일링"]},
        0.04,
    ),
    (
        "SYNCOPE",
        "마취 후 실신 이력",
        "procedure",
        "눕힌 자세로 마취, 마취 후 관찰",
        _ANESTHESIA,
        0.01,
    ),
]
ALERT_TYPES = {
    code: {
        "label": label,
        "category": category,
        "action": action,
        "procedures": frozenset(procs),
        **(
            {"source": "visit", "rules": [], "rate": target}
            if isinstance(target, float)
            else {"source": "intake", "rules": target}
        ),
    }
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
    for p in PROCEDURES
    if p["is_tooth_level"]
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


def close_time(weekday: int) -> str:
    """그 요일 마감 'HH:MM' — 토요일은 토요일 마감"""
    return HOURS["saturday_close" if weekday == SATURDAY else "weekday_close"]


def business_minutes(weekday: int) -> int:
    """개원 ~ 그 요일 마감 분 — 통화·전화 예약·문자 같은 사건 시각을 뽑는 범위 (§2.6 사건 시각). 점심은 따지지 않는다"""
    return _minutes(close_time(weekday)) - _minutes(HOURS["open"])


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
        "adult": {
            "레진": 27,
            "잇몸치료": 15,
            "아말감/GI": 10,
            "신경치료": 9,
            "단순발치": 8,
            "크라운": 7,
            "교정 월 체크": 7,
            "사랑니 발치": 6,
            "인레이": 5,
            "임플란트 1차": 3,
            "미백": 2,
            "브릿지": 1,
        },
        "senior": {
            "잇몸치료": 26,
            "단순발치": 18,
            "임플란트 1차": 14,
            "레진": 12,
            "아말감/GI": 10,
            "신경치료": 8,
            "크라운": 8,
            "브릿지": 4,
        },
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
_INSURED_NAMES = (
    "스케일링",
    "구강검진",
    "실란트",
    "아말감/GI",
    "신경치료",
    "단순발치",
    "사랑니 발치",
    "임플란트 1차",
    "잇몸치료",
)
_REASON_ROWS = [
    # (key, category, type, note, 맞는 시술 이름 — None은 급여 시술 전부)
    ("R01", "C", "requirement", "동일 부위 재시행 간격 기준 미충족", ("아말감/GI", "신경치료")),
    ("R02", "C", "requirement", "치석제거 후 후속 치주치료 확인 불가", ("잇몸치료",)),
    ("R03", "C", "requirement", "치주치료 산정 기준 소견 미확인", ("잇몸치료",)),
    ("R04", "B", "requirement", "실란트 대상 치아 기준 적용착오", ("실란트",)),
    ("R05", "AK", "requirement", "발치 난이도 산정 근거 부족으로 하향 인정", ("사랑니 발치",)),
    ("R06", "B", "requirement", "임플란트 급여 대상 부위 기준 적용착오", ("임플란트 1차",)),
    (
        "D01",
        "C",
        "documentation",
        "내역설명 미기재로 인정 근거 없음",
        ("잇몸치료", "신경치료", "사랑니 발치", "임플란트 1차", "실란트"),
    ),
    (
        "D02",
        "M",
        "documentation",
        "방사선 등 증빙자료 미확인",
        ("신경치료", "단순발치", "사랑니 발치", "임플란트 1차"),
    ),
    ("D03", "K", "documentation", "부위·산정 내역 기재 누락", None),
    ("C01", "B", "diagnosis", "상병과 시행 행위 불일치", None),
    ("E01", "C", "external", "연 1회 산정 횟수 초과(타 기관 이력)", ("스케일링",)),
]
_CODE_BY_NAME = {p["name"]: p["code"] for p in PROCEDURES}
ADJUST_REASONS = {
    key: {
        "category": category,
        "type": adjust_type,
        "note": note,
        "procedures": frozenset(_CODE_BY_NAME[n] for n in (names or _INSURED_NAMES)),
    }
    for key, category, adjust_type, note, names in _REASON_ROWS
}


# 차트 상병 (§4.3, 5b단계). 시술처럼 가상 코드 — 뜻은 이름이 갖는다. emr_diagnoses에 적재
DIAGNOSES = {
    "D01": "상아질 우식",
    "D02": "치수염",
    "D03": "만성 근단 치주염",
    "D04": "만성 치은염",
    "D05": "만성 치주염",
    "D06": "치아 침착물(치석)",
    "D07": "매복치",
    "D08": "치아 상실",
    "D09": "치아 위치 이상",
    "D10": "치아 변색",
    "D11": "치과 검사",
    "D12": "우식 예방 처치",
}
# 시술별 허용 상병 — 차트 상병이 이 밖이면 상병 착오(⑤-2 상병 확인 경고). 예방 시술(불소도포·실란트 → D12)은 확신이 낮다
_DX_BY_NAME = {
    "스케일링": ("D06", "D04"),
    "구강검진": ("D11",),
    "불소도포": ("D12",),
    "실란트": ("D12",),
    "레진": ("D01",),
    "아말감/GI": ("D01",),
    "인레이": ("D01",),
    "신경치료": ("D02", "D03"),
    "크라운": ("D01", "D03"),
    "브릿지": ("D08",),
    "단순발치": ("D03", "D05"),
    "사랑니 발치": ("D07",),
    "임플란트 1차": ("D08",),
    "임플란트 2차": ("D08",),
    "교정 상담": ("D09",),
    "교정 월 체크": ("D09",),
    "미백": ("D10",),
    "잇몸치료": ("D05",),
    "상담": ("D11",),
}
PROCEDURE_DIAGNOSES = {_CODE_BY_NAME[n]: frozenset(dx) for n, dx in _DX_BY_NAME.items()}

# 임상 요건 (§4.3, 5b단계) — 요건 사유(ADJUST_REASONS의 requirement 유형)와 1:1. 차트에 그 요건의 근거가 있는지가 ⑤-2·⑤-3의 기준.
# 요건 줄: 이 시술의 창 안 급여 줄은 항상 요건 줄(잇몸치료는 직전 PERIO_LINK_DAYS일 안에 치석제거가 있으면 R02, 아니면 R03)
CHART_REQUIREMENT = {
    _CODE_BY_NAME[n]: key
    for n, key in (
        ("잇몸치료", "R03"),
        ("실란트", "R04"),
        ("사랑니 발치", "R05"),
        ("임플란트 1차", "R06"),
    )
}
PERIO_LINK_DAYS = 90
# 아말감/GI·신경치료는 같은 치아에 같은 시술을 다시 할 때(장부에 이전 진료가 있거나 이 사유로 깎였을 때)만 요건 줄
RETREATMENT_REQUIREMENT = "R01"
# 제출 월·전월 오류 비율 — ⑤-2 평가가 성립하는 평가용 분포(확정 월은 심사 결과에서 정한다). 진료 1건당 오류 최대 1개
REQUIREMENT_ERROR_RATE = 0.25  # 요건 줄 중 임상 요건형 오류
DIAGNOSIS_ERROR_RATE = 0.03  # 요건 오류가 아닌 급여 줄 중 상병 착오
# 디코이 = 특이 케이스 수 × 이 배수만큼 평범한 창 안 줄을 서술(시술별 비례 층화)
CHART_DECOY_RATIO = 1.5
# 추가 처치 누락 (§4.3, 5b단계): 오류가 없는 급여 내원 중 이 비율의 차트에 같은 날 다른 치아의 추가 처치를 서술하고
# 진료 기록·청구에는 넣지 않는다(truth_claim_errors '추가 처치 누락'). 비율은 추정값 — 리포트로 확인
SAME_DAY_EXTRA_RATE = 0.03
# 추가 처치 시술 — 치아 단위 급여 시술, 발치 제외. 그 시술의 급여 조건(실란트는 소아)은 쓰는 쪽이 본다
SAME_DAY_EXTRA = frozenset(_CODE_BY_NAME[n] for n in ("아말감/GI", "실란트"))
# 계획에서 같은 시술이 이어질 때 같은 치아를 유지하는 시술(여러 회차로 한 치아를 치료) — 나머지(실란트·레진·아말감/GI 등)의
# 같은 시술 반복은 다른 치아. 다른 시술로 넘어갈 때(신경치료 → 크라운, 발치 → 임플란트)는 늘 앞 치아 유지 (§2.3)
SAME_TOOTH_PROCEDURES = frozenset(_CODE_BY_NAME[n] for n in ("신경치료", "크라운", "인레이"))
CHART_FORBIDDEN_WORDS = (
    "청구 오류",
    "삭감",
    "조정",
    "요건",
    "급여 기준",
)  # 차트 본문에 정답 어휘 금지 (§4.3 서술 규칙)


def adjust_group(procedure_code: str) -> str:
    """심사 결과 추첨의 시술군 — 요건 사유가 있는 시술이 clinical (REVIEW_OUTCOME_WEIGHTS·ADJUST_TYPE_WEIGHTS의 키)"""
    return (
        "clinical"
        if any(
            r["type"] == "requirement" and procedure_code in r["procedures"]
            for r in ADJUST_REASONS.values()
        )
        else "other"
    )


# 통화 슬롯 (§4.1, 6단계). 목적·노이즈·방향 비중은 §4.1 표가 설계 목표 — 어긋나면 tests/generator/test_doc_sync.py가 실패.
# 목적·목적 안 방향·노이즈는 할당량(최대 잔여 반올림)으로 맞추고, 급함·플래그는 건마다 추첨한다
CALL_PURPOSE_SHARE = {
    "book": 0.18,
    "reschedule": 0.15,
    "confirm": 0.08,
    "cancel": 0.06,
    "recall_q": 0.08,
    "treatment_q": 0.10,
    "insurance_q": 0.06,
    "price_q": 0.08,
    "billing_q": 0.04,
    "document_req": 0.04,
    "emergency": 0.05,
    "complaint": 0.03,
    "other": 0.03,
    "unknown": 0.02,
}
# 예약 이벤트에서 역산하는 목적 — 이벤트 하나에 통화 최대 1건
BOOKING_PURPOSES = ("book", "reschedule", "cancel", "confirm")
# outbound 비중(전체 통화 대비 %p). 목적 안 방향 할당량 = 이 값 / 목적 비중. 나머지 목적은 전부 inbound
CALL_OUTBOUND_SHARE = {
    "billing_q": 0.03,
    "treatment_q": 0.03,
}
OUTBOUND_MULTI_SHARE = (
    0.5  # outbound 중 구조상 복합 주제(noise_type 없음) — 노이즈 '주제 2개 섞임'과 별개
)
# 통화 날짜 최근 가중 — 창 시작 1에서 TODAY 이 값까지 직선. 녹음은 최근 것일수록 많이 남는다(§4.1 6단계)
CALL_RECENCY_WEIGHT = 3


def call_day_weight(d: date, start: date, today: date) -> float:
    """통화 날짜 d의 추첨 가중치 — start에서 1, today에서 CALL_RECENCY_WEIGHT"""
    return 1 + (CALL_RECENCY_WEIGHT - 1) * (d - start).days / (today - start).days


CALL_NOISE_SHARE = {
    "two_topics": 0.10,
    "interrupted": 0.08,
    "ambiguous_pain": 0.04,
    "guardian_confused": 0.03,
}
# 상대 enum. 비율은 정하지 않는다 — 예약류는 이벤트 환자, 그 밖은 CALL_COUNTERPART_WEIGHTS에서 나온다(리포트로만)
CALL_COUNTERPARTS = ("patient", "guardian", "new_caller", "insurer", "vendor", "other")
URGENCIES = ("urgent", "normal", "low")
# 목적별 상대 가중(%) — 예약류 아닌 목적. side = 환자 측(통화일에 소아면 guardian, 아니면 patient). outbound는 항상 side
CALL_COUNTERPART_WEIGHTS = {
    "recall_q": {"side": 100},
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
CALL_FLAGS = (
    "emergency",
    "complaint",
    "price_inquiry",
    "wants_appt",
    "appt_confirmed",
    "has_todo",
)
IMPLIED_FLAG = {
    "emergency": "emergency",
    "complaint": "complaint",
    "price_q": "price_inquiry",
    "book": "wants_appt",
}
# 부 주제 플래그 → accepted_purposes에 들어가는 목적
SUBTOPIC_PURPOSE = {
    "price_inquiry": "price_q",
    "complaint": "complaint",
    "emergency": "emergency",
    "wants_appt": "book",
}
# 목적별로 가능한 부 주제 플래그(주 목적이 함의하는 것 제외). 빈 목적은 복합 주제를 만들지 않는다
CALL_SUBTOPICS = {
    "book": ("price_inquiry",),
    "reschedule": ("price_inquiry", "complaint"),
    "cancel": ("complaint",),
    "confirm": ("price_inquiry",),
    "recall_q": ("wants_appt", "price_inquiry"),
    "treatment_q": ("price_inquiry", "wants_appt", "emergency"),
    "insurance_q": ("price_inquiry", "wants_appt"),
    "price_q": ("wants_appt",),
    "billing_q": ("complaint",),
    "document_req": ("wants_appt",),
    "emergency": ("wants_appt",),
    "complaint": ("wants_appt",),
    "other": (),
    "unknown": (),
}
CALL_PAIN_PURPOSES = ("book", "treatment_q")  # 노이즈 '급함 애매한 통증' 대상
PAIN_URGENT_SHARE = 0.5  # 그중 urgent (나머지 normal)
CALL_URGENCY_WEIGHTS = {"normal": 85, "low": 15}  # emergency 플래그·애매한 통증이 아닌 통화
APPT_CONFIRMED_SHARE = 0.7  # book·reschedule 중 appt_confirmed
HAS_TODO_SHARE = {"confirm": 0.3, "recall_q": 0.3}  # 나머지 목적은 HAS_TODO_DEFAULT
HAS_TODO_DEFAULT = 0.8
CALL_DURATION_SEC = (60, 180)  # 양 끝 포함 — 전사 규칙 분량(§4.1 절차 5)
# 통화 전사 (§4.1 '7단계 구현 결정'). 발화 글자 수(화자 표시·공백 제외) ÷ 이 값 = 통화 길이(초)
TRANSCRIPT_CHARS_PER_SEC = 3.5  # 말 사이 틈·조회 대기를 포함한 통화 평균
# 라벨 어휘 — 전사에 그대로 나오면 검증 실패(§4.1 절차 6(b)). 목적 이름(§4.1 목적 분포 표)과 그 조각
CALL_LABEL_WORDS = (
    "예약 잡기",
    "예약 변경",
    "예약 확인",
    "예약 취소",
    "리콜",
    "치료 문의",
    "보험 문의",
    "가격 문의",
    "비용 문의",
    "수납 문의",
    "청구 문의",
    "서류 요청",
    "기록 요청",
    "응급",
    "불만",
)
# 7단계 응급·불만 통화의 상황 — 통화 번호로 하나씩 정해 입력에 넣는다(없으면 모델이 거의 모두 '어젯밤부터 오른쪽 아래
# 어금니'·'한 시간 기다렸다'로 쓴다). 응급은 상황 × 시작 시점
EMERGENCY_SCENES = (
    "밤사이 갑자기 심해진 위 어금니 치통",
    "씹다가 깨진 아래 작은어금니",
    "넘어지면서 부딪혀 흔들리는 앞니",
    "때운 것이 빠지고 심하게 시린 어금니",
    "잇몸이 붓고 고름이 나오는 어금니 쪽",
    "한쪽 볼이 부어 오른 아래 어금니 쪽",
    "씌운 것이 빠지고 아픈 앞니 쪽",
    "운동하다 부딪혀 깨진 앞니",
)
# 응급 내원 (§2.6 (5), 6b단계) — 환자 측 응급 통화(emergency 플래그)마다 원장 내원 1건: 통화 뒤 EMERGENCY_LEAD_MIN분이
# 지난 첫 빈 슬롯(그날 자리가 없으면 다음 진료일 개원부터). 상황마다 그날 처치와 치아 후보 — 맞는 시술이 없는 상황(외상으로
# 흔들림·보철 탈락)과 치아 단위 시술을 못 하는 나이는 상담(진찰). 치아는 장부·치료계획에 없는 치아에서 통화 번호로 고른다
_UPPER_MOLARS = ("16", "17", "26", "27")
_LOWER_MOLARS = ("36", "37", "46", "47")
EMERGENCY_TREATMENT = dict(
    zip(
        EMERGENCY_SCENES,
        (
            ("신경치료", _UPPER_MOLARS),
            ("레진", ("34", "35", "44", "45")),
            ("상담", ()),
            ("아말감/GI", _UPPER_MOLARS + _LOWER_MOLARS),
            ("신경치료", _UPPER_MOLARS + _LOWER_MOLARS),
            ("신경치료", _LOWER_MOLARS),
            ("상담", ()),
            ("레진", ("11", "12", "21", "22")),
        ),
        strict=True,
    )
)
# 그날 먼저 진료를 받고 간 뒤 건 응급 통화는 이 상황(그 치료와 이어진 통증)으로 — 해시로 고른 상황이면 1시간 전 내원과 모순
EMERGENCY_AFTER_VISIT_SCENE = "오늘 치료받은 이가 마취가 풀리면서 심하게 아픈"
EMERGENCY_AFTER_VISIT_ONSET = "진료받고 돌아간 뒤부터"
EMERGENCY_TREATMENT[EMERGENCY_AFTER_VISIT_SCENE] = ("상담", ())
# 소아(AGE_BANDS child)에게 맞지 않는 상황 → 대신 쓰는 상황(씌운 치아가 빠짐 → 넘어져 흔들리는 앞니)
EMERGENCY_CHILD_SCENES = {EMERGENCY_SCENES[6]: EMERGENCY_SCENES[2]}
# 이력이 있어야 하는 상황 → (통화 전 그 시술군 진료가 있어야 함, 없으면 대신 쓰는 상황) — 때운 적 없는데 '때운 게 빠짐'을 지어내지 않게
EMERGENCY_HISTORY_SCENES = {
    EMERGENCY_SCENES[3]: (
        ("restorative", "prosth"),
        EMERGENCY_SCENES[1],
    ),  # 때운 것 → 씹다 깨진 이
    EMERGENCY_SCENES[6]: (("prosth",), EMERGENCY_SCENES[2]),  # 씌운 것 → 넘어져 흔들리는 앞니
}
EMERGENCY_LEAD_MIN = 30
# 응급 내원 표시 — 통화 사실의 최근 진료·다음 예약에 붙인다. 뜻을 담은 말이라 모델이 응급 진찰을 일반 상담으로 이어 쓰지 않는다
EMERGENCY_VISIT_MARK = "응급 내원 — 일반 상담·진료로 말하지 않는다"
# 갑자기 빠진 상황 — 시작 시점 '점점'과 맞지 않는다(덜 빠진 것처럼 써져 차트의 '탈락'과 어긋남)
EMERGENCY_SUDDEN_SCENES = (EMERGENCY_SCENES[3], EMERGENCY_SCENES[6])
EMERGENCY_GRADUAL_ONSETS = ("이틀 전부터 점점",)
# 상황별 상병 — 시술 허용 상병(PROCEDURE_DIAGNOSES) 안에서 그 상황에 맞는 것. 없으면 허용 상병의 첫 번째
EMERGENCY_DX = {
    EMERGENCY_SCENES[0]: "D02",  # 밤사이 치통 — 치수염
    EMERGENCY_SCENES[4]: "D03",  # 잇몸 고름 — 근단 치주염
    EMERGENCY_SCENES[5]: "D03",  # 볼 부음
}
# 때운 것이 빠진 상황 — 그 환자가 전에 다른 수복 시술로 때운 치아에서 고른다(녹취록이 그 기록을 말해도 맞게). 없으면 위 규칙대로
EMERGENCY_RESTORED_SCENES = (EMERGENCY_SCENES[3],)
EMERGENCY_ONSETS = ("오늘 아침부터", "어제 점심 무렵부터", "방금 전부터", "이틀 전부터 점점")
COMPLAINT_TOPICS = (
    "예약 시간보다 오래 기다림",
    "치료 비용이 미리 들은 것과 다름",
    "치료 전에 설명이 부족했음",
    "치료받은 곳이 다시 불편함",
    "접수·전화 응대가 불친절했음",
    "예약이 잘못 잡혀 헛걸음함",
    "치료 중 아프다고 했는데 그대로 진행함",
    "안내 문자를 못 받아 예약을 놓침",
)
# 보험사 등 제3자 통화의 병원 규칙 — 전화로 환자 진료 내용을 알려 주지 않는다(의료법 — 환자 동의 없는 기록 제공 금지)
THIRD_PARTY_RULE = (
    "보험사 등 제3자에게는 전화로 환자의 진료 날짜·내용을 알려 주지 않는다. "
    "서류는 환자 본인의 요청이나 동의서가 있어야 보낸다"
)
# 환자 측(예약류 아님) 환자 조건 — (목적, 방향) → (규칙, 값). 없으면 'visited'(통화일 전 completed 내원이 있음)
# recall_text: 통화일 값[0]~값[1]일 전(그 시각 전)에 리콜 문자를 받았고 그 시점 잡힌 예약 없음 / balance: 그 시점 잔액 > 0 /
# recent_treatment: 통화일 값[0]~값[1]일 전에 진료(treatment) 내원 / recent_visit: 통화일 값일 안에 내원
CALL_PATIENT_RULES = {
    ("recall_q", "inbound"): ("recall_text", (0, 7)),
    ("billing_q", "outbound"): ("balance", None),
    ("treatment_q", "outbound"): ("recent_treatment", (1, 7)),
    ("complaint", "inbound"): ("recent_visit", 30),
}
# 판정 풀 층화 (평가 설계 §2.1): 희귀 카테고리는 각 전사의 이 비율, 나머지는 비례 + split당 최소 건수
EVAL_RARE_PURPOSES = ("complaint", "emergency", "unknown")
EVAL_RARE_SHARE = 0.6
EVAL_MIN_PER_SPLIT = 2


# 문자 (§2.5, 8단계). 리마인드는 리마인드 날, 리콜은 그달 첫 진료일 개원 시각(HOURS["open"])에 일괄
MESSAGE_CONFIRM_DELAY_MIN = (1, 5)  # 예약 확정 문자 = 예약을 잡은 뒤 (양 끝 포함, 분)
MESSAGE_DELAY_MIN = (1, 30)  # 노쇼 문자(진료가 끝났을 시각 뒤)·답장(out 뒤)
# 트리거별 본문. date = '11월 3일(화)', 리마인드도 '내일'이라 쓰지 않는다(직전 진료일이 전날이 아닐 수 있음)
MESSAGE_TEMPLATES = {
    "appt_confirm": "[{clinic}] {name}님, {date} {time} {procedure} 예약이 확정되었습니다.",
    "reminder": "[{clinic}] {name}님, {date} {time} {procedure} 예약 안내드립니다.",
    "noshow": "[{clinic}] {name}님, 오늘 {time} 예약에 내원하지 않으셔서 연락드립니다. 다시 예약하시려면 연락 주세요.",
    "balance": "[{clinic}] {name}님, {date} 진료비 중 {amount}원이 미납되어 안내드립니다.",
    "doc_ready": "[{clinic}] {name}님, 요청하신 서류가 준비되었습니다. 내원하실 때 받아 가세요.",
}
# 리콜 문자 (§2.5) — 기한(마지막 completed 위생 내원 + RECALL_CYCLE)이 다음 달인 환자에게 한 번(before, month = 기한 달),
# 기한이 지난 다음 달에 한 번 더(after). 둘 다 그달 첫 진료일 개원 시각, 잡힌 예약이 없을 때만
RECALL_TEMPLATES = {
    "before": "[{clinic}] {name}님, {month}월에 정기 검진 시기가 다가와 안내드립니다. 예약은 전화로 연락 주세요.",
    "after": "[{clinic}] {name}님, 정기 검진 시기가 지났습니다. 편하실 때 전화로 예약해 주세요.",
}
# 예약 확정·리마인드 답장 — 확인 답장은 리마인드면 confirmed 예약에만, 시간 변경은 그 뒤 실제로 바뀐 예약에만(§2.5)
MESSAGE_REPLY_OK = ("네", "확인했습니다")
MESSAGE_REPLY_CHANGE = "시간 변경 가능할까요?"
# 예약 취소 답장 (§2.5, 8a단계) — 통화가 없는 시각의 취소(개원 전 당일 취소·장면)는 환자가 그 예약의 문자에 답해서 들어온다
MESSAGE_REPLY_CANCEL = "죄송한데 예약 취소해 주세요. 다시 연락드릴게요"
# 8a단계 장면 — TODAY 전 달 노쇼 중 이만큼을 개원 전 문자 당일 취소로 바꾼다(최근 달에 그 모양이 보이게)
NOSHOW_TO_CANCEL = 3
# 트리거별 답장 문구 (균등 — 예약 확정·리마인드는 뽑은 뒤 예약 상태에 맞춘다)
MESSAGE_REPLIES = {
    "appt_confirm": (*MESSAGE_REPLY_OK, MESSAGE_REPLY_CHANGE),
    "reminder": (*MESSAGE_REPLY_OK, MESSAGE_REPLY_CHANGE),
    "noshow": ("죄송합니다. 다시 연락드릴게요", "깜빡했어요"),
    "balance": ("다음에 갈 때 낼게요", "확인했습니다"),
    "doc_ready": ("감사합니다", "네, 들를게요"),
    "recall": ("확인했습니다", "다음에 전화드릴게요", "요즘 바빠서요"),
}
# 이미 냈다는 balance 답장 (§2.5 reply(in)) — 장부 실수(입력 누락·금액 오입력, §2.4)로 생긴 미수의 답장(균등)
MESSAGE_REPLIES_PAID = ("그날 다 냈는데요", "이미 계산했어요")
# 결제수단을 말하는 이미 냈다는 답장 — 그 미수 내원의 실제 결제수단(장부 실수면 정답의 실제 값) 문구만 후보에 더한다
MESSAGE_REPLIES_PAID_METHOD = {
    "card": "카드로 결제했는데 확인 부탁드려요",
    "cash": "현금으로 냈는데 확인 부탁드려요",
    "transfer": "이체했는데 확인 부탁드려요",
}
# 수납 문의 발신 중 장부 실수 미수(§2.4) 환자에게 거는 통화 수(그 미수가 장부에 들어간 뒤 LEDGER_DEBT_CALL_DAYS일 안) — 전사에 이미 냈다는 말(already_paid, ⑧-1 근거)이 있게.
# 균등 선택이면 장부 실수 환자가 적어 거의 안 걸린다
LEDGER_DEBT_CALLS = 3
LEDGER_DEBT_CALL_DAYS = 30
# 진짜 미수의 balance 답장 중 MESSAGE_REPLIES_PAID에서 뽑는 비율(착각한 환자). 추정값 — 리포트로 확인
PAID_REPLY_NOISE_RATE = 0.10


# 재무 원본 fin_* (§2.7, 8b·8c단계 — ⑧). 카드사·거래처 이름은 전부 가상
# 연동 실패일 수 — 그날은 카드 수납도 손으로 입력해 승인번호가 없다(§2.4). 장부 실수·불일치는 평가 달(recon_months)에만
LINK_FAILURE_DAYS = 3
# 장부 실수 종류(truth_ledger_errors.kind) — 건수는 RECON_MISMATCH_COUNTS의 같은 이름. 금액 오입력은 0 하나 빠뜨림이라
# 이 금액 이상 수납에서만 (§2.4)
LEDGER_ERROR_KINDS = ("entry_missing", "amount_typo", "date_shift", "method_wrong")
AMOUNT_TYPO_MIN = 10_000
# ⑧-1·⑧-2 평가 달 = TODAY가 속한 달 직전 RECON_MONTH_COUNT개월(recon_months). 마지막 RECON_TEST_COUNT달이
# test(가장 최근 기간으로 시험), 나머지 dev
RECON_MONTH_COUNT = 4
RECON_TEST_COUNT = 2


def recall_due_year(sent: date, kind: str, due_month: int) -> int:
    """리콜 문자의 기한 해 — '다가옴'(before)은 다음 달, '지남'(after)은 지난달이 기한이라 해를 넘을 수 있다(§2.5)"""
    if kind == "before" and due_month < sent.month:
        return sent.year + 1
    if kind == "after" and due_month > sent.month:
        return sent.year - 1
    return sent.year


def recon_months(today: date) -> tuple[str, ...]:
    """평가 달('YYYY-MM', claim_month와 같은 꼴) — TODAY가 속한 달 직전 RECON_MONTH_COUNT개월, 오래된 달부터"""
    base = today.year * 12 + today.month - 1
    return tuple(
        f"{(base - k) // 12}-{(base - k) % 12 + 1:02d}" for k in range(RECON_MONTH_COUNT, 0, -1)
    )


def recon_test_months(today: date) -> tuple[str, ...]:
    """평가 달 중 test — 마지막 RECON_TEST_COUNT달"""
    return recon_months(today)[-RECON_TEST_COUNT:]


# 카드사 (§2.7 fin_card_deposits): 이름 → 카드 수납 중 비중(%)·수수료율·정산 지연(매출일 뒤 영업일). 가상 값 — 비중·율은 추정값
CARD_ISSUERS = {
    "가온카드": {"weight": 35, "fee_rate": 0.0120, "settle_days": 2},
    "다솜카드": {"weight": 30, "fee_rate": 0.0135, "settle_days": 2},
    "라온카드": {"weight": 20, "fee_rate": 0.0110, "settle_days": 3},
    "미르카드": {"weight": 15, "fee_rate": 0.0145, "settle_days": 3},
}
# 현금 입금 요일(월=0) — 그 사이 현금 합계를 한 번에 입금(주 단위 합산 입금 디코이, §2.7). 추정값
CASH_DEPOSIT_WEEKDAYS = (0,)
# 통장 입금의 시각·적요 (§2.7) — 카드 정산 입금 적요는 카드사 이름, 환자 이체는 입금자 이름. 가상 값
BANK_TIMES = {
    "card": "08:00",
    "cash": "13:10",
    "nhis": "10:00",
    "transfer_out": "17:00",
    "auto_debit": "06:00",
}
CASH_DEPOSIT_MEMO = "현금 입금"
NHIS_MEMO = "국민건강보험공단"


def bank_day(d: date, holidays) -> bool:
    """은행 영업일 — 평일·공휴일 아님(치과 휴진 요일과 무관)"""
    return d.weekday() < 5 and d not in holidays


def add_bank_days(d: date, n: int, holidays) -> date:
    """d 뒤 n번째 은행 영업일 (카드 정산 입금일 — §2.7)"""
    while n:
        d += timedelta(days=1)
        n -= bank_day(d, holidays)
    return d


def cash_deposit_day(d: date, holidays) -> date:
    """d에 받은 현금이 들어가는 통장 입금일 — d 뒤 첫 CASH_DEPOSIT_WEEKDAYS 요일(은행 영업일이 아니면 다음 영업일).
    입금은 그날 아침이라 입금일 당일 현금은 다음 입금으로 간다 (§2.7)"""
    x = d + timedelta(days=1)
    while x.weekday() not in CASH_DEPOSIT_WEEKDAYS:
        x += timedelta(days=1)
    while not bank_day(x, holidays):
        x += timedelta(days=1)
    return x


# 소아 환자 이체 중 입금자명이 보호자 이름인 비율(디코이, §2.7). 추정값 — 리포트로 확인
GUARDIAN_PAYER_SHARE = 0.8
# 정산 지연의 최대 — 장부 실수·불일치는 정산·현금 입금이 TODAY 전에 오는 날에만 (§2.7 TODAY 경계)
MAX_SETTLE_DAYS = max(i["settle_days"] for i in CARD_ISSUERS.values())
# 입금 지연(deposit_delay) 불일치의 늦어지는 은행 영업일 (양 끝 포함). 추정값
DEPOSIT_DELAY_DAYS = (1, 2)
# 수수료 차이(fee_mismatch) 불일치 — 그 정산의 수수료율이 카드사 율보다 이만큼 높다. 추정값
FEE_MISMATCH_RATE_DIFF = 0.003
# 분할결제·취소 후 재승인은 이 금액 이상 카드 수납에서 (분할한 두 승인이 모두 의미 있는 금액이게)
SPLIT_MIN_AMOUNT = 20_000
# ⑧-1 불일치 원인별 건수 (§2.7 불일치 심기, 평가 달 전체 합 65 — 손으로 입력한 수납의 장부 실수가 10% 안쪽이게). 원인 enum = 평가 설계 §5 —
# 통장·카드 쪽은 8b가 심고, 장부 쪽은 5단계 장부 실수(§2.4)를 원인으로 연결
RECON_MISMATCH_COUNTS = {
    "split_payment": 6,
    "cancel_reapprove": 6,
    "fee_mismatch": 6,
    "deposit_delay": 6,
    "non_clinical_sale": 8,
    "entry_missing": 12,
    "amount_typo": 7,
    "date_shift": 8,
    "method_wrong": 6,
}
# 데스크 구강용품 가격표 — 진료 외 매출(non_clinical_sale)은 이 금액(여러 개면 합)의 장부 없는 카드 승인.
# 직원 매뉴얼 '구강용품 판매 안내'(doc_catalog)가 같은 값을 쓴다. 가상 값
ORAL_CARE_PRICES = {
    "칫솔": 3_000,
    "치간칫솔": 4_500,
    "치실": 5_000,
    "구강 세정제": 7_000,
    "시린이 치약": 8_500,
}

# ⑧-2 계정 과목 (§2.7) — 출금 1건마다 하나
ACCOUNTS = (
    "재료비",
    "외주가공비",
    "소모품비",
    "복리후생비",
    "지급임차료",
    "수도광열비",
    "통신비",
    "지급수수료",
    "수선비",
)
# ⑧-2 거래처 (§2.7). kind는 EXPENSE_ITEMS의 키. pay: month_end_transfer(월말 이체 = 그달 증빙 합계, 1:N) /
# card(병원 카드) / auto_debit(자동이체). per_month = 달마다 증빙(카드는 사용) 건수 범위(양 끝 포함),
# amount = 건당 금액 범위(원, 기공소는 None — 창 안 기공 진료에서 파생). card_name = 카드 내역 가맹점명(결제대행사 표기 차이).
# 규모 목표는 창 전체 출금 ~305·증빙 ~615 — 건수·금액 범위는 추정값, 리포트로 확인
_VENDOR_ROWS = [
    # (kind, 가상 이름, pay, per_month, amount, 증빙 종류, 계정 과목, card_name)
    (
        "material",
        "바른덴탈상사",
        "month_end_transfer",
        (10, 14),
        (50_000, 400_000),
        ("statement", "tax_invoice"),
        "재료비",
        None,
    ),
    (
        "material",
        "하늘치재",
        "month_end_transfer",
        (10, 14),
        (30_000, 300_000),
        ("statement", "tax_invoice"),
        "재료비",
        None,
    ),
    (
        "lab",
        "온새미치과기공소",
        "month_end_transfer",
        (4, 5),
        None,
        ("statement",),
        "외주가공비",
        None,
    ),
    (
        "shopping",
        "모아덴탈몰",
        "card",
        (4, 8),
        (20_000, 200_000),
        ("order_email",),
        "소모품비",
        "페이온*모아덴탈몰",
    ),
    ("meal", "솔밭식당", "card", (6, 8), (30_000, 120_000), ("receipt",), "복리후생비", None),
    ("meal", "달빛베이커리", "card", (4, 6), (10_000, 60_000), ("receipt",), "복리후생비", None),
    (
        "rent",
        "한결빌딩관리",
        "auto_debit",
        (1, 1),
        (3_300_000, 3_300_000),
        ("tax_invoice",),
        "지급임차료",
        None,
    ),
    (
        "utility",
        "전기요금",
        "auto_debit",
        (1, 1),
        (250_000, 450_000),
        ("bill",),
        "수도광열비",
        None,
    ),
    ("utility", "수도요금", "auto_debit", (1, 1), (30_000, 60_000), ("bill",), "수도광열비", None),
    (
        "utility",
        "도시가스",
        "auto_debit",
        (1, 1),
        (20_000, 120_000),
        ("bill",),
        "수도광열비",
        None,
    ),
    (
        "telecom",
        "이음통신",
        "auto_debit",
        (1, 1),
        (60_000, 60_000),
        ("bill",),
        "통신비",
        None,
    ),
    (
        "telecom",
        "새빛전화",
        "auto_debit",
        (1, 1),
        (40_000, 50_000),
        ("bill",),
        "통신비",
        None,
    ),
    (
        "service",
        "정도세무회계",
        "auto_debit",
        (1, 1),
        (330_000, 330_000),
        ("tax_invoice",),
        "지급수수료",
        None,
    ),
    (
        "service",
        "덴트차트",
        "auto_debit",
        (1, 1),
        (110_000, 110_000),
        ("bill",),
        "지급수수료",
        None,
    ),
    (
        "service",
        "청정의료환경",
        "auto_debit",
        (1, 1),
        (88_000, 88_000),
        ("tax_invoice",),
        "지급수수료",
        None,
    ),
    (
        "repair",
        "든든장비서비스",
        "card",
        (0, 1),
        (80_000, 600_000),
        ("statement",),
        "수선비",
        None,
    ),
]
VENDORS = [
    dict(
        zip(
            ("kind", "name", "pay", "per_month", "amount", "docs", "account", "card_name"),
            row,
            strict=True,
        )
    )
    for row in _VENDOR_ROWS
]
# ⑧-2 증빙 품목 (§2.7 fin_expense_documents 템플릿) — 거래처 종류 → (품목, 계정 과목). 대부분 거래처의 계정 과목이고,
# 다른 과목 품목(쇼핑몰의 간식·커피 → 복리후생비, 재료상의 소모품)은 품목을 읽어야 정해지는 계정 과목. 가상 값
EXPENSE_ITEMS = {
    "material": (
        ("레진 시린지", "재료비"),
        ("본딩제", "재료비"),
        ("인상재", "재료비"),
        ("마취 카트리지", "재료비"),
        ("GI 시멘트", "재료비"),
        ("근관 파일", "재료비"),
        ("니트릴 장갑", "소모품비"),
        ("석션 팁", "소모품비"),
    ),
    "lab": (("인레이", "외주가공비"), ("크라운", "외주가공비"), ("브릿지", "외주가공비")),
    "shopping": (
        ("종이컵", "소모품비"),
        ("핸드타월", "소모품비"),
        ("복사 용지", "소모품비"),
        ("프린터 토너", "소모품비"),
        ("환자용 칫솔", "소모품비"),
        ("커피 원두", "복리후생비"),
        ("직원 간식", "복리후생비"),
    ),
    "meal": (("점심 식대", "복리후생비"), ("간식", "복리후생비"), ("음료", "복리후생비")),
    "rent": (("월 임대료", "지급임차료"), ("관리비", "지급임차료")),
    "utility": (
        ("전기 사용료", "수도광열비"),
        ("수도 사용료", "수도광열비"),
        ("가스 사용료", "수도광열비"),
    ),
    "telecom": (("인터넷 요금", "통신비"), ("전화 요금", "통신비")),
    "service": (
        ("세무 기장료", "지급수수료"),
        ("전자차트 사용료", "지급수수료"),
        ("의료폐기물 수거", "지급수수료"),
    ),
    "repair": (
        ("유닛체어 수리", "수선비"),
        ("석션 모터 교체", "수선비"),
        ("핸드피스 수리", "수선비"),
    ),
}
# 증빙이 있는 출금 중 출금액이 증빙 합계보다 조금 많은 비율(배송비·이전 미지급분 등), 그 차이(증빙 합계 대비, 100원 단위
# 내림 — 최소 100원) (§2.7 어긋남)
EXPENSE_AMOUNT_DIFF_RATE = 0.05
EXPENSE_AMOUNT_DIFF_RANGE = (0.01, 0.05)
# 병원 카드 사용 중 증빙이 없는 비율 — 영수증을 잃기 쉬운 카드 사용에만(이체·자동이체는 증빙이 온다). 전체 출금의 약 5%
EXPENSE_NO_DOC_RATE = 0.08
# 기공소 외주 단가(원) — 기공 명세서 줄 = 창 안 인레이·크라운·브릿지 진료 1건 (§2.7). 가상 값
# 자동이체 고지서·세금계산서는 출금 며칠 전에 온다 (양 끝 포함, §2.7)
EXPENSE_DOC_LEAD_DAYS = (3, 10)
LAB_FEES = {"인레이": 80_000, "크라운": 150_000, "브릿지": 400_000}
