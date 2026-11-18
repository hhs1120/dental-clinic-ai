"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import { ExcerptNote, hasCuts, showCuts } from "@/components/ask/doc-body";
import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import type { DocType, SearchCard } from "@/lib/api";
import { DOC_TYPE, docHref, docIdOf } from "@/lib/doc-types";
import { highlight } from "@/lib/highlight";
import { cn } from "@/lib/utils";

function kindLabel(c: SearchCard): string {
  if (c.source_type === "document")
    return DOC_TYPE[c.doc_type as DocType] ?? "문서";
  if (c.source_type === "call") return `통화 · ${c.direction}`;
  if (c.source_type === "message_thread") return "문자";
  return "차트 기록";
}

/** 이름 표시 — 통화·문자는 이름(차트 번호), 환자 없는 통화는 상대, 차트는 번호만(앱 설계 §9) */
function who(c: SearchCard): string | null {
  if (c.patient_name) return `${c.patient_name}(${c.chart_no})`;
  if (c.caller_name) return `상대: ${c.caller_name}`;
  return c.chart_no;
}

/** 문서 청크는 마크다운 원문이라 카드(평문·검색어 강조)에서는 인용 표시 ">"를 떼고, 제목 경로
 * '문서 > 절'(backend/rag_index.py)은 기준 조항 카드처럼 ›로 보인다 */
const plainTitle = (t: string) => t.replaceAll(" > ", " › ");
const plainBody = (b: string) => b.replace(/^>[ \t]?/gm, "");

export function ResultCard({ card, q }: { card: SearchCard; q: string }) {
  const [open, setOpen] = useState(false);
  const [clamped, setClamped] = useState(false);
  const body = useRef<HTMLParagraphElement>(null);

  // 접힌 상태에서 잘리는 본문만 펼치기 버튼을 둔다
  useEffect(() => {
    const el = body.current;
    if (!el || open) return;
    const observer = new ResizeObserver(() =>
      setClamped(el.scrollHeight > el.clientHeight),
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, [open]);

  const isDoc = card.source_type === "document";
  const docId = docIdOf(card.id);
  const isNotice = card.doc_type === "notice";
  const meta = [
    isNotice && card.version
      ? `v${card.version} · 시행 ${card.event_date}`
      : card.event_date,
    who(card),
  ].filter(Boolean);

  return (
    <Card className="gap-2 p-4 shadow-card">
      <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
        <Badge variant={isDoc ? "kind" : "muted"}>{kindLabel(card)}</Badge>
        {meta.map((m) => (
          <span key={m}>{m}</span>
        ))}
        {card.is_outdated && <Badge variant="outdated">옛 버전</Badge>}
        {docId !== null && (
          <Link
            href={docHref(docId)}
            className="ml-auto font-medium text-link hover:underline"
          >
            문서함에서 보기
          </Link>
        )}
      </div>
      {card.title && (
        <h4
          className={cn(
            "font-bold",
            card.is_outdated && "text-muted-foreground",
          )}
        >
          {highlight(isDoc ? plainTitle(card.title) : card.title, q)}
        </h4>
      )}
      <p
        ref={body}
        className={cn("text-sm whitespace-pre-line", !open && "line-clamp-3")}
      >
        {highlight(isDoc ? showCuts(plainBody(card.body)) : card.body, q)}
      </p>
      {(clamped || open) && (
        <button
          type="button"
          onClick={() => setOpen(!open)}
          className="self-start text-sm font-medium text-link hover:underline"
        >
          {open ? "접기" : "펼치기"}
        </button>
      )}
      {isDoc && hasCuts(card.body) && <ExcerptNote />}
    </Card>
  );
}
