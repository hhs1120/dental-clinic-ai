import { Suspense } from "react";

import { ReportView } from "@/components/report/report-view";

export default function ReportPage() {
  return (
    <div className="flex flex-col gap-5">
      <header className="flex flex-col gap-1">
        <h1 className="text-2xl font-bold">경영 리포트</h1>
        <p className="text-muted-foreground">
          숫자는 병원 기록에서 코드가 계산하고, AI는 이전 기간 대비 주목할
          변화를 골라 설명합니다. 인건비는 실제 급여가 아니라 가정값이라 그 값이
          들어간 숫자에는 [추정]을 붙입니다.
        </p>
      </header>
      {/* useSearchParams는 Suspense 안에 있어야 빌드된다(cacheComponents) */}
      <Suspense fallback={null}>
        <ReportView />
      </Suspense>
    </div>
  );
}
