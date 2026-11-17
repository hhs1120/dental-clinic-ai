"use client";

import { useSearchParams } from "next/navigation";

import { AccuracyTab } from "@/components/eval/accuracy-tab";
import { BlindTab } from "@/components/eval/blind-tab";
import { ImproveTab } from "@/components/eval/improve-tab";
import { JudgeTab } from "@/components/eval/judge-tab";
import { useLabels } from "@/lib/calls-api";
import { EVAL_TABS } from "@/lib/nav";

/** AI 평가 — 탭은 URL에(/eval?tab=…), 고르는 링크는 목록 칸(components/shell) (통화·② 판정 화면 설계, 기능 정의 §4 ②) */
export function EvalView() {
  const params = useSearchParams();
  const raw = params.get("tab");
  const tab = EVAL_TABS.some(([k]) => k === raw) ? (raw as string) : "judge";
  const names = useLabels();
  return (
    <div className="flex flex-col gap-4">
      {tab === "judge" && <JudgeTab names={names} />}
      {tab === "blind" && <BlindTab />}
      {tab === "acc" && <AccuracyTab />}
      {tab === "improve" && <ImproveTab />}
    </div>
  );
}
