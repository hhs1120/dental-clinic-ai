import { Suspense } from "react";

import { AppHeader } from "@/components/app-header";
import { AskFloating } from "@/components/ask/ask-floating";

export default function PortalLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <>
      {/* usePathname이 동적 경로 아래에서 멈출 수 있어 머리 띠를 Suspense로 감싼다 */}
      <Suspense fallback={<div className="h-15 border-b bg-header" />}>
        <AppHeader />
      </Suspense>
      {/* 아래 여백 pb-24 — 떠 있는 물어보기 버튼(h-12 + bottom-6)이 마지막 줄·더보기를 가리지 않게 */}
      <main className="mx-auto w-full max-w-[90rem] px-4 pt-8 pb-24 sm:px-6">
        {children}
      </main>
      {/* 메뉴를 옮겨도 창이 남게 layout에 둔다. usePathname 때문에 Suspense로 감싼다 */}
      <Suspense fallback={null}>
        <AskFloating />
      </Suspense>
    </>
  );
}
