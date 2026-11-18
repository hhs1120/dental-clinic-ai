import { ChartOcrView } from "@/components/chart-ocr/chart-ocr-view";

export default function ChartOcrPage() {
  return (
    <div className="flex flex-col gap-5">
      <header className="flex flex-col gap-1">
        <h1 className="text-2xl font-bold">종이 차트 이관</h1>
        <p className="text-muted-foreground">
          EMR 이전의 종이 차트를 스캔하면 업스테이지 OCR이 글자를 읽고 항목을
          정리합니다. 자신 없는 칸은 표시되고, 사람이 원본 손글씨와 비교해 고친
          뒤 승인하면 EMR 진료 기록에 &ldquo;종이 차트 이관&rdquo;으로 붙습니다.
        </p>
      </header>
      <ChartOcrView />
    </div>
  );
}
