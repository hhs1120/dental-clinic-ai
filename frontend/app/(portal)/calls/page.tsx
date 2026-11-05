import { Suspense } from "react";

import { CallsView } from "@/components/calls/calls-view";

export default function CallsPage() {
  return (
    <div className="flex flex-col gap-5">
      <header className="flex flex-col gap-1">
        <h1 className="text-2xl font-bold">통화</h1>
        <p className="text-muted-foreground">
          끝난 통화마다 AI가 요약·분류·할 일을 붙입니다. 오늘 할 일과 연락할
          사람을 먼저 봅니다.
        </p>
      </header>
      {/* useSearchParams는 Suspense 안에 있어야 빌드된다(cacheComponents) */}
      <Suspense fallback={null}>
        <CallsView />
      </Suspense>
    </div>
  );
}
