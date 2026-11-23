"use client";

import {
  ChevronsRightIcon,
  MenuIcon,
  MoonIcon,
  SunIcon,
  UserIcon,
  XIcon,
} from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  type MouseEvent,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";

import { MobileTabs, SpacePanel } from "@/components/shell/space-panel";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useClinic } from "@/lib/api";
import { SPACES, spaceOf } from "@/lib/nav";
import { PART_KEY, PARTS } from "@/lib/part";
import {
  currentTheme,
  saveListCollapsed,
  saveTheme,
  watchTheme,
} from "@/lib/theme";
import { cn } from "@/lib/utils";

/** 아이콘 줄 한 칸 — 아이콘 위, 작은 이름 아래 */
const railItem = (current = false) =>
  cn(
    "flex w-full flex-col items-center gap-1 rounded-md px-1 py-2 text-xs text-sidebar-foreground hover:bg-sidebar-hover",
    current &&
      "bg-sidebar-accent font-semibold text-sidebar-accent-foreground shadow-sidebar-active hover:bg-sidebar-accent",
  );

/** 담당 파트 — 화면은 바뀌지 않는다(기능 정의 §4). 저장소가 막힌 브라우저에서도 동작한다 */
function PartMenu() {
  // 빈 문자열로 시작 — 첫 렌더는 서버 HTML과 같게 비워 두고 아래 효과에서 읽는다(하이드레이션 불일치 방지)
  const [part, setPart] = useState("");
  useEffect(() => {
    let saved: string | null = null;
    try {
      saved = localStorage.getItem(PART_KEY);
    } catch {}
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
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant="rail"
          size="rail"
          aria-label={`담당 파트: ${part}`}
          title="담당 파트"
        >
          <UserIcon />
          {part || "\u00a0"}
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent side="right" align="end">
        <DropdownMenuLabel>담당 파트</DropdownMenuLabel>
        <DropdownMenuRadioGroup value={part} onValueChange={change}>
          {PARTS.map((p) => (
            <DropdownMenuRadioItem key={p} value={p}>
              {p}
            </DropdownMenuRadioItem>
          ))}
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/** 화면 테마 — 라이트↔다크 단추(lib/theme). 고른 적이 없으면 OS 설정을 따른다 */
function ThemeToggle() {
  // 서버 HTML에는 테마가 없어 빈 값 — 그리기 전 스크립트가 붙인 data-theme을 하이드레이션 뒤 읽는다
  const theme = useSyncExternalStore(watchTheme, currentTheme, () => null);
  const dark = theme === "dark";
  const Icon = dark ? MoonIcon : SunIcon;
  return (
    <Button
      variant="rail"
      size="rail"
      aria-label={dark ? "라이트 테마로 바꾸기" : "다크 테마로 바꾸기"}
      title={dark ? "라이트 테마로 바꾸기" : "다크 테마로 바꾸기"}
      onClick={() => saveTheme(dark ? "light" : "dark")}
    >
      <Icon />
      {theme === null ? "\u00a0" : dark ? "다크" : "라이트"}
    </Button>
  );
}

/** 앱 셸 — 왼쪽 아이콘 줄(공간 5개, 아래에 테마·파트)과 목록 칸, 오른쪽 본문(AI 비서 설계 §3.1).
 * 넓은 화면에서는 목록 칸을 «·» 단추로 접고 편다. 좁은 화면에서는 둘 다 서랍으로 접고 위 띠의 버튼으로 연다
 * (자료실·AI 평가 탭은 본문 위 가로 줄로도 보인다) */
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
  // 목록 칸 접힘은 <html data-list-collapsed>를 CSS가 읽는다(list-collapsed 변형) — 그리기 전 스크립트가 붙여
  // 새로 고침해도 펼쳐진 채 깜빡이지 않고, 서버 HTML과 렌더가 같다. 서랍(좁은 화면)에서는 접힘을 무시한다
  // 아이콘 줄로 다른 공간에 가면 목록 칸을 편다 — 같은 공간 안에서 옮길 때는 접힘을 그대로 둔다.
  // 펴기는 경로가 바뀌어 새 공간을 그린 뒤(그리기 전 layout effect)에 한다 — 누르자마자 펴면 옛 공간 목록이 잠깐 보인다
  // 새 탭·새 창으로 여는 클릭(수식키·가운데 단추)은 이 탭의 경로를 바꾸지 않으니 펴기를 걸어 두지 않는다
  const enter = (e: MouseEvent, key: string) => {
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0)
      return;
    if (key !== space) expandFor.current = key;
  };
  const expandFor = useRef<string | null>(null);
  useLayoutEffect(() => {
    if (expandFor.current !== space) return;
    expandFor.current = null;
    saveListCollapsed(false);
  }, [space]);
  useEffect(() => {
    if (!open) return;
    const esc = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [open]);

  return (
    <div className="flex min-h-screen">
      {open && (
        <Button
          variant="ghost"
          aria-label="메뉴 닫기"
          onClick={() => setOpen(false)}
          className="fixed inset-0 z-30 h-auto rounded-none bg-foreground/30 hover:bg-foreground/30 md:hidden print:hidden"
        />
      )}
      <aside
        className={cn(
          // 아이콘 줄과 목록 칸은 한 면 — 경계는 본문 쪽 한 줄만
          "z-40 border-r border-sidebar-border bg-sidebar print:hidden",
          open
            ? "fixed inset-y-0 left-0 flex"
            : "hidden md:sticky md:top-0 md:flex md:h-screen",
        )}
      >
        <div className="flex w-18 shrink-0 flex-col items-center gap-1 px-1.5 pt-6 pb-3">
          <Link
            href="/"
            aria-label={name || "홈"}
            onClick={(e) => enter(e, "assistant")}
            className="mb-2 flex size-8 shrink-0 items-center justify-center rounded-md bg-primary text-xs font-bold text-primary-foreground"
          >
            AX
          </Link>
          {open && (
            <Button
              variant="ghost"
              size="icon-sm"
              aria-label="메뉴 닫기"
              onClick={() => setOpen(false)}
              className="mb-1 md:hidden"
            >
              <XIcon />
            </Button>
          )}
          <nav aria-label="공간" className="flex w-full flex-col gap-1">
            {SPACES.map(({ key, label, icon: Icon, href }) => (
              <Link
                key={key}
                href={href}
                aria-current={key === space ? "page" : undefined}
                onClick={(e) => enter(e, key)}
                className={railItem(key === space)}
              >
                <Icon className="size-5" />
                {label}
              </Link>
            ))}
          </nav>
          <div className="mt-auto flex w-full flex-col gap-1">
            <ThemeToggle />
            <PartMenu />
          </div>
        </div>
        <SpacePanel
          space={space}
          onCollapse={open ? undefined : () => saveListCollapsed(true)}
          className={open ? undefined : "md:list-collapsed:hidden"}
        />
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <div className="flex items-center gap-2 border-b border-sidebar-border bg-sidebar px-4 py-2 md:hidden print:hidden">
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
        <main
          className={cn(
            "relative mx-auto w-full max-w-[90rem] px-4 pt-6 pb-12 sm:px-6 print:pb-0",
            space !== "emr" && "md:list-collapsed:pl-14",
          )}
        >
          {/* 접은 목록 칸을 펴는 단추 — 접기 단추가 있던 높이, 본문 왼쪽 위 */}
          {space !== "emr" && (
            <Button
              variant="ghost"
              size="icon-sm"
              aria-label="목록 펴기"
              title="목록 펴기"
              onClick={() => saveListCollapsed(false)}
              className="absolute top-6 left-3 hidden text-muted-foreground md:list-collapsed:inline-flex print:hidden"
            >
              <ChevronsRightIcon />
            </Button>
          )}
          <MobileTabs space={space} />
          {children}
        </main>
      </div>
    </div>
  );
}
