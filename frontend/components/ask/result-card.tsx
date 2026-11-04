"use client";

import { useEffect, useRef, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import type { SearchCard } from "@/lib/api";
import { highlight } from "@/lib/highlight";
import { cn } from "@/lib/utils";

const DOC_TYPE: Record<string, string> = {
  notice: "공지",
  manual: "매뉴얼",
  faq: "FAQ",
  insurance_guide: "보험 안내",
  claim_standard: "청구 기준",
  education: "교육 자료",
  external: "외부 자료",
};

function kindLabel(c: SearchCard): string {
  if (c.source_type === "document") return DOC_TYPE[c.doc_type ?? ""] ?? "문서";
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
      </div>
      {card.title && (
        <h4
          className={cn(
            "font-bold",
            card.is_outdated && "text-muted-foreground",
          )}
        >
          {highlight(card.title, q)}
        </h4>
      )}
      <p
        ref={body}
        className={cn("text-sm whitespace-pre-line", !open && "line-clamp-3")}
      >
        {highlight(card.body, q)}
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
    </Card>
  );
}
