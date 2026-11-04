"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";

import {
  DEFAULT_NAV,
  EmrWindow,
  type EmrNav,
  type Highlight,
} from "@/components/emr/emr-window";
import { ExplainCard, type Stage } from "@/components/emr/explain-card";
import { TOUR_STEPS } from "@/components/emr/tour";
import { StatusDot } from "@/components/ui/badge";
import { ApiError, useClinic } from "@/lib/api";
import {
  fetchPatient,
  fetchSchedule,
  fetchTour,
  type PatientRecord,
  type ScheduleDay,
  type TourPatient,
} from "@/lib/emr-api";

type Loaded<T> =
  { key: string; data: T } | { key: string; error: string; status?: number };
const failure = (key: string, e: unknown): Loaded<never> => ({
  key,
  error: e instanceof ApiError ? e.message : "서버에 연결하지 못했습니다",
});

/** 화면 이동 수 — 모드·히스토리 탭·옆 패널 탭이 바뀔 때마다 1씩(와이어프레임과 같은 규칙) */
export const countMoves = (a: EmrNav, b: EmrNav) =>
  Number(a.mode !== b.mode) +
  Number(a.tab !== b.tab) +
  Number(a.panel !== b.panel);

function usePart(): string {
  const [part, setPart] = useState("데스크");
  useEffect(() => {
    try {
      const saved = localStorage.getItem("ax.part");
      // eslint-disable-next-line react-hooks/set-state-in-effect
      if (saved) setPart(saved);
    } catch {}
  }, []);
  return part;
}

/** 전환 전 EMR — /emr?chart=…&view=chart|desk. chart가 없으면 따라 보기 기준 환자로 연다 */
export function EmrView() {
  const params = useSearchParams();
  const router = useRouter();
  const clinic = useClinic();
  const part = usePart();
  const urlChart = params.get("chart");
  const fromSchedule = params.get("view") === "chart";

  const [tour, setTour] = useState<Loaded<TourPatient> | null>(null);
  useEffect(() => {
    const ac = new AbortController();
    fetchTour(ac.signal).then(
      (data) => setTour({ key: "tour", data }),
      (e) => !ac.signal.aborted && setTour(failure("tour", e)),
    );
    return () => ac.abort();
  }, []);
  const tourPatient = tour && "data" in tour ? tour.data : null;
  const chart = urlChart ?? tourPatient?.chart_no ?? null;

  const [today, setToday] = useState<ScheduleDay | null>(null);
  useEffect(() => {
    const ac = new AbortController();
    fetchSchedule(undefined, ac.signal).then(setToday, () => undefined);
    return () => ac.abort();
  }, []);

  const [record, setRecord] = useState<Loaded<PatientRecord> | null>(null);
  useEffect(() => {
    if (!chart) return;
    const ac = new AbortController();
    fetchPatient(chart, ac.signal).then(
      (data) => setRecord({ key: chart, data }),
      (e) => !ac.signal.aborted && setRecord(failure(chart, e)),
    );
    return () => ac.abort();
  }, [chart]);

  // 환자가 바뀌면 이동 수 0, 기본 화면(예약표에서 왔으면 처음 환자는 전자차트)
  const [state, setState] = useState({
    chart,
    nav: fromSchedule
      ? { ...DEFAULT_NAV, mode: "chart" as const }
      : DEFAULT_NAV,
    moves: 0,
  });
  if (state.chart !== chart)
    setState({
      chart,
      nav: state.chart === null && fromSchedule ? state.nav : DEFAULT_NAV,
      moves: 0,
    });

  const navigate = (next: EmrNav) =>
    setState((s) => ({
      ...s,
      nav: next,
      moves: s.moves + countMoves(s.nav, next),
    }));
  const pickPatient = (chartNo: string) => {
    const next = new URLSearchParams(params);
    next.set("chart", chartNo);
    next.delete("view");
    router.replace(`/emr?${next}`);
  };

  const current =
    record?.key === chart && "data" in record ? record.data : null;

  // 설명 카드 — 시작 화면에서 오면 펼침, 예약표에서 오면 한 줄로 접힘
  const [stage, setStage] = useState<Stage>(
    fromSchedule ? { kind: "collapsed" } : { kind: "map" },
  );
  const startTour = () => {
    setStage({ kind: "tour", step: 0 });
    if (!tourPatient) return;
    if (chart !== tourPatient.chart_no) pickPatient(tourPatient.chart_no);
    else setState((s) => ({ ...s, nav: TOUR_STEPS[0].nav, moves: 0 }));
  };
  const moveStep = (step: number) => {
    navigate(TOUR_STEPS[step].nav);
    setStage({ kind: "tour", step });
  };
  const onNext = () => {
    if (stage.kind !== "tour") return;
    if (stage.step < TOUR_STEPS.length - 1) moveStep(stage.step + 1);
    else setStage({ kind: "done", moves: state.moves });
  };
  const onPrev = () => {
    if (stage.kind === "tour" && stage.step > 0) moveStep(stage.step - 1);
  };
  const highlight: Highlight =
    stage.kind === "tour" ? TOUR_STEPS[stage.step].highlight : null;
  const tourRecord =
    current && tourPatient && current.patient.chart_no === tourPatient.chart_no
      ? current
      : null;

  if (record?.key === chart && "error" in record)
    return (
      <div className="flex flex-col gap-3">
        <ErrorBand text={record.error} />
        {tourPatient && (
          <Link
            href={`/emr?chart=${tourPatient.chart_no}`}
            className="text-link"
          >
            기준 환자({tourPatient.name})로 가기
          </Link>
        )}
      </div>
    );
  if (!current || !clinic || clinic === "error")
    return (
      <p className="py-10 text-center text-sm text-muted-foreground">
        불러오는 중…
      </p>
    );

  const emrWindow = (
    <EmrWindow
      record={current}
      today={clinic.today}
      todaySchedule={today}
      nav={state.nav}
      onNav={navigate}
      moves={state.moves}
      highlight={highlight}
      onPickPatient={pickPatient}
      locked={stage.kind === "tour"}
      part={part}
    />
  );
  const card = tourPatient ? (
    <ExplainCard
      stage={stage}
      tour={tourPatient}
      record={tourRecord}
      onStart={startTour}
      onPrev={onPrev}
      onNext={onNext}
      onExpand={() => setStage({ kind: "map" })}
    />
  ) : tour && "error" in tour ? (
    <p className="rounded-md border bg-card px-4 py-3 text-sm text-muted-foreground">
      기준일 예약이 없어 따라 보기를 할 수 없습니다
    </p>
  ) : null;

  return (
    <div className="flex flex-col gap-4">
      {stage.kind === "tour" && tourPatient && (
        <div className="flex flex-wrap items-center gap-3 rounded-md bg-accent px-4 py-2.5 text-sm text-accent-foreground">
          <b>따라 보기 중</b>
          <span>
            데스크 직원이 {tourPatient.time} {tourPatient.name} 님을 맞기 전에
            하는 일
          </span>
          <button
            type="button"
            onClick={() => setStage({ kind: "map" })}
            className="ml-auto font-medium underline"
          >
            그만 보기
          </button>
        </div>
      )}
      {stage.kind === "collapsed" && card}
      <div className="flex flex-col gap-4 xl:flex-row xl:items-start">
        <div className="min-w-0 flex-1">{emrWindow}</div>
        {stage.kind !== "collapsed" && (
          <div className="order-first xl:order-none xl:w-80 xl:shrink-0">
            {card}
          </div>
        )}
      </div>
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
