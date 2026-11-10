"use client";

import { MessageCircleQuestionIcon, XIcon } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { ChatPanel, type Latest } from "@/components/ask/chat-panel";
import { Button } from "@/components/ui/button";

/** 떠 있는 버튼을 두지 않는 곳 — 물어보기 화면 자체와 전환 전 EMR 공간(기능 정의 §4 앱 구성) */
const HIDDEN = ["/ask", "/emr"];

const isHidden = (path: string) =>
  HIDDEN.some((h) => path === h || path.startsWith(`${h}/`));

/** 떠 있는 물어보기 — 오른쪽 아래 버튼, 누르면 오른쪽 질문 창(기능 정의 §4 앱 구성, 앱 설계 §9).
 * 창은 왼쪽 화면을 가리지 않고 메뉴를 옮겨도 남는다(포털 layout에 둔다). 겹침은 화면·옆 창 위,
 * 확인 창(Dialog) 아래 */
export function AskFloating() {
  const pathname = usePathname();
  const hidden = isHidden(pathname);
  const [open, setOpen] = useState(false);
  const [used, setUsed] = useState(false); // 한 번 연 뒤로는 창을 계속 그려 둔다
  const [draft, setDraft] = useState("");
  const [latest, setLatest] = useState<Latest>({ busy: false, logId: null });
  const fabRef = useRef<HTMLButtonElement>(null);
  const asideRef = useRef<HTMLElement>(null);

  // 물어보기·EMR로 옮기면 닫는다 — 전체 화면으로·문서함에서 보기를 누른 뒤 돌아와도 창이 다시 뜨지 않게.
  // 창은 숨기기만 해서 쓰던 질문·답·초안과 받던 답이 그대로 남는다
  const [path, setPath] = useState(pathname);
  if (path !== pathname) {
    setPath(pathname);
    if (hidden) setOpen(false);
  }

  const show = () => {
    setUsed(true);
    setOpen(true);
  };

  // 열면 입력칸으로, X·Esc로 닫으면 버튼으로 커서를 보낸다
  const opened = useRef(false);
  useEffect(() => {
    if (open) asideRef.current?.querySelector("textarea")?.focus();
    else if (opened.current) fabRef.current?.focus();
    opened.current = open;
  }, [open]);

  return (
    <>
      {!open && !hidden && (
        <Button
          ref={fabRef}
          size="fab"
          className="fixed right-6 bottom-6 z-40 print:hidden"
          onClick={show}
        >
          <MessageCircleQuestionIcon />
          물어보기
        </Button>
      )}
      {/* 닫아도 그리기만 숨긴다 — 다시 열면 쓰던 질문·답이 남는다.
          창 안에서 친 키는 밖으로 보내지 않는다 — AI 평가 화면의 숫자·Enter 단축키가 같이 눌리지 않게 */}
      {used && (
        <aside
          ref={asideRef}
          aria-label="물어보기"
          hidden={!open || hidden}
          onKeyDown={(e) => {
            e.stopPropagation();
            // 한글 조합 중 Esc, 창 안 확인 창이 먼저 쓴 Esc(defaultPrevented)는 창을 닫지 않는다
            if (
              e.key === "Escape" &&
              !e.nativeEvent.isComposing &&
              !e.defaultPrevented
            )
              setOpen(false);
          }}
          className="fixed inset-y-0 right-0 z-40 flex w-full flex-col border-l bg-background shadow-card sm:w-100 print:hidden"
        >
          <div className="flex items-center gap-3 border-b px-4 py-3">
            <h2 className="font-bold">물어보기</h2>
            {/* 답을 기다리는 동안은 끈다 — 지금 질문이 아닌 이전 답을 열지 않게 */}
            {latest.busy ? (
              <span
                aria-disabled="true"
                className="text-sm font-medium text-muted-foreground"
              >
                전체 화면으로
              </span>
            ) : (
              <Link
                href={
                  latest.logId === null
                    ? "/ask?tab=chat"
                    : `/ask?tab=chat&log=${latest.logId}`
                }
                className="text-sm font-medium text-link hover:underline"
              >
                전체 화면으로
              </Link>
            )}
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
              onLatest={setLatest}
              className="max-w-none rounded-none p-4 shadow-none ring-0"
            />
          </div>
        </aside>
      )}
    </>
  );
}
