import { Suspense } from "react";

import { ClaimsView } from "@/components/claims/claims-view";

export default function ClaimsPage() {
  return (
    <div className="flex flex-col gap-5">
      <header className="flex flex-col gap-1">
        <h1 className="text-2xl font-bold">청구 점검</h1>
        <p className="text-muted-foreground">
          깎인 청구를 재심 신청 기한 순으로 보이고, 청구 전에 상병을 확인합니다.
          청구 수정·재심 제출은 사람이 합니다.
        </p>
      </header>
      {/* useSearchParams는 Suspense 안에 있어야 빌드된다(cacheComponents) */}
      <Suspense fallback={null}>
        <ClaimsView />
      </Suspense>
    </div>
  );
}
