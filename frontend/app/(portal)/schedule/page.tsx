import { Suspense } from "react";

import { ScheduleView } from "@/components/schedule/schedule-view";

export default function SchedulePage() {
  return (
    <div className="flex flex-col gap-5">
      <header className="flex flex-col gap-1">
        <h1 className="text-2xl font-bold">예약</h1>
        <p className="text-muted-foreground">
          날짜별 예약을 담당자별로 봅니다. 예약을 잡거나 바꾸는 일은 EMR에서
          합니다.
        </p>
      </header>
      {/* useSearchParams는 Suspense 안에 있어야 빌드된다(cacheComponents) */}
      <Suspense fallback={null}>
        <ScheduleView />
      </Suspense>
    </div>
  );
}
