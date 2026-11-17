"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { AssistantCard } from "@/components/assistant/cards";
import { TurnSteps, type Step } from "@/components/assistant/turn-steps";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { ApiError } from "@/lib/api";
import { getConversation, streamTurn, type Card } from "@/lib/assistant-api";
import { readPart } from "@/lib/part";

// 시작 화면 칩(AI 비서 설계 §3.2) — 누르면 채우고 바로 보낸다
const CHIPS = [
  "📞 지난주 전화로 예약 원했는데 안 잡힌 사람",
  "🩺 내일 환자 중 미수·주의사항 있는 사람",
  "🧾 재심 낼 만한 삭감 건",
  "💳 이번 주 카드 입금 안 맞는 날",
  "☎️ 김영철 님 지난번에 전화로 뭐 물어봤지",
];
const MAX_TEXT = 1000; // backend/api.py ASSISTANT_MAX_TEXT
const CUT = "응답이 끊겼습니다 — 다시 말해 주세요";
const NO_SERVER = "서버에 연결하지 못했습니다";

type Item =
  | { id: number; role: "user"; text: string }
  | {
      id: number;
      role: "assistant";
      text: string;
      cards: Card[];
      steps: Step[];
      running: boolean;
      error?: string;
    };

/** AI 비서 — 시작·대화. 대화 번호는 URL(/?c=)에 두고, 지난 대화는 저장된 그대로 읽는다(모델을 다시 부르지 않음) */
export function AssistantView() {
  const params = useSearchParams();
  const router = useRouter();
  const c = params.get("c");
  const [items, setItems] = useState<Item[]>([]);
  // ?prefill=은 채우기만 — 새 대화일 때 처음 그리는 순간에 입력창에 넣는다
  const [draft, setDraft] = useState(() =>
    c === null ? (params.get("prefill") ?? "").slice(0, MAX_TEXT) : "",
  );
  const [busy, setBusy] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const convId = useRef<number | null>(null);
  const next = useRef(0);
  const handled = useRef(false);
  const bottom = useRef<HTMLDivElement>(null);

  const patch = (id: number, f: (it: Item & { role: "assistant" }) => Item) =>
    setItems((all) =>
      all.map((it) => (it.id === id && it.role === "assistant" ? f(it) : it)),
    );

  const send = async (raw: string) => {
    const text = raw.trim();
    if (!text || busy) return;
    const aid = next.current++;
    setItems((all) => [
      ...all,
      { id: next.current++, role: "user", text },
      {
        id: aid,
        role: "assistant",
        text: "",
        cards: [],
        steps: [],
        running: true,
      },
    ]);
    setDraft("");
    setBusy(true);
    // 화면을 떠나도 턴은 서버에서 끝까지 돈다(저장됨) — 스트림을 끊지 않는다(개발 모드 이중 실행에도 안전)
    const ac = new AbortController();
    let ended = false; // reply·error를 받았나 — 못 받고 끝나면 끊긴 것
    try {
      await streamTurn(
        { conversation_id: convId.current, text, part: readPart() },
        (e) => {
          ended ||= e.type !== "step";
          if (e.type === "step")
            patch(aid, (it) => ({
              ...it,
              steps: [...it.steps, { tool: e.tool, args: e.args }],
            }));
          else if (e.type === "reply") {
            const created = convId.current === null;
            convId.current = e.conversation_id;
            patch(aid, (it) => ({
              ...it,
              text: e.text,
              cards: e.cards,
              running: false,
              steps: e.tool_calls.length
                ? e.tool_calls.map((t) => ({ tool: t.name, args: t.args }))
                : it.steps,
            }));
            // 새 대화면 주소를 /?c=<번호>로(목록 칸이 같은 키로 다시 읽는다)
            if (created) router.replace(`/?c=${e.conversation_id}`);
          } else
            patch(aid, (it) => ({ ...it, running: false, error: e.message }));
        },
        ac.signal,
      );
    } catch {
      // 읽기 오류 — 아래에서 끊김으로 표시
    } finally {
      if (!ended) patch(aid, (it) => ({ ...it, running: false, error: CUT }));
      setBusy(false);
    }
  };

  // 목록 칸에서 지난 대화를 열거나 새 대화로 돌아올 때 — 지금 대화와 같은 번호면 다시 읽지 않는다
  useEffect(() => {
    if (c === null) {
      if (convId.current !== null) {
        convId.current = null;
        setItems([]);
        setLoadError(null);
      }
      return;
    }
    if (String(convId.current) === c) return;
    const ac = new AbortController();
    getConversation(Number(c), ac.signal).then(
      (conv) => {
        convId.current = conv.id;
        setLoadError(null);
        setItems(
          conv.messages.map((m) =>
            m.role === "user"
              ? { id: next.current++, role: "user", text: m.text }
              : {
                  id: next.current++,
                  role: "assistant",
                  text: m.text,
                  cards: m.cards,
                  steps: [],
                  running: false,
                },
          ),
        );
      },
      (e) => {
        if (!ac.signal.aborted)
          setLoadError(e instanceof ApiError ? e.message : NO_SERVER);
      },
    );
    return () => ac.abort();
  }, [c]);

  // 다른 화면의 버튼이 연 질문 — ?draft=는 바로 보내고 ?prefill=은 채우기만(새 대화일 때만)
  useEffect(() => {
    if (handled.current || c !== null) return;
    const d = params.get("draft");
    const p = params.get("prefill");
    if (!d && !p) return;
    handled.current = true;
    router.replace("/");
    if (d) void send(d.slice(0, MAX_TEXT));
    // 처음 한 번만
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    bottom.current?.scrollIntoView?.({ block: "end" });
  }, [items]);

  const retry = () => setDraft("다른 시간으로");

  const form = (
    <form
      aria-label="말하기"
      className="flex items-end gap-2 rounded-2xl border bg-card p-2 shadow-card"
      onSubmit={(e) => {
        e.preventDefault();
        void send(draft);
      }}
    >
      <Textarea
        aria-label="말 입력"
        rows={1}
        placeholder="물어보거나 시킬 일을 적고 Enter (줄바꿈은 Shift+Enter)"
        value={draft}
        maxLength={MAX_TEXT}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
            e.preventDefault();
            void send(draft);
          }
        }}
        className="min-h-10 min-w-0 flex-1 resize-none border-0 bg-transparent shadow-none focus-visible:ring-0 dark:bg-transparent"
      />
      <Button type="submit" disabled={busy || !draft.trim()}>
        보내기
      </Button>
    </form>
  );

  if (items.length === 0)
    return (
      <div className="mx-auto flex max-w-2xl flex-col items-center gap-6 py-16 text-center">
        <h1 className="text-3xl font-bold">무엇을 도와드릴까요?</h1>
        {loadError && <p className="text-destructive">{loadError}</p>}
        <div className="w-full text-left">{form}</div>
        <div className="flex flex-wrap justify-center gap-2">
          {CHIPS.map((chip) => (
            <Button
              key={chip}
              variant="outline"
              size="sm"
              disabled={busy}
              onClick={() => void send(chip)}
            >
              {chip}
            </Button>
          ))}
        </div>
        <p className="text-sm text-muted-foreground">
          조회하고 예약 초안까지 — 확정은 사람이 합니다
        </p>
      </div>
    );

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-6">
      <h1 className="sr-only">AI 비서 대화</h1>
      {items.map((it) =>
        it.role === "user" ? (
          <p
            key={it.id}
            className="max-w-[80%] self-end whitespace-pre-wrap rounded-2xl bg-muted px-4 py-2"
          >
            {it.text}
          </p>
        ) : (
          <div key={it.id} className="flex flex-col gap-3">
            <TurnSteps steps={it.steps} running={it.running} />
            {it.text && <p className="whitespace-pre-wrap">{it.text}</p>}
            {it.error && <p className="text-destructive">{it.error}</p>}
            {it.cards.map((card) => (
              <AssistantCard
                key={`${card.type}:${card.id}`}
                card={card}
                onRetry={retry}
              />
            ))}
          </div>
        ),
      )}
      <div ref={bottom} />
      <div className="sticky bottom-0 bg-background pb-2">{form}</div>
    </div>
  );
}
