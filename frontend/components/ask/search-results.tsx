import { ResultCard } from "@/components/ask/result-card";
import { StatusDot } from "@/components/ui/badge";
import type { SearchCard, SearchResponse } from "@/lib/api";
import {
  manualEmptyReason,
  sourceSummary,
  type SearchState,
} from "@/lib/search-query";

export type SearchStatus =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "done"; data: SearchResponse };

const Note = ({ children }: { children: React.ReactNode }) => (
  <p className="py-6 text-center text-sm text-muted-foreground">{children}</p>
);

function Column({
  title,
  cards,
  q,
  empty,
}: {
  title: string;
  cards: SearchCard[];
  q: string;
  empty: string;
}) {
  return (
    <section className="flex min-w-0 flex-col gap-3">
      <h3 className="flex items-center gap-2 text-sm font-bold text-muted-foreground">
        {title}
        <span className="rounded-full bg-secondary px-2 text-xs font-medium text-foreground">
          {cards.length}
        </span>
      </h3>
      {cards.length ? (
        cards.map((c) => <ResultCard key={c.id} card={c} q={q} />)
      ) : (
        <Note>{empty}</Note>
      )}
    </section>
  );
}

export function SearchResults({
  status,
  search,
}: {
  status: SearchStatus;
  search: SearchState;
}) {
  if (status.kind === "idle")
    return <Note>검색어를 넣고 찾기를 누르세요.</Note>;
  if (status.kind === "loading") return <Note>찾는 중…</Note>;
  if (status.kind === "error")
    return (
      <p
        role="alert"
        className="flex items-center gap-2 rounded-md border border-status-danger-border bg-status-danger-bg px-3 py-2.5 text-sm font-bold text-status-danger-fg"
      >
        <StatusDot status="danger" />
        {status.message}
      </p>
    );

  const { manual_results: manual, case_results: cases } = status.data;
  if (!manual.length && !cases.length) return <Note>결과가 없습니다.</Note>;
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        {/* 벡터 검색에 임계값이 없어 의미 없는 검색어도 결과가 나온다 — 건수를 '관련도 상위'로 보인다 */}
        <h2 className="text-base font-bold">
          관련도 상위 {manual.length + cases.length}건
        </h2>
        <span className="text-sm text-muted-foreground">
          &quot;{search.q}&quot; · 출처 {sourceSummary(search)}
        </span>
      </div>
      <div className="grid grid-cols-1 gap-6 md:grid-cols-2">
        <Column
          title="매뉴얼 결과"
          cards={manual}
          q={search.q}
          empty={manualEmptyReason(search) ?? "결과가 없습니다."}
        />
        <Column
          title="실제 사례 결과"
          cards={cases}
          q={search.q}
          empty="결과가 없습니다."
        />
      </div>
    </div>
  );
}
