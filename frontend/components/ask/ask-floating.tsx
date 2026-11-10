"use client";

import { MessageCircleQuestionIcon, XIcon } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";

import { ChatPanel } from "@/components/ask/chat-panel";
import { Button } from "@/components/ui/button";

/** 떠 있는 버튼을 두지 않는 곳 — 물어보기 화면 자체와 전환 전 EMR 공간(기능 정의 §4 앱 구성) */
const HIDDEN = ["/ask", "/emr"];

const isHidden = (path: string) =>
  HIDDEN.some((h) => path === h || path.startsWith(`${h}/`));

/** 떠 있는 물어보기 — 오른쪽 아래 버튼, 누르면 오른쪽 질문 창(기능 정의 §4 앱 구성, 앱 설계 §9).
 * 창은 왼쪽 화면을 가리지 않고 메뉴를 옮겨도 남는다(포털 layout에 둔다). 겹침은 화면·옆 창 위(z-40),
 * 확인 창(Dialog, z-50) 아래 — 확인 창이 Esc를 먼저 쓰면(defaultPrevented) 창은 닫지 않는다 */
export function AskFloating() {
  const pathname = usePathname();
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState("");
  const [logId, setLogId] = useState<number | null>(null);

  // 물어보기·EMR로 옮기면 닫는다 — 전체 화면으로·문서함에서 보기를 누른 뒤 돌아와도 창이 다시 뜨지 않게
  const [path, setPath] = useState(pathname);
  if (path !== pathname) {
    setPath(pathname);
    if (isHidden(pathname)) setOpen(false);
  }

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !e.defaultPrevented) setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  if (isHidden(pathname)) return null;

  return (
    <>
      {!open && (
        <Button
          size="fab"
          className="fixed right-6 bottom-6 z-40"
          onClick={() => setOpen(true)}
        >
          <MessageCircleQuestionIcon />
          물어보기
        </Button>
      )}
      {/* 닫아도 그리기만 숨긴다 — 다시 열면 쓰던 질문·답이 남는다 */}
      <aside
        aria-label="물어보기"
        hidden={!open}
        className="fixed inset-y-0 right-0 z-40 flex w-full flex-col border-l bg-background shadow-card sm:w-100"
      >
        <div className="flex items-center gap-3 border-b px-4 py-3">
          <h2 className="font-bold">물어보기</h2>
          <Link
            href={
              logId === null ? "/ask?tab=chat" : `/ask?tab=chat&log=${logId}`
            }
            className="text-sm font-medium text-link hover:underline"
          >
            전체 화면으로
          </Link>
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label="닫기"
            className="ml-auto"
            onClick={() => setOpen(false)}
          >
            <XIcon />
          </Button>
        </div>
        <div className="flex-1 overflow-y-auto">
          <ChatPanel
            draft={draft}
            onDraftChange={setDraft}
            onAnswer={setLogId}
            className="max-w-none rounded-none p-4 shadow-none ring-0"
          />
        </div>
      </aside>
    </>
  );
}
