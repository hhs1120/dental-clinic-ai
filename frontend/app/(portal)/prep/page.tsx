import { Suspense } from "react";

import { PrepView } from "@/components/prep/prep-view";
import { PageHeader } from "@/components/shell/page-header";

export default function PrepPage() {
  return (
    <div className="flex flex-col gap-5 print:gap-2">
      <PageHeader
        title="준비표"
        description={
          <>
            오늘 예약 환자마다 EMR에 흩어진 사실을 한 줄로 모으고, 챙길 일을
            AI가 한두 줄로 제안합니다. 칩은 코드가 켜고 제안은 사실과 대조해
            거릅니다.
          </>
        }
      />
      {/* useSearchParams는 Suspense 안에 있어야 빌드된다(cacheComponents) */}
      <Suspense fallback={null}>
        <PrepView />
      </Suspense>
    </div>
  );
}
