// ⑧ 재무 대조 API 타입과 호출 — 응답 모양의 원본은 backend/recon.py ledger·backend/expense.py sheet
// (설계 docs/superpowers/specs/2026-11-11-finance-recon-design.md §1·§2)
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

// ── ⑧-2 지출 증빙 — 응답 모양의 원본은 backend/expense.py sheet(설계 §2) ──

export type OutflowTable = "fin_bank_transactions" | "fin_card_expenses";

export const DOC_KIND_LABEL: Record<string, string> = {
  statement: "명세서",
  tax_invoice: "세금계산서",
  order_email: "주문 메일",
  receipt: "영수증",
  bill: "고지서",
};

/** 계정 과목이 어디서 왔나 — 이 출금의 확정 / 같은 출금처의 지난 확정 / 거래처 기본 과목 */
export const ACCOUNT_FROM_LABEL: Record<string, string> = {
  confirmed: "확정",
  memory: "지난 확정",
  vendor: "거래처 기본",
};

export interface ExpenseDoc {
  id: number;
  kind: string;
  issuer: string;
  issued_on: string;
  amount: number;
  body: string;
}

export interface ExpenseRow {
  table: OutflowTable;
  id: number;
  at: string;
  /** 출금처 — 통장 적요 또는 카드 가맹점명 글자 그대로 */
  label: string;
  amount: number;
  /** 규칙이 가린 거래처(없으면 null) */
  vendor: string | null;
  /** 확정했으면 확정한 증빙, 아니면 규칙이 찾은 증빙 */
  docs: ExpenseDoc[];
  /** 출금액 − 증빙 합(증빙 없으면 null) */
  diff: number | null;
  account: string | null;
  account_from: "confirmed" | "memory" | "vendor" | null;
  /** 과목 확인 필요 — 과목이 비었거나 품목으로 정하는 거래처 */
  check: boolean;
  confirmed: { by: string; at: string } | null;
}

export interface ExpenseSheet {
  months: string[];
  month: string | null;
  accounts: string[];
  chips: {
    outflows: number;
    amount: number;
    found: number;
    none: number;
    check: number;
    open: number;
  };
  rows: ExpenseRow[];
}

export const fetchExpenses = (month: string | null, signal?: AbortSignal) =>
  getJson<ExpenseSheet>(
    "/finance/expenses",
    month ? new URLSearchParams({ month }) : undefined,
    signal,
  );

export const fetchCandidates = (
  table: OutflowTable,
  id: number,
  signal?: AbortSignal,
) =>
  getJson<{ docs: ExpenseDoc[] }>(
    `/finance/expenses/${table}/${id}/candidates`,
    undefined,
    signal,
  );

export const confirmExpense = (
  table: OutflowTable,
  id: number,
  documentIds: number[],
  account: string,
  part: string,
) =>
  postJson<{ ok: true }>(`/finance/expenses/${table}/${id}/confirm`, {
    document_ids: documentIds,
    account,
    part,
  });
