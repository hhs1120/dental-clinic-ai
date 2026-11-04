// 따라 보기 네 단계 — 진료 전에 데스크가 확인하는 것과 그것이 전환 전 EMR의 어디에 있는지(전환 전 EMR·일별 예약표 설계안)
import type { EmrNav, Highlight } from "@/components/emr/emr-window";
import { type PatientRecord, won } from "@/lib/emr-api";

export type StepKey = "balance" | "alert" | "remaining" | "confirm";
export type TourStep = {
  key: StepKey;
  what: string;
  where: string;
  nav: EmrNav;
  highlight: Exclude<Highlight, null>;
};

export const TOUR_STEPS: TourStep[] = [
  {
    key: "balance",
    what: "미수 잔액",
    where: "데스크 › 당일 수납",
    nav: { mode: "desk", tab: "진료기록", panel: "기본" },
    highlight: "balance",
  },
  {
    key: "alert",
    what: "주의사항",
    where: "전자차트 › 맨 위 Alert",
    nav: { mode: "chart", tab: "진료기록", panel: "기본" },
    highlight: "alert",
  },
  {
    key: "remaining",
    what: "남은 치료",
    where: "전자차트 › 옆 패널 계획",
    nav: { mode: "chart", tab: "진료기록", panel: "계획" },
    highlight: "plan",
  },
  {
    key: "confirm",
    what: "예약 확인 문자 응답",
    where: "데스크 › 히스토리 문자",
    nav: { mode: "desk", tab: "문자", panel: "계획" },
    highlight: "messages",
  },
];

/** 주의사항 종류별 이유 — 이 순서가 우선순위(주의사항이 여럿이면 앞의 것) */
const ALERT_WHY: [string, string][] = [
  [
    "ANTICOAG",
    "항응고제를 먹는 환자는 피가 잘 멈추지 않아, 진료 전에 원장님께 알려야 합니다.",
  ],
  ["PENICILLIN", "페니실린 알레르기가 있으면 처방 약을 바꿔야 합니다."],
  ["LATEX", "라텍스 알레르기가 있으면 장갑과 기구를 바꿔야 합니다."],
  ["EPI", "에피네프린에 민감하면 마취약을 바꿔야 합니다."],
  [
    "PROSTHESIS",
    "인공관절·인공판막이 있으면 시술 전 예방 항생제가 필요할 수 있습니다.",
  ],
  ["OSTEO", "골다공증 약을 먹으면 발치·임플란트 전에 확인이 필요합니다."],
  ["PREGNANT", "임신 중이면 X-ray와 약을 조심해야 합니다."],
  ["SYNCOPE", "마취 후 실신한 적이 있어 체어 옆에서 지켜봐야 합니다."],
  ["HTN", "혈압이 높으면 마취 전에 혈압을 잽니다."],
  ["DM", "당뇨가 있으면 상처가 늦게 낫고 감염을 조심해야 합니다."],
  ["UPRIGHT", "체어를 눕히지 말고 진료해야 합니다."],
];
const ALERT_WHY_DEFAULT = "진료 전에 미리 알아 두어야 하는 주의사항입니다.";
const MISSING_WHY = "없어도 확인하려면 여기까지 와야 합니다.";

function topAlert(record: PatientRecord) {
  const rank = (code: string) => {
    const i = ALERT_WHY.findIndex(([c]) => c === code);
    return i < 0 ? ALERT_WHY.length : i;
  };
  return (
    [...record.summary.alerts].sort((a, b) => rank(a.code) - rank(b.code))[0] ??
    null
  );
}

function confirmState(
  record: PatientRecord,
  apptId: number,
): "확인됨" | "무응답" | null {
  const appt = record.appointments.find((a) => a.id === apptId);
  const linked = record.messages.filter(
    (m) => m.related_appointment_id === apptId,
  );
  if (!linked.some((m) => m.trigger === "appt_confirm")) return null; // 확인 문자 자체가 없음
  return appt?.confirmed || linked.some((m) => m.trigger === "reply")
    ? "확인됨"
    : "무응답";
}

export function stepValue(
  step: TourStep,
  record: PatientRecord,
  apptId: number,
): { found: boolean; text: string } {
  if (step.key === "balance") {
    const b = record.summary.balance;
    return b > 0
      ? { found: true, text: won(b) }
      : { found: false, text: "미수 없음" };
  }
  if (step.key === "alert") {
    const a = topAlert(record);
    const more = record.summary.alerts.length - 1;
    return a
      ? { found: true, text: a.label + (more > 0 ? ` 외 ${more}건` : "") }
      : { found: false, text: "주의사항 없음" };
  }
  if (step.key === "remaining") {
    const todayItem =
      record.appointments.find((a) => a.id === apptId)?.plan_item_id ?? null;
    const open = record.plans.filter(
      (x) => x.status === "open" && x.id !== todayItem,
    );
    if (!open.length) return { found: false, text: "남은 치료 없음" };
    const first = open[0];
    const tooth = first.tooth !== "00" ? ` #${first.tooth}` : "";
    return {
      found: true,
      text: `${first.procedure}${tooth}${open.length > 1 ? ` 외 ${open.length - 1}건` : ""}`,
    };
  }
  const state = confirmState(record, apptId);
  return state
    ? { found: true, text: state }
    : { found: false, text: "확인 문자 없음" };
}

export function stepWhy(
  step: TourStep,
  record: PatientRecord,
  apptId: number,
): string {
  const { found, text } = stepValue(step, record, apptId);
  if (!found) return MISSING_WHY;
  if (step.key === "balance")
    return "오늘 수납할 때 함께 안내해야 합니다. 이 금액은 데스크 화면에만 있습니다.";
  if (step.key === "alert") {
    const code = topAlert(record)?.code;
    const why = ALERT_WHY.find(([c]) => c === code)?.[1] ?? ALERT_WHY_DEFAULT;
    return `${why} 데스크 화면에는 없어 전자차트로 넘어왔습니다.`;
  }
  if (step.key === "remaining")
    return "오늘 진료가 끝나면 다음 회차를 잡아야 합니다. 남은 치료는 차트 옆 계획 탭에만 있습니다.";
  return text === "무응답"
    ? "확인 문자에 답이 없었습니다. 노쇼일 수 있어 진료 전에 전화를 한 번 더 겁니다."
    : "확인 문자에 답했습니다. 문자 기록은 데스크의 다른 탭에 있어 또 옮겨 왔습니다.";
}
