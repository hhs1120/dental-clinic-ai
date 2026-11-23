import { Suspense } from "react";

import { ReportView } from "@/components/report/report-view";
import { PageHeader } from "@/components/shell/page-header";

export default function ReportPage() {
  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        title="경영 리포트"
        description={
          <>
            숫자는 병원 기록에서 코드가 계산하고, AI는 이전 기간 대비 주목할
            변화를 골라 설명합니다.
          </>
        }
      />
      {/* useSearchParams는 Suspense 안에 있어야 빌드된다(cacheComponents) */}
      <Suspense fallback={null}>
        <ReportView />
      </Suspense>
    </div>
  );
}
