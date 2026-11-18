"use client";

import { ChevronsLeftIcon, Trash2Icon } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { deleteConversation } from "@/lib/assistant-api";
import { errorText, getJson, useClinic, useFetch } from "@/lib/api";
import { readPart } from "@/lib/part";
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

/** 업무 메뉴 줄 옆 건수 — 오늘 화면(GET /today)과 같은 값, 0건·못 불러온 칸은 비운다.
 * 업무 안에서 화면을 옮길 때마다 다시 센다(fetchToday가 같은 순간의 요청을 나눠 쓴다) */
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
  const { data } = useFetch<TodaySheet>(`shell-today:${pathname}`, () =>
    fetchToday(),
  );
  // 화면을 옮겨 다시 불러오는 동안(data null) 지난 숫자를 보인다 — 숫자가 깜박이지 않게
  const [last, setLast] = useState(data);
  if (data && data !== last) setLast(data);
  const n = counts(data ?? last);
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

/** 목록 칸은 세로 메뉴, 좁은 화면 본문 위에서는 가로 탭 줄(MobileTabs) */
const menuClass = (row: boolean) =>
  row
    ? "flex gap-1 overflow-x-auto *:shrink-0 *:whitespace-nowrap"
    : "flex flex-col gap-0.5";

function EvalMenu({ tab, row = false }: { tab: string; row?: boolean }) {
  return (
    <nav aria-label={row ? "탭" : "메뉴"} className={menuClass(row)}>
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

/** 자료실 목록 칸 — 기록 검색·문서함(?tab=). 탭을 바꿔도 검색어 등 나머지 주소 값은 남긴다 */
function LibraryMenu({
  params,
  row = false,
}: {
  params: URLSearchParams;
  row?: boolean;
}) {
  const tab = params.get("tab") === "docs" ? "docs" : "search";
  return (
    <nav aria-label={row ? "탭" : "메뉴"} className={menuClass(row)}>
      {LIBRARY_TABS.map(([key, label]) => {
        const next = new URLSearchParams(params);
        next.set("tab", key);
        return (
          <Link
            key={key}
            href={`/ask?${next}`}
            aria-current={tab === key ? "page" : undefined}
            className={itemClass(tab === key)}
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
  const key = `conv:${current}`;
  const first = useFetch<Conversation[]>(key, (s) =>
    getJson<Conversation[]>("/assistant/conversations", undefined, s),
  );
  // 더 불러온 쪽은 첫 쪽 키에 묶는다 — 첫 쪽을 다시 받으면(대화를 바꾸면) 비우고 더보기도 다시 보인다
  const [paged, setPaged] = useState({
    key,
    more: [] as Conversation[],
    done: false,
  });
  const { more, done } =
    paged.key === key ? paged : { more: [] as Conversation[], done: false };
  const router = useRouter();
  const [target, setTarget] = useState<Conversation | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  const [gone, setGone] = useState<number[]>([]); // 방금 지운 대화 — 더 불러온 쪽에서도 뺀다
  const rows = [...(first.data ?? []), ...more].filter(
    (c) => !gone.includes(c.id),
  );
  const confirmDelete = async () => {
    if (!target) return;
    try {
      await deleteConversation(target.id, readPart());
    } catch (e) {
      setFailed(errorText(e));
      return;
    }
    setGone((g) => [...g, target.id]);
    if (current === String(target.id)) router.push("/");
    first.reload();
    setTarget(null);
  };
  const loadMore = async () => {
    const last = rows[rows.length - 1];
    const page = await getJson<Conversation[]>(
      "/assistant/conversations",
      new URLSearchParams({ before: String(last.id) }),
    ).catch(() => []);
    setPaged((p) => ({
      key,
      more: [...(p.key === key ? p.more : []), ...page],
      done: page.length < PAGE,
    }));
  };
  return (
    <div className="flex flex-col gap-0.5">
      <Link href="/" className={itemClass(current === null)}>
        + 새 대화
      </Link>
      {first.data?.length === 0 && (
        <p className="mt-2 px-3 text-xs text-muted-foreground">
          지난 대화가 아직 없습니다. 물어본 대화는 여기에 쌓입니다.
        </p>
      )}
      <nav aria-label="지난 대화" className="mt-2 flex flex-col gap-0.5">
        {rows.map((c) => (
          <div key={c.id} className="group relative">
            <Link
              href={`/?c=${c.id}`}
              aria-current={current === String(c.id) ? "page" : undefined}
              className={cn(itemClass(current === String(c.id)), "pr-9")}
            >
              <span className="truncate">{c.title}</span>
            </Link>
            <Button
              variant="ghost"
              size="icon-sm"
              aria-label={`대화 삭제: ${c.title}`}
              onClick={() => {
                setFailed(null);
                setTarget(c);
              }}
              className={cn(
                // 터치 화면(hover 없음)에서는 늘 보인다 — 보이지 않는 단추를 눌러 지우지 않게
                "absolute top-1/2 right-1 -translate-y-1/2 opacity-0 group-hover:opacity-100 focus-visible:opacity-100 [@media(hover:none)]:opacity-100",
                current === String(c.id) && "opacity-100",
              )}
            >
              <Trash2Icon />
            </Button>
          </div>
        ))}
      </nav>
      <Dialog
        open={target !== null}
        onOpenChange={(o) => !o && setTarget(null)}
      >
        <DialogContent showCloseButton={false}>
          <DialogHeader>
            <DialogTitle>대화 삭제</DialogTitle>
            <DialogDescription>
              이 대화를 목록에서 지울까요? 예약 기록에서는 계속 볼 수 있습니다.
            </DialogDescription>
          </DialogHeader>
          {failed && <p className="text-sm text-destructive">{failed}</p>}
          <DialogFooter>
            <Button variant="outline" onClick={() => setTarget(null)}>
              취소
            </Button>
            <Button variant="destructive" onClick={() => void confirmDelete()}>
              삭제
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      {first.data && first.data.length >= PAGE && !done && (
        <Button variant="ghost" size="sm" onClick={loadMore}>
          더보기
        </Button>
      )}
    </div>
  );
}

const LIBRARY_TABS = [
  ["search", "기록 검색"],
  ["docs", "문서함"],
] as const;

const TITLES: Record<SpaceKey, string> = {
  assistant: "대화",
  work: "업무",
  library: "자료실",
  eval: "AI 평가",
  emr: "EMR",
};

/** 좁은 화면(md 미만) 본문 위 가로 탭 줄 — 자료실·AI 평가는 탭이 목록 칸에만 있어 서랍 없이도 닿게 한다 */
export function MobileTabs({ space }: { space: SpaceKey }) {
  const params = useSearchParams();
  if (space !== "eval" && space !== "library") return null;
  return (
    <div className="mb-4 border-b pb-2 md:hidden print:hidden">
      {space === "eval" ? (
        <EvalMenu tab={params.get("tab") ?? ""} row />
      ) : (
        <LibraryMenu params={params} row />
      )}
    </div>
  );
}

/** 목록 칸 — 공간마다 내용이 다르다(AI 비서 설계 §3.1). 기준일은 맨 아래. EMR은 목록 칸이 없다.
 * onCollapse가 있으면(넓은 화면) 제목 옆 « 단추로 접는다 — 펴는 단추는 아이콘 줄(app-shell) */
export function SpacePanel({
  space,
  onCollapse,
  className,
}: {
  space: SpaceKey;
  onCollapse?: () => void;
  className?: string;
}) {
  const pathname = usePathname();
  const params = useSearchParams();
  const clinic = useClinic();
  const today = clinic === "error" ? "—" : (clinic?.today ?? "");
  if (space === "emr") return null;
  return (
    <div
      className={cn(
        "flex w-46 flex-col gap-3 border-r bg-card px-3 pt-6 pb-3",
        className,
      )}
    >
      <div className="flex h-8 items-center justify-between gap-1 pl-3">
        <h2 className="text-xs font-bold text-muted-foreground">
          {TITLES[space]}
        </h2>
        {onCollapse && (
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label="목록 접기"
            title="목록 접기"
            onClick={onCollapse}
            className="text-muted-foreground"
          >
            <ChevronsLeftIcon />
          </Button>
        )}
      </div>
      <div className="flex-1 overflow-y-auto">
        {space === "assistant" && (
          <ConversationList current={params.get("c")} />
        )}
        {space === "work" && <WorkMenu pathname={pathname} />}
        {space === "eval" && <EvalMenu tab={params.get("tab") ?? ""} />}
        {space === "library" && <LibraryMenu params={params} />}
      </div>
      <p className="px-3 text-xs text-muted-foreground">
        기준일 <strong className="font-medium text-foreground">{today}</strong>
      </p>
    </div>
  );
}
