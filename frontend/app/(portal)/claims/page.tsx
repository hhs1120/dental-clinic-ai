import { Suspense } from "react";

import { ClaimsView } from "@/components/claims/claims-view";
import { PageHeader } from "@/components/shell/page-header";

export default function ClaimsPage() {
  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        title="청구 점검"
        description={
          <>
            깎인 청구를 재심 신청 기한 순으로 보이고, 청구 전에 상병을
            확인합니다. 청구 수정·재심 제출은 사람이 합니다.
          </>
        }
      />
      {/* useSearchParams는 Suspense 안에 있어야 빌드된다(cacheComponents) */}
      <Suspense fallback={null}>
        <ClaimsView />
      </Suspense>
    </div>
  );
}
