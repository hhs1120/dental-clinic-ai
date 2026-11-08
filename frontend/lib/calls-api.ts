// 통화 화면 API — 응답 모양의 원본은 backend/calls.py·backend/api.py (통화·② 판정 화면 설계 "API")
import { getJson, postJson, useFetch } from "@/lib/api";

export type Direction = "inbound" | "outbound";
export type TodoKind = "contact" | "internal";

export interface TodoItem {
  kind: TodoKind;
  text: string;
  who: string;
  after: number; // 먼저 끝내야 할 안쪽 일 번호, 없으면 -1
  item: number;
  done: boolean;
  done_by: string | null;
  done_at: string | null;
}

export interface BoardItem extends TodoItem {
  call_uid: string;
  classification_id: number;
  at: string;
  name: string | null;
  chart_no: string | null;
  urgent: boolean;
  emergency: boolean;
}

export interface Board {
  internal: BoardItem[];
  contact: BoardItem[];
  done: BoardItem[];
}

export type Labels = Record<string, string | number | boolean>;

export interface CallCard {
  call_uid: string;
  at: string;
  direction: Direction;
  name: string | null;
  chart_no: string | null;
  phone: string | null;
  source: "generated" | "pasted";
  status: "ok" | "failed" | null;
  purpose: string | null;
  urgency: string | null;
  emergency: boolean;
  disagree: boolean;
  todo_total: number;
  todo_left: number;
  summary: string | null;
  closed: boolean;
}

export interface CallList {
  days: { date: string; count: number; calls: CallCard[] }[];
  next: string | null;
}

export interface CallDetail {
  call: {
    call_uid: string;
    at: string;
    direction: Direction;
    name: string | null;
    chart_no: string | null;
    phone: string | null;
    duration_sec: number | null;
    transcript: string;
    source: "generated" | "pasted";
  };
  classification: {
    id: number | null;
    status: "ok" | "failed" | null;
    labels: Labels | null;
    summary: string | null;
    todo: TodoItem[];
  };
  judge: Labels | null;
  disagree: { items: string[]; is_core: boolean } | null;
  /** 되돌리지 않은 자동 종결(기능 정의 §4 ①) */
  autoclose: { id: number; reason: string; at: string } | null;
}

export interface LabelNames {
  purpose: Record<string, string>;
  counterpart: Record<string, string>;
  urgency: Record<string, string>;
  flag: Record<string, string>;
}

export const DIRECTION_LABEL: Record<Direction, string> = {
  inbound: "걸려 온 전화",
  outbound: "건 전화",
};

export const fetchBoard = (signal?: AbortSignal) =>
  getJson<Board>("/calls/today", undefined, signal);
export const fetchCall = (uid: string, signal?: AbortSignal) =>
  getJson<CallDetail>(`/calls/${encodeURIComponent(uid)}`, undefined, signal);
export const fetchCalls = (
  o: { filter?: string; q?: string; before?: string; limit?: string },
  signal?: AbortSignal,
) => {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(o)) if (v) p.set(k, v);
  return getJson<CallList>("/calls", p, signal);
};
export const setTodoDone = (
  uid: string,
  item: number,
  done: boolean,
  part: string,
) =>
  postJson(`/calls/${encodeURIComponent(uid)}/todos/${item}/done`, {
    part,
    done,
  });
export const addCall = (body: {
  direction: Direction;
  transcript: string;
  phone: string | null;
  part: string;
}) => postJson<{ call_uid: string }>("/calls", body);
export const revertAuto = (id: number, part: string) =>
  postJson(`/auto-actions/${id}/revert`, { part });
export const reclassify = (uid: string) =>
  postJson<CallDetail>(`/calls/${encodeURIComponent(uid)}/classify`);

let labelsRequest: Promise<LabelNames> | null = null;

/** 라벨 우리말 이름 — 한 번만 가져온다 */
export function useLabels(): LabelNames | null {
  return useFetch("labels", () => {
    labelsRequest ??= getJson<LabelNames>("/labels");
    return labelsRequest.catch((e) => {
      labelsRequest = null;
      throw e;
    });
  }).data;
}

/** 2026-11-30 18:05:00 → 11/30 18:05 */
export const shortAt = (at: string) =>
  `${at.slice(5, 7)}/${at.slice(8, 10)} ${at.slice(11, 16)}`;

/** 녹취록 → [{speaker, text}] — "직원: …" 꼴, 머리가 없으면 앞 줄에 붙인다 */
export function transcriptLines(
  t: string,
): { speaker: string; text: string }[] {
  const out: { speaker: string; text: string }[] = [];
  for (const line of t.split("\n")) {
    const m = /^([^:：]{1,6})[:：]\s*(.*)$/.exec(line.trim());
    if (m) out.push({ speaker: m[1], text: m[2] });
    else if (line.trim() && out.length)
      out[out.length - 1].text += ` ${line.trim()}`;
    else if (line.trim()) out.push({ speaker: "", text: line.trim() });
  }
  return out;
}
