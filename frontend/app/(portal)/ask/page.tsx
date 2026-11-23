import { Suspense } from "react";

import { AskView } from "@/components/ask/ask-view";
import { PageHeader } from "@/components/shell/page-header";

export default function AskPage() {
  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="자료실"
        description={<>매뉴얼·공지와 지난 통화·문자·차트 기록을 찾습니다.</>}
      />
      {/* useSearchParams는 Suspense 안에 있어야 빌드된다(cacheComponents) */}
      <Suspense fallback={null}>
        <AskView />
      </Suspense>
    </div>
  );
}
