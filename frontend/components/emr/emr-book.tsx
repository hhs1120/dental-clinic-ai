"use client";

import { useEffect, useState } from "react";

import { DayTable, Th } from "@/components/emr/emr-desk";
import { Box } from "@/components/emr/emr-ui";
import { fetchSchedule, type ScheduleDay } from "@/lib/emr-api";
import { cn } from "@/lib/utils";

const WEEK = "일월화수목금토";

function addDays(date: string, n: number): string {
  const d = new Date(`${date}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}
const weekday = (date: string) => new Date(`${date}T00:00:00Z`).getUTCDay();
const label = (date: string) => `${date.slice(5)} (${WEEK[weekday(date)]})`;

/** 예약 — 달력·예약 통계·일간/주간 예약(옛 프로그램식). 예약 칸을 누르면 그 환자로 바뀐다 */
export function EmrBook({
  today,
  onPickPatient,
}: {
  today: string;
  onPickPatient: (chartNo: string) => void;
}) {
  const [date, setDate] = useState(today);
  const [week, setWeek] = useState(false);
  const monday = addDays(date, -((weekday(date) + 6) % 7));
  const days = week
    ? Array.from({ length: 7 }, (_, i) => addDays(monday, i))
    : [date];
  const key = days.join(",");
  const [loaded, setLoaded] = useState<{
    key: string;
    days: ScheduleDay[];
  } | null>(null);
  useEffect(() => {
    const ac = new AbortController();
    Promise.all(key.split(",").map((d) => fetchSchedule(d, ac.signal))).then(
      (list) => setLoaded({ key, days: list }),
      () => undefined,
    );
    return () => ac.abort();
  }, [key]);
  const list = loaded?.key === key ? loaded.days : null;
  const current = list?.find((d) => d.date === date) ?? null;
  const range = current?.range;
  const live = (current?.appointments ?? []).filter(
    (a) => a.status !== "cancelled" && a.status !== "noshow",
  );
  const count = (t: string) => live.filter((a) => a.type === t).length;

  return (
    <div className="grid grid-cols-[16rem_minmax(0,1fr)] gap-1.5">
      <div className="flex flex-col gap-1.5">
        <Box>
          <Calendar date={date} today={today} range={range} onPick={setDate} />
        </Box>
        <Box title={`예약 통계 · ${label(date)}`}>
          <table className="w-full border-collapse">
            <tbody>
              {[
                ["전체", live.length],
                ["진료", count("treatment")],
                ["위생", count("hygiene")],
                ["상담", count("consult")],
              ].map(([k, v]) => (
                <tr key={k}>
                  <Th>{k}</Th>
                  <td className="border border-emr-line-soft px-1.5 text-right tabular-nums">
                    {v}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Box>
        <Box title="예약 메모">
          <p className="text-emr-fg-muted">메모 없음</p>
        </Box>
        <p className="text-emr-fg-muted">
          예약 칸을 누르면 그 환자로 바뀝니다. 왼쪽 파란 띠는 신환입니다.
        </p>
      </div>
      <Box className="flex-1">
        <div className="mb-1 flex items-center gap-2">
          <h3 className="text-xs font-bold">
            {week ? "주간" : "일간"} 예약 · {label(date)}
            {week ? " 주" : ` · 총 ${live.length}건`}
          </h3>
          <span className="ml-auto flex">
            {[false, true].map((w) => (
              <button
                key={String(w)}
                type="button"
                onClick={() => setWeek(w)}
                className={cn(
                  "-ml-px border border-emr-line px-2",
                  week === w ? "bg-emr-surface font-bold" : "bg-emr-tab-off",
                )}
              >
                {w ? "주간" : "일간"}
              </button>
            ))}
          </span>
        </div>
        {!list ? null : week ? (
          <WeekTable
            days={list}
            selected={date}
            onPickDate={setDate}
            onPickPatient={onPickPatient}
          />
        ) : (
          <DayTable day={current} onPickPatient={onPickPatient} />
        )}
      </Box>
    </div>
  );
}

function Calendar({
  date,
  today,
  range,
  onPick,
}: {
  date: string;
  today: string;
  range?: { from: string; to: string };
  onPick: (d: string) => void;
}) {
  const first = `${date.slice(0, 8)}01`;
  const cells: (string | null)[] = Array.from(
    { length: weekday(first) },
    () => null,
  );
  for (let d = first; d.slice(0, 7) === date.slice(0, 7); d = addDays(d, 1))
    cells.push(d);
  const inRange = (d: string) => !range || (d >= range.from && d <= range.to);
  const prev = `${addDays(first, -1).slice(0, 8)}01`;
  const next = `${addDays(first, 32).slice(0, 8)}01`;
  return (
    <div>
      <div className="mb-1 flex items-center justify-between">
        <button
          type="button"
          onClick={() => onPick(prev)}
          disabled={!inRange(addDays(first, -1))}
          className="px-1 disabled:opacity-40"
        >
          ◀
        </button>
        <b>
          {date.slice(0, 4)}년 {Number(date.slice(5, 7))}월
        </b>
        <button
          type="button"
          onClick={() => onPick(next)}
          disabled={!inRange(next)}
          className="px-1 disabled:opacity-40"
        >
          ▶
        </button>
      </div>
      <div className="grid grid-cols-7 gap-px text-center">
        {[...WEEK].map((w) => (
          <i key={w} className="text-emr-fg-muted not-italic">
            {w}
          </i>
        ))}
        {cells.map((d, i) =>
          d ? (
            <button
              key={d}
              type="button"
              disabled={!inRange(d)}
              onClick={() => onPick(d)}
              className={cn(
                "py-0.5 disabled:opacity-30",
                d === date && "bg-emr-title text-emr-title-fg",
                d === today && d !== date && "font-bold underline",
              )}
            >
              {Number(d.slice(8))}
            </button>
          ) : (
            <span key={`b${i}`} />
          ),
        )}
      </div>
    </div>
  );
}

function WeekTable({
  days,
  selected,
  onPickDate,
  onPickPatient,
}: {
  days: ScheduleDay[];
  selected: string;
  onPickDate: (d: string) => void;
  onPickPatient: (chartNo: string) => void;
}) {
  return (
    <div className="overflow-auto">
      <table className="w-full table-fixed border-collapse">
        <thead>
          <tr>
            {days.map((d) => (
              <Th
                key={d.date}
                className={cn(d.date === selected && "bg-emr-surface")}
              >
                <button
                  type="button"
                  onClick={() => onPickDate(d.date)}
                  className="underline"
                >
                  {label(d.date)}
                </button>{" "}
                · {d.counts.total}
              </Th>
            ))}
          </tr>
        </thead>
        <tbody>
          <tr>
            {days.map((d) => {
              const rows = d.appointments.filter(
                (a) => a.status !== "cancelled",
              );
              return (
                <td
                  key={d.date}
                  className="border border-emr-line-soft p-0.5 align-top"
                >
                  {rows.length ? (
                    rows.map((a) => (
                      <button
                        key={a.id}
                        type="button"
                        onClick={() => onPickPatient(a.chart_no)}
                        className={cn(
                          "mb-0.5 block w-full truncate border-l-4 border-emr-line bg-emr-surface-2 px-1 text-left",
                          a.new_patient && "border-l-status-info-dot",
                        )}
                      >
                        {a.time} {a.name}
                      </button>
                    ))
                  ) : (
                    <span className="text-emr-fg-muted">
                      {d.hours.closed ? "휴진" : "예약 없음"}
                    </span>
                  )}
                </td>
              );
            })}
          </tr>
        </tbody>
      </table>
    </div>
  );
}
