import { Suspense } from "react";

import { PrepView } from "@/components/prep/prep-view";

export default function PrepPage() {
  return (
    <div className="flex flex-col gap-5 print:gap-2">
      <header className="flex flex-col gap-1">
        <h1 className="text-2xl font-bold">준비표</h1>
        <p className="text-muted-foreground print:hidden">
          오늘 예약 환자마다 EMR에 흩어진 사실을 한 줄로 모으고, 챙길 일을 AI가
          한두 줄로 제안합니다. 칩은 코드가 켜고 제안은 사실과 대조해 거릅니다.
        </p>
      </header>
      {/* useSearchParams는 Suspense 안에 있어야 빌드된다(cacheComponents) */}
      <Suspense fallback={null}>
        <PrepView />
      </Suspense>
    </div>
  );
}
