// ⑥ 경영 리포트 API 타입과 호출 — 응답 모양의 원본은 backend/report.py sheet (설계 2026-11-13 리포트 §5)
import { getJson } from "@/lib/api";

export type Scope = "monthly" | "weekly" | "daily";

export const SCOPE_LABEL: Record<Scope, string> = {
  monthly: "월간",
  weekly: "주간",
  daily: "일간",
};

export interface Point {
  text: string;
  used: string[];
}

export interface Interpretation {
  points: Point[];
  values: Record<string, number | null>; // 꼬리표 값 — 해석을 만든 때의 숫자
  model: string;
  created_at: string;
  stale: boolean; // 해석을 만든 뒤 숫자가 바뀜(⑧ 확정 등)
}

export type Metrics = Record<string, number>;

export interface ReportSheet {
  scope: Scope;
  periods: string[];
  period: string;
  prev_period: string | null;
  metrics: Metrics;
  interpretation: Interpretation | null;
  trend: ({ period: string } & Record<string, number | null>)[];
  dx_top: { name: string; n: number; lost: number } | null;
}

export const fetchReport = (
  scope: Scope,
  period: string | null,
  signal?: AbortSignal,
) =>
  getJson<ReportSheet>(
    "/report",
    new URLSearchParams({ scope, ...(period ? { period } : {}) }),
    signal,
  );

// ── 근거 꼬리표 — metrics 키를 한국어 이름·단위로(키 규칙은 backend/report.py metrics — 키를 더하면 BASE도) ──
type Unit = "원" | "건" | "명" | "일" | "%" | "";

const BASE: Record<string, [string, Unit]> = {
  sales: ["매출", "원"],
  collected: ["수금", "원"],
  patient_paid: ["환자 수납", "원"],
  insurer_paid: ["공단 지급", "원"],
  claimed: ["청구액", "원"],
  visits: ["예약(취소 제외)", "건"],
  noshow: ["노쇼", "건"],
  noshow_rate: ["노쇼율", "%"],
  new_patients: ["신환", "명"],
  treatment_days: ["진료일", "일"],
  material: ["재료비", "원"],
  lab: ["기공비", "원"],
  material_unconfirmed: ["재료비·기공비 미확정", "건"],
  account_none: ["과목 없는 출금", "건"],
  account_none_amount: ["과목 없는 출금액", "원"],
  outflow: ["출금 합", "원"],
  profit_ex_labor: ["인건비 제외 손익", "원"],
  dx_errors: ["상병 착오", "건"],
  dx_lost: ["상병 착오 못 받은 금액", "원"],
};

// 시술 분류(emr_procedures.category) — sales.<분류>
const CATEGORY: Record<string, string> = {
  preventive: "예방",
  restorative: "보존",
  endo: "신경",
  prosth: "보철",
  surgery: "외과",
  implant: "임플란트",
  ortho: "교정",
  cosmetic: "미용",
  perio: "잇몸",
  consult: "상담",
};

const PREFIXES = ["prev.", "diff.", "pct."] as const;

function split(key: string) {
  const prefix = PREFIXES.find((p) => key.startsWith(p)) ?? null;
  const base = prefix ? key.slice(prefix.length) : key;
  const cat = base.startsWith("sales.") ? base.slice(6) : null;
  const [name, unit]: [string, Unit] = cat
    ? [`${CATEGORY[cat] ?? cat} 매출`, "원"]
    : (BASE[base] ?? [base, ""]); // 모르는 키(옛 버전 행 등)는 단위를 짐작하지 않는다
  return { prefix, name, unit };
}

/** 꼬리표 한 줄 — "임플란트 매출 증감 -1,300,000원", "매출 증감률 -10%", "이전 노쇼 0건", "노쇼율 증감 -2%p" */
export function metricTag(key: string, value: number | null): string {
  const { prefix, name, unit } = split(key);
  const label =
    prefix === "prev."
      ? `이전 ${name}`
      : prefix === "diff."
        ? `${name} 증감`
        : prefix === "pct."
          ? `${name} 증감률`
          : name;
  if (value === null) return `${label} —`;
  const signed = prefix === "diff." || prefix === "pct.";
  const sign = signed && value > 0 ? "+" : "";
  const num = value.toLocaleString("ko-KR");
  const tail =
    prefix === "pct." ? "%" : prefix === "diff." && unit === "%" ? "%p" : unit;
  return `${label} ${sign}${num}${tail}`;
}
