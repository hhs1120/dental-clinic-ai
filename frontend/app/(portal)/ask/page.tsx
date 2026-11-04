import { Suspense } from "react";

import { AskView } from "@/components/ask/ask-view";

export default function AskPage() {
  return (
    <div className="flex flex-col gap-6">
      <header className="flex flex-col gap-1">
        <h1 className="text-2xl font-bold">물어보기</h1>
        <p className="text-muted-foreground">
          매뉴얼·공지와 지난 통화·문자·차트 기록을 찾습니다.
        </p>
      </header>
      {/* useSearchParams는 Suspense 안에 있어야 빌드된다(cacheComponents) */}
      <Suspense fallback={null}>
        <AskView />
      </Suspense>
    </div>
  );
}
