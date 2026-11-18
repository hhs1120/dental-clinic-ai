"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import Markdown, { type Components } from "react-markdown";

import { ReplyCards } from "@/components/assistant/cards";
import { TurnSteps, type Step } from "@/components/assistant/turn-steps";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { ApiError, useFetch } from "@/lib/api";
import { getConversation, streamTurn, type Card } from "@/lib/assistant-api";
import { readPart } from "@/lib/part";
import { fetchToday, type TodaySheet } from "@/lib/today-api";

// 시작 화면 칩(AI 비서 설계 §3.2) — 누르면 채우고 바로 보낸다
const CHIPS = [
  "📞 지난주 전화로 예약 원했는데 안 잡힌 사람",
  "🩺 내일 환자 중 미수·주의사항 있는 사람",
  "🧾 재심 기한이 한 달 안 남은 삭감 건",
  "💳 지난달 카드 입금 안 맞는 날",
  "☎️ 어제 온 전화 중 다시 연락할 곳",
  "📋 임플란트 건강보험 적용 기준",
];
const MAX_TEXT = 1000; // backend/api.py ASSISTANT_MAX_TEXT
const CUT = "응답이 끊겼습니다 — 다시 말해 주세요";
const NO_SERVER = "서버에 연결하지 못했습니다";

/** 대화 중 입력칸 아래 안내 — 비서의 말이 아니라 초안 카드의 승인만 예약을 만든다(기능 정의 §4 AI 비서). 예약 이야기가
 * 나온 대화에서만(초안 카드가 떴거나 말·답에 "예약"이 있으면 — 카드 없이 "예약을 잡았다"고 쓴 답도 걸린다) */
const DRAFT_NOTE = (
  <p className="mt-2 text-center text-xs text-muted-foreground">
    예약은 초안 카드에서 <strong className="font-bold">승인</strong>을 눌러야
    확정됩니다. 초안 카드가 없으면 예약되지 않은 것입니다.
  </p>
);

/** 시작 화면 오늘 요약 한 줄 — 오늘 화면(GET /today)과 같은 값, 0건은 빼고 못 불러오면 숨긴다(기능 정의 §4 AI 비서) */
function TodayLine() {
  const { data } = useFetch<TodaySheet>("assistant-today", () => fetchToday());
  if (!data) return null;
  const parts = (
    [
      ["할 일", data.cards.todo.items],
      ["예약", data.cards.appointments.total],
      ["급한 통화", data.people.urgent_calls.length],
    ] as const
  ).filter(([, n]) => n > 0);
  return (
    <Link
      href="/today"
      className="mt-2 flex max-w-full flex-wrap items-center justify-center gap-x-3 gap-y-1 rounded-full bg-muted px-4 py-1.5 text-sm hover:bg-accent"
    >
      <span className="flex flex-wrap items-center gap-x-1.5">
        <span className="font-bold">오늘</span>
        {parts.length === 0 ? (
          <span className="text-muted-foreground">볼 일이 없습니다</span>
        ) : (
          parts.map(([label, n], i) => (
            <span key={label} className="text-muted-foreground">
              {i > 0 && "· "}
              {label} <strong className="text-foreground">{n}</strong>건
            </span>
          ))
        )}
      </span>
      <span className="shrink-0 font-medium text-primary">오늘 화면 →</span>
    </Link>
  );
}

// 답 본문은 마크다운으로 그린다 — 모델이 쓰는 "- " 목록이 항목 사이를 띄운 진짜 목록이 되게(원시 HTML은 그리지 않는다,
// react-markdown 기본값). 취소선 같은 GFM은 쓰지 않는다("11-23 ~ 11-29"의 ~가 지워지지 않게)
const REPLY_MD: Components = {
  p: ({ children }) => <p className="my-2 first:mt-0 last:mb-0">{children}</p>,
  ul: ({ children }) => (
    <ul className="my-2 flex list-disc flex-col gap-1.5 pl-5">{children}</ul>
  ),
  ol: ({ children, start }) => (
    <ol start={start} className="my-2 flex list-decimal flex-col gap-1.5 pl-5">
      {children}
    </ol>
  ),
};

const LIST_LINE = /^\s*([-*]|\d+\.)\s/;

/** 목록 뒤에 빈 줄 없이 붙은 글(마크다운은 앞 항목에 이어 붙인다)을 떼어 낸다 → [본문, 끝맺음]. 끝맺음은 마지막
 * 목록 뒤의 안내(신환 등록·예약 초안 제안 등)라 목록과 가는 선으로 떼어 보인다. 목록이 없으면 끝맺음도 없다 */
function splitTail(text: string): [string, string] {
  const lines = text.split("\n");
  let last = -1;
  lines.forEach((l, i) => {
    if (LIST_LINE.test(l)) last = i;
  });
  if (last < 0) return [text, ""];
  while (last + 1 < lines.length && /^\s+\S/.test(lines[last + 1])) last++; // 들여 쓴 항목 이어짐
  return [
    lines.slice(0, last + 1).join("\n"),
    lines
      .slice(last + 1)
      .join("\n")
      .trim(),
  ];
}

function ReplyText({ text }: { text: string }) {
  const [body, tail] = splitTail(text);
  return (
    <div className="leading-relaxed">
      <Markdown components={REPLY_MD}>{body}</Markdown>
      {tail && (
        <div className="mt-3 border-t pt-3 text-sm">
          <Markdown components={REPLY_MD}>{tail}</Markdown>
        </div>
      )}
    </div>
  );
}

/** 고정 문구로 끝난 답(상태가 ok가 아님)은 초안 카드만 — 백엔드도 이제 그렇게 저장하지만, 그 전에 저장된 대화도
 * 같게 보이도록(backend/assistant.py _reply) */
const shownCards = (status: string | undefined, cards: Card[]) =>
  !status || status === "ok" ? cards : cards.filter((c) => c.type === "draft");

const aboutBooking = (items: Item[]) =>
  items.some(
    (it) =>
      it.text.includes("예약") ||
      (it.role === "assistant" && it.cards.some((c) => c.type === "draft")),
  );

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
  const [readOnly, setReadOnly] = useState(false); // 목록에서 지운 대화 — 볼 수만 있다
  const convId = useRef<number | null>(null);
  const epoch = useRef(0); // 대화를 열거나 새로 시작할 때마다 올린다 — 옛 스트림의 이벤트를 가려낸다
  const next = useRef(0);
  const handled = useRef(false);
  const bottom = useRef<HTMLDivElement>(null);

  const patch = (id: number, f: (it: Item & { role: "assistant" }) => Item) =>
    setItems((all) =>
      all.map((it) => (it.id === id && it.role === "assistant" ? f(it) : it)),
    );

  const send = async (raw: string) => {
    const text = raw.trim();
    if (!text || busy || readOnly) return;
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
    const mine = epoch.current;
    const ac = new AbortController();
    let ended = false; // reply·error를 받았나 — 못 받고 끝나면 끊긴 것
    try {
      await streamTurn(
        { conversation_id: convId.current, text, part: readPart() },
        (e) => {
          if (epoch.current !== mine) return; // 그 사이 다른 대화로 옮겼다 — 이 턴은 서버에 저장되고 화면은 건드리지 않는다
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
              cards: shownCards(e.status, e.cards),
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
      if (!ended && epoch.current === mine)
        patch(aid, (it) => ({ ...it, running: false, error: CUT }));
      if (epoch.current === mine) setBusy(false);
    }
  };

  // 목록 칸에서 지난 대화를 열거나 새 대화로 돌아올 때 — 지금 대화와 같은 번호면 다시 읽지 않는다
  useEffect(() => {
    if (c === null) {
      if (convId.current !== null) {
        epoch.current++;
        setBusy(false);
        convId.current = null;
        setItems([]);
        setLoadError(null);
        setReadOnly(false);
      }
      return;
    }
    if (String(convId.current) === c) return;
    epoch.current++;
    setBusy(false);
    const ac = new AbortController();
    getConversation(Number(c), ac.signal).then(
      (conv) => {
        convId.current = conv.id;
        setLoadError(null);
        setReadOnly(conv.deleted);
        setItems(
          conv.messages.map((m) =>
            m.role === "user"
              ? { id: next.current++, role: "user", text: m.text }
              : {
                  id: next.current++,
                  role: "assistant",
                  text: m.text,
                  cards: shownCards(m.status, m.cards),
                  steps: m.tool_calls.map((t) => ({
                    tool: t.name,
                    args: t.args,
                  })),
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
        disabled={readOnly}
        maxLength={MAX_TEXT}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
            e.preventDefault();
            void send(draft);
          }
        }}
        className="min-h-9 min-w-0 flex-1 resize-none border-0 bg-transparent shadow-none focus-visible:ring-0 dark:bg-transparent"
      />
      <Button
        type="submit"
        size="lg"
        disabled={busy || readOnly || !draft.trim()}
      >
        보내기
      </Button>
    </form>
  );

  if (items.length === 0)
    return (
      // 시작 화면은 본문 높이(화면 − main 위아래 여백, 좁은 화면은 위 띠까지)의 세로 가운데보다 조금 위 — 넘치면 위부터 쌓인다
      <div className="mx-auto flex min-h-[calc(100dvh-4.5rem)] max-w-2xl flex-col items-center justify-center gap-6 pt-8 pb-[6dvh] text-center max-md:min-h-[calc(100dvh-7.5rem)]">
        <h1 className="text-3xl font-bold">무엇을 도와드릴까요?</h1>
        {loadError && <p className="text-destructive">{loadError}</p>}
        <div className="w-full text-left">{form}</div>
        <div className="mt-6 flex flex-col items-center gap-2">
          <p className="text-xs text-muted-foreground">
            이렇게 물어볼 수 있어요
          </p>
          <div className="flex flex-wrap justify-center gap-2">
            {CHIPS.map((chip) => (
              <Button
                key={chip}
                variant="chip"
                size="sm"
                disabled={busy}
                onClick={() => void send(chip)}
              >
                {chip}
              </Button>
            ))}
          </div>
        </div>
        <TodayLine />
        <p className="text-sm text-muted-foreground">
          조회하고 예약 초안까지 — 확정은 사람이 합니다
        </p>
      </div>
    );

  return (
    // 본문 아래 여백(main pb-12)을 상쇄해 맨 끝에서도 입력 칸이 화면 바닥에 붙는다
    <div className="mx-auto -mb-12 flex max-w-3xl flex-col gap-6">
      <h1 className="sr-only">AI 비서 대화</h1>
      {readOnly && (
        <p
          role="status"
          className="rounded-md bg-muted px-3 py-2 text-sm text-muted-foreground"
        >
          삭제된 대화 — 읽기 전용
        </p>
      )}
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
            {it.text && <ReplyText text={it.text} />}
            {it.error && <p className="text-destructive">{it.error}</p>}
            <ReplyCards cards={it.cards} onRetry={retry} readOnly={readOnly} />
          </div>
        ),
      )}
      <div ref={bottom} />
      <div className="sticky bottom-0 bg-background pb-6">
        {form}
        {aboutBooking(items) && DRAFT_NOTE}
      </div>
    </div>
  );
}
