"use client";

import { stepValue, stepWhy, TOUR_STEPS } from "@/components/emr/tour";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import type { PatientRecord, TourPatient } from "@/lib/emr-api";
import { cn } from "@/lib/utils";

export type Stage =
  | { kind: "collapsed" }
  | { kind: "map" }
  | { kind: "tour"; step: number }
  | { kind: "done"; moves: number };

/** 설명 카드 — 흩어짐 지도 → 따라 보기 → 하루 규모(시안 캔버스 '합친 안'). 따라 보기 중 EMR 창은 잠긴다 */
export function ExplainCard({
  stage,
  tour,
  record,
  onStart,
  onPrev,
  onNext,
  onExpand,
}: {
  stage: Stage;
  tour: TourPatient;
  record: PatientRecord | null; // 따라 보기·끝에서는 기준 환자의 기록
  onStart: () => void;
  onPrev: () => void;
  onNext: () => void;
  onExpand: () => void;
}) {
  if (stage.kind === "collapsed")
    return (
      <button
        type="button"
        onClick={onExpand}
        className="w-full rounded-md border bg-card px-4 py-2.5 text-left text-sm shadow-control hover:bg-accent"
      >
        <b>왜 화면을 옮겨 다니나?</b>{" "}
        <span className="text-link">따라 보기</span>
      </button>
    );

  const current = stage.kind === "tour" ? stage.step : -1;
  const showValues = stage.kind !== "map" && record !== null;

  return (
    <Card className="gap-2 p-5 text-sm shadow-card">
      <h2 className="text-base leading-snug font-bold">
        {tour.name} 님 진료 전에 알아야 할 것이 흩어진 곳
      </h2>
      {stage.kind === "map" && (
        <Button onClick={onStart} className="my-1 h-11 font-bold">
          ▶ 따라 보기
        </Button>
      )}

      <ol className="flex flex-col gap-1.5">
        {TOUR_STEPS.map((step, i) => {
          const done = stage.kind === "done" || i < current;
          const now = i === current;
          const value = showValues
            ? stepValue(step, record, tour.appointment_id)
            : null;
          return (
            <li
              key={step.key}
              aria-current={now ? "step" : undefined}
              className={cn(
                "flex flex-col gap-1.5 rounded-md border bg-card px-3 py-2",
                now && "border-primary bg-accent ring-1 ring-primary",
              )}
            >
              <div className="flex items-center gap-2.5">
                <span
                  className={cn(
                    "grid size-5.5 shrink-0 place-items-center rounded-full text-xs font-bold",
                    done || now
                      ? "bg-primary text-primary-foreground"
                      : "bg-secondary",
                  )}
                >
                  {done ? "✓" : i + 1}
                </span>
                <div className="flex min-w-0 flex-col">
                  <b>{step.what}</b>
                  <span className="text-xs text-muted-foreground">
                    {step.where}
                  </span>
                </div>
                {value && (done || now) && (
                  <span
                    className={cn(
                      "ml-auto text-right text-xs font-bold",
                      !value.found && "text-muted-foreground",
                    )}
                  >
                    {value.text}
                  </span>
                )}
              </div>
              {now && record && (
                <p className="text-xs leading-relaxed text-accent-foreground">
                  {stepWhy(step, record, tour.appointment_id)}
                </p>
              )}
            </li>
          );
        })}
      </ol>

      {stage.kind === "tour" && (
        <div className="mt-1 flex gap-2">
          <Button
            variant="outline"
            className="h-11 flex-1"
            onClick={onPrev}
            disabled={stage.step === 0}
          >
            이전
          </Button>
          <Button className="h-11 flex-1 font-bold" onClick={onNext}>
            {stage.step === TOUR_STEPS.length - 1 ? "마치기" : "다음"}
          </Button>
        </div>
      )}

      {stage.kind === "done" && (
        <>
          <dl className="mt-1 flex flex-col gap-1.5 rounded-md border bg-card p-3">
            <div className="flex justify-between">
              <dt className="text-muted-foreground">환자 한 명</dt>
              <dd className="font-bold">화면 이동 {stage.moves}회</dd>
            </div>
            <div className="flex justify-between">
              <dt className="text-muted-foreground">× 오늘 예약 환자</dt>
              <dd className="font-bold">{tour.appointments_today}명</dd>
            </div>
            <div className="flex items-baseline justify-between border-t pt-1.5">
              <dt>데스크의 하루</dt>
              <dd className="text-2xl font-bold text-primary">
                {stage.moves * tour.appointments_today}회
              </dd>
            </div>
          </dl>
          <p className="rounded-md border border-dashed border-muted-foreground px-3 py-2 text-xs text-muted-foreground">
            준비표가 생기면 — 같은 정보가 한 줄에, 화면 이동 0회
          </p>
          <button
            type="button"
            onClick={onStart}
            className="self-start text-sm font-medium text-link hover:underline"
          >
            다시 보기
          </button>
        </>
      )}
    </Card>
  );
}
