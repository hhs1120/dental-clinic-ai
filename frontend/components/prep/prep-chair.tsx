"use client";

import { useEffect, useState } from "react";

import { SuggestionLine } from "@/components/prep/prep-table";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { type PrepRow, type PrepSheet } from "@/lib/prep-api";
import { cn } from "@/lib/utils";

type Go = (patch: Record<string, string | null>) => void;

const CONSULT = "consult-room"; // URL chair 값 — 체어 없는 상담
const chairKey = (r: PrepRow) => (r.chair_no ? String(r.chair_no) : CONSULT);
const chairLabel = (k: string) => (k === CONSULT ? "상담실" : `${k}번`);
const minutes = (hhmm: string) =>
  Number(hhmm.slice(0, 2)) * 60 + Number(hhmm.slice(3, 5));

/** 실제 시계의 시:분(분) — 1분마다 다시 그린다 */
function useClockMinutes(): number {
  const read = () => {
    const d = new Date();
    return d.getHours() * 60 + d.getMinutes();
  };
  const [now, setNow] = useState(read);
  useEffect(() => {
    const t = setInterval(() => setNow(read()), 60_000);
    return () => clearInterval(t);
  }, []);
  return now;
}

/** 체어 보기 — 체어의 오늘 순서와 지금 환자 한 장. "지금" = 기준일 + 실제 시계의 시:분(기능 정의 §4 ④) */
export function PrepChair({
  sheet,
  today,
  params,
  go,
}: {
  sheet: PrepSheet;
  today: string | null;
  params: URLSearchParams;
  go: Go;
}) {
  const clock = useClockMinutes();
  const [picked, setPicked] = useState<number | string | null>(null);
  const chairs = [...new Set(sheet.rows.map(chairKey))].sort((a, b) =>
    a === CONSULT ? 1 : b === CONSULT ? -1 : Number(a) - Number(b),
  );
  const chair = chairs.includes(params.get("chair") ?? "")
    ? params.get("chair")!
    : chairs[0];
  const list = sheet.rows
    .filter((r) => chairKey(r) === chair)
    .sort((a, b) => a.time.localeCompare(b.time));

  const isToday = sheet.date === today;
  const nowIdx = isToday
    ? list.findIndex(
        (r) =>
          minutes(r.time) <= clock && clock < minutes(r.time) + r.duration_min,
      )
    : -1;
  const nextIdx = isToday ? list.findIndex((r) => minutes(r.time) > clock) : -1;
  const label = (i: number) =>
    i === nowIdx ? "지금" : i === nextIdx ? "다음" : ""; // 지금 환자가 있어도 바로 뒤 환자는 "다음"
  // 고르지 않았으면 지금 → 다음 → (끝났으면) 마지막, 지난 날짜는 첫 환자
  const auto =
    nowIdx >= 0
      ? nowIdx
      : nextIdx >= 0
        ? nextIdx
        : isToday
          ? list.length - 1
          : 0;
  const pick = list.find((r) => r.appointment_id === picked) ?? list[auto];

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap gap-1.5" aria-label="체어">
        {chairs.map((k) => (
          <Button
            key={k}
            size="sm"
            variant={k === chair ? "secondary" : "outline"}
            aria-pressed={k === chair}
            onClick={() => {
              setPicked(null);
              go({ chair: k });
            }}
          >
            {chairLabel(k)} {sheet.rows.filter((r) => chairKey(r) === k).length}
          </Button>
        ))}
      </div>
      <div className="grid gap-4 md:grid-cols-3">
        <ul className="flex flex-col gap-1">
          {list.map((r, i) => (
            <li key={r.appointment_id}>
              <button
                type="button"
                onClick={() => setPicked(r.appointment_id)}
                className={cn(
                  "flex w-full gap-3 rounded-md border px-3 py-2 text-left hover:bg-muted/50",
                  pick === r && "border-primary bg-muted/50",
                )}
              >
                <span className="tabular-nums">
                  <b>{r.time}</b>
                  <span
                    className={cn(
                      "block text-xs",
                      label(i)
                        ? "font-bold text-primary"
                        : "text-muted-foreground",
                    )}
                  >
                    {label(i) || `${r.duration_min}분`}
                  </span>
                </span>
                <span className="text-sm">
                  <b>{r.patient.name}</b> · {r.procedure}
                  {String(r.appointment_id).startsWith("ax-") && (
                    <Badge variant="kind" className="ml-1.5">
                      AX
                    </Badge>
                  )}
                  {r.alerts.some((a) => a.related) && (
                    <span className="block text-xs text-status-danger-fg">
                      {r.alerts
                        .filter((a) => a.related)
                        .map((a) => a.label)
                        .join(", ")}
                    </span>
                  )}
                </span>
              </button>
            </li>
          ))}
        </ul>
        {pick && (
          <ChairCard
            row={pick}
            tag={label(list.indexOf(pick)) || "선택한 환자"}
          />
        )}
      </div>
    </div>
  );
}

function ChairCard({ row, tag }: { row: PrepRow; tag: string }) {
  const related = row.alerts.filter((a) => a.related);
  const others = row.alerts.filter((a) => !a.related);
  return (
    <section
      aria-label="지금 환자"
      className="flex flex-col gap-4 rounded-lg border p-6 md:col-span-2"
    >
      <div className="flex items-center gap-2">
        <Badge variant={tag === "선택한 환자" ? "muted" : "info"}>
          {tag === "선택한 환자" ? tag : `${tag} 환자`}
        </Badge>
        <span className="text-sm text-muted-foreground tabular-nums">
          {row.time} · {row.duration_min}분 · {row.staff}
        </span>
      </div>
      <div className="flex items-baseline gap-3">
        <h2 className="text-3xl font-medium tracking-tight">
          {row.patient.name}
        </h2>
        <span className="text-muted-foreground tabular-nums">
          {row.patient.age}세 {row.patient.sex === "M" ? "남" : "여"} ·{" "}
          {row.patient.chart_no}
        </span>
      </div>
      <div className="flex flex-col gap-2">
        {related.length ? (
          related.map((a) => (
            <div
              key={a.code}
              className="rounded-md border border-status-danger-border bg-status-danger-bg px-4 py-3 text-status-danger-fg"
            >
              <p className="text-xl font-bold">{a.label}</p>
              <p className="text-sm">{a.action}</p>
            </div>
          ))
        ) : (
          <p className="rounded-md border border-dashed px-4 py-3 text-muted-foreground">
            오늘 시술 관련 주의사항 없음
          </p>
        )}
        {others.length > 0 && (
          <p className="text-sm text-muted-foreground">
            그 밖의 주의사항(오늘 시술과 무관):{" "}
            {others.map((a) => a.label).join(", ")}
          </p>
        )}
      </div>
      <dl className="flex flex-col gap-1">
        <div className="flex gap-3">
          <dt className="w-24 shrink-0 text-muted-foreground">예정 시술</dt>
          <dd className="font-bold">{row.procedure}</dd>
        </div>
        <div className="flex gap-3">
          <dt className="w-24 shrink-0 text-muted-foreground">마지막 내원</dt>
          <dd className="tabular-nums">{row.last_visit ?? "첫 내원"}</dd>
        </div>
        {row.open_plan.length > 0 && (
          <div className="flex gap-3">
            <dt className="w-24 shrink-0 text-muted-foreground">남은 계획</dt>
            <dd>
              {row.open_plan
                .map(
                  (p) =>
                    `${p.procedure}${p.tooth === "00" ? "" : ` ${p.tooth}`}`,
                )
                .join(", ")}
            </dd>
          </div>
        )}
      </dl>
      <div className="flex flex-col gap-1">
        <h3 className="text-sm font-medium tracking-tight">제안 (AI)</h3>
        <SuggestionLine row={row} />
      </div>
    </section>
  );
}
