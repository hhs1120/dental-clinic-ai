// 오늘 화면 API 타입과 호출 — 응답 모양의 원본은 backend/today.py sheet(기능 정의 §4 오늘 화면)
import type { Cause } from "@/lib/finance-api";
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
    /** 재심 신청 기한 30일 이내(상병 착오 제외) — /claims soon 칩과 같은 함수 */
    appeal_soon: number;
    /** 청구 전 상병 확인 경고 줄 — /claims?tab=pre와 같은 함수 */
    preclaim_warning: number;
    /** 청구 전 요건 근거 못 찾음 줄 수(backend/claims.py counts.ai_missing — 처치 누락만 있는 줄은 뺌) */
    preclaim_ai: number;
    /** 확정 안 된 일계표 불일치 건 — /finance와 같은 함수(backend/recon.py open_count) */
    recon_open: number;
    /** 미확정 건이 있는 가장 이른 달 — 줄 링크가 그 달로 간다 */
    recon_month: string | null;
    /** 지난달 출금 중 증빙 못 찾음·과목 확인 필요·규칙과 다름이면서 확정·자동 확정 안 된 수 — /finance 지출 증빙과 같은 함수(backend/expense.py open_summary) */
    expense_open: number;
    /** 줄 링크가 가는 달(기준일의 지난달), 0건이면 null */
    expense_month: string | null;
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
    /** 일계표 자동 마감(backend/today.py _recon_auto) — 정책 꺼짐이면 on false */
    recon_auto: {
      on: boolean;
      total: number;
      recent: {
        id: number;
        key: string;
        date: string;
        cause: Cause;
        at: string;
      }[];
    };
    /** 지출 증빙 자동 확정(backend/today.py _expense_auto) — 정책 꺼짐이면 on false */
    expense_auto: {
      on: boolean;
      total: number;
      recent: {
        id: number;
        key: string;
        at: string;
        label: string;
        amount: number;
        account: string;
      }[];
    };
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
