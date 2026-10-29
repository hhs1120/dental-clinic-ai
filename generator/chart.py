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

import json
from collections import defaultdict
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
    EXTRACTION_CODES,
    FULL_MOUTH,
    PERIO_LINK_DAYS,
    PROCEDURE_DIAGNOSES,
    PROCEDURES,
    RECALL_CYCLE_MONTHS,
    REQUIREMENT_CUT_ERROR_SHARE,
    REQUIREMENT_ERROR_RATE,
    RETREATMENT_REQUIREMENT,
    SAME_DAY_EXTRA,
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


# 근거 없는 요건 줄의 C.C — 환자 말이 근거로 읽히지 않게('빠진 자리'는 무치악, '까만 점'은 우식, '홈 메우기'는 소와열구 근거)
CC_NO_EVIDENCE = {
    "R01": ("예전에 치료한 이가 신경 쓰여요",),
    "R03": ("잇몸 상태 확인하러 왔어요",),
    "R04": ("아이 치아 관리 받으러 왔어요",),
    "R06": ("임플란트 치료 받으러 왔어요",),
}
# 요건 근거를 드러내는 말 (§4.3) — 근거가 있으면 그 말을 꼭 넣고(R01은 사유마다 하나), 없으면 모두 금지
EVIDENCE_WORDS = {
    "R01": ("파절", "2차 우식"),
    "R02": ("재평가",),
    "R04": ("소와열구",),
    "R05": ("매복", "골삭제"),
    "R06": ("무치악",),
}
R01_REASON_WORD = {"기존 충전물 파절": "파절", "2차 우식": "2차 우식"}


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
    settled_rng, open_rng, fact_rng, extra_rng = rng.spawn(
        4
    )  # 앞 셋은 추가 처치 누락을 넣기 전과 같다
    birth = {
        pid: date.fromisoformat(b)
        for pid, b in run.conn.execute("SELECT id, birth_date FROM emr_patients")
    }
    # 환자 → 장부 진료·치료계획 항목·이미 고른 추가 처치의 치아 (추가 처치 치아는 여기 없는 것 — "남은 치료"와 겹치지 않게)
    ledger_teeth = defaultdict(set)
    for pid, tooth in run.conn.execute("SELECT patient_id, tooth_no FROM emr_treatments"):
        ledger_teeth[pid].add(tooth)
    fact_seed = int(fact_rng.integers(1 << 62))
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
            "cc": _pick(
                rng,
                CC_NO_EVIDENCE.get(key, CC[r["code"]])
                if requirement and not requirement["evidence"]
                else CC[r["code"]],
            ),
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


TEMPLATES = (
    "{site}{procedure} 진행했습니다.",
    "{cc}라고 하셔서 {site}{procedure} 시행했습니다.",
    "{site}{procedure} 시행했습니다. 특이사항은 없습니다.",  # 주의사항이 있는 줄에는 쓰지 않음
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


def _alert_sentences(f) -> list[str]:
    return [f"{a['label']} 주의사항이 있어 {a['action']}." for a in f["alerts"]] + [
        f"진료 중 확인: {label}." for label in f["discovered"]
    ]


def _ro(word: str) -> str:
    """조사 '(으)로' — 받침이 있으면(ㄹ 받침 제외) '으로'"""
    batchim = (ord(word[-1]) - 0xAC00) % 28 if "가" <= word[-1] <= "힣" else 0
    return word + ("으로" if batchim and batchim != 8 else "로")


def _ago(days: int) -> str:
    return f"{days}일 전" if days < 60 else f"약 {round(days / 30)}개월 전"


def draft_note(f) -> str:
    """서술 초안 — 사실을 그대로 문장으로. 9b단계가 자연스러운 서술로 덮어쓴다"""
    out = [f'"{f["cc"]}"라고 하시며 내원했습니다.']
    if f["findings"]:
        x = f["findings"]
        out.append(
            f"치주낭 깊이 {x['pocket_mm']}mm, 출혈 {'있음' if x['bleeding'] else '없음'}, 치석 {x['calculus']},"
            f" 동요도 {x['mobility']}도로 확인했습니다."
        )
    req = f["requirement"]
    if req and req["key"] == "R01":
        history = (
            f"타 기관에서 약 {req['prev_months']}개월 전"
            if req.get("other_clinic")
            else _ago(req["prev_days"])
        )
        out.append(
            f"같은 치아에 {history} 같은 치료를 한 이력이 있습니다."
            + (f" {_ro(req['reason'])} 다시 치료합니다." if req["reason"] else "")
        )
    elif req and req["key"] == "R02":
        where = "타 기관에서 " if req.get("other_clinic") else ""
        out.append(
            f"{where}{req['scaling_days_ago']}일 전 치석제거를 했습니다."
            + (" 재평가 후 치주치료를 이어서 진행합니다." if req["linked"] else "")
        )
    elif req and req["key"] == "R04":
        out.append(f"대상 치아는 {req['surface']}입니다.")
    elif req and req["key"] == "R05" and req["impaction"]:
        out.append(f"{req['impaction']} 상태로 골삭제 후 발치합니다.")
    elif req and req["key"] == "R06" and req.get("extracted_days_ago"):
        out.append(f"{_ago(req['extracted_days_ago'])} 발치한 무치악 부위입니다.")
    elif req and req["key"] == "R06" and req.get("other_clinic"):
        out.append(f"타 기관에서 발치 후 {req['edentulous_months']}개월 지난 무치악 부위입니다.")
    out.append(f"{_site(f)}{f['procedure']} 시행했습니다.")
    if f.get("extra"):
        out.append(f"같은 날 {_site(f['extra'])}{f['extra']['procedure']}도 시행했습니다.")
    out += _alert_sentences(f)
    plan = f["plan"]
    if plan and "items" in plan:
        out.append(
            f"남은 치료는 {', '.join(_site(i) + i['procedure'] for i in plan['items'])}입니다."
        )
    elif plan:
        out.append(f"{plan['recall_months']}개월 후 정기 검진을 안내했습니다.")
    return " ".join(out)


def stage_notes(run, rng, fake) -> None:
    lines = run.state["chart"]
    decoys = _decoys(rng, lines)
    notes, truth = [], []
    for x in lines:
        narrative = x["special"] or x["id"] in decoys
        note = draft_note(x["facts"]) if narrative else template_note(rng, x["facts"])
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


def _requirement_words(req: dict) -> list[str]:
    """요건 수치 + 근거가 있으면 근거 말"""
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
    """근거 없는 요건 줄에 쓰면 안 되는 근거 말"""
    req = f["requirement"]
    return list(EVIDENCE_WORDS.get(req["key"], ())) if req and not req["evidence"] else []


def narrative_must_include(f: dict) -> list[list[str]]:
    """꼭 넣을 사실 — 치아 'NN번'·시술 이름(진료 기록이라 표준 이름만)·추가 처치의 치아·시술·치주낭 'Nmm'·주의사항·
    발견한 주의사항과 새로 알게 됐다는 말·요건 수치·타 기관 이력이면 그 말·다음 계획과 계획이라는 말"""
    out = [[f"{f['tooth']}번"]] if f["tooth"] else []
    out.append([f["procedure"]])
    if f.get("extra"):
        out += [[f"{f['extra']['tooth']}번"], [f["extra"]["procedure"]]]
    if f["findings"]:
        out.append([f"{f['findings']['pocket_mm']}mm"])
    out += [[a["label"]] for a in f["alerts"]] + [[label] for label in f["discovered"]]
    if f["discovered"]:
        out.append(list(DISCOVERED_WORDS))
    if f["requirement"]:
        out += [[w] for w in _requirement_words(f["requirement"])]
        if f["requirement"].get("other_clinic"):
            out.append(list(OTHER_CLINIC_WORDS))
    plan = f["plan"]
    if plan and "items" in plan:
        for i in plan["items"]:
            out += ([[f"{i['tooth']}번"]] if i["tooth"] else []) + [[i["procedure"]]]
        out.append(list(PLAN_WORDS))
    elif plan:
        out.append([f"{plan['recall_months']}개월"])
    return out


def narrative_payload(f: dict, draft: str) -> dict:
    """LLM 입력 — 요건 사유 키·충족 여부(정답)는 빼고 사실 필드만 history로. 근거가 없는 오류 줄은 그 사실이 비어 있다"""
    history = {k: v for k, v in (f["requirement"] or {}).items() if k not in ("key", "evidence")}
    facts = {k: v for k, v in f.items() if k != "requirement"} | (
        {"history": history} if history else {}
    )
    return {"facts": facts, "draft": draft}


def narrative_checks(f: dict, patient_name: str) -> dict:
    return {
        "field": "note",
        "must_include": narrative_must_include(f),
        "forbidden_words": [*CHART_FORBIDDEN_WORDS, *evidence_forbidden(f), patient_name],
        "forbidden_patterns": llm_jobs.forbidden_patterns(),
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
