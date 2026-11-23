"use client";

import Link from "next/link";
import { Fragment, useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { buttonVariants } from "@/components/ui/button";
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

// 같은 검색의 요약 요청을 나눠 쓴다 — 개발 모드의 효과 두 번 실행과 탭 오가기에도 한 번만 부른다.
// 실패·오류는 지워서 다시 검색하면 다시 부른다(서버 캐시와 같은 기준)
const requests = new Map<string, Promise<SummaryResponse>>();

function summaryOf(requestKey: string, search: SearchState) {
  let p = requests.get(requestKey);
  if (!p) {
    p = postJson<SummaryResponse>(
      "/search/summary",
      Object.fromEntries(toApiParams(search)),
    );
    requests.set(requestKey, p);
    p.then(
      (d) => {
        if (d.status !== "answered" && d.status !== "refused")
          requests.delete(requestKey);
      },
      () => requests.delete(requestKey),
    );
  }
  return p;
}

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
 * 요약이 실패해도 "AI 비서에게 묻기"는 보인다(설계 2026-11-09 검색 요약 "화면") */
export function SearchSummary({
  search,
  requestKey,
}: {
  search: SearchState;
  requestKey: string;
}) {
  const [state, setState] = useState<State | null>(null);
  useEffect(() => {
    let live = true; // 늦게 온 옛 응답은 버린다
    summaryOf(requestKey, search).then(
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
        <h2 className="flex items-baseline gap-2 font-medium tracking-tight">
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
                  // 긴 문서 제목은 칩 안에서 줄인다(좁은 화면에서 카드 밖으로 나가지 않게), 전체는 title로
                  const chip = (
                    <Badge
                      variant="outline"
                      title={label}
                      className={
                        docId !== null
                          ? "max-w-full hover:bg-muted"
                          : "max-w-full"
                      }
                    >
                      <span className="truncate">{label}</span>
                    </Badge>
                  );
                  return docId !== null ? (
                    <Link
                      key={c.id}
                      href={docHref(docId)}
                      className="max-w-full min-w-0"
                    >
                      {chip}
                    </Link>
                  ) : (
                    <Fragment key={c.id}>{chip}</Fragment>
                  );
                })}
              </div>
            )}
          </>
        )}
        <Link
          href={`/?${new URLSearchParams({ prefill: search.q })}`}
          className={buttonVariants({
            size: "sm",
            variant: "outline",
            className: "self-start",
          })}
        >
          AI 비서에게 묻기
        </Link>
      </Card>
    </section>
  );
}
