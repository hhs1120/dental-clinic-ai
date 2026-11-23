import { Suspense } from "react";

import { MissedView } from "@/components/missed/missed-view";
import { PageHeader } from "@/components/shell/page-header";

export default function MissedPage() {
  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        title="미예약 환자"
        description={
          <>
            다시 올 이유가 있는데 다음 예약이 없는 환자를, 연락하면 결과가 날
            가능성 순으로 보입니다. 칩과 순위는 코드가 정하고, EMR에 예약이
            잡히거나 &ldquo;예약함&rdquo;을 남기면 빠집니다.
          </>
        }
      />
      {/* useSearchParams는 Suspense 안에 있어야 빌드된다(cacheComponents) */}
      <Suspense fallback={null}>
        <MissedView />
      </Suspense>
    </div>
  );
}
