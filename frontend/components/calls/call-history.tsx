"use client";

import { useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { errorText, useFetch } from "@/lib/api";
import {
  type CallList,
  DIRECTION_LABEL,
  fetchCalls,
  type LabelNames,
} from "@/lib/calls-api";
import { cn } from "@/lib/utils";

const FILTERS = [
  ["", "전체"],
  ["inbound", "걸려 온 전화"],
  ["outbound", "건 전화"],
  ["emergency", "응급"],
  ["todo_left", "할 일 남음"],
  ["disagree", "AI 엇갈림"],
] as const;
const WEEK = "일월화수목금토";

/** 2026-11-30 → 11월 30일 (일) */
function dayTitle(date: string): string {
  const d = new Date(`${date}T00:00:00Z`);
  return `${d.getUTCMonth() + 1}월 ${d.getUTCDate()}일 (${WEEK[d.getUTCDay()]})`;
}

/** 통화 기록 — 날짜별 카드, 필터·검색, 더 보기(통화·② 판정 화면 설계 "통화 기록") */
export function CallHistory({
  names,
  selected,
  onOpen,
  version,
}: {
  names: LabelNames | null;
  selected: string | null;
  onOpen: (uid: string) => void;
  version: number; // 체크·새 통화 뒤 다시 불러오기
}) {
  const [filter, setFilter] = useState("");
  const [q, setQ] = useState("");
  const [query, setQuery] = useState("");
  // 더 불러온 쪽 — 조건(key)이 바뀌면 버린다
  const [extra, setExtra] = useState<{ key: string; pages: CallList[] }>({
    key: "",
    pages: [],
  });
  const [moreError, setMoreError] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);

  // 검색어는 잠깐 멈췄을 때 반영한다
  useEffect(() => {
    const t = setTimeout(() => setQuery(q.trim()), 300);
    return () => clearTimeout(t);
  }, [q]);

  const key = `${filter}|${query}|${version}`;
  const first = useFetch(key, (signal) =>
    fetchCalls({ filter, q: query }, signal),
  );
  const pages = first.data
    ? [first.data, ...(extra.key === key ? extra.pages : [])]
    : [];
  const error = first.error ?? moreError;
  const loading = !first.data && !first.error;

  const more = async () => {
    const last = pages[pages.length - 1];
    if (!last?.next) return;
    setLoadingMore(true);
    setMoreError(null);
    try {
      const p = await fetchCalls({ filter, q: query, before: last.next });
      setExtra({ key, pages: [...pages.slice(1), p] });
    } catch (e) {
      setMoreError(errorText(e));
    } finally {
      setLoadingMore(false);
    }
  };

  // 쪽마다 같은 날짜가 이어질 수 있어 날짜로 다시 묶는다
  const days = new Map<
    string,
    { count: number; calls: CallList["days"][0]["calls"] }
  >();
  for (const p of pages)
    for (const d of p.days) {
      const cur = days.get(d.date) ?? { count: d.count, calls: [] };
      cur.calls.push(...d.calls);
      days.set(d.date, cur);
    }
  const next = pages[pages.length - 1]?.next;

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        {FILTERS.map(([k, l]) => (
          <Button
            key={k}
            size="sm"
            variant={filter === k ? "secondary" : "outline"}
            aria-pressed={filter === k}
            onClick={() => setFilter(k)}
          >
            {l}
          </Button>
        ))}
        <Input
          className="ml-auto w-56"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="이름·번호·차트번호"
          aria-label="통화 검색"
        />
      </div>
      {error && <p className="text-sm text-muted-foreground">{error}</p>}
      {!error && days.size === 0 && !loading && (
        <p className="py-10 text-center text-sm text-muted-foreground">
          조건에 맞는 통화가 없습니다
        </p>
      )}
      {[...days].map(([date, d]) => (
        <section key={date} className="flex flex-col gap-1.5">
          <h2 className="mt-2 font-bold">
            {dayTitle(date)}
            <span className="ml-1.5 text-sm font-normal text-muted-foreground">
              · {d.count}통
            </span>
          </h2>
          {d.calls.map((c) => (
            <button
              key={c.call_uid}
              type="button"
              onClick={() => onOpen(c.call_uid)}
              className={cn(
                "flex flex-col gap-1 rounded-lg border bg-card px-3 py-2.5 text-left shadow-card",
                selected === c.call_uid && "border-primary",
              )}
            >
              <span className="flex flex-wrap items-center gap-1.5">
                <b>{c.name ?? "이름 없음"}</b>
                <span className="text-sm text-muted-foreground">
                  {c.at.slice(11, 16)} · {DIRECTION_LABEL[c.direction]}
                  {c.purpose
                    ? ` · ${names?.purpose[c.purpose] ?? c.purpose}`
                    : ""}
                </span>
                {c.emergency && <Badge variant="danger">응급</Badge>}
                {!c.emergency && c.urgency === "urgent" && (
                  <Badge variant="danger">급함</Badge>
                )}
                {c.disagree && <Badge variant="warn">AI 엇갈림</Badge>}
                {c.status !== "ok" && <Badge variant="danger">분류 없음</Badge>}
                {c.todo_total > 0 &&
                  (c.todo_left > 0 ? (
                    <Badge variant="warn">할 일 {c.todo_left}</Badge>
                  ) : (
                    <Badge variant="ok">처리함</Badge>
                  ))}
              </span>
              {c.summary && <span className="text-sm">{c.summary}</span>}
            </button>
          ))}
        </section>
      ))}
      {next && (
        <Button variant="outline" onClick={more} disabled={loadingMore}>
          {loadingMore ? "불러오는 중…" : "더 보기"}
        </Button>
      )}
    </div>
  );
}
