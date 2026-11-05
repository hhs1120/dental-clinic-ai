"use client";

import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import { type Board, type BoardItem, shortAt } from "@/lib/calls-api";
import { cn } from "@/lib/utils";

/** "오늘 할 일" 두 칸 — 왼쪽 병원 안에서 할 일, 오른쪽 연락할 사람. 칸마다 통화 하나에 카드 하나(통화·② 판정 화면 설계 "오늘") */
export function TodayBoard({
  board,
  selected,
  onOpen,
  onCheck,
}: {
  board: Board;
  selected: string | null;
  onOpen: (uid: string) => void;
  onCheck: (item: BoardItem, done: boolean) => void;
}) {
  // 연락 항목의 "먼저 끝낼 일" — 같은 통화의 그 번호가 아직 안 끝난 안쪽 일일 때만
  const waitingFor = (it: BoardItem) =>
    it.after >= 0
      ? board.internal.find(
          (x) => x.call_uid === it.call_uid && x.item === it.after,
        )
      : undefined;
  return (
    <div className="flex flex-col gap-4">
      <div className="grid gap-4 md:grid-cols-2">
        <Column
          title="할 일"
          hint="병원 안에서"
          items={board.internal}
          selected={selected}
          onOpen={onOpen}
          onCheck={onCheck}
          empty="남은 할 일이 없습니다"
          tone="internal"
        />
        <Column
          title="연락할 사람"
          hint="전화·문자"
          items={board.contact}
          selected={selected}
          onOpen={onOpen}
          onCheck={onCheck}
          empty="연락할 사람이 없습니다"
          note={(it) => waitingFor(it)?.text ?? null}
          tone="contact"
        />
      </div>
      <DoneList items={board.done} onOpen={onOpen} onCheck={onCheck} />
    </div>
  );
}

function Column({
  title,
  hint,
  items,
  selected,
  onOpen,
  onCheck,
  empty,
  note,
  tone,
}: {
  title: string;
  hint: string;
  items: BoardItem[];
  selected: string | null;
  onOpen: (uid: string) => void;
  onCheck: (item: BoardItem, done: boolean) => void;
  empty: string;
  note?: (it: BoardItem) => string | null;
  tone: "internal" | "contact";
}) {
  // 두 칸을 글자만이 아니라 영역으로 구분한다 — 칸마다 바탕과 머리 띠 색
  return (
    <section
      className="flex flex-col gap-2 rounded-xl border bg-muted/40 p-3"
      aria-label={title}
    >
      <h2
        className={cn(
          "flex items-center gap-2 rounded-lg px-3 py-2 font-bold",
          tone === "contact"
            ? "bg-accent text-accent-foreground"
            : "bg-secondary text-secondary-foreground",
        )}
      >
        {title}
        <span className="rounded-full bg-background px-2 text-sm">
          {items.length}
        </span>
        <span className="text-sm font-normal opacity-80">{hint}</span>
      </h2>
      {items.length === 0 && (
        <p className="rounded-lg border border-dashed px-3 py-6 text-center text-sm text-muted-foreground">
          {empty}
        </p>
      )}
      {groups(items).map((g) => (
        <CallCard
          key={g[0].call_uid}
          items={g}
          on={selected === g[0].call_uid}
          onOpen={onOpen}
          onCheck={onCheck}
          note={note}
        />
      ))}
    </section>
  );
}

/** 같은 통화의 할 일을 한 카드로 — 칸 안의 순서(응급·급함 먼저, 최근 통화)는 첫 항목 자리를 따른다 */
function groups(items: BoardItem[]): BoardItem[][] {
  const by = new Map<string, BoardItem[]>();
  for (const it of items) {
    const g = by.get(it.call_uid);
    if (g) g.push(it);
    else by.set(it.call_uid, [it]);
  }
  return [...by.values()];
}

/** 통화 하나 = 카드 하나, 안에 할 일 여러 줄(칸마다 — 같은 통화가 두 칸에 한 번씩 나올 수 있다) */
function CallCard({
  items,
  on,
  onOpen,
  onCheck,
  note,
}: {
  items: BoardItem[];
  on: boolean;
  onOpen: (uid: string) => void;
  onCheck: (item: BoardItem, done: boolean) => void;
  note?: (it: BoardItem) => string | null;
}) {
  const head = items[0];
  const name = head.name ?? (head.kind === "contact" ? head.who : null);
  return (
    <div
      className={cn(
        "flex flex-col gap-1.5 rounded-lg border bg-card px-3 py-2.5 shadow-card",
        on && "border-primary",
      )}
    >
      <button
        type="button"
        className="flex flex-wrap items-center gap-1.5 text-left"
        onClick={() => onOpen(head.call_uid)}
      >
        <b className="whitespace-nowrap">{name ?? "이름 없음"}</b>
        {head.emergency && <Badge variant="danger">응급</Badge>}
        {!head.emergency && head.urgent && <Badge variant="danger">급함</Badge>}
        <span className="text-xs text-muted-foreground">
          {shortAt(head.at)} 통화{head.chart_no ? ` · ${head.chart_no}` : ""}
        </span>
      </button>
      {items.map((it) => {
        const first = note?.(it) ?? null;
        return (
          <div key={it.item} className="flex flex-col gap-1">
            <div className="flex items-start gap-3">
              <Checkbox
                className="mt-0.5"
                checked={it.done}
                onCheckedChange={(v) => onCheck(it, v === true)}
                aria-label={`${it.text} 끝냄`}
              />
              <button
                type="button"
                className="text-left"
                onClick={() => onOpen(it.call_uid)}
              >
                {/* 연락 상대가 카드 머리 이름(환자)과 다르면 앞에 — 보호자·가족에게 거는 연락 */}
                {it.kind === "contact" &&
                  it.who &&
                  it.who !== name &&
                  it.who !== "환자" && <b>{it.who} · </b>}
                {it.text}
              </button>
            </div>
            {first && (
              <span className="ml-7 flex items-center gap-1.5 text-xs">
                <Badge variant="warn">먼저</Badge>
                {/* 먼저 끝낼 할 일 — 회색 맥락 글자와 구분되게 배지와 같은 계열 색 */}
                <span className="font-medium text-status-warn-fg">{first}</span>
              </span>
            )}
          </div>
        );
      })}
    </div>
  );
}

function DoneList({
  items,
  onOpen,
  onCheck,
}: {
  items: BoardItem[];
  onOpen: (uid: string) => void;
  onCheck: (item: BoardItem, done: boolean) => void;
}) {
  const [open, setOpen] = useState(false);
  if (items.length === 0) return null;
  return (
    <section className="flex flex-col gap-2">
      <button
        type="button"
        className="w-fit text-sm font-bold text-muted-foreground"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
      >
        {open ? "▾" : "▸"} 끝낸 일 {items.length}
      </button>
      {open &&
        items.map((it) => (
          <div
            key={`${it.classification_id}-${it.item}`}
            className="flex items-center gap-3 px-3 text-sm text-muted-foreground"
          >
            <Checkbox
              checked
              onCheckedChange={() => onCheck(it, false)}
              aria-label={`${it.text} 되돌리기`}
            />
            <button
              type="button"
              className="flex-1 text-left line-through"
              onClick={() => onOpen(it.call_uid)}
            >
              {it.kind === "contact" && (it.who || it.name)
                ? `${it.who || it.name} · `
                : ""}
              {it.text}
            </button>
            <span className="text-xs">
              {it.done_by} {it.done_at?.slice(11, 16)}
            </span>
          </div>
        ))}
    </section>
  );
}
