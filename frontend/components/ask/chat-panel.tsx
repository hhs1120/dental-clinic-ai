"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import { ResultCard } from "@/components/ask/result-card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Textarea } from "@/components/ui/textarea";
import { ApiError, type SearchCard } from "@/lib/api";
import { getSavedChat, streamChat } from "@/lib/chat-api";
import { ALL_TYPES, writeSearch } from "@/lib/search-query";
import { cn } from "@/lib/utils";

// 진행 단계 이름 — 도구 이름은 backend/chat.py registry
const CUT = "응답이 끊겼습니다 — 다시 물어봐 주세요";

const STEP_LABEL: Record<string, string> = {
  search_docs: "문서·기록 찾는 중",
  query_patient: "환자 조회 중",
  query_schedule: "예약 조회 중",
  query_claims: "청구 조회 중",
  query_charts: "차트 조회 중",
};

/** 마지막 질문의 상태 — 답을 기다리는 중인지, 받은 답의 저장 번호(실패·끊김이면 null) */
export interface Latest {
  busy: boolean;
  logId: number | null;
}

interface ChatItem {
  id: number;
  question: string;
  steps: string[];
  answer?: { status: string; text: string; sources: SearchCard[] };
  error?: string;
}

/** 질문하기 — 질문 1개에 한 번 답한다(멀티턴 없음). 이 화면에서 한 질문은 위에서 아래로 쌓인다.
 * 물어보기 화면과 떠 있는 물어보기 창이 같이 쓴다(기능 정의 §4 앱 구성). log가 있으면 저장된 그 질문·답을
 * 다시 묻지 않고 읽어 첫 줄에 놓는다(전체 화면으로), onLatest는 마지막 질문의 상태를 알린다 */
export function ChatPanel({
  draft,
  onDraftChange: setDraft,
  log,
  onLatest,
  className,
}: {
  draft: string;
  onDraftChange: (v: string) => void;
  log?: number;
  onLatest?: (latest: Latest) => void;
  className?: string;
}) {
  const [items, setItems] = useState<ChatItem[]>([]);
  const [busy, setBusy] = useState(false);
  const [logError, setLogError] = useState<string | null>(null);
  const next = useRef(0);
  const live = useRef<AbortController | null>(null);
  useEffect(() => () => live.current?.abort(), []); // 화면을 떠나면 받던 스트림을 끊는다

  useEffect(() => {
    if (log === undefined) return;
    const ac = new AbortController();
    getSavedChat(log, ac.signal).then(
      ({ question, steps, status, text, sources }) => {
        setLogError(null);
        // 불러오는 사이 새로 물은 질문이 있어도 지우지 않고 그 앞에 놓는다
        setItems((all) => [
          {
            id: next.current++,
            question,
            steps,
            answer: { status, text, sources },
          },
          ...all,
        ]);
      },
      (e) => {
        if (!ac.signal.aborted)
          setLogError(
            e instanceof ApiError ? e.message : "서버에 연결하지 못했습니다",
          );
      },
    );
    return () => ac.abort();
  }, [log]);

  const update = (id: number, patch: (it: ChatItem) => ChatItem) =>
    setItems((all) => all.map((it) => (it.id === id ? patch(it) : it)));

  const ask = async () => {
    const question = draft.trim();
    if (!question || busy) return;
    const id = next.current++;
    setItems((all) => [...all, { id, question, steps: [] }]);
    setDraft("");
    setBusy(true);
    onLatest?.({ busy: true, logId: null });
    let logId: number | null = null;
    const ac = new AbortController();
    live.current = ac;
    let ended = false; // answer·error를 받았나 — 못 받고 끝나면 끊긴 것
    try {
      await streamChat(
        question,
        (e) => {
          ended ||= e.type !== "step";
          if (e.type === "step")
            update(id, (it) => ({ ...it, steps: [...it.steps, e.tool] }));
          else if (e.type === "answer") {
            update(id, (it) => ({ ...it, answer: e }));
            logId = e.log_id;
          } else update(id, (it) => ({ ...it, error: e.message }));
        },
        ac.signal,
      );
    } catch {
      // 읽기·해석 오류 — 아래에서 끊김으로 표시
    } finally {
      if (!ended && !ac.signal.aborted)
        update(id, (it) => ({ ...it, error: CUT }));
      setBusy(false);
      onLatest?.({ busy: false, logId });
    }
  };

  return (
    <Card
      className={cn("flex max-w-4xl flex-col gap-5 p-5 shadow-card", className)}
    >
      {logError && <p className="text-destructive">{logError}</p>}
      {items.length === 0 && (
        <p className="text-muted-foreground">
          예) 틀니 수리 맡기면 며칠 걸려? · 오늘 미수 있는 환자 몇 명? ·
          스케일링하고 닷새도 안 돼서 잇몸치료로 넘어간 차트
        </p>
      )}
      {items.map((it) => (
        <div key={it.id} className="flex flex-col gap-3">
          <p className="self-end rounded-lg bg-muted px-4 py-2">
            {it.question}
          </p>
          <div className="flex flex-wrap gap-2">
            {it.steps.map((s, i) => (
              <Badge key={i} variant="outline">
                {STEP_LABEL[s] ?? s}
              </Badge>
            ))}
            {!it.answer && !it.error && (
              <span className="text-sm text-muted-foreground">답 쓰는 중…</span>
            )}
          </div>
          {it.error && <p className="text-destructive">{it.error}</p>}
          {(it.answer || it.error) && (
            <Link
              href={`/ask?${writeSearch({ q: it.question, types: ALL_TYPES, from: "", to: "", chart: "" })}`}
              className="self-start text-sm font-medium text-link hover:underline"
            >
              근거 더 보기
            </Link>
          )}
          {it.answer && (
            <div className="flex flex-col gap-3">
              <p
                className={
                  it.answer.status === "answered"
                    ? "whitespace-pre-wrap"
                    : "font-semibold"
                }
              >
                {it.answer.text}
              </p>
              {it.answer.sources.length > 0 && (
                <div className="flex flex-col gap-2">
                  <span className="text-sm text-muted-foreground">출처</span>
                  {it.answer.sources.map((c) => (
                    <ResultCard key={c.id} card={c} q="" />
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      ))}
      <form
        aria-label="질문"
        className="flex items-stretch gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          void ask();
        }}
      >
        <Textarea
          aria-label="질문 입력"
          rows={2}
          placeholder="질문을 적고 Enter (줄바꿈은 Shift+Enter)"
          value={draft}
          maxLength={500}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (
              e.key === "Enter" &&
              !e.shiftKey &&
              !e.nativeEvent.isComposing
            ) {
              e.preventDefault();
              void ask();
            }
          }}
          className="min-w-0 flex-1 bg-card"
        />
        <Button type="submit" disabled={busy || !draft.trim()}>
          묻기
        </Button>
      </form>
      <p className="text-sm text-muted-foreground">
        멀티턴 없음 · 질문 하나에 조사 도구를 골라 한 번 답합니다. 근거가 없으면
        &quot;문서함에 없음&quot;
      </p>
    </Card>
  );
}
