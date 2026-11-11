import { Suspense } from "react";

import { FinanceView } from "@/components/finance/finance-view";

export default function FinancePage() {
  return (
    <div className="flex flex-col gap-5">
      <header className="flex flex-col gap-1">
        <h1 className="text-2xl font-bold">재무 대조</h1>
        <p className="text-muted-foreground">
          장부 수납과 카드·통장 입금을 맞춰 봅니다. 장부 수정은 전자차트에서
          합니다.
        </p>
      </header>
      {/* useSearchParams는 Suspense 안에 있어야 빌드된다(cacheComponents) */}
      <Suspense fallback={null}>
        <FinanceView />
      </Suspense>
    </div>
  );
}
