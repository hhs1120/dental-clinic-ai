// ⑤ 청구 점검 API 타입과 호출 — 응답 모양의 원본은 backend/claims.py adjusted·pre_claim
// (설계 docs/superpowers/specs/2026-11-10-claim-review-design.md §1)
import { getJson, postJson } from "@/lib/api";

export type Chip =
  "all" | "diagnosis" | "soon" | "denied" | "adjusted" | "expired";

/** 삭감 대응 칩 이름(설계 §1 그대로) — 순서 = 화면 순서(backend/claims.py CHIPS) */
export const CHIP_LABEL: Record<Chip, string> = {
  all: "전체",
  diagnosis: "상병 착오",
  soon: "신청 기한 30일 이내",
  denied: "전액 불능",
  adjusted: "일부 조정",
  expired: "기한 지남",
};
export const CHIPS = Object.keys(CHIP_LABEL) as Chip[];

export type Judgment = "justified" | "contestable" | "resubmit";

/** 판단 이름 — backend/claims.py JUDGMENTS */
export const JUDGMENT_LABEL: Record<Judgment, string> = {
  justified: "정당",
  contestable: "다툴 만함",
  resubmit: "보완 후 재청구",
};
export const JUDGMENTS = Object.keys(JUDGMENT_LABEL) as Judgment[];

export interface AdjustRow {
  claim_id: number;
  chart_no: string;
  name: string;
  service_date: string;
  tooth_no: string;
  procedure_name: string;
  status: "adjusted" | "denied";
  claim_amount: number;
  paid_amount: number;
  unpaid: number;
  adjust_note: string;
  type: "requirement" | "documentation" | "diagnosis" | "external";
  reviewed_at: string;
  deadline: string;
  days_left: number;
  expired: boolean;
  chips: Chip[];
  suggestion: Judgment | null;
  reason: string;
  fill_item: string | null;
  judgment: { value: Judgment; by: string; at: string } | null;
}

export interface AdjustSheet {
  date: string;
  appeal_days: number;
  chips: Record<Chip, { count: number; amount: number }>;
  rows: AdjustRow[];
}

export interface PreRow {
  claim_id: number;
  service_date: string;
  chart_no: string;
  name: string;
  procedure_name: string;
  tooth_no: string;
  claim_amount: number;
  dx_code: string | null;
  dx_name: string | null;
  warning: {
    allowed: string[];
    past_count: number;
    past_amount: number;
  } | null;
}

export interface PreSheet {
  counts: { lines: number; warning: number };
  rows: PreRow[];
}

export const fetchClaims = (signal?: AbortSignal) =>
  getJson<AdjustSheet>("/claims", undefined, signal);

export const fetchPreClaim = (signal?: AbortSignal) =>
  getJson<PreSheet>("/claims/pre", undefined, signal);

export const postJudgment = (
  claimId: number,
  judgment: Judgment,
  part: string,
) => postJson<{ ok: true }>(`/claims/${claimId}/judgment`, { judgment, part });

/** 12340 → ₩12,340 */
export const krw = (n: number) => `₩${n.toLocaleString("ko-KR")}`;

/** 2026-12-08(또는 시각) → 12/08 */
export const monthDay = (d: string) => `${d.slice(5, 7)}/${d.slice(8, 10)}`;

/** 치아 단위가 아닌 시술('00')은 치아를 쓰지 않는다 */
export const toothText = (t: string) => (t === "00" ? "" : `${t}번`);
