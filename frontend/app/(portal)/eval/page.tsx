import { Suspense } from "react";

import { EvalView } from "@/components/eval/eval-view";
import { PageHeader } from "@/components/shell/page-header";

export default function EvalPage() {
  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        title="AI 평가"
        description={
          <>
            판정기가 모든 통화를 판정하고, 사람은 두 AI가 엇갈린 통화와 표본만
            봅니다. 모델 라벨은 저장한 뒤에만 보입니다.
          </>
        }
      />
      <Suspense fallback={null}>
        <EvalView />
      </Suspense>
    </div>
  );
}
