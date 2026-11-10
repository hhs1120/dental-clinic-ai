// 질문하기 SSE — 이벤트 모양의 원본은 backend/api.py POST /chat (설계 2026-11-09 챗 "API·화면")
import { getJson, type SearchCard } from "@/lib/api";

/** log_id — 이 질문의 저장 번호(ax_search_logs.id). 전체 화면이 이 번호로 같은 답을 다시 읽는다 */
export interface ChatAnswer {
  log_id: number;
  status: string;
  text: string;
  sources: SearchCard[];
}

export type ChatEvent =
  | { type: "step"; tool: string }
  | ({ type: "answer" } & ChatAnswer)
  | { type: "error"; message: string };

/** GET /chat/{log_id} — 저장된 질문 1개. 모델을 다시 부르지 않는다(앱 설계 §9) */
export interface SavedChat extends ChatAnswer {
  question: string;
  steps: string[];
}

export const getSavedChat = (logId: number, signal: AbortSignal) =>
  getJson<SavedChat>(`/chat/${logId}`, undefined, signal);

const NO_SERVER = "서버에 연결하지 못했습니다";

/** event:/data: 블록 하나 → 이벤트. 모르는 형식은 버린다 */
function parse(block: string): ChatEvent | null {
  let kind = "";
  let data = "";
  for (const line of block.split("\n")) {
    if (line.startsWith("event: ")) kind = line.slice(7);
    else if (line.startsWith("data: ")) data += line.slice(6);
  }
  if (!kind || !data) return null;
  return { type: kind, ...JSON.parse(data) } as ChatEvent;
}

/** 질문 1개를 보내고 이벤트를 도착 순서대로 넘긴다. HTTP 오류는 error 이벤트로 */
export async function streamChat(
  question: string,
  onEvent: (e: ChatEvent) => void,
  signal: AbortSignal,
): Promise<void> {
  let res: Response;
  try {
    res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
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
      else if (Array.isArray(detail)) message = "질문은 1~500자로 써 주세요";
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
