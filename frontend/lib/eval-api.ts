// AI 평가 화면 API — 응답 모양의 원본은 backend/judge_queue.py·blind.py·metrics.py (통화·② 판정 화면 설계 "AI 평가 화면")
import { getJson, postJson } from "@/lib/api";
import type { Direction, Labels } from "@/lib/calls-api";

export interface JudgeNext {
  call_uid: string;
  direction: Direction;
  at: string;
  transcript: string;
  left: number;
  done_today: number;
  done_total: number;
}

export interface Revealed {
  classifier: Labels | null;
  judge: Labels | null;
}

export interface BlindSide {
  classification_id: number;
  text: string;
}

export interface BlindNext {
  call_uid: string;
  direction: Direction;
  at: string;
  transcript: string;
  left: BlindSide;
  right: BlindSide;
  left_count: number;
}

export type Choice = "left" | "right" | "tie" | "both_wrong";

export interface Rates {
  b_better: number;
  a_better: number;
  tie: number;
  both_wrong: number;
  n: number;
  left: number;
  right: number;
}

export interface Accuracy {
  agreement: Record<string, Record<string, { k: number; n: number }>>;
  blind: { llm: Rates; human: Rates; agree: { k: number; n: number } } | null;
  human_share: { k: number; n: number };
  versions: {
    classify: string[];
    active: string;
    judge: string | null;
    blind: [string, string] | null;
  };
}

export const fetchJudgeNext = (signal?: AbortSignal) =>
  getJson<JudgeNext | null>("/judge/next", undefined, signal);
export const saveJudgment = (body: {
  call_uid: string;
  labels: Labels;
  note: string | null;
  part: string;
}) => postJson<Revealed>("/judgments", body);
export const skipJudgment = (uid: string) =>
  postJson(`/calls/${encodeURIComponent(uid)}/skip`);
export const fetchBlindNext = (signal?: AbortSignal) =>
  getJson<BlindNext | null>("/blind/next", undefined, signal);
export const saveBlindVote = (body: {
  left_id: number;
  right_id: number;
  choice: Choice;
  part: string;
}) => postJson<{ left: string; right: string }>("/blind-votes", body);
export const fetchAccuracy = (signal?: AbortSignal) =>
  getJson<Accuracy>("/metrics/accuracy", undefined, signal);

// 자동 개선 이력 — 원본은 evaluation/improve.py 행(reports/improve_runs.jsonl), backend/api.py /improve/runs
export interface ImproveRun {
  at: string;
  base: string;
  candidate: string;
  attempt: number;
  error_types: { name: string; n: number }[] | null;
  changed_rule: string | null;
  gate: { passed: boolean; failed: string[] } | null;
  failed: string[];
  adopted: boolean;
  cost_usd: number;
}

export interface ImproveRuns {
  runs: ImproveRun[];
  pending: string | null;
  active: string;
}

export const fetchImproveRuns = (signal?: AbortSignal) =>
  getJson<ImproveRuns>("/improve/runs", undefined, signal);
