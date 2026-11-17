"use client";

import { ChevronRight } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/** 도구 이름 → 한 줄 이름 — 도구는 backend/assistant_tools.py·query_tools.py */
const LABEL: Record<string, string> = {
  query_patient: "환자 조회",
  query_schedule: "예약 조회",
  query_claims: "청구 조회",
  query_charts: "차트 조회",
  find_calls: "통화 찾기",
  unbooked_requests: "예약 원했는데 안 잡힌 환자",
  free_slots: "빈 시간 찾기",
  prep_rows: "준비표 조회",
  claim_items: "청구 점검 조회",
  recon_days: "일계표 불일치 조회",
  missed_patients: "미예약 환자 조회",
  ask_records: "문서·기록 찾기",
  draft_appointment: "예약 초안 만들기",
};

export interface Step {
  tool: string;
  args: Record<string, unknown>;
}

const argText = (args: Record<string, unknown>) =>
  Object.entries(args)
    .filter(([, v]) => v !== null && v !== undefined && v !== "")
    .map(([k, v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`)
    .join(", ");

/** 접힌 "조회 n번 ›" 줄 — 누르면 단계마다 도구·조건. 진행 중이면 펼쳐 한 줄씩 쌓인다 */
export function TurnSteps({
  steps,
  running = false,
}: {
  steps: Step[];
  running?: boolean;
}) {
  const [open, setOpen] = useState(false);
  if (steps.length === 0 && !running) return null;
  const shown = open || running;
  return (
    <div className="flex flex-col gap-1 text-sm text-muted-foreground">
      <Button
        type="button"
        variant="ghost"
        size="sm"
        aria-expanded={shown}
        onClick={() => setOpen(!open)}
        className="w-fit gap-1 px-1 text-muted-foreground"
      >
        <ChevronRight
          aria-hidden="true"
          className={cn("size-4 transition-transform", shown && "rotate-90")}
        />
        조회 {steps.length}번
        {running && <span className="ml-1">· 진행 중…</span>}
      </Button>
      {shown && (
        <ol className="ml-5 flex flex-col gap-0.5">
          {steps.map((s, i) => (
            <li key={i}>
              <span className="font-medium text-foreground">
                {LABEL[s.tool] ?? s.tool}
              </span>
              {argText(s.args) && <span> — {argText(s.args)}</span>}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
