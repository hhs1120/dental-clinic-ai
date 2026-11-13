"""RAG 문서 목록 (가상 데이터 생성 설계 §4.2 '9단계 구현 결정') — 문서마다 종류·제목·주제·심을 사실·짝.
9단계 문서 계획(generator/documents.py)이 이 표로 ax_documents·truth_doc_meta를 만들고, LLM은 본문만 쓴다.
- 사실의 words = 본문에 그대로 들어갈 말(생성 검증·verify). 사람은 생성 후 이 사실로 ③ 질문을 쓴다
- EMR과 겹치는 값(가격·나이·진료시간·예약 길이·문자 시각)은 master에서 읽는다 — 여기에 숫자를 따로 쓰지 않는다
- 공지는 최신(version 2)과 옛 버전(old_version, version 1) 한 쌍. 옛 버전 시행일 < 생성 기간 시작, EMR과 겹치는 값을
  다루는 공지(emr=True)는 최신 시행일 ≤ EMR 최초 기록일(창 이전 내원 포함 — tests/generator/test_doc_catalog.py)
- 방해 문서: similar = 짝과 비슷한 주제를 다루되 skip(짝의 사실이 있는 내용)은 쓰지 않음 — 짝과 avoid 문서의 사실 말 금지 /
  external = 같은 주제를 다른 값으로(타 병원·일반)
- 같은 사실을 담은 핵심 문서(공지와 FAQ·매뉴얼 등)는 Fact.same 묶음 키가 같다 — ③ 질문의 정답 문서를 함께 묶는 기준
- 공지는 시행일('N월 M일')도 꼭 넣을 말. 문서를 빼면 번호가 다시 섞이니 ③ 질문을 쓰기 전에 목록을 확정한다.
  더한 문서는 기존 번호를 두고 뒤에 붙인다 — 키를 documents.APPENDED에 더한 순서대로 넣는다
"""

from dataclasses import dataclass, field, replace

from generator.claim_standards import REAL, VIRTUAL
from generator.master import (
    AGE_BANDS,
    CONDITIONAL_INSURED_MIN_AGE,
    HOURS,
    ORAL_CARE_PRICES,
    PERIO_LINK_DAYS,
    PERIO_POCKET_MIN_MM,
    PROCEDURES,
    SCALING_MIN_AGE,
)

KINDS = ("core", "old_version", "similar", "external")


@dataclass(frozen=True)
class Fact:
    id: str
    text: str  # 사실 문장 — 프롬프트·초안·질문 작성의 재료
    words: tuple[str, ...]  # 본문에 그대로 들어갈 말 (전부)
    reason: str | None = None  # 청구 기준 절이면 상세 사유 키(master.ADJUST_REASONS)
    same: str | None = None  # 같은 사실 묶음 키 — 다른 문서의 같은 사실과 같은 값


@dataclass(frozen=True)
class Doc:
    key: str  # 경로·짝 참조용 고유 이름
    type: str  # ax_documents.type
    title: str
    topic: str  # 다룰 내용
    facts: tuple[Fact, ...] = ()
    kind: str = "core"
    pair: str | None = None  # similar·external: 흉내 내는 문서 / old_version: 대체한 최신 공지
    effective: str | None = None  # 시행일 (공지만)
    emr: bool = False  # 공지가 EMR과 겹치는 값을 다룸
    skip: str | None = None  # similar: 다루지 않을 내용(짝의 사실이 있는 부분)
    avoid: tuple[str, ...] = ()  # similar: 짝 말고도 사실 말을 쓰면 안 되는 핵심 문서(주제가 겹침)
    recurring: bool = False  # 해마다 다시 내는 공지(휴진) — 이전 값(previous)을 주지 않음
    sections: tuple[str, ...] = field(default=())  # 청구 기준: 다룰 상세 사유 키 순서
    forbidden: tuple[str, ...] = ()  # 쓰면 안 되는 말 — 사실 일부를 목록 전부처럼 쓰지 않게


def _won(n: int) -> str:
    return f"{n:,}원"


def _clock(hhmm: str) -> str:
    """'18:30' → '6시 30분' (오전·오후는 문장이 붙인다 — 그대로 들어갈 말에는 넣지 않음)"""
    h, m = (int(x) for x in hhmm.split(":"))
    h = h - 12 if h > 12 else h
    return f"{h}시" + (f" {m}분" if m else "")


_P = {p["name"]: p for p in PROCEDURES}
_SHARE = round(_P["스케일링"]["patient_share_rate"] * 100)  # 급여 본인부담률(%)
_SCALING_SHARE = _won(round(_P["스케일링"]["fee_total"] * _P["스케일링"]["patient_share_rate"]))
_WHITENING = _won(_P["미백"]["fee_total"])
_CLOSED = {3: "목요일", 6: "일요일"}
_OPEN, _CLOSE, _SAT = (
    _clock(HOURS["open"]),
    _clock(HOURS["weekday_close"]),
    _clock(HOURS["saturday_close"]),
)
_LUNCH = tuple(_clock(t) for t in HOURS["lunch"])
_CLOSED_DAYS = tuple(_CLOSED[d] for d in HOURS["closed_weekdays"])
_UNINSURED = tuple(n for n in ("레진", "인레이", "크라운", "미백") if _P[n]["is_insured"] == "no")
_CHILD_MAX = next(
    hi for name, _, hi, _ in AGE_BANDS if name == "child"
)  # 실란트 적용 연령 상한(소아 구간)


def F(fid, text, *words, reason=None, same=None) -> Fact:
    return Fact(fid, text, tuple(words), reason, same)


def _korean_date(iso: str) -> str:
    y, m, d = (int(x) for x in iso.split("-"))
    return f"{m}월 {d}일"


def _with_effective(d: Doc) -> Doc:
    """공지는 시행일도 꼭 넣을 말 — '언제 바뀌었어?'의 답"""
    if d.type != "notice":
        return d
    when = _korean_date(d.effective)
    return replace(d, facts=d.facts + (F(f"{d.key}.effective", f"{when}부터 시행한다", when),))


# 청구 기준 절 소제목 — 상세 사유 하나 = 절 하나 (⑤-3 정답)
CLAIM_HEADINGS = {
    "R01": "같은 부위 재시행 간격",
    "R02": "치석제거 뒤 치주치료 연결",
    "R03": "치주치료 산정 소견",
    "R04": "실란트 대상 치아",
    "R05": "발치 난이도 산정 근거",
    "R06": "임플란트 급여 대상 부위",
    "D01": "내역설명 기재",
    "D02": "방사선 등 증빙자료",
    "D03": "부위·산정 내역 기재",
    "C01": "상병과 시행 행위의 일치",
    "E01": "스케일링 연간 산정 횟수",
}
CLAIM_DOCS = {  # 분야 → 사유 (고시처럼 분야별 문서)
    "예방": ("R04", "E01"),
    "치주": ("R02", "R03"),
    "보존보철": ("R01",),
    "외과임플란트": ("R05", "R06"),
    "공통": ("D01", "D02", "D03", "C01"),
}
# 절에 넣을 기준 — 본문은 generator/claim_standards.py(실제 고시 발췌 또는 병원 내부 점검 기준), 꼭 넣을 말은 그 절 안 말.
# 사실 문장은 실제 절이면 인용 문단을 이은 것, 내부 기준 절이면 첫 기준 문장. 같은 사실 묶음은 master 값과 같은 말일 때만
# (실란트 나이는 원문에서 뺐다 — 설계 2026-11-13 "R04")
_SAME = {"E01": "scaling.age", "R06": "implant.age"}


def _claim_rule(r: str) -> Fact:
    if r in VIRTUAL:
        text, words = VIRTUAL[r][0], _VIRTUAL_WORDS[r]
    else:
        text, words = " ".join(REAL[r].quote), REAL[r].words
    return F(f"claim.{r}", text.rstrip("."), *words, reason=r, same=_SAME.get(r))


_VIRTUAL_WORDS = {
    "R01": ("다시 하는 사유",),
    "R02": (f"{PERIO_LINK_DAYS}일",),
    "R03": (f"{PERIO_POCKET_MIN_MM}mm", "출혈"),
}
_CLAIM_RULE = {r: _claim_rule(r) for r in CLAIM_HEADINGS}


def _claim_doc(field_name: str, reasons: tuple[str, ...]) -> Doc:
    facts = tuple(_CLAIM_RULE[r] for r in reasons)
    heads = tuple(
        Fact(f"claim.{r}.heading", f"절 '{CLAIM_HEADINGS[r]}'", (f"## {CLAIM_HEADINGS[r]}",), r)
        for r in reasons
    )
    return Doc(
        f"claim-{field_name}",
        "claim_standard",
        f"{field_name} 분야 청구 기준",
        f"{field_name} 분야 시술의 급여 청구 기준과 기록 요건",
        heads + facts,
        sections=reasons,
    )


_DOCS: tuple[Doc, ...] = (
    # ── 직원 매뉴얼 28 ──
    Doc(
        "manual-denture-repair",
        "manual",
        "틀니 수리 접수 절차",
        "틀니 파손 접수, 기공소 의뢰, 완료 연락",
        (
            F(
                "denture.days",
                "틀니 수리는 기공소 의뢰 후 영업일 기준 3일이 걸린다",
                "영업일 기준 3일",
            ),
            F("denture.loaner", "수리 기간에는 임시 틀니를 빌려주지 않는다", "임시 틀니"),
        ),
    ),
    Doc(
        "manual-implant-parts",
        "manual",
        "구형 임플란트 부품 주문",
        "부품 규격 확인, 주문, 입고 연락",
        (
            F(
                "parts.record",
                "부품 규격은 차트의 식립 기록(제조사·직경·길이)에서 확인한다",
                "식립 기록",
            ),
            F(
                "parts.weeks",
                "단종 부품은 대리점 재고 확인 후 주문하며 입고까지 2주가 걸린다",
                "2주",
            ),
        ),
    ),
    Doc(
        "manual-noshow",
        "manual",
        "예약 부도 관리",
        "연락 없이 오지 않은 환자 관리와 누적 시 조치",
        (
            F(
                "noshow.limit",
                "예약 부도가 3회 누적되면 3개월 동안 전화 예약만 받는다",
                "3회",
                "3개월",
                same="noshow",
            ),
            F(
                "noshow.call",
                "예약 시간이 지나도 오지 않으면 부도 안내 문자를 보낸다",
                "부도 안내 문자",
            ),
        ),
    ),
    Doc(
        "manual-new-patient",
        "manual",
        "신환 접수 절차",
        "첫 방문 환자 접수 순서",
        (
            F(
                "new.form",
                "문진표와 개인정보 동의서를 받고 건강보험 자격을 조회한다",
                "문진표",
                "건강보험 자격",
            ),
        ),
    ),
    Doc(
        "manual-insurance-docs",
        "manual",
        "실손보험 서류 발급",
        "진료비 영수증·세부내역서·진단서 발급",
        (
            F("docs.fee", "진료비 세부내역서는 무료, 진단서는 2만 원이다", "2만 원"),
            F("docs.id", "대리 발급은 위임장과 신분증 사본이 필요하다", "위임장과 신분증 사본"),
        ),
    ),
    Doc(
        "manual-sterilization",
        "manual",
        "기구 소독·멸균 관리",
        "기구 세척·포장·멸균·보관",
        (
            F("steril.pack", "멸균 포장한 기구는 30일 안에 사용하고 지나면 다시 멸균한다", "30일"),
            F(
                "steril.check",
                "멸균기는 매주 월요일 첫 가동 때 생물학적 표지자로 점검한다",
                "생물학적 표지자",
            ),
        ),
    ),
    Doc(
        "manual-emergency",
        "manual",
        "진료 중 응급 상황 대응",
        "실신·과호흡·출혈 대응과 응급 키트",
        (
            F(
                "emerg.kit",
                "응급 키트는 2번 진료실 수납장에 두고 매월 첫 진료일에 점검한다",
                "2번 진료실",
                "첫 진료일",
            ),
        ),
    ),
    Doc(
        "manual-payment-refund",
        "manual",
        "수납·환불 처리",
        "수납, 카드 취소, 환불 승인",
        (
            F(
                "refund.approve",
                "환불은 원장 승인 후 처리하고 카드 결제 취소는 결제한 카드로만 한다",
                "원장 승인",
            ),
            F("refund.cash", "현금으로 낸 진료비는 계좌로 돌려준다", "계좌로"),
        ),
    ),
    Doc(
        "manual-reminder",
        "manual",
        "예약 안내 문자 발송",
        "예약 확정·리마인드 문자 발송 기준",
        (
            F(
                "remind.time",
                f"리마인드 문자는 예약 직전 진료일 오전 {_OPEN}에 일괄 발송한다",
                _OPEN,
                same="reminder",
            ),
            F("remind.confirm", "예약을 잡으면 바로 예약 확정 문자를 보낸다", "확정 문자"),
        ),
    ),
    Doc(
        "manual-privacy",
        "manual",
        "전화 개인정보 확인",
        "전화로 진료 기록을 물을 때 본인 확인",
        (
            F(
                "privacy.check",
                "전화 문의는 생년월일과 등록된 전화번호로 본인을 확인한다",
                "생년월일",
                "등록된 전화번호",
            ),
            F(
                "privacy.family",
                "가족이 묻는 경우 보호자로 등록된 사람에게만 알려 준다",
                "보호자로 등록된",
            ),
        ),
    ),
    Doc(
        "manual-inventory",
        "manual",
        "재료·약품 재고 관리",
        "재고 점검과 발주",
        (
            F(
                "stock.day",
                "재고 점검은 매주 월요일 오전에 하고 기준 수량 아래면 발주한다",
                "월요일",
            ),
            F("stock.expiry", "마취제는 유효기간이 3개월 남은 것부터 먼저 쓴다", "유효기간"),
        ),
    ),
    Doc(
        "manual-daily-close",
        "manual",
        "일계표 마감",
        "진료 마감 후 하루 수납 정리",
        (
            F(
                "close.match",
                "진료 마감 후 카드 전표 합계와 현금 시재를 프로그램 수납과 맞춘다",
                "현금 시재",
            ),
            F(
                "close.report",
                "금액이 맞지 않으면 차이 내역을 일계표 비고란에 적고 원장에게 보고한다",
                "비고란",
            ),
        ),
    ),
    Doc(
        "manual-happy-call",
        "manual",
        "수술 후 경과 확인 전화",
        "발치·임플란트 수술 환자 경과 확인",
        (
            F(
                "happy.when",
                "진료 뒤 며칠 안에 전화하고, 발치·임플란트 수술 환자는 빠뜨리지 않는다",
                "며칠 안에",
            ),
            F("happy.memo", "통화 내용은 차트 메모에 남긴다", "차트 메모"),
        ),
    ),
    Doc(
        "manual-lab-work",
        "manual",
        "기공물 접수·발송",
        "기공소로 보내는 기공물과 돌아온 기공물 확인",
        (
            F("lab.pickup", "기공소 수거는 매일 오후 5시다", "오후 5시"),
            F("lab.check", "돌아온 기공물은 장착 예약 전날까지 도착을 확인한다", "장착 예약 전날"),
        ),
    ),
    Doc(
        "manual-consent",
        "manual",
        "수술 동의서 받기",
        "임플란트·사랑니 발치 전 동의서",
        (
            F(
                "consent.scan",
                "임플란트·사랑니 발치 전 동의서를 받고 서명본은 스캔해 차트에 첨부한다",
                "차트에 첨부",
            ),
        ),
    ),
    Doc(
        "manual-xray",
        "manual",
        "방사선 촬영 절차",
        "파노라마·치근단 촬영 전 확인",
        (
            F("xray.order", "파노라마는 원장 지시가 있을 때만 찍는다", "원장 지시"),
            F("xray.pregnant", "촬영 전 가임기 여성은 임신 여부를 먼저 확인한다", "임신 여부"),
        ),
    ),
    Doc(
        "manual-identity-check",
        "manual",
        "건강보험 본인확인",
        "접수 때 수진자 본인 확인",
        (
            F(
                "identity.id",
                "건강보험으로 진료하려면 접수 때 신분증이나 모바일 건강보험증으로 본인을 확인한다",
                "모바일 건강보험증",
            ),
        ),
    ),
    Doc(
        "manual-slot-length",
        "manual",
        "예약 시간 배정 규칙",
        "임플란트·신경치료·스케일링 예약 길이",
        (
            F(
                "slot.lengths",
                f"예약 길이는 임플란트 1차 {_P['임플란트 1차']['duration_min']}분, "
                f"신경치료 {_P['신경치료']['duration_min']}분, 스케일링 {_P['스케일링']['duration_min']}분이다",
                f"{_P['임플란트 1차']['duration_min']}분",
                f"{_P['신경치료']['duration_min']}분",
            ),
        ),
    ),
    Doc(
        "manual-unpaid",
        "manual",
        "미수금 안내",
        "남은 진료비 안내와 정산",
        (
            F(
                "unpaid.message",
                "미수가 생기면 필요한 경우 다음 진료일에 미수 안내 문자를 보내고 다음 내원 때 정산한다",
                "미수 안내 문자",
                "다음 내원 때 정산",
            ),
        ),
    ),
    Doc(
        "manual-medical-waste",
        "manual",
        "의료폐기물 처리",
        "손상성·일반 의료폐기물 분리와 수거",
        (
            F(
                "waste.sharps",
                "주삿바늘은 손상성 폐기물 전용 용기에 버리고 4분의 3이 차면 교체한다",
                "4분의 3",
            ),
            F("waste.pickup", "의료폐기물 수거는 매주 화요일이다", "화요일"),
        ),
    ),
    Doc(
        "manual-estimate",
        "manual",
        "비급여 견적 상담",
        "보철·미백 등 비급여 진료 견적",
        (
            F(
                "estimate.valid",
                "상담실장이 견적서를 출력해 주며 견적 유효기간은 1개월이다",
                "유효기간은 1개월",
            ),
        ),
    ),
    Doc(
        "manual-prosthesis-recement",
        "manual",
        "보철물 탈락·재부착 처리",
        "빠진 크라운 처리 기준",
        (F("recement.free", "크라운이 장착 후 1년 안에 빠지면 무상으로 다시 붙인다", "1년 안에"),),
    ),
    Doc(
        "manual-child-guardian",
        "manual",
        "소아 진료 보호자 동반",
        "소아 진료 때 보호자 동반과 동의",
        (F("child.guardian", "만 14세 미만은 보호자가 함께 오고 보호자 동의서를 받는다", "14세"),),
    ),
    Doc(
        "manual-prescription",
        "manual",
        "처방전 발행",
        "진료 후 약 처방",
        (
            F(
                "rx.outside",
                "처방은 원외 처방전으로 하고 항생제는 알레르기를 확인한 뒤 원장이 처방한다",
                "원외",
                "알레르기",
            ),
        ),
    ),
    Doc(
        "manual-waitlist",
        "manual",
        "예약 대기자 관리",
        "원하는 날이 찼을 때 대기 순서 관리",
        (
            F(
                "wait.order",
                "취소가 생기면 대기 명단 순서대로 전화하고 2시간 안에 답이 없으면 다음 사람에게 연락한다",
                "대기 명단",
                "2시간",
            ),
        ),
    ),
    Doc(
        "manual-record-copy",
        "manual",
        "진료 기록 사본·영상 반출",
        "진료 기록 사본·엑스레이 CD 발급",
        (
            F(
                "copy.ready",
                "본인 신청서와 신분증을 받고 다음 진료일에 준비해 준비 완료 문자를 보낸다",
                "준비 완료 문자",
            ),
            F("copy.proxy", "가족이 대신 신청하면 가족관계증명서가 필요하다", "가족관계증명서"),
        ),
    ),
    Doc(
        "manual-oral-care-sales",
        "manual",
        "구강용품 판매 안내",
        "데스크에서 파는 구강용품 가격과 판매 기록",
        (
            F(
                "oralcare.prices",
                "데스크 판매 가격은 "
                + ", ".join(f"{n} {_won(p)}" for n, p in ORAL_CARE_PRICES.items())
                + "이다",
                *(_won(p) for p in ORAL_CARE_PRICES.values()),
            ),
            F(
                "oralcare.record",
                "구강용품은 데스크 카드 단말기로 결제하고 판매 대장에 적는다 — 전자차트 수납에는 들어가지 않아"
                " 그날 카드 승인 합계가 수납보다 그만큼 많다",
                "판매 대장",
            ),
        ),
    ),
    Doc(
        "manual-plan-followup",
        "manual",
        "치료계획 후속 연락",
        "치료계획을 세우고 예약하지 않은 환자에게 연락",
        (
            F(
                "planfollow.call",
                "치료계획을 세우고 3주가 지나도 예약하지 않은 환자에게는 상담실장이 전화로 안내한다",
                "3주",
                "상담실장",
            ),
            F(
                "planfollow.log",
                "두 차례 연락해도 답이 없으면 차트에 연락 기록을 남기고 다음 리콜 때 다시 안내한다",
                "연락 기록",
            ),
        ),
    ),
    # ── FAQ 10 ──
    Doc(
        "faq-hours",
        "faq",
        "진료시간 안내",
        "진료 요일·시간·점심시간·휴진일",
        (
            F(
                "hours.weekday",
                f"평일 오전 {_OPEN}부터 오후 {_CLOSE}까지 진료한다",
                f"오전 {_OPEN}부터",
                f"오후 {_CLOSE}까지",
            ),
            F(
                "hours.sat",
                f"토요일은 오후 {_SAT}까지 진료하고, 평일 점심시간은 오후 {_LUNCH[0]}부터 {_LUNCH[1]}까지다",
                f"오후 {_SAT}까지",
                f"오후 {_LUNCH[0]}부터",
                same="hours.sat",
            ),
            F("hours.closed", f"{'·'.join(_CLOSED_DAYS)}과 공휴일은 휴진한다", *_CLOSED_DAYS),
        ),
    ),
    Doc(
        "faq-parking",
        "faq",
        "주차 안내",
        "건물 주차장 이용",
        (
            F(
                "parking.free",
                "건물 지하 주차장을 진료 당일 2시간 무료로 쓸 수 있다",
                "2시간",
                same="parking",
            ),
        ),
    ),
    Doc(
        "faq-scaling",
        "faq",
        "스케일링 주기",
        "스케일링을 얼마나 자주 받는지, 보험 적용",
        (
            F(
                "faq.scaling",
                f"{SCALING_MIN_AGE}세 이상은 해마다 1회 건강보험이 적용된다",
                f"{SCALING_MIN_AGE}세",
                same="scaling.age",
            ),
        ),
    ),
    Doc(
        "faq-implant-care",
        "faq",
        "임플란트 수술 후 관리",
        "수술 당일·한 주 동안 주의할 점",
        (
            F("implant.care", "수술 후 24시간 동안 빨대 사용과 흡연을 피한다", "24시간"),
            F("implant.cold", "수술 당일에는 냉찜질로 붓기를 줄인다", "냉찜질"),
        ),
    ),
    Doc(
        "faq-child-first",
        "faq",
        "아이 첫 치과 방문",
        "아이 첫 방문 시기와 예방 치료",
        (
            F(
                "child.sealant",
                f"만 {_CHILD_MAX}세 이하는 실란트에 건강보험이 적용된다",
                f"{_CHILD_MAX}세",
                same="sealant.age",
            ),
        ),
    ),
    Doc(
        "faq-whitening",
        "faq",
        "치아 미백 비용과 기간",
        "미백 비용과 효과 유지 기간",
        (
            F(
                "white.fee",
                f"미백은 {_WHITENING}이고 보험이 적용되지 않는다",
                _WHITENING,
                same="whitening.fee",
            ),
            F("white.last", "미백 효과는 보통 1~2년 유지된다", "1~2년"),
        ),
    ),
    Doc(
        "faq-root-canal",
        "faq",
        "신경치료 중 주의할 점",
        "신경치료 기간 동안 주의할 점",
        (
            F(
                "endo.temp",
                "치료 사이에 임시로 막아 둔 재료가 빠지면 바로 연락한다",
                "임시로 막아 둔",
            ),
        ),
    ),
    Doc(
        "faq-certificate",
        "faq",
        "진료확인서 발급",
        "환자가 직접 서류를 받는 방법",
        (
            F(
                "cert.where",
                "진료확인서는 접수대에서 신분증을 확인한 뒤 바로 발급한다",
                "바로 발급",
            ),
        ),
    ),
    Doc(
        "faq-booking",
        "faq",
        "예약 방법",
        "전화·방문 예약과 변경",
        (F("book.change", "예약 변경은 하루 전까지 전화로 알려 주면 된다", "하루 전"),),
    ),
    Doc(
        "faq-payment",
        "faq",
        "결제 방법",
        "쓸 수 있는 결제 수단",
        (F("pay.methods", "카드·현금·계좌이체로 결제할 수 있다", "계좌이체"),),
    ),
    # ── 보험 안내 6 ──
    Doc(
        "ins-scaling",
        "insurance_guide",
        "스케일링 건강보험 적용",
        "적용 나이·횟수·본인부담",
        (
            F(
                "ins.scaling.age",
                f"{SCALING_MIN_AGE}세 이상 연 1회 급여",
                f"{SCALING_MIN_AGE}세",
                "연 1회",
                same="scaling.age",
            ),
            F(
                "ins.scaling.pay",
                f"본인부담은 {_SCALING_SHARE}이다",
                _SCALING_SHARE,
                same="scaling.pay",
            ),
        ),
    ),
    Doc(
        "ins-implant",
        "insurance_guide",
        "임플란트 건강보험 적용",
        "적용 나이와 본인부담",
        (
            F(
                "ins.implant.age",
                f"만 {CONDITIONAL_INSURED_MIN_AGE}세 이상이면 급여가 적용된다",
                f"{CONDITIONAL_INSURED_MIN_AGE}세",
                same="implant.age",
            ),
            F("ins.implant.share", f"본인부담률은 {_SHARE}%다", f"{_SHARE}%", same="share"),
        ),
    ),
    Doc(
        "ins-sealant",
        "insurance_guide",
        "실란트 건강보험 적용",
        "적용 나이와 대상 치아",
        (
            F(
                "ins.sealant",
                f"만 {_CHILD_MAX}세 이하 우식 없는 어금니에 적용된다",
                f"{_CHILD_MAX}세",
                same="sealant.age",
            ),
        ),
    ),
    Doc(
        "ins-perio",
        "insurance_guide",
        "잇몸치료 건강보험 적용",
        "잇몸치료 급여와 연결 진료",
        (
            F(
                "ins.perio",
                f"잇몸치료는 급여이고 치석제거 후 {PERIO_LINK_DAYS}일 안에 이어서 받는 경우가 많다",
                f"{PERIO_LINK_DAYS}일",
            ),
        ),
    ),
    Doc(
        "ins-uninsured",
        "insurance_guide",
        "비급여 진료 안내",
        "보험이 적용되지 않는 진료",
        (
            F(
                "ins.uninsured",
                "레진·인레이·크라운·미백 등은 비급여다",
                *_UNINSURED,
            ),
        ),
        forbidden=("네 가지",),
    ),
    Doc(
        "ins-share",
        "insurance_guide",
        "급여 진료 본인부담",
        "급여 진료비 중 환자가 내는 몫",
        (F("ins.share", f"급여 진료의 본인부담률은 {_SHARE}%다", f"{_SHARE}%", same="share"),),
    ),
    # ── 청구 기준 5 ──
    *(_claim_doc(name, reasons) for name, reasons in CLAIM_DOCS.items()),
    # ── 원내 공지 12 (최신) ──
    Doc(
        "notice-chuseok",
        "notice",
        "추석 연휴 휴진 안내",
        "추석 연휴 휴진 날짜",
        effective="2026-09-09",
        recurring=True,
        facts=(F("chuseok.days", "9월 24일부터 9월 26일까지 휴진한다", "9월 24일", "9월 26일"),),
    ),
    Doc(
        "notice-seollal",
        "notice",
        "설 연휴 휴진 안내",
        "설 연휴 휴진 날짜",
        effective="2026-02-02",
        recurring=True,
        facts=(F("seollal.days", "2월 16일부터 2월 18일까지 휴진한다", "2월 16일", "2월 18일"),),
    ),
    Doc(
        "notice-hours",
        "notice",
        "토요일 진료시간 변경",
        "토요일 진료 종료 시각",
        effective="2023-10-30",
        emr=True,
        facts=(
            F(
                "notice.sat",
                f"토요일 진료는 오후 {_SAT}까지다",
                f"오후 {_SAT}까지",
                same="hours.sat",
            ),
        ),
    ),
    Doc(
        "notice-whitening",
        "notice",
        "미백 비용 변경",
        "미백 비용",
        effective="2023-10-30",
        emr=True,
        facts=(
            F("notice.white", f"미백 비용은 {_WHITENING}이다", _WHITENING, same="whitening.fee"),
        ),
    ),
    Doc(
        "notice-scaling-fee",
        "notice",
        "스케일링 본인부담 변경",
        "급여 스케일링 본인부담금",
        effective="2023-11-01",
        emr=True,
        facts=(
            F(
                "notice.scaling",
                f"스케일링 본인부담은 {_SCALING_SHARE}이다",
                _SCALING_SHARE,
                same="scaling.pay",
            ),
        ),
    ),
    Doc(
        "notice-parking",
        "notice",
        "주차 무료 시간 변경",
        "진료 당일 무료 주차 시간",
        effective="2026-06-01",
        facts=(F("notice.parking", "무료 주차는 2시간이다", "2시간", same="parking"),),
    ),
    Doc(
        "notice-reminder",
        "notice",
        "리마인드 문자 발송 시각 변경",
        "예약 리마인드 문자 발송 시각",
        effective="2023-10-30",
        emr=True,
        facts=(
            F(
                "notice.remind",
                f"리마인드 문자는 예약 직전 진료일 오전 {_OPEN}에 보낸다",
                _OPEN,
                same="reminder",
            ),
        ),
    ),
    Doc(
        "notice-noshow",
        "notice",
        "예약 부도 규정 변경",
        "부도 누적 시 조치",
        effective="2026-03-03",
        facts=(
            F(
                "notice.noshow",
                "부도 3회 누적 시 3개월간 전화 예약만 받는다",
                "3회",
                "3개월",
                same="noshow",
            ),
        ),
    ),
    Doc(
        "notice-sterile-log",
        "notice",
        "멸균 기록 마감 시각 변경",
        "멸균 기록지 마감 시각",
        effective="2026-08-03",
        facts=(
            F(
                "notice.sterile",
                "멸균 기록지는 그날 진료 종료 전까지 마감한다",
                "진료 종료 전까지",
            ),
        ),
    ),
    Doc(
        "notice-installment",
        "notice",
        "카드 무이자 할부 안내",
        "무이자 할부 개월 수",
        effective="2026-05-01",
        facts=(
            F(
                "notice.install",
                "50만 원 이상 결제는 3개월 무이자 할부가 된다",
                "50만 원",
                "3개월",
            ),
        ),
    ),
    Doc(
        "notice-implant-warranty",
        "notice",
        "임플란트 보증 기간 변경",
        "임플란트 보증 기간",
        effective="2026-07-01",
        facts=(F("notice.warranty", "임플란트 보증 기간은 5년이다", "5년"),),
    ),
    Doc(
        "notice-wifi",
        "notice",
        "대기실 와이파이 변경",
        "대기실 무선 인터넷 이름",
        effective="2026-04-06",
        facts=(F("notice.wifi", "대기실 와이파이 이름은 AXDENTAL-GUEST다", "AXDENTAL-GUEST"),),
    ),
    # ── 치과 상식 5 (심을 사실 없음) ──
    Doc("edu-brushing", "education", "올바른 칫솔질", "칫솔질 방법과 시간"),
    Doc("edu-gum", "education", "잇몸병 예방", "잇몸병의 원인과 예방"),
    Doc("edu-whitening", "education", "치아 미백 상식", "미백의 원리와 주의점"),
    Doc("edu-implant", "education", "임플란트란", "임플란트 구조와 치료 과정"),
    Doc("edu-wisdom", "education", "사랑니 이야기", "사랑니를 빼야 하는 경우"),
    # ── 방해: 공지 옛 버전 12 ──
    Doc(
        "old-chuseok",
        "notice",
        "추석 연휴 휴진 안내",
        "추석 연휴 휴진 날짜",
        kind="old_version",
        pair="notice-chuseok",
        effective="2024-09-02",
        facts=(F("old.chuseok", "9월 16일부터 9월 18일까지 휴진한다", "9월 16일", "9월 18일"),),
    ),
    Doc(
        "old-seollal",
        "notice",
        "설 연휴 휴진 안내",
        "설 연휴 휴진 날짜",
        kind="old_version",
        pair="notice-seollal",
        effective="2024-01-29",
        facts=(F("old.seollal", "2월 9일부터 2월 12일까지 휴진한다", "2월 9일", "2월 12일"),),
    ),
    Doc(
        "old-hours",
        "notice",
        "토요일 진료시간 변경",
        "토요일 진료 종료 시각",
        kind="old_version",
        pair="notice-hours",
        effective="2023-03-06",
        facts=(F("old.sat", "토요일 진료는 오후 2시까지다", "오후 2시까지"),),
    ),
    Doc(
        "old-whitening",
        "notice",
        "미백 비용 변경",
        "미백 비용",
        kind="old_version",
        pair="notice-whitening",
        effective="2023-01-02",
        facts=(F("old.white", "미백 비용은 250,000원이다", "250,000원"),),
    ),
    Doc(
        "old-scaling-fee",
        "notice",
        "스케일링 본인부담 변경",
        "급여 스케일링 본인부담금",
        kind="old_version",
        pair="notice-scaling-fee",
        effective="2023-01-01",
        facts=(F("old.scaling", "스케일링 본인부담은 17,000원이다", "17,000원"),),
    ),
    Doc(
        "old-parking",
        "notice",
        "주차 무료 시간 변경",
        "진료 당일 무료 주차 시간",
        kind="old_version",
        pair="notice-parking",
        effective="2024-06-03",
        facts=(F("old.parking", "무료 주차는 1시간이다", "1시간"),),
    ),
    Doc(
        "old-reminder",
        "notice",
        "리마인드 문자 발송 시각 변경",
        "예약 리마인드 문자 발송 시각",
        kind="old_version",
        pair="notice-reminder",
        effective="2023-04-03",
        facts=(F("old.remind", "리마인드 문자는 예약 전날 오후 6시에 보낸다", "전날 오후 6시"),),
    ),
    Doc(
        "old-noshow",
        "notice",
        "예약 부도 규정 변경",
        "부도 누적 시 조치",
        kind="old_version",
        pair="notice-noshow",
        effective="2024-03-04",
        facts=(F("old.noshow", "부도 2회 누적 시 1개월간 전화 예약만 받는다", "2회", "1개월"),),
    ),
    Doc(
        "old-sterile-log",
        "notice",
        "멸균 기록 마감 시각 변경",
        "멸균 기록지 마감 시각",
        kind="old_version",
        pair="notice-sterile-log",
        effective="2024-08-05",
        facts=(
            F("old.sterile", "멸균 기록지는 다음 날 오전 10시까지 마감한다", "다음 날 오전 10시"),
        ),
    ),
    Doc(
        "old-installment",
        "notice",
        "카드 무이자 할부 안내",
        "무이자 할부 개월 수",
        kind="old_version",
        pair="notice-installment",
        effective="2024-05-03",
        facts=(
            F("old.install", "100만 원 이상 결제는 2개월 무이자 할부가 된다", "100만 원", "2개월"),
        ),
    ),
    Doc(
        "old-implant-warranty",
        "notice",
        "임플란트 보증 기간 변경",
        "임플란트 보증 기간",
        kind="old_version",
        pair="notice-implant-warranty",
        effective="2024-07-01",
        facts=(F("old.warranty", "임플란트 보증 기간은 3년이다", "3년"),),
    ),
    Doc(
        "old-wifi",
        "notice",
        "대기실 와이파이 변경",
        "대기실 무선 인터넷 이름",
        kind="old_version",
        pair="notice-wifi",
        effective="2024-04-08",
        facts=(F("old.wifi", "대기실 와이파이 이름은 AX-WAITING이다", "AX-WAITING"),),
    ),
    # ── 방해: 비슷한 주제 10 (짝의 사실은 쓰지 않음) ──
    Doc(
        "sim-booking-basics",
        "manual",
        "예약 접수 기본",
        "전화·방문 예약을 받는 순서",
        kind="similar",
        pair="manual-noshow",
        skip="부도가 쌓였을 때의 조치와 부도 안내 문자",
        avoid=("faq-booking", "manual-reminder", "manual-waitlist", "manual-slot-length"),
    ),
    Doc(
        "sim-denture-care",
        "manual",
        "틀니 관리 안내",
        "환자에게 틀니 세척·보관을 안내하는 법",
        kind="similar",
        pair="manual-denture-repair",
        skip="틀니 수리 접수와 수리 기간",
        avoid=("manual-lab-work",),
    ),
    Doc(
        "sim-implant-stock",
        "manual",
        "임플란트 재료 재고 목록",
        "임플란트 재료 보관과 목록 정리",
        kind="similar",
        pair="manual-implant-parts",
        skip="부품 규격 확인과 주문·입고 기간",
        avoid=("manual-inventory",),
    ),
    Doc(
        "sim-cleaning",
        "manual",
        "진료실 청소 점검표",
        "진료실 표면 소독과 청소 순서",
        kind="similar",
        pair="manual-sterilization",
        skip="기구 멸균 포장의 사용 기한과 멸균기 점검",
        avoid=("manual-medical-waste",),
    ),
    Doc(
        "sim-insurer-call",
        "manual",
        "보험사 문의 응대",
        "보험사 직원 전화 응대 순서",
        kind="similar",
        pair="manual-insurance-docs",
        skip="실손 서류 발급 비용과 대리 발급",
        avoid=("manual-record-copy", "faq-certificate"),
    ),
    Doc(
        "sim-directions",
        "faq",
        "오시는 길",
        "대중교통·도보 안내",
        kind="similar",
        pair="faq-parking",
        skip="주차",
        avoid=("faq-hours",),
    ),
    Doc(
        "sim-whitening-after",
        "faq",
        "미백 후 주의사항",
        "미백 뒤 음식·통증 관리",
        kind="similar",
        pair="faq-whitening",
        skip="미백 비용과 효과 유지 기간",
        avoid=("notice-whitening",),
    ),
    Doc(
        "sim-endo-pain",
        "faq",
        "신경치료 후 통증",
        "신경치료 뒤 통증 관리",
        kind="similar",
        pair="faq-root-canal",
        skip="치료 사이 임시 재료가 빠졌을 때",
        avoid=("manual-prescription",),
    ),
    Doc(
        "sim-health-insurance",
        "insurance_guide",
        "건강보험 일반 안내",
        "건강보험 자격과 진료비 구조",
        kind="similar",
        pair="ins-scaling",
        skip="스케일링 급여 나이·횟수·본인부담금",
        avoid=(
            "ins-share",
            "ins-implant",
            "ins-sealant",
            "ins-perio",
            "manual-identity-check",
            "faq-scaling",
        ),
    ),
    Doc(
        "sim-emergency-kit",
        "manual",
        "응급 키트 구성품",
        "응급 키트에 들어 있는 물품",
        kind="similar",
        pair="manual-emergency",
        skip="응급 키트 보관 위치와 점검 주기",
    ),
    # ── 방해: 타 병원·일반 5 (같은 주제를 다른 값으로) ──
    Doc(
        "ext-noshow",
        "external",
        "다른 치과의 예약 부도 규정 예시",
        "부도 누적 시 조치",
        kind="external",
        pair="manual-noshow",
        facts=(F("ext.noshow", "부도 2회 누적 시 6개월간 예약을 받지 않는다", "2회", "6개월"),),
    ),
    Doc(
        "ext-hours",
        "external",
        "일반 치과 진료시간 예시",
        "진료시간 구성",
        kind="external",
        pair="faq-hours",
        facts=(
            F(
                "ext.hours",
                "평일 오후 7시까지, 토요일 오후 3시까지 진료한다",
                "오후 7시까지",
                "오후 3시까지",
            ),
        ),
    ),
    Doc(
        "ext-whitening",
        "external",
        "타 치과 미백 가격표",
        "미백 비용",
        kind="external",
        pair="faq-whitening",
        facts=(F("ext.white", "미백은 350,000원이다", "350,000원"),),
    ),
    Doc(
        "ext-insurance-claim",
        "external",
        "실손보험 청구 일반 안내",
        "보험사에 내는 서류",
        kind="external",
        pair="manual-insurance-docs",
        facts=(F("ext.claim", "진단서 발급 비용은 1만 원이다", "1만 원"),),
    ),
    Doc(
        "ext-sterilization",
        "external",
        "치과 감염관리 일반 지침",
        "멸균 기구 보관 기한",
        kind="external",
        pair="manual-sterilization",
        facts=(F("ext.steril", "멸균 포장 기구는 7일 안에 사용한다", "7일"),),
    ),
)

CATALOG: tuple[Doc, ...] = tuple(_with_effective(d) for d in _DOCS)
BY_KEY = {d.key: d for d in CATALOG}
