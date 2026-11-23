"use client";

import { usePathname } from "next/navigation";
import { type ReactNode, useLayoutEffect, useRef } from "react";

import { ListExpandButton } from "@/components/shell/list-expand-button";
import { SPACES, spaceOf } from "@/lib/nav";

/** 화면 제목 띠 — 본문 열 맨 위에 붙는다(넓은 화면). 공간 이름·제목·설명(디자인 새로 입히기 설계 "배치").
 * 띠 높이를 <html>의 --page-band-h로 알려, 본문 안에서 위에 붙는 칸이 띠 아래에 붙게 한다 */
export function PageHeader({
  title,
  description,
}: {
  title: string;
  description: ReactNode;
}) {
  const key = spaceOf(usePathname());
  const space = SPACES.find((s) => s.key === key)?.label;
  const ref = useRef<HTMLElement>(null);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const root = document.documentElement.style;
    const ro = new ResizeObserver(() =>
      root.setProperty("--page-band-h", `${el.offsetHeight}px`),
    );
    ro.observe(el);
    return () => {
      ro.disconnect();
      root.removeProperty("--page-band-h");
    };
  }, []);
  return (
    <header
      ref={ref}
      data-slot="page-header"
      // 본문 패딩만큼 음수 여백 — 띠가 본문 열 끝까지 닿는다. 인쇄는 제목만
      className="z-20 -mx-4 mb-2 flex items-start gap-2 border-b bg-card px-4 py-3.5 md:sticky md:-mx-12 md:px-12 md:top-0 md:-mt-6 md:list-collapsed:pl-3 print:static print:m-0 print:border-0 print:bg-transparent print:p-0!"
    >
      <ListExpandButton className="mt-3" />
      <div className="flex min-w-0 flex-col">
        <span className="text-xs text-muted-foreground print:hidden">
          {space}
        </span>
        <h1 className="text-page-title font-medium tracking-tight">{title}</h1>
        <p className="text-page-desc text-muted-foreground print:hidden">
          {description}
        </p>
      </div>
    </header>
  );
}
