import { Suspense } from "react";

import { AppHeader } from "@/components/app-header";

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
      <main className="mx-auto w-full max-w-6xl px-4 py-8 sm:px-6">
        {children}
      </main>
    </>
  );
}
