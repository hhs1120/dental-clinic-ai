import { Suspense } from "react";

import { AppShell } from "@/components/shell/app-shell";

export default function PortalLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  // usePathname·useSearchParams가 동적 경로 아래에서 멈출 수 있어 셸을 Suspense로 감싼다
  return (
    <Suspense fallback={<div className="min-h-screen bg-header" />}>
      <AppShell>{children}</AppShell>
    </Suspense>
  );
}
