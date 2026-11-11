"""5b단계 차트 사실 — emr_chart_notes·truth_chart_notes·truth_claim_errors (가상 데이터 생성 설계 §4.3·§2.4).
사실은 코드가 정하고 서술은 9b단계(LLM)가 한다. 이 단계는 서술 줄에도 사실로 만든 초안 본문을 넣어 두고 9b가 덮어쓴다
(9b 전에도 진료 1건당 차트 1행이 성립하게, 가짜 게이트웨이(테스트)면 초안이 그대로 남는다).
두 단계로 나눠 난수를 분리한다 — 디코이·본문 모양을 바꿔도 오류·상병·요건 사실은 그대로:
- chart_facts: 요건 줄(master.CHART_REQUIREMENT·재치료 R01), 청구 오류(확정 월은 심사 결과에서, 제출 월·전월은 비율로),
  상병(허용 상병, 상병 착오면 다른 시술의 상병), 사실(C.C·소견·요건 근거·주의사항 처리·다음 계획)
- chart_notes: 디코이(특이 × CHART_DECOY_RATIO, 시술별 비례 층화), 본문(템플릿 / 서술 초안)
9b단계 chart_narratives: 서술 줄의 초안을 LLM 서술로 덮어쓴다(§4.3 '9b단계 구현 결정' — 채우기는 llm_jobs).
창 이전 내원은 템플릿만(특이·디코이 대상 아님).
"""

import hashlib
import json
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

import numpy as np

from generator import llm_jobs
from generator.llm_jobs import Task
from generator.master import (
    ADJUST_REASONS,
    ALERT_TYPES,
    CHART_DECOY_RATIO,
    CHART_FORBIDDEN_WORDS,
    CHART_REQUIREMENT,
    CHECKUP_CODE,
    DIAGNOSES,
    DIAGNOSIS_ERROR_RATE,
    EMERGENCY_AFTER_VISIT_SCENE,
    EMERGENCY_SCENES,
    EVIDENCE_NEGATED_SHARE,
    EVIDENCE_PARAPHRASE_SHARE,
    EXTRACTION_CODES,
    FULL_MOUTH,
    PERIO_LINK_DAYS,
    PERIO_NEAR_MISS_SHARE,
    PERIO_POCKET_MIN_MM,
    PERIO_QUALITATIVE_SHARE,
    PROCEDURE_DIAGNOSES,
    PROCEDURES,
    RECALL_CYCLE_MONTHS,
    REQUIREMENT_CUT_ERROR_SHARE,
    REQUIREMENT_ERROR_RATE,
    RETREATMENT_REQUIREMENT,
    SAME_DAY_EXTRA,
    SAME_DAY_EXTRA_ADD_RATE,
    SAME_DAY_EXTRA_RATE,
    SCALING_CODE,
    SUBMITTED_MONTHS,
    TOOTH_CHOICES,
    age_on,
    insured_on,
)
from generator.records import open_plan_items

NAME = {p["code"]: p["name"] for p in PROCEDURES}
PROCEDURE_BY_CODE = {p["code"]: p for p in PROCEDURES}
DURATION = {p["code"]: p["duration_min"] for p in PROCEDURES}
CODE = {p["name"]: p["code"] for p in PROCEDURES}
REASON_KEY = {(r["category"], r["note"]): key for key, r in ADJUST_REASONS.items()}
PERIO_CODE = CODE["잇몸치료"]
HYGIENE_CODES = {SCALING_CODE, CHECKUP_CODE, CODE["불소도포"]}

# C.C — 시술별 환자 말 (§4.3 사실 (3))
_FILLING = ("찬 거 먹으면 이가 시려요", "이에 까만 점이 생겼어요", "음식이 자꾸 껴요")
CC = {
    CODE[n]: cc
    for n, cc in {
        "스케일링": (
            "잇몸에서 피가 나요",
            "치석이 많이 낀 것 같아요",
            "정기 검진 겸 스케일링 받으러 왔어요",
        ),
        "구강검진": ("정기 검진 받으러 왔어요", "특별히 불편한 곳은 없어요"),
        "불소도포": ("아이 충치 예방하려고 왔어요",),
        "실란트": ("어금니 홈 메우려고 왔어요", "아이 어금니 충치를 예방하고 싶어요"),
        "레진": _FILLING,
        "아말감/GI": _FILLING,
        "인레이": _FILLING,
        "신경치료": ("밤에 이가 욱신거려요", "씹을 때 아파요", "뜨거운 걸 먹으면 아파요"),
        "크라운": ("신경치료한 이를 씌우러 왔어요", "이가 깨졌어요"),
        "브릿지": ("빠진 자리를 채우고 싶어요",),
        "단순발치": ("이가 흔들려요", "부러진 이를 빼야 한대요"),
        "사랑니 발치": ("사랑니 쪽이 붓고 아파요", "사랑니 빼러 왔어요"),
        "임플란트 1차": ("빠진 자리에 임플란트를 하고 싶어요",),
        "임플란트 2차": ("임플란트 마무리하러 왔어요",),
        "교정 상담": ("치아 배열이 신경 쓰여요",),
        "교정 월 체크": ("교정 장치 점검하러 왔어요",),
        "미백": ("이가 누렇게 보여요",),
        "잇몸치료": ("잇몸이 붓고 피가 나요", "잇몸이 내려간 것 같아요"),
        "상담": ("치료 계획을 상담받고 싶어요",),
    }.items()
}

# C.C를 차트식으로 줄인 말 — 초안의 반쯤은 환자 말을 따옴표 대신 이 말로 쓴다(실제 차트는 줄여 쓰는 일이 많다).
# '(으)로 내원'·'호소' 앞에 붙어도 자연스럽게, 근거 말(EVIDENCE_WORDS)은 넣지 않는다
CC_SHORT = {
    "잇몸에서 피가 나요": "잇몸 출혈",
    "치석이 많이 낀 것 같아요": "치석 제거 희망",
    "정기 검진 겸 스케일링 받으러 왔어요": "정기 검진·스케일링 희망",
    "정기 검진 받으러 왔어요": "정기 검진 희망",
    "특별히 불편한 곳은 없어요": "특별한 불편 없이 검진 희망",
    "아이 충치 예방하려고 왔어요": "소아 충치 예방 희망",
    "어금니 홈 메우려고 왔어요": "어금니 홈 메우기 희망",
    "아이 어금니 충치를 예방하고 싶어요": "소아 어금니 충치 예방 희망",
    "찬 거 먹으면 이가 시려요": "냉수 시 시림",
    "이에 까만 점이 생겼어요": "치아 까만 점 발견",
    "음식이 자꾸 껴요": "음식물 낌",
    "밤에 이가 욱신거려요": "야간 욱신거림",
    "씹을 때 아파요": "저작 시 통증",
    "뜨거운 걸 먹으면 아파요": "온열 자극 시 통증",
    "신경치료한 이를 씌우러 왔어요": "신경치료 치아 보철 희망",
    "이가 깨졌어요": "치아 깨짐",
    "빠진 자리를 채우고 싶어요": "결손부 수복 희망",
    "이가 흔들려요": "치아 흔들림",
    "부러진 이를 빼야 한대요": "부러진 치아 발치 권유",
    "사랑니 쪽이 붓고 아파요": "사랑니 부위 부종·통증",
    "사랑니 빼러 왔어요": "사랑니 발치 희망",
    "빠진 자리에 임플란트를 하고 싶어요": "결손부 임플란트 희망",
    "임플란트 마무리하러 왔어요": "임플란트 마무리 희망",
    "치아 배열이 신경 쓰여요": "치열 고민",
    "교정 장치 점검하러 왔어요": "교정 장치 점검",
    "이가 누렇게 보여요": "치아 변색 고민",
    "잇몸이 붓고 피가 나요": "잇몸 부종·출혈",
    "잇몸이 내려간 것 같아요": "잇몸 퇴축 느낌",
    "치료 계획을 상담받고 싶어요": "치료 계획 상담 희망",
    "예전에 치료한 이가 신경 쓰여요": "이전 치료 치아 불편감",
    "잇몸 상태 확인하러 왔어요": "잇몸 상태 확인 희망",
    "아이 치아 관리 받으러 왔어요": "소아 치아 관리 희망",
    "임플란트 치료 받으러 왔어요": "임플란트 치료 희망",
}

# 증상을 말하는 C.C — 초안이 '"…" 호소'로 쓸 수 있는 말(검진·예방·상담 희망, '~것 같아요' 같은 느낌에는 '호소'가 어색하다 —
# 모델이 다른 말로 바꾸다 조사를 틀린다)
SYMPTOM_CC = frozenset(
    (
        "잇몸에서 피가 나요",
        *_FILLING,
        "밤에 이가 욱신거려요",
        "씹을 때 아파요",
        "뜨거운 걸 먹으면 아파요",
        "이가 깨졌어요",
        "이가 흔들려요",
        "사랑니 쪽이 붓고 아파요",
        "잇몸이 붓고 피가 나요",
    )
)


# 응급 내원(6b단계)의 C.C — 상황(master.EMERGENCY_SCENES)마다 환자 말과 차트식으로 줄인 말. 모두 증상이라 '호소'가 자연스럽다
EMERGENCY_CC = dict(
    zip(
        EMERGENCY_SCENES,
        (
            ("밤새 위 어금니가 너무 아파요", "위 어금니 급성 통증"),
            ("씹다가 이가 깨졌어요", "저작 중 치아 깨짐"),
            ("넘어져서 앞니가 흔들려요", "외상 후 앞니 흔들림"),
            ("때운 게 빠져서 너무 시려요", "충전물 탈락·시림"),
            ("어금니 쪽 잇몸이 붓고 고름이 나요", "어금니 잇몸 부종·배농"),
            ("볼이 부을 정도로 아래 어금니가 아파요", "아래 어금니 통증·안면 부종"),
            ("씌운 게 빠지고 앞니가 아파요", "보철물 탈락·통증"),
            ("운동하다 앞니가 깨졌어요", "외상 후 앞니 깨짐"),
        ),
        strict=True,
    )
)
EMERGENCY_CC[EMERGENCY_AFTER_VISIT_SCENE] = (
    "치료받은 이가 마취 풀리고 너무 아파요",
    "치료 후 통증",
)
CC_SHORT |= dict(EMERGENCY_CC.values())
SYMPTOM_CC |= {cc for cc, _ in EMERGENCY_CC.values()}
# 응급 내원 서술에 꼭 넣을 말(그중 하나)과 초안 문장
EMERGENCY_WORDS = ("응급",)
EMERGENCY_SENTENCE = "응급 내원."

# 요건 근거를 드러내는 말 (§4.3) — 근거가 있으면 그 말을 꼭 넣고(R01은 사유마다 하나), 없으면 모두 금지
EVIDENCE_WORDS = {
    "R01": ("파절", "2차 우식"),
    "R02": ("재평가",),
    "R04": ("소와열구",),
    "R05": ("매복", "골삭제"),
    "R06": ("무치악",),
}
R01_REASON_WORD = {"기존 충전물 파절": "파절", "2차 우식": "2차 우식"}
# 근거 표현 방식(데이터 재설계 설계 §3) — 바꿔 말하기는 근거 낱말 자리에, 부정은 근거 없는 줄에 한 문장으로
PARAPHRASE = {  # 받침이 원래 낱말과 같은 말 — 초안의 조사가 그대로 맞게
    "파절": "균열",
    "2차 우식": "충전물 경계 우식",
    "재평가": "다시 평가",
    "소와열구": "열구",
    "부분 매복": "부분 매몰",
    "완전 매복": "완전 매몰",
    "골삭제": "뼈 다듬기",
    "무치악": "치아 결손",
}
NEGATION = {
    "R01": "파절·2차 우식 소견 없음",
    "R02": "재평가 예정",
    "R04": "소와열구 얕음",
    "R05": "매복 없음",
    "R06": "무치악 기간 확인 안 됨",
}
PERIO_QUALITATIVE = "치주낭 깊음"  # R03 숫자 없이 깊이만


def _pick(rng, seq):
    return seq[int(rng.integers(len(seq)))]


def _bucket(today: date, month: str) -> str:
    """청구월 구간 — pending(전월 이후) / submitted(그 전 SUBMITTED_MONTHS개월) / settled"""
    y, m = map(int, month.split("-"))
    before = today.year * 12 + today.month - (y * 12 + m)
    return (
        "pending" if before <= 1 else "submitted" if before <= 1 + SUBMITTED_MONTHS else "settled"
    )


def _lines(run) -> list[dict]:
    rows = [
        dict(
            zip(
                (
                    "id",
                    "appointment_id",
                    "patient_id",
                    "code",
                    "tooth",
                    "performed_at",
                    "claim_month",
                    "adjust_code",
                    "adjust_note",
                ),
                r,
                strict=True,
            )
        )
        for r in run.conn.execute(
            "SELECT t.id, t.appointment_id, t.patient_id, t.procedure_code, t.tooth_no, t.performed_at,"
            " c.claim_month, c.adjust_code, c.adjust_note FROM emr_treatments t"
            " LEFT JOIN emr_claims c ON c.treatment_id = t.id ORDER BY t.patient_id, t.performed_at, t.id"
        )
    ]
    for r in rows:
        r["day"] = datetime.fromisoformat(r["performed_at"]).date()
        r["window"] = r["day"] >= run.range_start
        r["claimed"] = r["claim_month"] is not None
        r["bucket"] = _bucket(run.today, r["claim_month"]) if r["claimed"] else None
        r["cut"] = (
            REASON_KEY.get((r["adjust_code"], r["adjust_note"]))
            if r["bucket"] == "settled"
            else None
        )
    return rows


def _prior_course(r, treated: dict) -> date | None:
    """같은 치아·같은 시술을 다른 코스에서 한 마지막 날 — 코스 안 반복(신경치료 2회차)은 이어지는 치료라 재시행이 아니다"""
    days = [
        day
        for day, root in treated[r["patient_id"]].get((r["tooth"], r["code"]), ())
        if root != r["root"]
    ]
    return max(days) if days else None


def _requirement(r, treated: dict, scaled: dict) -> str | None:
    """요건 사유 키 (창 안 급여 줄만) — 확정 월 요건 사유로 깎였으면 그 사유, 아니면 시술 규칙"""
    if not (r["window"] and r["claimed"]):
        return None
    if r["cut"] and ADJUST_REASONS[r["cut"]]["type"] == "requirement":
        return r["cut"]
    if r["code"] in CHART_REQUIREMENT:
        if r["code"] == PERIO_CODE and _last_scaling(r, scaled) is not None:
            return "R02"
        return CHART_REQUIREMENT[r["code"]]
    if r["code"] in ADJUST_REASONS[RETREATMENT_REQUIREMENT]["procedures"] and _prior_course(
        r, treated
    ):
        return RETREATMENT_REQUIREMENT  # 다른 코스에서 같은 치아·같은 시술 — 같은 치아의 다음 단계 치료(신경치료 뒤 충전)는 아님
    return None


def _last_scaling(r, scaled: dict) -> int | None:
    """직전 PERIO_LINK_DAYS일 안 치석제거로부터 지난 일수"""
    days = [
        (r["day"] - d).days
        for d in scaled[r["patient_id"]]
        if 0 < (r["day"] - d).days <= PERIO_LINK_DAYS
    ]
    return min(days) if days else None


def _requirement_facts(
    rng, key: str, evidence: bool, r, treated: dict, scaled: dict, extracted: dict
) -> dict:
    """요건 사실 — 장부에 근거 사건이 있으면 그 날짜에서 계산하고, 없으면(확정 월 사유로 정해진 줄) '타 기관' 이력
    (other_clinic)으로 둔다. 장부 날짜를 지어내면 ⑤-3이 장부를 보고 모순을 찾는다"""
    f = {"key": key, "evidence": evidence}
    if key == "R01":
        prev = _prior_course(r, treated)
        if prev:
            f["prev_days"] = (r["day"] - prev).days
        else:
            f["other_clinic"], f["prev_months"] = True, int(rng.integers(3, 25))
        f["reason"] = _pick(rng, ("기존 충전물 파절", "2차 우식")) if evidence else None
    elif key == "R02":
        days = _last_scaling(r, scaled)
        if days is not None:
            f["scaling_days_ago"] = days
        else:
            f["other_clinic"], f["scaling_days_ago"] = True, int(rng.integers(14, 61))
        f["linked"] = evidence
    elif key == "R04":
        f["surface"] = (
            "깊은 소와열구가 있고 우식이 없는 교합면" if evidence else "이미 충전물이 있는 교합면"
        )
    elif key == "R05":
        f["impaction"] = _pick(rng, ("부분 매복", "완전 매복")) if evidence else None
        f["bone_removal"] = evidence
    elif key == "R06" and evidence:
        pulled = extracted[r["patient_id"]].get(r["tooth"])
        if pulled:
            f["extracted_days_ago"] = (r["day"] - pulled).days
        else:
            f["other_clinic"], f["edentulous_months"] = True, int(rng.integers(3, 13))
    return f


def _findings(rng, r, requirement: dict | None, perio_plan: bool) -> dict | None:
    """소견 수치 — 위생·잇몸치료 내원만. 잇몸치료는 R03 근거가 없을 때만 치주낭 3mm 이하.
    치주 계획을 만든 위생 내원(perio_plan)은 계획과 맞게 잇몸치료처럼 깊은 치주낭·출혈"""
    if r["code"] not in HYGIENE_CODES and r["code"] != PERIO_CODE:
        return None
    perio = r["code"] == PERIO_CODE or perio_plan
    shallow = perio and requirement and requirement["key"] == "R03" and not requirement["evidence"]
    return {
        "pocket_mm": int(rng.integers(2, 4)) if shallow or not perio else int(rng.integers(4, 7)),
        "bleeding": True if perio and not shallow else bool(rng.random() < 0.4),
        "calculus": _pick(rng, ("적음", "중간", "많음")),
        "mobility": int(rng.integers(0, 2)) if perio else 0,
    }


def _alerts(run) -> dict:
    by_patient = defaultdict(list)
    for pid, code, recorded, ends, appt in run.conn.execute(
        "SELECT patient_id, code, recorded_at, ends_at, appointment_id FROM emr_patient_alerts ORDER BY code"
    ):
        by_patient[pid].append((code, recorded, ends, appt))
    return by_patient


def _plan(r, plan_items: list) -> dict | None:
    """다음 계획 — 그 내원을 마친 시각에 열린 치료계획 항목(§2.3 — 다음 예약이 잡혔는지·나중에 바뀌었는지와 무관),
    없으면 위생 내원은 다음 정기 검진"""
    end = datetime.fromisoformat(r["performed_at"]) + timedelta(minutes=DURATION[r["code"]])
    items = open_plan_items(plan_items, r["patient_id"], end.strftime("%Y-%m-%d %H:%M:%S"))
    if items:
        return {
            "items": [
                i | {"tooth": None if i["tooth"] == FULL_MOUTH else i["tooth"]} for i in items
            ]
        }
    if r["code"] in HYGIENE_CODES:
        return {"recall_months": RECALL_CYCLE_MONTHS}
    return None


def _extra(rng, r, teeth: set, birth: date) -> tuple[str, str] | None:
    """추가 처치 누락의 (시술, 치아) — SAME_DAY_EXTRA 중 그날 나이에 급여이고 장부에 없는 치아 후보가 있는 시술 균등,
    치아는 후보 중 균등 (§4.3). 후보가 없으면 None"""
    age = age_on(birth, r["day"])
    options = {
        code: [t for t in TOOTH_CHOICES[code] if t not in teeth]
        for code in sorted(SAME_DAY_EXTRA)
        if insured_on(PROCEDURE_BY_CODE[code], age)
    }
    codes = [c for c, ts in options.items() if ts]
    if not codes:
        return None
    code = codes[int(rng.integers(len(codes)))]
    return code, options[code][int(rng.integers(len(options[code])))]


def stage_facts(run, rng, fake) -> None:
    """난수를 나눈다 — 확정 월 오류(심사 결과 따라감) / 제출 월·전월 오류(⑤-2 정답) / 상병·사실(진료마다 따로 시드 —
    조건에 따라 뽑는 횟수가 달라도 다른 줄이 밀리지 않게). 심사 결과 가중을 튜닝해도 제출 월·전월 오류와 사실이 그대로"""
    settled_rng, open_rng, fact_rng, extra_rng, style_rng, add_rng = rng.spawn(
        6
    )  # 앞 셋은 추가 처치 누락을 넣기 전과, 앞 넷은 근거 표현 방식을, 앞 다섯은 추가 처치 누락 더하기를 넣기 전과 같다
    birth = {
        pid: date.fromisoformat(b)
        for pid, b in run.conn.execute("SELECT id, birth_date FROM emr_patients")
    }
    # 환자 → 장부 진료·치료계획 항목·이미 고른 추가 처치의 치아 (추가 처치 치아는 여기 없는 것 — "남은 치료"와 겹치지 않게)
    ledger_teeth = defaultdict(set)
    for pid, tooth in run.conn.execute("SELECT patient_id, tooth_no FROM emr_treatments"):
        ledger_teeth[pid].add(tooth)
    fact_seed = int(fact_rng.integers(1 << 62))
    style_seed = int(style_rng.integers(1 << 62))
    plan_items = run.state["plan_items"]
    for i in plan_items:
        ledger_teeth[i["patient_id"]].add(i["tooth"])
    perio_plans = {
        plan.appointment_id for plan in run.state["plans"]
    }  # 위생 내원이 만든 계획은 치주 계획
    alerts = _alerts(run)
    episode = run.state["episode"]  # 예약 → 코스. 창 이전 내원은 없음 — 예약마다 따로
    treated = defaultdict(lambda: defaultdict(list))  # 환자 → (치아, 시술) → [(진료일, 코스)]
    extracted = defaultdict(dict)  # 환자 → 치아 → 발치일
    scaled = defaultdict(list)
    lines, errors = [], []
    for r in _lines(run):
        r["root"] = episode.get(r["appointment_id"], ("pre", r["appointment_id"]))
        key = _requirement(r, treated, scaled)
        error = None
        if r["window"] and r["claimed"]:
            if r["bucket"] == "settled":
                cut_type = ADJUST_REASONS[r["cut"]]["type"] if r["cut"] else None
                if cut_type == "diagnosis":
                    error = "diagnosis"
                elif (
                    cut_type == "requirement"
                    and settled_rng.random() < REQUIREMENT_CUT_ERROR_SHARE
                ):
                    error = "requirement"
            else:
                if key and open_rng.random() < REQUIREMENT_ERROR_RATE:
                    error = "requirement"
                elif open_rng.random() < DIAGNOSIS_ERROR_RATE:
                    error = "diagnosis"
        extra = None
        if (
            error is None
            and r["window"]
            and r["claimed"]
            and r["bucket"] != "settled"
            and extra_rng.random() < SAME_DAY_EXTRA_RATE
        ):
            extra = _extra(extra_rng, r, ledger_teeth[r["patient_id"]], birth[r["patient_id"]])
            if extra:
                error = "omitted_extra"
                ledger_teeth[r["patient_id"]].add(extra[1])
        if (  # 데이터 재설계 — 기존 추가 처치 누락은 그대로, 같은 조건의 줄에 새 자식 난수로 더한다
            error is None
            and r["window"]
            and r["claimed"]
            and r["bucket"] != "settled"
            and add_rng.random() < SAME_DAY_EXTRA_ADD_RATE
        ):
            extra = _extra(add_rng, r, ledger_teeth[r["patient_id"]], birth[r["patient_id"]])
            if extra:
                error = "omitted_extra"
                ledger_teeth[r["patient_id"]].add(extra[1])
        rng = np.random.default_rng([fact_seed, r["id"]])
        allowed = sorted(PROCEDURE_DIAGNOSES[r["code"]])
        dx = (
            _pick(rng, sorted(set(DIAGNOSES) - set(allowed)))
            if error == "diagnosis"
            else _pick(rng, allowed)
        )
        requirement = (
            _requirement_facts(rng, key, error != "requirement", r, treated, scaled, extracted)
            if key
            else None
        )
        day = r["day"].isoformat()
        visible = [
            (c, appt)
            for c, recorded, ends, appt in alerts[r["patient_id"]]
            if recorded <= day and (ends is None or day < ends)
        ]
        facts = {
            "procedure": NAME[r["code"]],
            "tooth": None if r["tooth"] == FULL_MOUTH else r["tooth"],
            "cc": _pick(rng, CC[r["code"]]),  # 근거 없는 줄 전용 C.C는 두지 않는다(데이터 재설계)
            "findings": _findings(
                rng,
                r,
                requirement,
                r["code"] in HYGIENE_CODES and r["appointment_id"] in perio_plans,
            ),
            "requirement": requirement,
            "alerts": [
                {"label": ALERT_TYPES[c]["label"], "action": ALERT_TYPES[c]["action"]}
                for c, appt in visible
                if appt != r["appointment_id"] and r["code"] in ALERT_TYPES[c]["procedures"]
            ],
            "discovered": [
                ALERT_TYPES[c]["label"] for c, appt in visible if appt == r["appointment_id"]
            ],
            "plan": _plan(r, plan_items),
            "extra": extra and {"procedure": NAME[extra[0]], "tooth": extra[1]},
        }
        if requirement:  # 근거 표현 방식 — 진료마다 따로 시드(다른 줄을 밀지 않게)
            _style(np.random.default_rng([style_seed, r["id"]]), facts)
        lines.append(
            {
                "id": r["id"],
                "window": r["window"],
                "code": r["code"],
                "dx": dx,
                "requirement": key,
                "evidence": None if key is None else int(error != "requirement"),
                "special": bool(key or error),
                "facts": facts,
            }
        )
        if error:
            errors.append(
                (r["id"], error, key if error == "requirement" else None, *(extra or (None, None)))
            )
        if r["tooth"] != FULL_MOUTH:
            treated[r["patient_id"]][(r["tooth"], r["code"])].append((r["day"], r["root"]))
        if r["code"] in EXTRACTION_CODES:
            extracted[r["patient_id"]][r["tooth"]] = r["day"]
        if r["code"] == SCALING_CODE:
            scaled[r["patient_id"]].append(r["day"])
    run.conn.executemany(
        "INSERT INTO truth.truth_claim_errors (treatment_id, kind, requirement, extra_procedure_code,"
        " extra_tooth_no) VALUES (?, ?, ?, ?, ?)",
        sorted(errors),
    )
    run.state["chart"] = sorted(lines, key=lambda x: x["id"])


def _decoys(rng, lines: list[dict]) -> set:
    """특이 × CHART_DECOY_RATIO개를 평범한 창 안 줄에서 — 시술별 줄 수에 비례(최대 나머지), 시술 안에서는 균등"""
    groups = defaultdict(list)
    for x in lines:
        if x["window"] and not x["special"]:
            groups[x["code"]].append(x["id"])
    total = sum(len(g) for g in groups.values())
    want = min(total, round(sum(x["special"] for x in lines if x["window"]) * CHART_DECOY_RATIO))
    if not want:
        return set()
    exact = {code: want * len(g) / total for code, g in groups.items()}
    quota = {code: int(e) for code, e in exact.items()}
    for code in sorted(exact, key=lambda c: (-(exact[c] - quota[c]), c))[
        : want - sum(quota.values())
    ]:
        quota[code] += 1
    picked = set()
    for code in sorted(groups):
        ids = groups[code]
        picked |= {ids[i] for i in rng.choice(len(ids), quota[code], replace=False).tolist()}
    return picked


def _site(f) -> str:
    return f"{f['tooth']}번 치아 " if f["tooth"] else ""


TEMPLATES = (  # 차트는 짧은 메모체 — '~함·~음'·명사로 끝낸다(§4.3)
    "{site}{procedure} 진행함.",
    '"{cc}"라며 내원, {site}{procedure} 시행함.',
    "{site}{procedure} 시행함. 특이사항 없음.",  # 주의사항이 있는 줄에는 쓰지 않음
)


def template_note(rng, f) -> str:
    """템플릿 — 시술·부위·소견 + 주의사항 처리와 그날 발견한 주의사항(§2.2 — 그날 차트에도 남긴다)"""
    alerts = _alert_sentences(f)
    note = _pick(rng, TEMPLATES[:2] if alerts else TEMPLATES).format(
        site=_site(f), procedure=f["procedure"], cc=f["cc"]
    )
    if f["findings"]:
        note += f" 치주낭 {f['findings']['pocket_mm']}mm, 치석 {f['findings']['calculus']}."
    return " ".join([note, *alerts])


def _alert_sentences(f, key=None) -> list[str]:
    """주의사항 처리 — 서술 초안(key 있음)은 꼴을 진료 번호로 고른다, 템플릿은 한 꼴"""
    forms = ("{label} 주의, {action}.", "{action}({label}).")
    return [
        (_form(f"{key}:{i}", "alert", forms) if key is not None else forms[0]).format(**a)
        for i, a in enumerate(f["alerts"])
    ] + [f"진료 중 {label} 새로 확인." for label in f["discovered"]]


def _ro(word: str) -> str:
    """조사 '(으)로' — 받침이 있으면(ㄹ 받침 제외) '으로'"""
    batchim = (ord(word[-1]) - 0xAC00) % 28 if "가" <= word[-1] <= "힣" else 0
    return word + ("으로" if batchim and batchim != 8 else "로")


def _ago(days: int) -> str:
    return f"{days}일 전" if days < 60 else f"약 {round(days / 30)}개월 전"


def plan_items(plan: dict) -> list[tuple[dict, int]]:
    """남은 치료 — 같은 시술·치아는 한 번만(횟수와 함께, 처음 나온 순서). 같은 말을 여러 번 넣으라고 하면 모델이 '다음 내원
    때 이어서' 같은 문장을 덧붙인다"""
    counts = Counter((i["tooth"], i["procedure"]) for i in plan["items"])
    seen = {}
    for i in plan["items"]:
        seen.setdefault((i["tooth"], i["procedure"]), i)
    return [(i, counts[k]) for k, i in seen.items()]


def _form(key, salt: str, options: tuple):
    """초안 꼴 고르기 — 진료 번호 해시로(난수를 쓰지 않아 다른 결과를 바꾸지 않는다). 프롬프트에 여러 꼴을 줘도 모델은 건마다
    따로 불려 한 꼴로 몰려서, 초안이 꼴을 정한다"""
    return options[int(hashlib.sha256(f"{salt}:{key}".encode()).hexdigest(), 16) % len(options)]


def _style(rng, f: dict) -> None:
    """요건 줄의 근거 표현 방식(데이터 재설계 설계 §3) — 정답(근거 있음·없음)은 그대로, 쓰는 말만 고른다.
    R03 부분 소견은 치주낭을 기준 이상으로 올리고 출혈을 없앤다(복합 기준 — 깊이 + 염증 소견)"""
    req = f["requirement"]
    if req["key"] == "R03":
        if req["evidence"]:
            req["style"] = "qualitative" if rng.random() < PERIO_QUALITATIVE_SHARE else "explicit"
        elif f["findings"] and rng.random() < PERIO_NEAR_MISS_SHARE:
            req["style"] = "near_miss"
            f["findings"] |= {
                "pocket_mm": int(rng.integers(PERIO_POCKET_MIN_MM, PERIO_POCKET_MIN_MM + 2)),
                "bleeding": False,
            }
        else:
            req["style"] = "plain"
    elif req["evidence"]:
        req["style"] = "paraphrase" if rng.random() < EVIDENCE_PARAPHRASE_SHARE else "explicit"
    else:
        req["style"] = (
            "negated"
            if req["key"] in NEGATION and rng.random() < EVIDENCE_NEGATED_SHARE
            else "plain"
        )


def style_note(f: dict, note: str) -> str:
    """초안에 표현 방식을 입힌다 — 바꿔 말하기는 근거 낱말을 바꾸고, 부정은 문장을 더하고, R03 숫자 없는 깊이는 mm를 지운다"""
    req = f.get("requirement") or {}
    style = req.get("style")
    if style == "paraphrase":
        for w in _plain_requirement_words(req):  # 꼭 넣을 말과 같은 단위로(부분 매복 → 부분 매몰)
            note = note.replace(w, PARAPHRASE.get(w, w))
    elif style == "negated":
        note += f" {NEGATION[req['key']]}."
    elif style == "qualitative" and f["findings"]:
        note = note.replace(f"치주낭 {f['findings']['pocket_mm']}mm", PERIO_QUALITATIVE)
    return note


def draft_note(f, key) -> str:
    """서술 초안 — 사실을 그대로 메모로. 환자 말(따옴표 또는 줄인 말)·소견·시행·주의사항·남은 계획의 꼴은 진료 번호(key)로 고른다.
    9b단계가 이 초안을 다시 쓴다(가짜 게이트웨이면 그대로 남는다)"""
    cc, x = f["cc"], f["findings"]
    opener = _form(key, "cc", ("라며", "라고 하여", "뒤", "호소"))
    if opener == "호소" and cc not in SYMPTOM_CC:
        opener = "라며"
    if _form(key, "quote", (True, False)):
        said = {
            "라며": f'"{cc}"라며 내원.',
            "라고 하여": f'"{cc}"라고 하여 내원.',
            "뒤": f'"{cc}"라고 함.',
            "호소": f'"{cc}" 호소.',
        }[opener]
    else:  # 차트식으로 줄인 말
        short = CC_SHORT[cc]
        said = (
            f"{short} 호소."
            if opener == "호소"
            else f"{short}."
            if opener == "뒤"
            else f"{_ro(short)} 내원."
        )
    out = [] if opener == "뒤" and x else [said]
    if f.get("emergency"):
        out.append(EMERGENCY_SENTENCE)
    if x:
        b = x["bleeding"]
        out.append(
            _form(
                key,
                "findings",
                (
                    f"치주낭 {x['pocket_mm']}mm, 출혈 {'있음' if b else '없음'}, 치석 {x['calculus']},"
                    f" 동요도 {x['mobility']}도.",
                    f"출혈 {'있고' if b else '없고'} 치주낭 {x['pocket_mm']}mm, 치석 {x['calculus']},"
                    f" 동요도 {x['mobility']}도.",
                    f"치석 {x['calculus']}, 치주낭 {x['pocket_mm']}mm, 출혈 {'있음' if b else '없음'},"
                    f" 동요도 {x['mobility']}도.",
                ),
            )
        )
        if opener == "뒤":
            out.append(said)
    req = f["requirement"]
    if req and req["key"] == "R01":
        history = (
            f"타 기관에서 약 {req['prev_months']}개월 전"
            if req.get("other_clinic")
            else _ago(req["prev_days"])
        )
        out.append(
            f"같은 치아 {history} 같은 치료 이력 있음."
            + (f" {_ro(req['reason'])} 재치료." if req["reason"] else "")
        )
    elif req and req["key"] == "R02":
        done = (
            f"타 기관에서 {req['scaling_days_ago']}일 전 치석제거 받음."
            if req.get("other_clinic")
            else f"{req['scaling_days_ago']}일 전 치석제거 시행함."
        )
        out.append(done + (" 재평가 후 치주치료 이어서 진행." if req["linked"] else ""))
    elif req and req["key"] == "R04":
        out.append(f"대상 치아 {req['surface']} 확인.")
    elif req and req["key"] == "R05" and req["impaction"]:
        out.append(f"{req['impaction']} 상태로 골삭제 후 발치.")
    elif req and req["key"] == "R06" and req.get("extracted_days_ago"):
        out.append(f"{_ago(req['extracted_days_ago'])} 발치한 무치악 부위.")
    elif req and req["key"] == "R06" and req.get("other_clinic"):
        out.append(f"타 기관에서 발치 후 {req['edentulous_months']}개월 지난 무치악 부위.")
    out.append(f"{_site(f)}{f['procedure']} {_form(key, 'done', ('시행함', '진행함'))}.")
    if f.get("extra"):
        site = f"{_site(f['extra'])}{f['extra']['procedure']}"
        out.append(  # 표현을 진료 번호로 고른다 — "같은 날" 한 낱말로 갈리지 않게(데이터 재설계)
            _form(
                key,
                "extra",
                (
                    f"같은 날 {site}도 시행함.",
                    f"이어서 {site} 함께 진행함.",
                    f"{site}도 추가로 시행함.",
                ),
            )
        )
    out += _alert_sentences(f, key)
    plan = f["plan"]
    if plan and "items" in plan:
        items = ", ".join(
            _site(i) + i["procedure"] + (f" {n}회" if n > 1 else "") for i, n in plan_items(plan)
        )
        out.append(
            _form(
                key,
                "plan",
                (
                    f"남은 치료로 {items} 예정.",
                    f"{items} 예정.",
                    f"{items} 남아 있음.",
                    f"다음 치료는 {items}.",
                ),
            )
        )
    elif plan:
        n = plan["recall_months"]
        out.append(
            _form(
                key, "recall", (f"{n}개월 후 정기 검진 안내함.", f"정기 검진 {n}개월 후로 안내.")
            )
        )
    return " ".join(out)


def emergency_facts(uid: str, r: dict, scene: str, alerts: dict, plan_items: list) -> dict:
    """응급 내원(6b단계)의 차트 사실 — 상황의 환자 말, 그날 보이는 관련 주의사항, 내원을 마친 시각의 남은 계획(§4.3과 같은 틀).
    요건·오류·소견·추가 처치는 없다. 환자 말 꼴은 통화 번호로"""
    day = r["performed_at"][:10]
    cc = EMERGENCY_CC[scene][0]
    return {
        "procedure": NAME[r["code"]],
        "tooth": None if r["tooth"] == FULL_MOUTH else r["tooth"],
        "cc": cc,
        "findings": None,
        "requirement": None,
        "alerts": [
            {"label": ALERT_TYPES[c]["label"], "action": ALERT_TYPES[c]["action"]}
            for c, recorded, ends, _ in alerts[r["patient_id"]]
            if recorded <= day
            and (ends is None or day < ends)
            and r["code"] in ALERT_TYPES[c]["procedures"]
        ],
        "discovered": [],
        "plan": _plan({**r, "day": date.fromisoformat(day)}, plan_items),
        "extra": None,
        "emergency": True,
    }


def stage_notes(run, rng, fake) -> None:
    lines = run.state["chart"]
    decoys = _decoys(rng, lines)
    notes, truth = [], []
    for x in lines:
        narrative = x["special"] or x["id"] in decoys
        note = (
            style_note(x["facts"], draft_note(x["facts"], x["id"]))
            if narrative
            else template_note(rng, x["facts"])
        )
        notes.append((x["id"], x["dx"], note))
        truth.append(
            (
                x["id"],
                "narrative" if narrative else "template",
                int(x["special"]),
                x["requirement"],
                x["evidence"],
                int(x["id"] in decoys),
                json.dumps(x["facts"], ensure_ascii=False, sort_keys=True),
            )
        )
    run.conn.executemany(
        "INSERT INTO emr_chart_notes (treatment_id, dx_code, note) VALUES (?, ?, ?)", notes
    )
    run.conn.executemany("INSERT INTO truth.truth_chart_notes VALUES (?, ?, ?, ?, ?, ?, ?)", truth)


# ── 9b단계 차트 서술 (§4.3 '9b단계 구현 결정') ──────────────────

NARRATIVE_SCHEMA = {
    "type": "object",
    "properties": {"note": {"type": "string", "description": "문장형 차트 본문"}},
    "required": ["note"],
    "additionalProperties": False,
}
# 가짜 모드(테스트)는 채우지 않는다 — 초안이 그대로 남는다
NARRATIVE_TASK = Task(
    name="chart", prompt="gen_chart", schema=NARRATIVE_SCHEMA, store=("note",), fake=False
)


def _period(days: int) -> str:
    """기간 숫자 — _ago와 같은 단위(60일 미만은 일, 이상은 개월)"""
    return f"{days}일" if days < 60 else f"{round(days / 30)}개월"


# 꼭 넣을 말 묶음(그중 하나) — 타 기관 이력(원내 이력처럼 쓰면 ⑤-3이 장부와 모순), 진료 중 발견, 남은 치료계획
OTHER_CLINIC_WORDS = ("타 기관", "다른 치과", "타 치과")
DISCOVERED_WORDS = ("진료 중", "새로")
PLAN_WORDS = ("남은", "남아", "예정", "다음")
# 차트 서술에 쓰면 안 되는 꼴 — 메모체가 아닌 끝맺음·높임말, 초안에 없는 시점(초안은 '다음 내원'을 쓰지 않는다),
# 치아 없는 진료에 붙인 부위 말, 시행한 골삭제를 '필요'로 바꾼 말
MEMO_STYLE_FORBIDDEN = (
    r"니다|하시며|하셨|했다\.|다음 (내원|진료|방문)|전체 구강|전악|전반적으로|골삭제 필요"
)


def _requirement_words(req: dict) -> list[str]:
    """요건 수치 + 근거 말(표현 방식대로 — 바꿔 말하기면 그 말, 부정이면 부정 문장)"""
    words = _plain_requirement_words(req)
    style = req.get("style")
    if style == "paraphrase":
        words = [PARAPHRASE.get(w, w) for w in words]
    elif style == "negated":
        words = [w for w in words if w not in EVIDENCE_WORDS.get(req["key"], ())] + [
            NEGATION[req["key"]]
        ]
    return words


def _plain_requirement_words(req: dict) -> list[str]:
    key = req["key"]
    if key == "R01":
        period = (
            f"{req['prev_months']}개월" if req.get("other_clinic") else _period(req["prev_days"])
        )
        return [period] + ([R01_REASON_WORD[req["reason"]]] if req["evidence"] else [])
    if key == "R02":
        return [f"{req['scaling_days_ago']}일"] + (["재평가"] if req["evidence"] else [])
    if key == "R04":
        return ["소와열구" if req["evidence"] else "충전물"]
    if key == "R05" and req["impaction"]:
        return [req["impaction"], "골삭제"]
    if key == "R06" and req.get("extracted_days_ago"):
        return [_period(req["extracted_days_ago"]), "무치악"]
    if key == "R06" and req.get("other_clinic"):
        return [f"{req['edentulous_months']}개월", "무치악"]
    return []


def evidence_forbidden(f: dict) -> list[str]:
    """쓰면 안 되는 근거 말 — 근거 없는 줄(부정 표현은 그 낱말을 부정으로 쓰므로 빼고)과 바꿔 말하기 줄은 근거 낱말,
    R03 숫자 없는 깊이는 그 mm"""
    req = f["requirement"]
    if not req:
        return []
    style = req.get("style")
    if style == "qualitative" and f["findings"]:
        return [f"{f['findings']['pocket_mm']}mm"]
    if (not req["evidence"] and style != "negated") or style == "paraphrase":
        return list(EVIDENCE_WORDS.get(req["key"], ()))
    return []


def narrative_must_include(f: dict) -> list[list[str]]:
    """꼭 넣을 사실 — 치아 'NN번'·시술 이름(진료 기록이라 표준 이름만)·추가 처치의 치아·시술·치주낭 'Nmm'·주의사항·
    발견한 주의사항과 새로 알게 됐다는 말·요건 수치·타 기관 이력이면 그 말·다음 계획과 계획이라는 말"""
    out = [[f"{f['tooth']}번"]] if f["tooth"] else []
    out.append([f["procedure"]])
    if f.get("emergency"):
        out.append(list(EMERGENCY_WORDS))
    if f.get("extra"):
        out += [[f"{f['extra']['tooth']}번"], [f["extra"]["procedure"]]]
    if f["findings"]:
        qualitative = (f["requirement"] or {}).get("style") == "qualitative"
        out.append([PERIO_QUALITATIVE] if qualitative else [f"{f['findings']['pocket_mm']}mm"])
    out += [[a["label"]] for a in f["alerts"]] + [[label] for label in f["discovered"]]
    if f["discovered"]:
        out.append(list(DISCOVERED_WORDS))
    if f["requirement"]:
        out += [[w] for w in _requirement_words(f["requirement"])]
        if f["requirement"].get("other_clinic"):
            out.append(list(OTHER_CLINIC_WORDS))
    plan = f["plan"]
    if plan and "items" in plan:
        for i, n in plan_items(plan):
            out += (
                ([[f"{i['tooth']}번"]] if i["tooth"] else [])
                + [[i["procedure"]]]
                + ([[f"{n}회"]] if n > 1 else [])
            )
        out.append(list(PLAN_WORDS))
    elif plan:
        out.append([f"{plan['recall_months']}개월"])
    return out


def narrative_payload(f: dict, draft: str) -> dict:
    """LLM 입력 — 요건 사유 키·충족 여부(정답)는 빼고 사실 필드만 history로. 근거가 없는 오류 줄은 그 사실이 비어 있다"""
    history = {
        k: v for k, v in (f["requirement"] or {}).items() if k not in ("key", "evidence", "style")
    }
    facts = {k: v for k, v in f.items() if k != "requirement"} | (
        {"history": history} if history else {}
    )
    return {"facts": facts, "draft": draft}


def narrative_checks(f: dict, patient_name: str) -> dict:
    return {
        "field": "note",
        "must_include": narrative_must_include(f),
        "forbidden_words": [*CHART_FORBIDDEN_WORDS, *evidence_forbidden(f), patient_name],
        "forbidden_patterns": [*llm_jobs.forbidden_patterns(), MEMO_STYLE_FORBIDDEN],
    }


def stage_narratives(run, rng, fake) -> None:
    ctx = run.state["llm"]
    rows = run.conn.execute(
        "SELECT n.treatment_id, n.note, c.facts, p.name FROM emr_chart_notes n"
        " JOIN truth.truth_chart_notes c USING (treatment_id) JOIN emr_treatments t ON t.id = n.treatment_id"
        " JOIN emr_patients p ON p.id = t.patient_id WHERE c.kind = 'narrative' ORDER BY n.treatment_id"
    ).fetchall()
    for treatment_id, draft, facts, patient_name in rows:
        f = json.loads(facts)
        out = ctx.resolve(
            NARRATIVE_TASK,
            str(treatment_id),
            narrative_payload(f, draft),
            narrative_checks(f, patient_name),
        )
        if out is not None:
            run.conn.execute(
                "UPDATE emr_chart_notes SET note = ? WHERE treatment_id = ?",
                (out["note"], treatment_id),
            )
