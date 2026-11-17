// AI 비서 API — 모양의 원본은 backend/api.py `/assistant/*`·backend/assistant.py (AI 비서 설계 §3.2·§6)
import { getJson, postJson, type SearchCard } from "@/lib/api";

export type DraftStatus = "draft" | "approved" | "rejected" | "cancelled";

/** 예약 초안 카드 내용 — backend/assistant.py card_detail("draft") */
export interface DraftData {
  id: number;
  scheduled_at: string;
  duration_min: number;
  type: string;
  chair_no: number | null;
  status: DraftStatus;
  reason: string | null;
  source_call_uid: string | null;
  decided_by: string | null;
  decided_at: string | null;
  chart_no: string;
  name: string;
  staff: string;
  staff_role: string;
  procedure: string | null;
}

export interface PatientData {
  chart_no: string;
  name: string;
  birth_date: string | null;
}

export interface CallData {
  call_uid: string;
  at: string;
  direction: string;
  name: string | null;
  chart_no: string | null;
  purpose: string | null;
  summary: string | null;
}

export interface ClaimData {
  claim_id: number;
  chart_no: string;
  name: string;
  service_date: string;
  procedure_name: string;
  claim_amount: number;
  status: string;
  paid_amount: number | null;
  adjust_note: string | null;
}

export interface ReconDayData {
  date: string;
  count: number;
  items: { key: string; channel: string; rule_cause: string | null }[];
}

export type Card =
  | { type: "draft"; id: string; data: DraftData }
  | { type: "patient"; id: string; data: PatientData }
  | { type: "call"; id: string; data: CallData }
  | { type: "claim"; id: string; data: ClaimData }
  | { type: "recon_day"; id: string; data: ReconDayData }
  | { type: "source"; id: string; data: SearchCard };

/** 턴 결과 — backend/assistant.py turn. tool_calls는 도구 이름·조건·성공 여부 */
export interface ToolCall {
  name: string;
  args: Record<string, unknown>;
  ok?: boolean;
}
export interface Reply {
  conversation_id: number;
  seq: number;
  status: string;
  text: string;
  cards: Card[];
  tool_calls: ToolCall[];
  run_id: number | null;
}

export type AssistantEvent =
  | { type: "step"; tool: string; args: Record<string, unknown> }
  | ({ type: "reply" } & Reply)
  | { type: "error"; message: string };

/** GET /assistant/conversations/{id} — 저장된 그대로(모델을 다시 부르지 않는다) */
export interface SavedMessage {
  seq: number;
  role: "user" | "assistant";
  text: string;
  cards: Card[];
  tool_calls: ToolCall[];
  status?: string;
}
export interface SavedConversation {
  id: number;
  title: string;
  messages: SavedMessage[];
}

export const getConversation = (id: number, signal: AbortSignal) =>
  getJson<SavedConversation>(
    `/assistant/conversations/${id}`,
    undefined,
    signal,
  );

export type ApproveResult =
  { ok: true; card: Card } | { ok: false; reasons: string[]; card: Card };

export const approveDraft = (id: number, part: string) =>
  postJson<ApproveResult>(`/assistant/drafts/${id}/approve`, { part });

export const cancelDraft = (id: number, part: string) =>
  postJson<{ card: Card }>(`/assistant/drafts/${id}/cancel`, { part });

const NO_SERVER = "서버에 연결하지 못했습니다";

/** event:/data: 블록 하나 → 이벤트. 모르는 형식은 버린다 */
function parse(block: string): AssistantEvent | null {
  let kind = "";
  let data = "";
  for (const line of block.split("\n")) {
    if (line.startsWith("event: ")) kind = line.slice(7);
    else if (line.startsWith("data: ")) data += line.slice(6);
  }
  if (!kind || !data) return null;
  return { type: kind, ...JSON.parse(data) } as AssistantEvent;
}

/** 말 1개를 보내고 이벤트를 도착 순서대로 넘긴다. HTTP 오류는 error 이벤트로(chat-api streamChat과 같은 방식) */
export async function streamTurn(
  body: { conversation_id: number | null; text: string; part: string },
  onEvent: (e: AssistantEvent) => void,
  signal: AbortSignal,
): Promise<void> {
  let res: Response;
  try {
    res = await fetch("/api/assistant/turn", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
    });
  } catch (e) {
    if (signal.aborted) throw e;
    onEvent({ type: "error", message: NO_SERVER });
    return;
  }
  if (!res.ok || !res.body) {
    let message = NO_SERVER;
    try {
      const detail = (await res.json()).detail;
      if (typeof detail === "string") message = detail;
      else if (Array.isArray(detail)) message = "말은 1~1000자로 써 주세요";
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
      const event = parse(buffer.slice(0, cut));
      buffer = buffer.slice(cut + 2);
      if (event) onEvent(event);
    }
  }
}
