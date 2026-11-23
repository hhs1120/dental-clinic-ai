import { Suspense } from "react";

import { TodayView } from "@/components/today/today-view";
import { PageHeader } from "@/components/shell/page-header";

export default function TodayPage() {
  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        title="오늘"
        description={
          <>
            오늘 남은 일과 AI가 한 일을 한 장에 모읍니다. 숫자는 전부 기록을
            코드로 센 값이고, 누르면 처리할 화면으로 갑니다.
          </>
        }
      />
      <Suspense fallback={null}>
        <TodayView />
      </Suspense>
    </div>
  );
}
