"""후속 처리 종류 8가지 — 후속 에이전트(backend/followup.py)와 처리 정답 라벨(evaluation/call_actions.py)이 같은 뜻을
읽는다(설계 docs/superpowers/specs/2026-11-08-followup-autoclose-design.md "PR ②")."""

ACTIONS = (
    "appointment",
    "doctor_reply",
    "doctor_note",
    "billing",
    "document",
    "insurance_cost",
    "scaling_timing",
    "other",
)
ACTION_KO = {
    "appointment": "예약 잡기·변경",
    "doctor_reply": "원장님 확인 후 회신",
    "doctor_note": "원장님께 전달",
    "billing": "수납·미수 확인",
    "document": "서류 준비",
    "insurance_cost": "보험·비용 안내",
    "scaling_timing": "스케일링 시기 안내",
    "other": "기타(사람 판단)",
}
ACTION_DESC = {
    "appointment": "예약 날짜·시간을 정하거나 옮기거나 취소를 처리해 알리는 일",
    "doctor_reply": "증상·치료·준비물·소요 시간처럼 원장님(진료실) 확인이 필요한 내용을 여쭙고 환자에게 회신하는 일",
    "doctor_note": "환자 상황·요청을 원장님(진료실)께 미리 알려 두는 일 — 환자에게 따로 회신하지 않는다(응급 내원 전 전달 등)",
    "billing": "이미 받은 치료의 결제·미납·남은 금액·영수 내역을 확인해 알리는 일",
    "document": "진단서·소견서·영수증·기록 사본 같은 서류를 준비하거나 보내는 일",
    "insurance_cost": "앞으로 받을 치료의 비용이나 보험(실손 포함) 적용 여부를 확인해 알리는 일",
    "scaling_timing": "스케일링을 언제 받을 수 있는지, 건강보험 적용 시기가 언제인지 확인해 알리는 일",
    "other": "위 어디에도 맞지 않는 일(불만 전달·업체 연락 등)",
}
