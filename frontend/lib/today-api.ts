// 오늘 화면 API 타입과 호출 — 응답 모양의 원본은 backend/today.py sheet(기능 정의 §4 오늘 화면)
import { getJson, postJson } from "@/lib/api";
import type { LabelNames } from "@/lib/calls-api";

export type HumanFlag = "emergency" | "urgent" | "complaint" | "has_todo";

/** 확인할 통화 — ai: closed 사람이 다시 연 자동 종결 · none AI 할 일 없음 · old_todo 할 일은 있지만 7일 창 밖 */
export interface Review {
  ai: "closed" | "none" | "old_todo";
  reopened: boolean;
  flags: HumanFlag[];
}

export interface ReviewCall extends Review {
  call_uid: string;
  name: string | null;
  at: string;
}

export interface TodaySheet {
  date: string;
  cards: {
    appointments: {
      total: number;
      treatment: number;
      hygiene: number;
      consult: number;
    };
    new_patients: number;
    alert_patients: number;
    todo: { items: number; calls: number; urgent_items: number };
  };
  people: {
    urgent_calls: {
      call_uid: string;
      name: string | null;
      text: string;
      at: string;
      emergency: boolean;
    }[];
    review_calls: ReviewCall[];
    unconfirmed: number;
    alert_patients: number;
    todo_left: { items: number; calls: number };
    judge_pending: number;
    refused: {
      id: number;
      kind: "chat" | "search_summary";
      question: string;
      at: string;
    }[];
  };
  ai: {
    autoclose_on: boolean;
    auto_total: number;
    auto_recent: {
      id: number;
      call_uid: string;
      name: string | null;
      reason: string;
      at: string;
    }[];
    prep: { rows: number; suggest: number; failed: number };
    classify_failed: number;
    followup_failed: number;
  };
}

export const fetchToday = (signal?: AbortSignal) =>
  getJson<TodaySheet>("/today", undefined, signal);

export const checkCall = (uid: string, part: string) =>
  postJson(`/calls/${encodeURIComponent(uid)}/check`, { part });

const AI_TEXT: Record<Review["ai"], string> = {
  closed: "종결",
  none: "할 일 없음",
  old_todo: "할 일 있음(지난 통화)",
};

/** 확인할 통화 문구 — "AI: …, 사람…: …". 사람 판정이 있으면 그쪽(둘 다 해당해도 한 줄), 항목 이름은 GET /labels */
export function reviewText(r: Review, names: LabelNames | null): string {
  const ai = `AI: ${AI_TEXT[r.ai]}`;
  if (!r.flags.length) return `${ai}, 사람: 다시 열림`;
  if (!names) return `${ai}, 사람 판정: …`; // 라벨 이름을 불러오기 전 — 빈 이름을 보이지 않는다
  const name = (f: HumanFlag) =>
    (f === "urgent" ? names?.urgency.urgent : names?.flag[f]) ?? "";
  return `${ai}, 사람 판정: ${r.flags.map(name).join("·")}`;
}

/** 2026-11-28 10:00:00 → 11/28 */
export const monthDay = (at: string) => `${at.slice(5, 7)}/${at.slice(8, 10)}`;
