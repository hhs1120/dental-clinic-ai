import { Suspense } from "react";

import { FinanceView } from "@/components/finance/finance-view";
import { PageHeader } from "@/components/shell/page-header";

export default function FinancePage() {
  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        title="재무 대조"
        description={
          <>
            장부 수납과 카드·통장 입금을 맞춰 봅니다. 장부 수정은 전자차트에서
            합니다.
          </>
        }
      />
      {/* useSearchParams는 Suspense 안에 있어야 빌드된다(cacheComponents) */}
      <Suspense fallback={null}>
        <FinanceView />
      </Suspense>
    </div>
  );
}
