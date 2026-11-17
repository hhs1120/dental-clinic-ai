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
  /** 지금 프롬프트 버전 조사 결과(backend/recon_agent.py results) — 조사 전이면 null */
  agent?: ReconAgent | null;
  /** 되돌리지 않은 자동 마감(backend/recon_auto.py) */
  auto?: ReconAuto | null;
}

/** 에이전트 원인 — 근거가 모자라면 unknown */
export type AgentCause = Cause | "unknown";

export const agentCauseLabel = (c: AgentCause | null | undefined) =>
  c == null ? "" : c === "unknown" ? "원인 미정" : CAUSE_LABEL[c];

/** 조사 과정 줄 — 원본은 backend/recon_agent.py lines (설계 §3 "조사 과정 줄 종류") */
export interface ReconLine {
  kind: "hypothesis" | "compare" | "note" | "lookup" | "blocked" | "conclusion";
  text: string;
  tool?: string;
  lookup_no?: number;
  cause?: AgentCause;
  runner_up?: AgentCause | null;
  evidence?: number[];
}

export interface ReconAgentResult {
  run_id: number;
  /** handed_off = 조사·비용 상한에 걸림(원인 미정) */
  status: "done" | "handed_off" | "running";
  cause: AgentCause | null;
  runner_up: AgentCause | null;
  evidence: number[];
  reason: string | null;
}

export interface ReconAgent extends ReconAgentResult {
  lines: ReconLine[];
}

export interface ReconAuto {
  id: number;
  cause: Cause;
  reason: string;
  at: string;
}

/** 조회 도구 이름 — 키는 backend/recon_tools.py LOOKUPS */
export const TOOL_LABEL: Record<string, string> = {
  ledger_day: "그날 장부",
  card_sales: "카드 승인",
  card_deposits: "정산 입금",
  bank_tx: "통장",
  visits_day: "그날 내원",
  patient_balance: "환자 미수",
  family: "가족",
  oral_care_prices: "구강용품 가격표",
  find_amount: "같은 금액 찾기",
  submit_cause: "원인 내기",
};

export type InvestigateEvent =
  | { type: "line"; line: ReconLine }
  | { type: "done"; agent: ReconAgentResult }
  | { type: "error"; message: string };

/** event:/data: 블록 하나 → 이벤트(lib/assistant-api.ts와 같은 형식). 모르는 형식은 버린다 */
function parseEvent(block: string): InvestigateEvent | null {
  let kind = "";
  let data = "";
  for (const line of block.split("\n")) {
    if (line.startsWith("event: ")) kind = line.slice(7);
    else if (line.startsWith("data: ")) data += line.slice(6);
  }
  if (!kind || !data) return null;
  return { type: kind, ...JSON.parse(data) } as InvestigateEvent;
}

/** POST /finance/recon/{key}/investigate — 조사 과정 줄을 도착 순서대로 넘긴다. HTTP 오류는 error 이벤트로 */
export async function streamInvestigate(
  key: string,
  onEvent: (e: InvestigateEvent) => void,
  signal: AbortSignal,
): Promise<void> {
  let res: Response;
  try {
    res = await fetch(`/api/finance/recon/${key}/investigate`, {
      method: "POST",
      signal,
    });
  } catch (e) {
    if (signal.aborted) throw e;
    onEvent({ type: "error", message: "서버에 연결하지 못했습니다" });
    return;
  }
  if (!res.ok || !res.body) {
    let message = "조사를 시작하지 못했습니다";
    try {
      const detail = (await res.json()).detail;
      if (typeof detail === "string") message = detail;
    } catch {}
    onEvent({ type: "error", message });
    return;
  }
  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += value;
    let cut;
    while ((cut = buffer.indexOf("\n\n")) >= 0) {
      const event = parseEvent(buffer.slice(0, cut));
      buffer = buffer.slice(cut + 2);
      if (event) onEvent(event);
    }
  }
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
  statement: "거래명세서",
  tax_invoice: "세금계산서",
  order_email: "주문 메일",
  receipt: "영수증",
  bill: "고지서",
};

/** 계정 과목이 어디서 왔나 — 이 출금의 확정 / 같은 출금처의 지난 확정 / 거래처 기본 과목 */
export const ACCOUNT_FROM_LABEL: Record<string, string> = {
  confirmed: "확정",
  auto: "자동 확정",
  memory: "지난 확정",
  vendor: "거래처 기본",
};

/** AI 확신도(backend/expense_agent.py SubmitArgs confidence) */
export const CONFIDENCE_LABEL: Record<ExpenseAgent["confidence"], string> = {
  high: "확신 높음",
  mid: "확신 중간",
  low: "확신 낮음",
};

/** 근거 글머리 하나 — 글과 근거 조회 번호 */
export interface ExpenseReason {
  text: string;
  lookups: number[];
}

/** 지금 프롬프트 버전 증빙 탐색 결과(backend/expense_agent.py results, 설계 §4). 제출 없이 끝났으면 account null */
export interface ExpenseAgent {
  document_ids: number[] | null;
  docs: ExpenseDoc[];
  account: string | null;
  confidence: "high" | "mid" | "low";
  reasons: ExpenseReason[];
  /** 직원이 볼 것 — 없으면 null */
  see: string | null;
  status: "done" | "handed_off" | "running";
  lines: ReconLine[];
}

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
  account_from: "confirmed" | "auto" | "memory" | "vendor" | null;
  /** 품목으로 과목이 정해지는 거래처(쇼핑몰) — 사람 확정을 기본값으로 기억하지 않는다 */
  by_item: boolean;
  /** 과목 확인 필요(확정 전) — 과목이 비었거나 품목으로 정하는 거래처 */
  check: boolean;
  confirmed: { by: string; at: string } | null;
  /** 출금 키(backend/expense.py outflow_key) */
  key: string;
  /** AI 결과 — 조사 전이면 null */
  agent: ExpenseAgent | null;
  /** 규칙과 같음(증빙 집합·과목) — AI 결과가 없으면 null */
  agree: boolean | null;
  /** 되돌리지 않은 자동 확정(backend/expense_auto.py) */
  auto: { id: number; at: string } | null;
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
    /** 확정·자동 확정 안 된 줄 중 규칙과 다름 */
    differ: number;
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
