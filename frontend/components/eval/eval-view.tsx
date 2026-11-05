"use client";

import { ChartColumn, ClipboardCheck, EyeOff } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";

import { AccuracyTab } from "@/components/eval/accuracy-tab";
import { BlindTab } from "@/components/eval/blind-tab";
import { JudgeTab } from "@/components/eval/judge-tab";
import { ScreenTabs } from "@/components/screen-tabs";
import { useLabels } from "@/lib/calls-api";

const TABS = [
  ["judge", "판정", ClipboardCheck],
  ["blind", "블라인드 비교", EyeOff],
  ["acc", "정확도 표", ChartColumn],
] as const;

/** AI 평가 — 탭은 URL에(/eval?tab=…). 자동 개선 탭은 개선 에이전트 때(통화·② 판정 화면 설계) */
export function EvalView() {
  const params = useSearchParams();
  const router = useRouter();
  const raw = params.get("tab");
  const tab = TABS.some(([k]) => k === raw) ? (raw as string) : "judge";
  const names = useLabels();
  return (
    <div className="flex flex-col gap-4">
      <ScreenTabs
        tabs={TABS.map(([key, label, icon]) => ({ key, label, icon }))}
        value={tab}
        onChange={(v) =>
          router.push(v === "judge" ? "/eval" : `/eval?tab=${v}`)
        }
      />
      {tab === "judge" && <JudgeTab names={names} />}
      {tab === "blind" && <BlindTab />}
      {tab === "acc" && <AccuracyTab />}
    </div>
  );
}
