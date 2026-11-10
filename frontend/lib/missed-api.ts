// ⑨ 미예약 환자 API 타입과 호출 — 응답 모양의 원본은 backend/missed.py sheet
// (설계 docs/superpowers/specs/2026-11-10-missed-patients-design.md §2·§4·§6)
import { getJson, postJson } from "@/lib/api";

export type Chip =
  | "recall"
  | "scaling_unused"
  | "noshow"
  | "balance"
  | "open_plan"
  | "other_clinic";

/** 칩 이름(설계 §2 표 그대로) — 순서 = 화면 순서(backend/missed.py CHIPS) */
export const CHIP_LABEL: Record<Chip, string> = {
  recall: "리콜 연체",
  scaling_unused: "올해 스케일링 급여 미사용",
  noshow: "노쇼 뒤 재예약 없음",
  balance: "미수 잔액",
  open_plan: "진료계획 미예약",
  other_clinic: "다른 치과 추정",
};
export const CHIPS = Object.keys(CHIP_LABEL) as Chip[];

export type Result =
  "booked" | "no_answer" | "later" | "declined" | "other_clinic";

/** 연락 결과(설계 §4, 사용자 결정 문구) — backend/missed.py RESULTS */
export const RESULT_LABEL: Record<Result, string> = {
  booked: "예약함",
  no_answer: "부재중·안 받음",
  later: "나중에 다시",
  declined: "안 오겠다고 함",
  other_clinic: "다른 치과 다님",
};
export const RESULTS = Object.keys(RESULT_LABEL) as Result[];
/** 남기면 목록에서 빠지는 결과 — 남기기 전에 한 번 더 묻는다(backend/missed.py _removed) */
export const REMOVES: Result[] = ["booked", "declined", "other_clinic"];

export type Channel = "call" | "text";
export const CHANNEL_LABEL: Record<Channel, string> = {
  call: "전화",
  text: "문자",
};

export interface Contact {
  result: Result;
  channel: Channel;
  by: string;
  at: string;
  /** 연락 효과(감점 기간)가 아직 있음 — 연락함 숨기기가 이것만 숨긴다(backend/missed.py _active) */
  active: boolean;
}

export interface MissedRow {
  patient_id: number;
  chart_no: string;
  name: string;
  age: number;
  phone: string;
  last_visit: string;
  last_hygiene: string | null;
  balance: number;
  chips: Chip[];
  signals: { months_since_visit: number };
  score: number;
  rank: number;
  contact: Contact | null;
}

export interface MissedSheet {
  date: string;
  /** 처음 몇 명·더보기 한 번에 더 보이는 수(backend/missed.py PAGE) */
  page: number;
  chip_counts: Record<Chip, number>;
  rows: MissedRow[];
  /** "순위는 어떻게 정해지나요?" — 지금 값으로 쓴 규칙과 값마다 이유(backend/missed.py rules) */
  rules: { name: string; value: string; reasons: string[] }[];
}

export const fetchMissed = (signal?: AbortSignal) =>
  getJson<MissedSheet>("/missed", undefined, signal);

export const recordContact = (
  patientId: number,
  result: Result,
  channel: Channel,
  part: string,
) =>
  postJson<{ ok: true }>(`/missed/${patientId}/contacts`, {
    result,
    channel,
    part,
  });
