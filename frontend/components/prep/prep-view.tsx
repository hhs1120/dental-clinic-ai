"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";

import { PrepChair } from "@/components/prep/prep-chair";
import { PrepTable } from "@/components/prep/prep-table";
import { StatusDot } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ApiError, useClinic } from "@/lib/api";
import {
  fetchJob,
  fetchPrep,
  type PrepSheet,
  regenerate,
} from "@/lib/prep-api";

const WEEK = "일월화수목금토";
const DATE = /^\d{4}-\d{2}-\d{2}$/;
const POLL_MS = 2000;

type Loaded<T> = { key: string; data: T } | { key: string; error: string };
const message = (e: unknown) =>
  e instanceof ApiError ? e.message : "서버에 연결하지 못했습니다";

function dayLabel(date: string): string {
  const d = new Date(`${date}T00:00:00Z`);
  return `${d.getUTCMonth() + 1}/${d.getUTCDate()}(${WEEK[d.getUTCDay()]})`;
}

/** ④ 준비표 — 날짜·보기·필터·펼친 행은 URL에 둔다(/prep?date=…&view=table|chair&chip=…&type=…&todo=1&open=…&chair=…) */
export function PrepView() {
  const params = useSearchParams();
  const router = useRouter();
  const clinic = useClinic();
  const date = params.get("date") ?? "";
  const view = params.get("view") === "chair" ? "chair" : "table";

  const go = (patch: Record<string, string | null>) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(patch)) {
      if (v === null) next.delete(k);
      else next.set(k, v);
    }
    router.replace(`/prep${next.size ? `?${next}` : ""}`);
  };

  const [attempt, setAttempt] = useState(0);
  const [sheet, setSheet] = useState<Loaded<PrepSheet> | null>(null);
  const key = `${date}#${attempt}`;
  useEffect(() => {
    const ac = new AbortController();
    fetchPrep(DATE.test(date) ? date : undefined, ac.signal).then(
      (data) => setSheet({ key, data }),
      (e) => !ac.signal.aborted && setSheet({ key, error: message(e) }),
    );
    return () => ac.abort();
  }, [date, key]);

  // 다시 만들기 — 작업을 폴링하고 끝나면 다시 조회한다
  const [job, setJob] = useState<{ id: number; date: string } | null>(null);
  const [jobError, setJobError] = useState<string | null>(null);
  useEffect(() => {
    if (!job) return;
    const timer = setInterval(() => {
      fetchJob(job.id).then(
        (j) => {
          if (j.status === "running") return;
          setJob(null);
          if (j.status === "failed") setJobError(j.error ?? "다시 만들기 실패");
          setAttempt((n) => n + 1);
        },
        (e) => {
          setJob(null);
          setJobError(message(e));
        },
      );
    }, POLL_MS);
    return () => clearInterval(timer);
  }, [job]);

  if (sheet && sheet.key === key && "error" in sheet)
    return <ErrorBand text={sheet.error} />;
  const current = sheet && "data" in sheet ? sheet.data : null;
  if (!current)
    return (
      <p className="py-10 text-center text-sm text-muted-foreground">
        불러오는 중…
      </p>
    );

  const today = clinic && clinic !== "error" ? clinic.today : null;
  const start = () => {
    setJobError(null);
    regenerate(current.date).then(
      ({ job_id }) => setJob({ id: job_id, date: current.date }),
      (e) => setJobError(message(e)),
    );
  };

  return (
    <div className="flex flex-col gap-4">
      <div
        role="tablist"
        aria-label="날짜"
        className="flex flex-wrap gap-1.5 print:hidden"
      >
        {current.days.map((d) => (
          <Button
            key={d.date}
            role="tab"
            size="sm"
            variant={d.date === current.date ? "secondary" : "outline"}
            aria-selected={d.date === current.date}
            onClick={() => go({ date: d.date, open: null })}
          >
            {d.date === today ? "오늘 " : ""}
            {dayLabel(d.date)} · {d.count}명
          </Button>
        ))}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <div className="flex gap-1 print:hidden" aria-label="보기">
          {(
            [
              ["table", "준비표"],
              ["chair", "체어 보기"],
            ] as const
          ).map(([k, label]) => (
            <Button
              key={k}
              size="sm"
              variant={view === k ? "secondary" : "outline"}
              aria-pressed={view === k}
              onClick={() => go({ view: k === "table" ? null : k })}
            >
              {label}
            </Button>
          ))}
        </div>
        <span className="text-sm text-muted-foreground">
          {dayLabel(current.date)}{" "}
          {current.generated_at
            ? `${current.generated_at.slice(11, 16)} 기준`
            : "· 아직 만들지 않음"}
        </span>
        <span className="flex-1" />
        <Button
          size="sm"
          variant="outline"
          className="print:hidden"
          disabled={job !== null}
          onClick={start}
        >
          {job ? "만드는 중…" : "다시 만들기"}
        </Button>
      </div>
      {jobError && <ErrorBand text={jobError} />}
      {current.rows.length === 0 ? (
        <p className="py-10 text-center text-sm text-muted-foreground">
          이 날은 예약이 없습니다
        </p>
      ) : view === "table" ? (
        <PrepTable sheet={current} today={today} params={params} go={go} />
      ) : (
        <PrepChair sheet={current} today={today} params={params} go={go} />
      )}
    </div>
  );
}

export function ErrorBand({ text }: { text: string }) {
  return (
    <p
      role="alert"
      className="flex items-center gap-2 rounded-md border border-status-danger-border bg-status-danger-bg px-3 py-2.5 text-sm font-bold text-status-danger-fg"
    >
      <StatusDot status="danger" />
      {text}
    </p>
  );
}
