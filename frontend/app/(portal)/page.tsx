import { Suspense } from "react";

import { AssistantView } from "@/components/assistant/assistant-view";

// AI 비서 — 시작 화면이자 대화 화면(AI 비서 설계 §3.2). useSearchParams는 Suspense 안에 있어야 빌드된다
export default function AssistantPage() {
  return (
    <Suspense fallback={null}>
      <AssistantView />
    </Suspense>
  );
}
