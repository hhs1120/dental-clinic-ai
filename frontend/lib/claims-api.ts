// ⑤ 청구 점검 API 타입과 호출 — 응답 모양의 원본은 backend/claims.py adjusted·pre_claim
// (설계 docs/superpowers/specs/2026-11-10-claim-review-design.md §1)
import { getJson, postJson } from "@/lib/api";

export type Chip =
  "all" | "diagnosis" | "soon" | "denied" | "adjusted" | "expired";

/** 삭감 대응 칩 — 순서 = 화면 순서(backend/claims.py CHIPS) */
export const CHIPS: Chip[] = [
  "all",
  "diagnosis",
  "soon",
  "denied",
  "adjusted",
  "expired",
];

/** 칩 이름(설계 §1 그대로) — 임박 일수는 서버가 내려준 soon_days */
export const chipLabel = (c: Chip, soonDays: number) =>
  ({
    all: "전체",
    diagnosis: "상병 착오",
    soon: `신청 기한 ${soonDays}일 이내`,
    denied: "전액 불능",
    adjusted: "일부 조정",
    expired: "기한 지남",
  })[c];

export type Judgment = "justified" | "contestable" | "resubmit";

/** 판단 이름 — backend/claims.py JUDGMENTS */
export const JUDGMENT_LABEL: Record<Judgment, string> = {
  justified: "삭감 수용",
  contestable: "재심 신청",
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
  /** 그 사유의 청구 기준 절(설계 §3) — 문서가 없으면 null */
  standard: StandardCard | null;
  /** 서류 사유 차트 문장(AI, backend/claim_evidence.py) — 결과 없으면 null, 빈 답은 [] */
  chart_quotes: string[] | null;
  /** ⑤-4 재심 사유서 초안(backend/claim_appeal.py) — 없으면 null, 화면은 판단이 재심 신청일 때만 보인다 */
  appeal: Appeal | null;
  judgment: { value: Judgment; by: string; at: string } | null;
  /** 추천을 낸 쪽 — 사유 유형 규칙, 요건 사유는 AI 점검(backend/claims.py AI_SUGGEST), 없으면 null */
  suggest_by: "rule" | "ai" | null;
  ai_evidence: Evidence[] | null;
}

export interface Evidence {
  source: "chart" | "standard";
  quote: string;
}

/** ⑤-2 AI 점검 한 줄 — 요건(2a backend/claim_check.py, 요건 대상이 아니거나 결과가 없으면 null)과
 * 추가 처치 누락(2b backend/claim_omission.py, 코드가 급여만 거른 항목) */
export interface ClaimCheck {
  requirement: "met" | "missing" | "na" | null;
  evidence: Evidence[];
  omissions: Omission[];
}

export interface Omission {
  procedure_code: string;
  procedure: string;
  tooth: string;
  quote: string;
}

export interface AdjustSheet {
  date: string;
  appeal_days: number;
  /** 꼬리표 경계 — 남은 날이 이 이하면 위험·주의(backend/claims.py SOON_DAYS·WATCH_DAYS) */
  soon_days: number;
  watch_days: number;
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
  ai: ClaimCheck | null;
}

export interface PreSheet {
  /** ai = 근거 못 찾음·누락 줄, ai_checked = 점검 저장 줄(0이면 AI 칩을 숨긴다) */
  counts: { lines: number; warning: number; ai: number; ai_checked: number };
  ai_at: string | null;
  /** 다시 점검할 줄 수 — 갱신 버튼이 탭별로 적는다(청구 전 대기 줄 · 삭감 대응 요건 사유 건) */
  ai_pending: { pre: number; adjust: number };
  rows: PreRow[];
}

export type AppealBasis = "adjustment" | "standard" | "chart" | "assumption";

export interface Appeal {
  /** 후검증 실패면 null */
  sentences:
    | {
        text: string;
        basis: AppealBasis;
        quote: string | null;
        assumption: number | null;
      }[]
    | null;
  assumptions: string[] | null;
  valid: boolean;
  stale: boolean; // 저장된 초안이 지금 규칙(입력·후검증)에 어긋남 — 다시 쓰기 필요(backend/claim_appeal.py _stale)
  reviewed: { by: string; at: string } | null;
  submitted: { by: string; at: string } | null;
  at: string;
}

export const draftAppeal = (claimId: number) =>
  postJson<Appeal>(`/claims/${claimId}/appeal`, {});

export const reviewAppeal = (claimId: number, part: string, checked: boolean) =>
  postJson<{ ok: true }>(`/claims/${claimId}/appeal/review`, { part, checked });

export const submitAppeal = (claimId: number, part: string) =>
  postJson<{ ok: true }>(`/claims/${claimId}/appeal/submit`, { part });

export interface StandardCard {
  doc_title: string;
  heading: string;
  body: string;
  internal: boolean; // 병원 내부 점검 기준 — 고시가 받치지 않는 절(backend/claim_check.py is_internal)
}

/** 청구 기준 찾기 결과 한 절(backend/claim_search.py) */
export interface StandardHit {
  chunk_id: string;
  doc_title: string;
  heading: string;
  text: string;
}

export const searchStandards = (q: string) =>
  getJson<{ results: StandardHit[] }>(
    "/claims/standards",
    new URLSearchParams({ q }),
  );

export const fetchClaims = (signal?: AbortSignal) =>
  getJson<AdjustSheet>("/claims", undefined, signal);

export const fetchPreClaim = (signal?: AbortSignal) =>
  getJson<PreSheet>("/claims/pre", undefined, signal);

export const runClaimCheck = () =>
  postJson<{ job_id: number }>("/jobs/claim-check", {});

/** AI 추천 문구 — 판단이 아니라 AI가 본 사실(사용자 결정) */
export const AI_SUGGEST_TEXT: Partial<Record<Judgment, string>> = {
  contestable: "AI가 차트에서 근거 찾음 → 재심 신청",
  justified: "AI가 차트에서 근거 못 찾음 → 삭감 수용",
};

export const postJudgment = (
  claimId: number,
  judgment: Judgment,
  part: string,
) => postJson<{ ok: true }>(`/claims/${claimId}/judgment`, { judgment, part });

/** 2026-12-08(또는 시각) → 12/08 */
export const monthDay = (d: string) => `${d.slice(5, 7)}/${d.slice(8, 10)}`;

/** 기준일과 같은 해 이후면 12/08, 지난 해면 2025/12/08 — 기한 지난 건·진료일은 해가 넘어간다 */
export const shortDate = (d: string, today: string) =>
  d.slice(0, 4) < today.slice(0, 4)
    ? `${d.slice(0, 4)}/${monthDay(d)}`
    : monthDay(d);

/** 치아 단위가 아닌 시술('00')은 치아를 쓰지 않는다 */
export const toothText = (t: string) => (t === "00" ? "" : `${t}번`);
