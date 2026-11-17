"use client";

import { MenuIcon, SunIcon, XIcon } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";

import { SpacePanel } from "@/components/shell/space-panel";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useClinic } from "@/lib/api";
import { SPACES, spaceOf } from "@/lib/nav";
import { PART_KEY, PARTS } from "@/lib/part";
import { readTheme, saveTheme, type Theme, THEMES } from "@/lib/theme";
import { cn } from "@/lib/utils";

/** 담당 파트 — 화면은 바뀌지 않는다(기능 정의 §4). 저장소가 막힌 브라우저에서도 동작한다 */
function PartSelect() {
  // 빈 문자열로 시작 — undefined면 비제어로 시작했다가 값이 들어가며 제어로 바뀌어 React 경고가 난다
  const [part, setPart] = useState("");
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
        className="w-full bg-card px-2 shadow-control"
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

/** 화면 테마 — 시스템·라이트·다크(lib/theme) */
function ThemeSelect() {
  const [theme, setTheme] = useState<Theme | "">(""); // 빈 문자열 — PartSelect와 같은 이유
  useEffect(() => {
    // 첫 렌더는 서버 HTML과 같게 비워 두고 여기서 읽는다(하이드레이션 불일치 방지)
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setTheme(readTheme());
  }, []);
  const change = (value: string) => {
    setTheme(value as Theme);
    saveTheme(value as Theme);
  };
  return (
    <Select value={theme} onValueChange={change}>
      <SelectTrigger
        aria-label="화면 테마"
        className="w-full bg-card px-2 shadow-control"
      >
        <SunIcon />
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        {THEMES.map(([v, l]) => (
          <SelectItem key={v} value={v}>
            {l}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

/** 앱 셸 — 왼쪽 아이콘 줄(공간 5개, 아래에 테마·파트)과 목록 칸, 오른쪽 본문(AI 비서 설계 §3.1).
 * 좁은 화면에서는 둘 다 서랍으로 접고 위 띠의 버튼으로 연다 */
export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const space = spaceOf(pathname);
  const clinic = useClinic();
  const name = clinic === "error" ? "—" : (clinic?.name ?? "");
  const [open, setOpen] = useState(false);

  // 화면을 옮기면 서랍을 닫는다
  const [path, setPath] = useState(pathname);
  if (path !== pathname) {
    setPath(pathname);
    setOpen(false);
  }
  useEffect(() => {
    if (!open) return;
    const esc = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [open]);

  return (
    <div className="flex min-h-screen">
      {open && (
        <button
          type="button"
          aria-label="메뉴 닫기"
          onClick={() => setOpen(false)}
          className="fixed inset-0 z-30 bg-foreground/30 md:hidden print:hidden"
        />
      )}
      <aside
        className={cn(
          "z-40 print:hidden",
          open
            ? "fixed inset-y-0 left-0 flex"
            : "hidden md:sticky md:top-0 md:flex md:h-screen",
        )}
      >
        <div className="flex w-22 shrink-0 flex-col items-center gap-1 border-r bg-header px-2 py-3">
          <Link
            href="/"
            aria-label={name || "홈"}
            className="mb-2 flex size-8 items-center justify-center rounded-md bg-primary text-xs font-bold text-primary-foreground"
          >
            AX
          </Link>
          <nav aria-label="공간" className="flex w-full flex-col gap-1">
            {SPACES.map(({ key, label, icon: Icon, href }) => (
              <Link
                key={key}
                href={href}
                aria-current={key === space ? "page" : undefined}
                className={cn(
                  "flex flex-col items-center gap-1 rounded-md px-1 py-2 text-xs text-muted-foreground hover:bg-accent hover:text-accent-foreground",
                  key === space && "bg-accent font-bold text-accent-foreground",
                )}
              >
                <Icon className="size-5" />
                {label}
              </Link>
            ))}
          </nav>
          <div className="mt-auto flex w-full flex-col gap-2">
            <ThemeSelect />
            <PartSelect />
          </div>
        </div>
        <SpacePanel space={space} />
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <div className="flex items-center gap-2 border-b bg-header px-4 py-2 md:hidden print:hidden">
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label={open ? "메뉴 닫기" : "메뉴 열기"}
            onClick={() => setOpen((o) => !o)}
          >
            {open ? <XIcon /> : <MenuIcon />}
          </Button>
          <span className="font-bold">{name}</span>
        </div>
        <main className="mx-auto w-full max-w-[90rem] px-4 pt-8 pb-12 sm:px-6 print:pb-0">
          {children}
        </main>
      </div>
    </div>
  );
}
