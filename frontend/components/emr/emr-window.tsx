"use client";

// 가상 EMR 창 — 버튼·탭은 옛 프로그램 모양을 위해 shadcn 대신 순수 <button>을 쓴다
import { useEffect, useState } from "react";

import { EmrBook } from "@/components/emr/emr-book";
import { EmrChart } from "@/components/emr/emr-chart";
import { EmrDesk } from "@/components/emr/emr-desk";
import {
  type PatientHit,
  type PatientRecord,
  type ScheduleDay,
  searchPatients,
} from "@/lib/emr-api";
import { cn } from "@/lib/utils";

export type EmrMode = "desk" | "book" | "chart";
export const EMR_TABS = [
  "진료기록",
  "수납",
  "청구",
  "치료계획",
  "주의사항",
  "예약",
  "문자",
] as const;
export const EMR_PANELS = [
  "기본",
  "메모",
  "계획",
  "접수",
  "예약",
  "수납",
  "가족",
] as const;
export type EmrTab = (typeof EMR_TABS)[number];
export type EmrPanel = (typeof EMR_PANELS)[number];
export type EmrNav = { mode: EmrMode; tab: EmrTab; panel: EmrPanel };
export type Highlight = "balance" | "panel" | "messages" | null;
export const DEFAULT_NAV: EmrNav = {
  mode: "desk",
  tab: "진료기록",
  panel: "기본",
};

const TOOLS: [EmrMode | null, string][] = [
  ["desk", "데스크"],
  ["book", "예약"],
  ["chart", "전자차트"],
  [null, "환자상담"],
  [null, "사진"],
  [null, "X-ray"],
  [null, "문서발급"],
  [null, "고객관리"],
  [null, "구강검진"],
  [null, "청구/EDI"],
  [null, "경영통계"],
  [null, "환경설정"],
];
const MODE_NAME: Record<EmrMode, string> = {
  desk: "데스크",
  book: "예약",
  chart: "전자차트",
};

export function EmrWindow({
  record,
  today,
  todaySchedule,
  nav,
  onNav,
  moves,
  highlight,
  onPickPatient,
  locked,
  part,
}: {
  record: PatientRecord;
  today: string;
  todaySchedule: ScheduleDay | null;
  nav: EmrNav;
  onNav: (next: EmrNav) => void;
  moves: number;
  highlight: Highlight;
  onPickPatient: (chartNo: string) => void;
  locked: boolean;
  part: string;
}) {
  const [notice, setNotice] = useState(false);
  useEffect(() => {
    if (!notice) return;
    const t = setTimeout(() => setNotice(false), 2000);
    return () => clearTimeout(t);
  }, [notice]);

  const p = record.patient;
  const recent = (todaySchedule?.appointments ?? [])
    .filter((a) => a.status !== "cancelled" && a.status !== "noshow")
    .slice(0, 4);

  return (
    <div className="overflow-x-auto rounded-md border">
      <div className="relative min-w-[68rem] bg-emr-chrome text-xs leading-4 text-emr-fg">
        {/* 따라 보기 중에는 inert — 마우스뿐 아니라 키보드(Tab·Enter)로도 창 안을 건드리지 못하게 */}
        <div inert={locked}>
          <div className="flex items-center justify-between bg-emr-title px-2.5 py-1 text-emr-title-fg">
            <span>덴탈차트 Pro v3.2 - [{MODE_NAME[nav.mode]}]</span>
            <span aria-hidden="true" className="tracking-widest">
              ─ ☐ ✕
            </span>
          </div>
          <div
            aria-hidden="true"
            className="flex gap-4 border-b border-emr-line px-2.5 py-0.5"
          >
            {[
              "파일",
              "환자",
              "예약",
              "진료",
              "수납",
              "청구",
              "통계",
              "환경설정",
              "도움말",
            ].map((m) => (
              <span key={m}>{m}</span>
            ))}
          </div>

          <div className="grid grid-cols-[14rem_15rem_minmax(0,1fr)] gap-2 border-b border-emr-line px-2 py-1.5">
            <div className="relative">
              <b className="text-lg leading-6">{p.name}</b>{" "}
              <span className="tabular-nums">({p.chart_no})</span>
              <PatientSearch onPick={onPickPatient} />
            </div>
            <div className="flex flex-col overflow-hidden border border-emr-line bg-emr-surface">
              {recent.map((a) => (
                <button
                  key={a.id}
                  type="button"
                  onClick={() => onPickPatient(a.chart_no)}
                  className="truncate px-1.5 text-left hover:bg-emr-surface-2"
                >
                  <span className="tabular-nums">{a.time}</span> {a.name} ·{" "}
                  {a.procedure}
                </button>
              ))}
            </div>
            <div className="flex items-stretch justify-end gap-0.5">
              {TOOLS.map(([mode, label]) => (
                <button
                  key={label}
                  type="button"
                  disabled={!mode}
                  title={mode ? undefined : "가상 프로그램에 없는 화면"}
                  onClick={() => mode && onNav({ ...nav, mode })}
                  className={cn(
                    "flex w-13 flex-col items-center gap-1 border border-transparent py-1 text-[11px] disabled:opacity-40",
                    mode === nav.mode &&
                      "border-emr-line bg-emr-surface font-bold",
                  )}
                >
                  <i
                    className={cn(
                      "grid size-6.5 place-items-center rounded-md bg-emr-line font-bold not-italic",
                      mode === nav.mode && "bg-emr-title text-emr-title-fg",
                    )}
                  >
                    {label[0]}
                  </i>
                  {label}
                </button>
              ))}
            </div>
          </div>

          <div className="min-h-[40rem] p-1.5">
            {nav.mode === "book" ? (
              <EmrBook today={today} onPickPatient={onPickPatient} />
            ) : nav.mode === "chart" ? (
              <EmrChart
                record={record}
                todaySchedule={todaySchedule}
                today={today}
                nav={nav}
                onNav={onNav}
                highlight={highlight}
                onPickPatient={onPickPatient}
              />
            ) : (
              <EmrDesk
                record={record}
                today={today}
                todaySchedule={todaySchedule}
                nav={nav}
                onNav={onNav}
                highlight={highlight}
                onPickPatient={onPickPatient}
              />
            )}
          </div>

          <div className="flex flex-wrap gap-4 border-t border-emr-line px-2 py-0.5 text-emr-fg-muted">
            <span>덴탈차트 Pro v3.2 · 가상 프로그램</span>
            <span>파트: {part}</span>
            <span>이 환자에서 화면 이동 {moves}회</span>
            <span>읽기 전용</span>
            <span>{today}</span>
          </div>
        </div>
        {locked && (
          <button
            type="button"
            aria-label="따라 보기 중에는 EMR 창을 누를 수 없습니다"
            onClick={() => setNotice(true)}
            className="absolute inset-0 z-30 cursor-not-allowed bg-emr-surface/30"
          >
            {notice && (
              <span className="absolute top-14 left-1/2 -translate-x-1/2 rounded-md bg-emr-title px-3 py-1.5 text-sm text-emr-title-fg">
                따라 보기 중에는 다음 버튼으로 넘어갑니다
              </span>
            )}
          </button>
        )}
      </div>
    </div>
  );
}

function PatientSearch({ onPick }: { onPick: (chartNo: string) => void }) {
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<{ q: string; list: PatientHit[] } | null>(
    null,
  );
  useEffect(() => {
    const query = q.trim();
    if (!query) return;
    const ac = new AbortController();
    const t = setTimeout(() => {
      searchPatients(query, ac.signal).then(
        (list) => setHits({ q: query, list: list.slice(0, 10) }),
        () => !ac.signal.aborted && setHits({ q: query, list: [] }),
      );
    }, 300);
    return () => {
      clearTimeout(t);
      ac.abort();
    };
  }, [q]);
  const shown = q.trim() && hits?.q === q.trim() ? hits.list : null;

  return (
    <div className="mt-1">
      <input
        aria-label="환자 검색"
        value={q}
        onChange={(e) => setQ(e.target.value)}
        placeholder="이름·차트번호 검색"
        className="w-full border border-emr-line bg-emr-surface px-1.5 py-0.5"
      />
      {shown && (
        <div className="absolute inset-x-0 z-20 flex flex-col border border-emr-line bg-emr-surface shadow-card">
          {shown.length ? (
            shown.map((h) => (
              <button
                key={h.chart_no}
                type="button"
                onClick={() => {
                  setQ("");
                  onPick(h.chart_no);
                }}
                className="px-1.5 py-0.5 text-left hover:bg-emr-surface-2"
              >
                {h.chart_no} {h.name} · {h.age}세
              </button>
            ))
          ) : (
            <span className="px-1.5 py-0.5 text-emr-fg-muted">
              검색 결과 없음
            </span>
          )}
        </div>
      )}
    </div>
  );
}
