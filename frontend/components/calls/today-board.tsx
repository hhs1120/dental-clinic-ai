"use client";

import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import { type Board, type BoardItem, shortAt } from "@/lib/calls-api";
import { cn } from "@/lib/utils";

/** "오늘" 두 칸 — 왼쪽 병원 안에서 할 일, 오른쪽 연락할 사람(통화·② 판정 화면 설계 "오늘") */
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
      {items.map((it) => (
        <Row
          key={`${it.classification_id}-${it.item}`}
          item={it}
          on={selected === it.call_uid}
          onOpen={onOpen}
          onCheck={onCheck}
          note={note?.(it) ?? null}
        />
      ))}
    </section>
  );
}

function Row({
  item,
  on,
  onOpen,
  onCheck,
  note,
}: {
  item: BoardItem;
  on: boolean;
  onOpen: (uid: string) => void;
  onCheck: (item: BoardItem, done: boolean) => void;
  note: string | null;
}) {
  const label = item.kind === "contact" ? item.who || item.name : item.name;
  return (
    <div
      className={cn(
        "flex items-start gap-3 rounded-lg border bg-card px-3 py-2.5 shadow-card",
        on && "border-primary",
      )}
    >
      <Checkbox
        className="mt-0.5"
        checked={item.done}
        onCheckedChange={(v) => onCheck(item, v === true)}
        aria-label={`${item.text} 끝냄`}
      />
      <button
        type="button"
        className="flex min-w-0 flex-1 flex-col items-start gap-0.5 text-left"
        onClick={() => onOpen(item.call_uid)}
      >
        <span className="flex flex-wrap items-center gap-1.5">
          {item.kind === "contact" && label && (
            <b className="whitespace-nowrap">{label}</b>
          )}
          {item.emergency && <Badge variant="danger">응급</Badge>}
          {!item.emergency && item.urgent && (
            <Badge variant="danger">급함</Badge>
          )}
          <span>{item.text}</span>
        </span>
        <span className="text-xs text-muted-foreground">
          {shortAt(item.at)} 통화{item.name ? ` · ${item.name}` : ""}
          {item.chart_no ? ` ${item.chart_no}` : ""}
        </span>
        {note && (
          <span className="mt-1 flex items-center gap-1.5 text-xs text-muted-foreground">
            <span className="shrink-0 rounded bg-muted px-1.5 py-0.5 font-medium text-foreground">
              먼저
            </span>
            {note}
          </span>
        )}
      </button>
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
