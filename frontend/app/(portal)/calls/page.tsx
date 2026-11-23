import { Suspense } from "react";

import { CallsView } from "@/components/calls/calls-view";
import { PageHeader } from "@/components/shell/page-header";

export default function CallsPage() {
  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        title="통화"
        description={
          <>
            끝난 통화마다 AI가 요약·분류·할 일을 붙입니다. 오늘 할 일과 연락할
            사람을 먼저 봅니다.
          </>
        }
      />
      {/* useSearchParams는 Suspense 안에 있어야 빌드된다(cacheComponents) */}
      <Suspense fallback={null}>
        <CallsView />
      </Suspense>
    </div>
  );
}
