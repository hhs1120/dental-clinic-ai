import { Suspense } from "react";

import { ScheduleView } from "@/components/schedule/schedule-view";
import { PageHeader } from "@/components/shell/page-header";

export default function SchedulePage() {
  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        title="예약"
        description={
          <>
            날짜별 예약을 담당자별로 봅니다. 예약을 잡거나 바꾸는 일은 EMR에서
            합니다.
          </>
        }
      />
      {/* useSearchParams는 Suspense 안에 있어야 빌드된다(cacheComponents) */}
      <Suspense fallback={null}>
        <ScheduleView />
      </Suspense>
    </div>
  );
}
