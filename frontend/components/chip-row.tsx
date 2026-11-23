"use client";

import { useEffect, useRef } from "react";

import { cn } from "@/lib/utils";

/** 기간 칩 한 줄 — 여러 줄로 접지 않고 가로로 민다(달·주·날짜 칩이 많은 화면).
 * 고른 칩(aria-pressed·aria-selected)이 보이게 처음과 고를 때 가로로만 옮긴다(페이지는 움직이지 않는다) */
export function ChipRow({
  label,
  role = "group",
  selected,
  className,
  children,
}: {
  label: string;
  role?: "group" | "tablist";
  selected: string | null | undefined; // 바뀌면 다시 맞춘다
  className?: string;
  children: React.ReactNode;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const box = ref.current;
    const on = box?.querySelector<HTMLElement>(
      '[aria-pressed="true"], [aria-selected="true"]',
    );
    if (!box || !on) return;
    const b = box.getBoundingClientRect();
    const r = on.getBoundingClientRect();
    if (r.left < b.left || r.right > b.right)
      box.scrollLeft += r.left - b.left - (b.width - r.width) / 2;
  }, [selected]);
  return (
    <div
      ref={ref}
      role={role}
      aria-label={label}
      className={cn(
        // pb-3 — 맥의 덮는 스크롤바가 칩 글씨를 가리지 않게 그 두께만큼 아래를 비운다
        "scroll-shadow-x flex gap-1.5 overflow-x-auto pb-3 *:shrink-0",
        className,
      )}
    >
      {children}
    </div>
  );
}
