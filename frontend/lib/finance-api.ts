// ⑧ 재무 대조 API 타입과 호출 — 응답 모양의 원본은 backend/recon.py ledger
// (설계 docs/superpowers/specs/2026-11-11-finance-recon-design.md §1)
import { getJson, postJson } from "@/lib/api";

export type Cause =
  | "split_payment"
  | "cancel_reapprove"
  | "fee_mismatch"
  | "deposit_delay"
  | "non_clinical_sale"
  | "entry_missing"
  | "amount_typo"
  | "date_shift"
  | "method_wrong";

/** 원인 이름 — 키는 backend 원인 enum(ax_master_values recon_causes), 순서 = 고르기 목록 순서 */
export const CAUSE_LABEL: Record<Cause, string> = {
  split_payment: "분할결제",
  cancel_reapprove: "취소 후 재승인",
  fee_mismatch: "수수료 차이",
  deposit_delay: "입금 지연",
  non_clinical_sale: "진료 외 매출",
  entry_missing: "입력 누락",
  amount_typo: "금액 오입력",
  date_shift: "날짜 밀림",
  method_wrong: "결제수단 오입력",
};
export const CAUSES = Object.keys(CAUSE_LABEL) as Cause[];

/** 장부 쪽 원인 — 확정되면 "장부 수정은 전자차트에서"(설계 §1) */
export const LEDGER_CAUSES: Cause[] = [
  "entry_missing",
  "amount_typo",
  "date_shift",
  "method_wrong",
];

export const METHOD_LABEL: Record<string, string> = {
  card: "카드",
  cash: "현금",
  transfer: "이체",
};

export interface Sums {
  ledger: number;
  fin: number;
  diff: number;
}

export interface LedgerRow {
  id: number;
  paid_at: string;
  name: string;
  amount_due: number;
  amount_paid: number;
  method: string;
  approval_no: string | null;
}

export interface FinRow {
  table: string;
  at: string;
  amount: number;
  /** 카드사·적요(취소면 "카드사 취소") */
  label: string;
  /** 정산 행만 — 받은 값과 코드가 계산한 기대값 */
  detail?: {
    sales_date: string;
    sales_total: number;
    fee: number;
    expected_fee: number;
    expected_on: string;
  };
}

export interface Confirmation {
  cause: Cause;
  proposed_cause: Cause;
  by: string;
  at: string;
}

export interface ReconItem {
  key: string;
  date: string;
  dates: string[];
  channel: "card" | "settlement" | "cash" | "transfer";
  ledger_total: number;
  fin_total: number;
  rule_cause: Cause;
  ledger_rows: LedgerRow[];
  fin_rows: FinRow[];
  confirmation: Confirmation | null;
}

export interface ReconDay {
  date: string;
  /** pending = 정산·현금 입금이 기준일 이후라 대조 전 */
  status: "ok" | "pending";
  card: Sums;
  transfer: Sums;
  cash: {
    ledger: number;
    /** 입금 기간의 마지막 진료일 줄에만 값 */
    fin: number | null;
    diff: number | null;
    deposit: { on: string; days: number } | null;
    /** 그날 현금이 든 입금 기간에 대조 전 날이 섞여 현금은 아직 대조 전 */
    pending: boolean;
  };
  items: ReconItem[];
}

export interface ReconSheet {
  months: string[];
  month: string | null;
  causes: Record<Cause, "rule" | "ambiguous">;
  /** open_items = 이 달, open_all = 전체 기간(오늘 화면 숫자와 같음) */
  chips: {
    days: number;
    mismatch_days: number;
    open_items: number;
    open_all: number;
  };
  days: ReconDay[];
}

export const fetchRecon = (month: string | null, signal?: AbortSignal) =>
  getJson<ReconSheet>(
    "/finance/recon",
    month ? new URLSearchParams({ month }) : undefined,
    signal,
  );

export const postConfirm = (key: string, cause: Cause, part: string) =>
  postJson<{ ok: true }>(`/finance/recon/${key}/confirm`, { cause, part });
