"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";

import { PatientSummary } from "@/components/patient-summary";
import { DayGrid } from "@/components/schedule/day-grid";
import { StatusDot } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ApiError, useClinic } from "@/lib/api";
import { cn } from "@/lib/utils";
import {
  fetchPatient,
  fetchSchedule,
  type PatientRecord,
  type ScheduleDay,
} from "@/lib/emr-api";

const WEEK = "일월화수목금토";
const DATE = /^\d{4}-\d{2}-\d{2}$/;

function addDays(date: string, n: number): string {
  const d = new Date(`${date}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}

function title(date: string): string {
  const d = new Date(`${date}T00:00:00Z`);
  return `${d.getUTCMonth() + 1}월 ${d.getUTCDate()}일 (${WEEK[d.getUTCDay()]})`;
}

type Loaded<T> = { key: string; data: T } | { key: string; error: string };
const message = (e: unknown) =>
  e instanceof ApiError ? e.message : "서버에 연결하지 못했습니다";

/** 일별 예약표 — 날짜·선택·취소 보기는 URL에 둔다(/schedule?date=…&appt=…&all=1) */
export function ScheduleView() {
  const params = useSearchParams();
  const router = useRouter();
  const clinic = useClinic();
  const date = params.get("date") ?? "";
  const appt = params.get("appt") || null;
  const showAll = params.get("all") === "1";

  const go = (patch: Record<string, string | null>, replace = false) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(patch)) {
      if (v === null) next.delete(k);
      else next.set(k, v);
    }
    const url = `/schedule${next.size ? `?${next}` : ""}`;
    if (replace) router.replace(url);
    else router.push(url);
  };

  const [day, setDay] = useState<Loaded<ScheduleDay> | null>(null);
  useEffect(() => {
    const ac = new AbortController();
    fetchSchedule(DATE.test(date) ? date : undefined, ac.signal).then(
      (data) => setDay({ key: date, data }),
      (e) => !ac.signal.aborted && setDay({ key: date, error: message(e) }),
    );
    return () => ac.abort();
  }, [date]);

  const current = day?.key === date && "data" in day ? day.data : null;
  // 형식이 틀리거나 데이터 범위 밖이면 기준일로
  useEffect(() => {
    if (!current || !date) return;
    if (
      !DATE.test(date) ||
      date < current.range.from ||
      date > current.range.to
    ) {
      go({ date: null, appt: null }, true);
    }
  });

  // 숨겨진(취소·노쇼 보기를 끈) 예약은 고른 것으로 치지 않는다
  const selected =
    current?.appointments.find(
      (a) =>
        String(a.id) === appt &&
        (showAll || (a.status !== "cancelled" && a.status !== "noshow")),
    ) ?? null;
  const [patient, setPatient] = useState<Loaded<PatientRecord> | null>(null);
  const chart = selected?.chart_no ?? "";
  useEffect(() => {
    if (!chart) return;
    const ac = new AbortController();
    fetchPatient(chart, ac.signal).then(
      (data) => setPatient({ key: chart, data }),
      (e) =>
        !ac.signal.aborted && setPatient({ key: chart, error: message(e) }),
    );
    return () => ac.abort();
  }, [chart]);

  if (day && day.key === date && "error" in day)
    return <ErrorBand text={day.error} />;
  if (!current)
    return (
      <p className="py-10 text-center text-sm text-muted-foreground">
        불러오는 중…
      </p>
    );

  const today = clinic && clinic !== "error" ? clinic.today : null;
  const c = current.counts;
  const record =
    patient?.key === chart && "data" in patient ? patient.data : null;

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <Button
          variant="outline"
          size="sm"
          aria-label="이전 날"
          disabled={current.date <= current.range.from}
          onClick={() => go({ date: addDays(current.date, -1), appt: null })}
        >
          ◀
        </Button>
        <b className="min-w-32 text-center text-base">{title(current.date)}</b>
        <Button
          variant="outline"
          size="sm"
          aria-label="다음 날"
          disabled={current.date >= current.range.to}
          onClick={() => go({ date: addDays(current.date, 1), appt: null })}
        >
          ▶
        </Button>
        <Button
          variant="outline"
          size="sm"
          onClick={() => go({ date: null, appt: null })}
        >
          오늘
        </Button>
        <Input
          type="date"
          aria-label="날짜 고르기"
          value={current.date}
          min={current.range.from}
          max={current.range.to}
          onChange={(e) =>
            e.target.value && go({ date: e.target.value, appt: null })
          }
          className="w-auto bg-card shadow-control"
        />
        <div className="ml-2 flex items-center gap-1.5">
          <Checkbox
            id="show-all"
            checked={showAll}
            onCheckedChange={(on) => go({ all: on ? "1" : null }, true)}
          />
          <Label htmlFor="show-all" className="font-normal">
            취소·노쇼 보기
          </Label>
        </div>
        <span className="ml-auto text-sm text-muted-foreground">
          총 {c.total}건 · 진료 {c.treatment} · 위생 {c.hygiene} · 상담{" "}
          {c.consult}
          {current.date === today ? ` · 내원 확인 전 ${c.unconfirmed}` : ""}
        </span>
      </div>

      <div
        className={cn(
          "grid items-start gap-4",
          selected && "lg:grid-cols-[minmax(0,1fr)_20rem]",
        )}
      >
        <div className="min-w-0 rounded-xl border bg-card shadow-card">
          <DayGrid
            day={current}
            showAll={showAll}
            selected={selected?.id ?? null}
            onSelect={(id) => go({ appt: String(id) }, true)}
          />
        </div>
        {selected &&
          (record ? (
            <PatientSummary
              patient={record.patient}
              summary={record.summary}
              appt={selected}
              date={current.date}
            />
          ) : patient?.key === chart && "error" in patient ? (
            <ErrorBand text={patient.error} />
          ) : (
            <p className="py-6 text-center text-sm text-muted-foreground">
              불러오는 중…
            </p>
          ))}
      </div>
    </div>
  );
}

function ErrorBand({ text }: { text: string }) {
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
