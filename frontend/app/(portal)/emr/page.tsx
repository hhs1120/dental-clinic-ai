import { Suspense } from "react";

import { EmrView } from "@/components/emr/emr-view";

export default function EmrPage() {
  return (
    // useSearchParams는 Suspense 안에 있어야 빌드된다(cacheComponents)
    <Suspense fallback={null}>
      <EmrView />
    </Suspense>
  );
}
