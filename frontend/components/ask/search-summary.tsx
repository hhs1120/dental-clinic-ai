"use client";

import Link from "next/link";
import { Fragment, useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import {
  errorText,
  postJson,
  type DocType,
  type SearchCard,
  type SummaryResponse,
} from "@/lib/api";
import { DOC_TYPE, docHref, docIdOf } from "@/lib/doc-types";
import { toApiParams, type SearchState } from "@/lib/search-query";

type State =
  { key: string; data: SummaryResponse } | { key: string; error: string };

/** 본문의 [n]을 작은 번호 표지로 — 번호 n = 아래 출처 칩 n(설계 2026-11-09 검색 요약 "출처 번호 형식") */
function Numbered({ text }: { text: string }) {
  return text.split(/\[(\d+)\]/).map((part, i) =>
    i % 2 ? (
      <sup key={i} className="mx-0.5 font-bold text-link">
        {part}
      </sup>
    ) : (
      <Fragment key={i}>{part}</Fragment>
    ),
  );
}

function sourceLabel(n: number, c: SearchCard): string {
  if (c.source_type === "document")
    return `${n} · ${DOC_TYPE[c.doc_type as DocType] ?? "문서"} · ${c.title}`;
  const kind =
    c.source_type === "call"
      ? "통화"
      : c.source_type === "message_thread"
        ? "문자"
        : "차트";
  return [n, kind, c.event_date, c.chart_no].filter(Boolean).join(" · ");
}

/** 기록 검색 AI 요약 — 결과가 있는 검색에서만 그린다. 결과가 먼저 뜨고 요약은 따로 채운다.
 * 요약이 실패해도 "이걸로 질문하기"는 보인다(설계 2026-11-09 검색 요약 "화면") */
export function SearchSummary({
  search,
  requestKey,
  onAsk,
}: {
  search: SearchState;
  requestKey: string;
  onAsk: (q: string) => void;
}) {
  const [state, setState] = useState<State | null>(null);
  useEffect(() => {
    let live = true; // 늦게 온 옛 응답은 버린다
    postJson<SummaryResponse>(
      "/search/summary",
      Object.fromEntries(toApiParams(search)),
    ).then(
      (data) => live && setState({ key: requestKey, data }),
      (e) => live && setState({ key: requestKey, error: errorText(e) }),
    );
    return () => {
      live = false;
    };
    // 검색은 requestKey로 정해진다
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [requestKey]);

  const now = state?.key === requestKey ? state : null;
  return (
    <section aria-label="AI 요약">
      <Card className="gap-3 p-4 shadow-card">
        <h2 className="flex items-baseline gap-2 font-bold">
          AI 요약
          <span className="text-xs font-normal text-muted-foreground">
            검색 결과로 AI가 만든 정리 · 문장 끝 번호는 아래 출처
          </span>
        </h2>
        {!now && (
          <p className="text-sm text-muted-foreground">요약 만드는 중…</p>
        )}
        {now && "error" in now && (
          <p className="text-sm text-muted-foreground">{now.error}</p>
        )}
        {now && "data" in now && (
          <>
            <p
              className={
                now.data.status === "answered"
                  ? "leading-relaxed"
                  : "text-sm text-muted-foreground"
              }
            >
              {now.data.status === "answered" ? (
                <Numbered text={now.data.text} />
              ) : (
                now.data.text
              )}
            </p>
            {now.data.sources.length > 0 && (
              <div className="flex flex-wrap gap-2">
                {now.data.sources.map((c, i) => {
                  const docId = docIdOf(c.id);
                  const label = sourceLabel(i + 1, c);
                  return docId !== null ? (
                    <Link key={c.id} href={docHref(docId)}>
                      <Badge variant="outline" className="hover:bg-muted">
                        {label}
                      </Badge>
                    </Link>
                  ) : (
                    <Badge key={c.id} variant="outline">
                      {label}
                    </Badge>
                  );
                })}
              </div>
            )}
          </>
        )}
        <Button
          size="sm"
          variant="outline"
          className="self-start"
          onClick={() => onAsk(search.q)}
        >
          이걸로 질문하기
        </Button>
      </Card>
    </section>
  );
}
