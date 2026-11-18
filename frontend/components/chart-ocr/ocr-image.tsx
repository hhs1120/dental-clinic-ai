"use client";

import { type OcrWord } from "@/lib/chart-ocr-api";
import { cn } from "@/lib/utils";

type Box = [number, number, number, number];

const pct = (v: number) => `${(v * 100).toFixed(3)}%`;

/** 스캔 이미지 + OCR 단어 상자. 신뢰도 낮은 단어는 경고색, focus는 지금 고치는 칸의 자리 */
export function OcrImage({
  src,
  words,
  lowConfidence,
  showBoxes,
  focus,
}: {
  src: string;
  words: OcrWord[];
  lowConfidence: number;
  showBoxes: boolean;
  focus: Box | null;
}) {
  return (
    <div className="relative overflow-hidden rounded-md border border-border bg-muted">
      {/* eslint-disable-next-line @next/next/no-img-element -- API가 주는 스캔 원본 */}
      <img src={src} alt="종이 차트 스캔" className="block w-full" />
      {showBoxes &&
        words.map((w, i) => (
          <div
            key={i}
            title={`${w.text} · 신뢰도 ${Math.round(w.confidence * 100)}%`}
            className={cn(
              "absolute rounded-sm border",
              w.confidence < lowConfidence
                ? "border-status-warn-border bg-status-warn-bg/60"
                : "border-status-info-border/70 bg-status-info-bg/20",
            )}
            style={{
              left: pct(w.box[0]),
              top: pct(w.box[1]),
              width: pct(w.box[2] - w.box[0]),
              height: pct(w.box[3] - w.box[1]),
            }}
          />
        ))}
      {focus && (
        <div
          className="pointer-events-none absolute rounded-sm border-2 border-primary bg-primary/10 shadow-card"
          style={{
            left: pct(focus[0]),
            top: pct(focus[1]),
            width: pct(focus[2] - focus[0]),
            height: pct(focus[3] - focus[1]),
          }}
        />
      )}
    </div>
  );
}

/** 고치는 칸 옆에 띄우는 손글씨 조각 — 이미지에서 box 부분만 잘라 보인다 */
export function OcrSnippet({
  src,
  size,
  box,
}: {
  src: string;
  size: [number, number];
  box: Box;
}) {
  const [x0, y0, x1, y1] = box.map((v) => Math.min(Math.max(v, 0), 1)) as Box;
  const bw = x1 - x0;
  const bh = y1 - y0;
  if (bw <= 0 || bh <= 0) return null;
  return (
    <div
      className="relative w-full overflow-hidden rounded-md border border-border bg-muted"
      style={{ aspectRatio: `${bw * size[0]} / ${bh * size[1]}` }}
    >
      {/* eslint-disable-next-line @next/next/no-img-element -- 같은 스캔의 일부 */}
      <img
        src={src}
        alt="손글씨 조각"
        className="absolute max-w-none"
        style={{
          width: pct(1 / bw),
          left: pct(-x0 / bw),
          top: pct(-y0 / bh),
        }}
      />
    </div>
  );
}
