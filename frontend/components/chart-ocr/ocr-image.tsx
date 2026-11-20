"use client";

import { Maximize2, ZoomIn, ZoomOut } from "lucide-react";
import {
  type Dispatch,
  type ReactNode,
  type SetStateAction,
  useEffect,
  useRef,
  useState,
} from "react";

import { Button } from "@/components/ui/button";
import { type OcrWord } from "@/lib/chart-ocr-api";
import { cn } from "@/lib/utils";

type Box = [number, number, number, number];

const pct = (v: number) => `${(v * 100).toFixed(3)}%`;

/** 원본 확대 — s는 배율, x·y는 보이는 창 왼쪽 위의 이미지 자리(0~1) */
export type View = { s: number; x: number; y: number };
export const FULL_VIEW: View = { s: 1, x: 0, y: 0 };
const MAX_SCALE = 6;
const STEP = 1.5;
// 손글씨 조각의 최소 높이(너비 대비)
const MIN_HEIGHT = 1 / 4;

const clampView = (s: number, x: number, y: number): View => {
  const sc = Math.min(Math.max(s, 1), MAX_SCALE);
  const room = 1 - 1 / sc;
  return {
    s: sc,
    x: Math.min(Math.max(x, 0), room),
    y: Math.min(Math.max(y, 0), room),
  };
};

/** 창 안 (px, py) 자리의 이미지 점을 그대로 두고 배율만 바꾼다 */
const zoomAt = (v: View, s: number, px = 0.5, py = 0.5): View => {
  const sc = Math.min(Math.max(s, 1), MAX_SCALE);
  return clampView(sc, v.x + px / v.s - px / sc, v.y + py / v.s - py / sc);
};

/** 배율을 두고 box를 창 가운데로 — 확대한 채 다른 칸으로 따라간다 */
export const centerOn = (v: View, box: Box): View =>
  clampView(
    v.s,
    (box[0] + box[2]) / 2 - 0.5 / v.s,
    (box[1] + box[3]) / 2 - 0.5 / v.s,
  );

/** 확대 창 — 휠·버튼으로 키우고 키운 뒤에는 끌어서 옮긴다. 안의 층은 % 자리라 상자도 같이 움직인다.
 * clickZoom이면 끌지 않고 누른 자리를 한 단계 키운다 */
function ZoomPane({
  aspect,
  view,
  onView,
  clickZoom = false,
  className,
  children,
}: {
  aspect: string;
  view: View;
  onView: Dispatch<SetStateAction<View>>;
  clickZoom?: boolean;
  className?: string;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const drag = useRef<{
    px: number;
    py: number;
    v: View;
    moved: boolean;
  } | null>(null);
  const [dragging, setDragging] = useState(false);

  // React onWheel은 passive라 막을 수 없다 — 그림 위 휠만 확대에 쓰고, 1배에서 줄이는 휠은 페이지로 넘긴다
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      const out = e.deltaY > 0;
      onView((v) => {
        if (out && v.s <= 1) return v;
        const r = el.getBoundingClientRect();
        const px = (e.clientX - r.left) / r.width;
        const py = (e.clientY - r.top) / r.height;
        return zoomAt(v, v.s * Math.exp(-e.deltaY * 0.002), px, py);
      });
      if (!(out && view.s <= 1)) e.preventDefault();
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [onView, view.s]);

  const zoomed = view.s > 1;
  const end = () => {
    drag.current = null;
    setDragging(false);
  };
  return (
    <div className="group relative">
      <div
        ref={ref}
        className={cn(
          "relative touch-none overflow-hidden rounded-md border border-border bg-muted select-none",
          zoomed
            ? dragging
              ? "cursor-grabbing"
              : "cursor-grab"
            : clickZoom && "cursor-zoom-in",
          className,
        )}
        style={{ aspectRatio: aspect }}
        onPointerDown={(e) => {
          drag.current = {
            px: e.clientX,
            py: e.clientY,
            v: view,
            moved: false,
          };
          e.currentTarget.setPointerCapture(e.pointerId);
        }}
        onPointerMove={(e) => {
          const d = drag.current;
          if (!d || !zoomed) return;
          const dx = e.clientX - d.px;
          const dy = e.clientY - d.py;
          if (!d.moved && Math.hypot(dx, dy) < 3) return;
          d.moved = true;
          setDragging(true);
          const r = e.currentTarget.getBoundingClientRect();
          onView(
            clampView(
              d.v.s,
              d.v.x - dx / r.width / d.v.s,
              d.v.y - dy / r.height / d.v.s,
            ),
          );
        }}
        onPointerUp={(e) => {
          const d = drag.current;
          end();
          if (!clickZoom || !d || d.moved || view.s >= MAX_SCALE) return;
          const r = e.currentTarget.getBoundingClientRect();
          onView((v) =>
            zoomAt(
              v,
              v.s * STEP,
              (e.clientX - r.left) / r.width,
              (e.clientY - r.top) / r.height,
            ),
          );
        }}
        onPointerCancel={end}
      >
        {/* 배율만큼 키운 층을 옮겨 보인다 — transform이 아니라 너비라서 상자 테두리 굵기는 그대로 */}
        <div
          className={cn(
            "absolute",
            !dragging && "transition-[left,top,width] duration-200",
          )}
          style={{
            aspectRatio: aspect,
            width: pct(view.s),
            left: pct(-view.x * view.s),
            top: pct(-view.y * view.s),
          }}
        >
          {children}
        </div>
      </div>
      <div
        className={cn(
          "absolute right-2 bottom-2 flex items-center gap-0.5 rounded-full border border-border bg-background/90 p-0.5 shadow-card backdrop-blur-sm transition-opacity",
          zoomed
            ? "opacity-100"
            : "opacity-0 group-hover:opacity-100 focus-within:opacity-100",
        )}
      >
        <Button
          size="icon-xs"
          variant="ghost"
          className="rounded-full"
          aria-label="축소"
          disabled={!zoomed}
          onClick={() => onView((v) => zoomAt(v, v.s / STEP))}
        >
          <ZoomOut />
        </Button>
        <span className="min-w-10 text-center text-xs text-muted-foreground tabular-nums">
          {Math.round(view.s * 100)}%
        </span>
        <Button
          size="icon-xs"
          variant="ghost"
          className="rounded-full"
          aria-label="확대"
          disabled={view.s >= MAX_SCALE}
          onClick={() => onView((v) => zoomAt(v, v.s * STEP))}
        >
          <ZoomIn />
        </Button>
        {/* 늘 두고 끄기만 — 나타나면 + 자리가 밀려 연달아 누르다 되돌아간다 */}
        <Button
          size="icon-xs"
          variant="ghost"
          className="rounded-full"
          aria-label="전체 보기"
          title="전체 보기"
          disabled={!zoomed}
          onClick={() => onView(FULL_VIEW)}
        >
          <Maximize2 />
        </Button>
      </div>
    </div>
  );
}

/** 스캔 이미지 + OCR 단어 상자. 신뢰도 낮은 단어는 경고색, focus는 지금 고치는 칸의 자리 */
export function OcrImage({
  src,
  size,
  words,
  lowConfidence,
  showBoxes,
  focus,
  view,
  onView,
}: {
  src: string;
  size: [number, number];
  words: OcrWord[];
  lowConfidence: number;
  showBoxes: boolean;
  focus: Box | null;
  view: View;
  onView: Dispatch<SetStateAction<View>>;
}) {
  return (
    <ZoomPane aspect={`${size[0]} / ${size[1]}`} view={view} onView={onView}>
      {/* eslint-disable-next-line @next/next/no-img-element -- API가 주는 스캔 원본 */}
      <img
        src={src}
        alt="종이 차트 스캔"
        draggable={false}
        className="block w-full"
      />
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
    </ZoomPane>
  );
}

/** 고치는 칸 옆에 띄우는 손글씨 조각 — 이미지에서 box 부분만 잘라 보이고, 누르거나 휠로 조각 안에서 확대한다.
 * 칸이 바뀌면 key로 새로 그려 배율을 되돌린다 */
export function OcrSnippet(props: {
  src: string;
  size: [number, number];
  box: Box;
}) {
  return <Snippet key={props.box.join(",")} {...props} />;
}

function Snippet({
  src,
  size,
  box,
}: {
  src: string;
  size: [number, number];
  box: Box;
}) {
  const [view, setView] = useState<View>(FULL_VIEW);
  const [x0, y0, x1, y1] = box.map((v) => Math.min(Math.max(v, 0), 1)) as Box;
  const bw = x1 - x0;
  if (bw <= 0 || y1 - y0 <= 0) return null;
  // 납작한 칸은 확대할 높이가 없다 — 원본 위아래를 더 잘라 높이를 너비의 MIN_HEIGHT 이상으로
  const need = Math.min((bw * size[0] * MIN_HEIGHT) / size[1], 1);
  const grow = Math.max(need - (y1 - y0), 0);
  const cy0 = Math.min(Math.max(y0 - grow / 2, 0), 1 - Math.max(need, y1 - y0));
  const bh = Math.max(need, y1 - y0);
  return (
    <ZoomPane
      aspect={`${bw * size[0]} / ${bh * size[1]}`}
      view={view}
      onView={setView}
      clickZoom
      className="w-full"
    >
      {/* eslint-disable-next-line @next/next/no-img-element -- 같은 스캔의 일부 */}
      <img
        src={src}
        alt="손글씨 조각"
        draggable={false}
        className="absolute max-w-none"
        style={{
          width: pct(1 / bw),
          left: pct(-x0 / bw),
          top: pct(-cy0 / bh),
        }}
      />
      {grow > 0 && (
        <div
          className="pointer-events-none absolute inset-x-0 rounded-sm border-2 border-primary"
          style={{ top: pct((y0 - cy0) / bh), height: pct((y1 - y0) / bh) }}
        />
      )}
    </ZoomPane>
  );
}
