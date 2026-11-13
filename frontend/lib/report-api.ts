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
  labor: {
    wage: Record<string, number>;
    headcount: Record<string, number>;
  } | null;
  dx_top: { name: string; n: number; lost: number } | null;
}

/** 인건비 가정값이 들어간 [추정] 키 — backend/report.py is_est와 같은 판별 */
export const isEst = (key: string) => key.includes("est.");

export const ROLE_LABEL: Record<string, string> = {
  hygienist: "위생사",
  desk: "데스크",
  consultant: "상담실장",
};

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
