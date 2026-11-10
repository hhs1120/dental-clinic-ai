import { Suspense } from "react";

import { MissedView } from "@/components/missed/missed-view";

export default function MissedPage() {
  return (
    <div className="flex flex-col gap-5">
      <header className="flex flex-col gap-1">
        <h1 className="text-2xl font-bold">미예약 환자</h1>
        <p className="text-muted-foreground">
          다시 올 이유가 있는데 다음 예약이 없는 환자를, 연락하면 결과가 날
          가능성 순으로 보입니다. 칩과 순위는 코드가 정하고, EMR에 예약이
          잡히거나 &ldquo;예약함&rdquo;을 남기면 빠집니다.
        </p>
      </header>
      {/* useSearchParams는 Suspense 안에 있어야 빌드된다(cacheComponents) */}
      <Suspense fallback={null}>
        <MissedView />
      </Suspense>
    </div>
  );
}
