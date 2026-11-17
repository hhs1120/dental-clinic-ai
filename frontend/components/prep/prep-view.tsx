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
  type PrepRow,
  type PrepSheet,
  regenerate,
} from "@/lib/prep-api";

const WEEK = "일월화수목금토";
const DATE = /^\d{4}-\d{2}-\d{2}$/;
export const POLL_MS = 2000;

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

  // AI 제안 갱신 — 작업을 폴링하고 끝나면 다시 조회한다. 갱신 전 행을 들고 있다가 만든 시각이 바뀐 행에 "방금 갱신"
  type Before = { date: string; rows: Map<number | string, PrepRow> };
  const [job, setJob] = useState<({ id: number } & Before) | null>(null);
  const [done, setDone] = useState<Before | null>(null);
  const [jobError, setJobError] = useState<string | null>(null);
  useEffect(() => {
    if (!job) return;
    const timer = setInterval(() => {
      fetchJob(job.id).then(
        (j) => {
          if (j.status === "running") return;
          setJob(null);
          if (j.status === "failed")
            setJobError(j.error ?? "AI 제안 갱신 실패");
          else setDone({ date: job.date, rows: job.rows });
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

  // 날짜 탭 밖(지난주 북마크 등)이나 틀린 날짜면 기준일로 — 날짜 없이도 실패하면 오류를 보인다
  const failed = sheet !== null && sheet.key === key && "error" in sheet;
  useEffect(() => {
    if (failed && date) go({ date: null, open: null });
  });
  if (failed && !date && sheet && "error" in sheet)
    return <ErrorBand text={sheet.error} />;
  const loaded = sheet && "data" in sheet ? sheet.data : null;
  if (!loaded)
    return (
      <p className="py-10 text-center text-sm text-muted-foreground">
        불러오는 중…
      </p>
    );

  // 다시 조회가 도착한 뒤에만 — 그 전 렌더는 옛 행이라 "갱신한 환자 없음"으로 잘못 보인다
  const fresh = sheet !== null && sheet.key === key;
  const marked =
    fresh && done?.date === loaded.date
      ? markRefreshed(loaded.rows, done.rows)
      : null;
  const current = marked ? { ...loaded, rows: marked } : loaded;
  const today = clinic && clinic !== "error" ? clinic.today : null;
  // 다른 날짜를 만드는 중이면 이 날짜는 누를 수 있다. 끝난 뒤 다시 조회가 올 때까지도 갱신 중
  const busy =
    job?.date === current.date || (done?.date === current.date && !fresh);
  const need = current.rows.filter((r) => r.refresh).length;
  const staleCount = current.rows.filter((r) => r.stale).length;
  const start = () => {
    setJobError(null);
    setDone(null);
    const rows = new Map(current.rows.map((r) => [r.appointment_id, r]));
    regenerate(current.date).then(
      ({ job_id }) => setJob({ id: job_id, date: current.date, rows }),
      (e) => setJobError(message(e)),
    );
  };
  const gen = current.generated_at;
  const refreshed = marked?.filter((r) => r.refreshed) ?? null;
  const failedNow = refreshed?.filter((r) => !r.suggestion).length ?? 0;

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
        <div className="ml-auto flex flex-wrap items-center justify-end gap-2">
          <span className="text-sm text-muted-foreground">
            AI 제안:{" "}
            {gen
              ? `${dayLabel(gen.slice(0, 10))} ${gen.slice(11, 16)} 기준`
              : "아직 만들지 않음"}
            {staleCount > 0 && ` · 사실이 바뀐 환자 ${staleCount}명`}
            {refreshed &&
              (refreshed.length
                ? ` · ${refreshed.length}명 갱신함`
                : " · 갱신한 환자 없음")}
            {failedNow > 0 && ` · ${failedNow}명은 후검증 실패로 비워 둠`}
          </span>
          <Button
            size="sm"
            variant="outline"
            className="print:hidden"
            disabled={busy || need === 0}
            onClick={start}
          >
            {busy
              ? "갱신 중…"
              : need
                ? `AI 제안 갱신하기 (${need}명)`
                : "AI 제안 최신"}
          </Button>
        </div>
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

/** 갱신 뒤 행 중 제안을 만든 시각이 갱신 전과 다른 행 — 이전 제안을 붙인다 */
function markRefreshed(rows: PrepRow[], before: Map<number | string, PrepRow>) {
  return rows.map((r) => {
    const b = before.get(r.appointment_id);
    if (!r.generated || b?.made_at === r.made_at) return r;
    return { ...r, refreshed: b?.generated ? { prev: b.suggestion } : {} };
  });
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
