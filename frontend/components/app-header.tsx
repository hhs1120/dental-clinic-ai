"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useClinic } from "@/lib/api";
import { MENU, SPACES } from "@/lib/nav";
import { cn } from "@/lib/utils";

const PARTS = ["원장", "데스크", "상담실장"];
const PART_KEY = "ax.part";

/** 담당 파트 — 화면은 바뀌지 않는다(기능 정의 §4). 저장소가 막힌 브라우저에서도 동작한다 */
function PartSelect() {
  const [part, setPart] = useState<string | undefined>(undefined);
  useEffect(() => {
    let saved: string | null = null;
    try {
      saved = localStorage.getItem(PART_KEY);
    } catch {}
    // 첫 렌더는 서버 HTML과 같게 비워 두고 여기서 읽는다(하이드레이션 불일치 방지)
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setPart(saved && PARTS.includes(saved) ? saved : "데스크");
  }, []);
  const change = (value: string) => {
    setPart(value);
    try {
      localStorage.setItem(PART_KEY, value);
    } catch {}
  };
  return (
    <Select value={part} onValueChange={change}>
      <SelectTrigger
        aria-label="담당 파트"
        className="w-28 bg-card shadow-control"
      >
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        {PARTS.map((p) => (
          <SelectItem key={p} value={p}>
            {p}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

export function AppHeader() {
  const pathname = usePathname();
  const router = useRouter();
  const clinic = useClinic();
  const space = SPACES[pathname.split("/")[1]];
  const name = clinic === "error" ? "—" : (clinic?.name ?? "");
  const today = clinic === "error" ? "—" : (clinic?.today ?? "");

  const back = () => (history.length > 1 ? router.back() : router.push("/"));

  return (
    <header className="flex min-h-15 flex-wrap items-center gap-x-6 gap-y-2 border-b bg-header px-4 py-2 sm:px-6">
      <Link href="/" className="flex items-center gap-2.5 font-bold">
        <span className="flex size-7.5 items-center justify-center rounded-md bg-primary text-xs text-primary-foreground">
          AX
        </span>
        <span>{name}</span>
      </Link>
      {space ? (
        <div className="flex flex-1 items-center gap-3">
          <span className="font-bold">{space}</span>
          <Button variant="ghost" size="sm" onClick={back}>
            ← 돌아가기
          </Button>
        </div>
      ) : (
        <nav aria-label="메뉴" className="flex flex-1 flex-wrap gap-1">
          {MENU.map((m) => {
            const current =
              pathname === m.href || pathname.startsWith(`${m.href}/`);
            return (
              <Link
                key={m.href}
                href={m.href}
                aria-current={current ? "page" : undefined}
                className={cn(
                  "rounded-md px-3 py-2 text-sm text-muted-foreground hover:bg-accent hover:text-accent-foreground",
                  current && "bg-accent font-bold text-accent-foreground",
                )}
              >
                {m.label}
              </Link>
            );
          })}
        </nav>
      )}
      <div className="flex items-center gap-4 text-sm text-muted-foreground">
        <PartSelect />
        <span>
          기준일{" "}
          <strong className="font-medium text-foreground">{today}</strong>
        </span>
      </div>
    </header>
  );
}
