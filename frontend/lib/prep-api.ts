// ④ 진료 준비표 API 타입과 호출 — 응답 모양의 원본은 backend/prep.py sheet (설계 2026-11-10 준비표 §4)
import { getJson, postJson } from "@/lib/api";

export type Chip =
  | "appointment"
  | "insurance"
  | "balance"
  | "family_recall"
  | "alert"
  | "open_plan"
  | "unconfirmed";

export const CHIP_LABEL: Record<Chip, string> = {
  appointment: "예약",
  insurance: "보험 잔여",
  balance: "미수 잔액",
  family_recall: "가족 리콜 지남",
  alert: "주의",
  open_plan: "남은 치료계획",
  unconfirmed: "예약 미확인",
};
export const CHIPS = Object.keys(CHIP_LABEL) as Chip[];

export interface Suggestion {
  action: "suggest" | "none";
  text: string;
  used_facts: { key: string; value: string }[];
}

export interface PrepRow {
  appointment_id: number;
  patient_id: number;
  time: string;
  duration_min: number;
  chair_no: number | null;
  type: "treatment" | "hygiene" | "consult";
  procedure: string;
  procedure_code: string;
  staff: string;
  status: "scheduled" | "completed" | "noshow";
  confirmed: boolean;
  patient: {
    name: string;
    chart_no: string;
    age: number;
    sex: "M" | "F";
    new_patient: boolean;
  };
  last_visit: string | null;
  balance: number;
  alerts: { code: string; label: string; related: boolean; action: string }[];
  scaling_unused: boolean;
  family_recall: { chart_no: string; name: string; last_hygiene: string }[];
  open_plan: { procedure: string; tooth: string; est_fee: number }[];
  chips: Chip[];
  generated: boolean;
  suggestion: Suggestion | null;
  suggestion_valid: boolean;
  stale: boolean;
}

export interface PrepSheet {
  date: string;
  generated_at: string | null;
  days: { date: string; count: number }[];
  rows: PrepRow[];
}

export interface Job {
  id: number;
  status: "running" | "done" | "failed";
  error: string | null;
}

export const TYPE_LABEL = {
  treatment: "진료",
  hygiene: "위생",
  consult: "상담",
};

export const fetchPrep = (date: string | undefined, signal?: AbortSignal) =>
  getJson<PrepSheet>(
    "/prep",
    date ? new URLSearchParams({ date }) : undefined,
    signal,
  );

export const regenerate = (date: string) =>
  postJson<{ job_id: number }>(
    `/jobs/prep-regenerate?${new URLSearchParams({ date })}`,
  );

export const fetchJob = (id: number) => getJson<Job>(`/jobs/${id}`);

/** 할 일 있는 환자 = 통과한 제안이 suggest */
export const hasTodo = (r: PrepRow) => r.suggestion?.action === "suggest";

export const won = (n: number) => `${n.toLocaleString("ko-KR")}원`;
