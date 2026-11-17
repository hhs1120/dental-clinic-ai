"use client";

import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { getJson, useClinic, useFetch } from "@/lib/api";
import { EVAL_TABS, MENU, type SpaceKey } from "@/lib/nav";
import { fetchToday, type TodaySheet } from "@/lib/today-api";
import { cn } from "@/lib/utils";

const PAGE = 20; // 대화 목록 한 쪽(GET /assistant/conversations 기본 20개)

interface Conversation {
  id: number;
  title: string;
  part: string;
  created_at: string;
}

const itemClass = (current: boolean) =>
  cn(
    "flex items-center justify-between gap-2 rounded-md px-3 py-2 text-sm text-muted-foreground hover:bg-accent hover:text-accent-foreground",
    current && "bg-accent font-bold text-accent-foreground",
  );

/** 업무 메뉴 줄 옆 건수 — 오늘 화면(GET /today)과 같은 값, 0건·못 불러온 칸은 비운다 */
function counts(sheet: TodaySheet | null): Record<string, number> {
  if (!sheet) return {};
  return {
    "/today": sheet.cards.todo.items,
    "/schedule": sheet.cards.appointments.total,
    "/calls": sheet.people.urgent_calls.length,
    "/claims": sheet.people.appeal_soon,
    "/finance": sheet.people.recon_open,
  };
}

function WorkMenu({ pathname }: { pathname: string }) {
  const { data } = useFetch<TodaySheet>("shell-today", fetchToday);
  const n = counts(data);
  return (
    <nav aria-label="메뉴" className="flex flex-col gap-0.5">
      {MENU.map((m) => {
        const current =
          pathname === m.href || pathname.startsWith(`${m.href}/`);
        return (
          <Link
            key={m.href}
            href={m.href}
            aria-current={current ? "page" : undefined}
            className={itemClass(current)}
          >
            {m.label}
            {n[m.href] ? (
              <span className="rounded-full bg-primary px-2 text-xs leading-5 text-primary-foreground">
                {n[m.href]}
              </span>
            ) : null}
          </Link>
        );
      })}
    </nav>
  );
}

function EvalMenu({ tab }: { tab: string }) {
  return (
    <nav aria-label="메뉴" className="flex flex-col gap-0.5">
      {EVAL_TABS.map(([key, label], i) => {
        const current =
          (EVAL_TABS.some(([k]) => k === tab) ? tab : "judge") === key;
        return (
          <Link
            key={key}
            href={i === 0 ? "/eval" : `/eval?tab=${key}`}
            aria-current={current ? "page" : undefined}
            className={itemClass(current)}
          >
            {label}
          </Link>
        );
      })}
    </nav>
  );
}

/** AI 비서 목록 칸 — 새 대화와 지난 대화(링크는 /?c=<번호>, 열기는 AI 비서 화면이 읽는다) */
function ConversationList({ current }: { current: string | null }) {
  const first = useFetch<Conversation[]>(`conv:${current}`, (s) =>
    getJson<Conversation[]>("/assistant/conversations", undefined, s),
  );
  const [more, setMore] = useState<Conversation[]>([]);
  const [done, setDone] = useState(false);
  const rows = [...(first.data ?? []), ...more];
  const loadMore = async () => {
    const last = rows[rows.length - 1];
    const page = await getJson<Conversation[]>(
      "/assistant/conversations",
      new URLSearchParams({ before: String(last.id) }),
    ).catch(() => []);
    setMore((m) => [...m, ...page]);
    if (page.length < PAGE) setDone(true);
  };
  return (
    <div className="flex flex-col gap-0.5">
      <Link href="/" className={itemClass(current === null)}>
        + 새 대화
      </Link>
      <nav aria-label="지난 대화" className="mt-2 flex flex-col gap-0.5">
        {rows.map((c) => (
          <Link
            key={c.id}
            href={`/?c=${c.id}`}
            aria-current={current === String(c.id) ? "page" : undefined}
            className={itemClass(current === String(c.id))}
          >
            <span className="truncate">{c.title}</span>
          </Link>
        ))}
      </nav>
      {first.data && first.data.length >= PAGE && !done && (
        <Button variant="ghost" size="sm" onClick={loadMore}>
          더보기
        </Button>
      )}
    </div>
  );
}

const TITLES: Record<SpaceKey, string> = {
  assistant: "대화",
  work: "업무",
  library: "자료실",
  eval: "AI 평가",
  emr: "EMR",
};

/** 목록 칸 — 공간마다 내용이 다르다(AI 비서 설계 §3.1). 기준일은 맨 아래. EMR은 목록 칸이 없다 */
export function SpacePanel({ space }: { space: SpaceKey }) {
  const pathname = usePathname();
  const params = useSearchParams();
  const clinic = useClinic();
  const today = clinic === "error" ? "—" : (clinic?.today ?? "");
  if (space === "emr") return null;
  return (
    <div className="flex w-56 flex-col gap-3 border-r bg-card p-3">
      <h2 className="px-3 pt-1 text-xs font-bold text-muted-foreground">
        {TITLES[space]}
      </h2>
      <div className="flex-1 overflow-y-auto">
        {space === "assistant" && (
          <ConversationList current={params.get("c")} />
        )}
        {space === "work" && <WorkMenu pathname={pathname} />}
        {space === "eval" && <EvalMenu tab={params.get("tab") ?? ""} />}
        {space === "library" && (
          <p className="px-3 text-sm text-muted-foreground">
            매뉴얼·공지와 지난 통화·문자·차트 기록을 찾습니다.
          </p>
        )}
      </div>
      <p className="px-3 text-xs text-muted-foreground">
        기준일 <strong className="font-medium text-foreground">{today}</strong>
      </p>
    </div>
  );
}
