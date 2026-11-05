"use client";

import { Badge } from "@/components/ui/badge";
import { type Appt, type ScheduleDay, STATUS_LABEL } from "@/lib/emr-api";
import { cn } from "@/lib/utils";

export const SLOT_MIN = 30; // 지금 데이터의 예약 단위(시각 :00·:30, 소요 30·60·90분)
const ROW = "2.75rem"; // 30분 한 칸 높이

const toMin = (t: string) => Number(t.slice(0, 2)) * 60 + Number(t.slice(3, 5));
const toTime = (m: number) =>
  `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;

const CARD: Record<Appt["type"], string> = {
  treatment: "bg-accent text-accent-foreground",
  hygiene: "bg-secondary text-secondary-foreground",
  consult: "border bg-card",
};

/** 담당자 × 30분 격자. 카드는 소요 시간만큼 늘린다(전환 전 EMR·일별 예약표 설계안 '예약표') */
export function DayGrid({
  day,
  showAll,
  selected,
  onSelect,
}: {
  day: ScheduleDay;
  showAll: boolean;
  selected: number | null;
  onSelect: (id: number) => void;
}) {
  if (day.hours.closed) {
    return (
      <p className="py-10 text-center text-muted-foreground">휴진일입니다</p>
    );
  }
  const open = toMin(day.hours.open);
  const slots = Math.ceil((toMin(day.hours.close) - open) / SLOT_MIN);
  // 칸 번호(헤더 다음이 2). 30분 단위가 아닌 시각은 반올림, 영업시간 밖은 안으로 붙인다
  const row = (t: string) =>
    Math.min(Math.max(Math.round((toMin(t) - open) / SLOT_MIN), 0), slots - 1) +
    2;
  const span = (start: string, minutes: number) =>
    Math.max(
      1,
      Math.min(Math.round(minutes / SLOT_MIN), slots + 2 - row(start)),
    );
  const shown = day.appointments.filter(
    (a) => showAll || (a.status !== "cancelled" && a.status !== "noshow"),
  );

  const byStaff = new Map(
    day.staff.map((st) => [
      st.id,
      lanes(shown.filter((a) => a.staff_id === st.id)),
    ]),
  );
  const weight = (id: number) =>
    Math.max(1, ...(byStaff.get(id) ?? []).map((x) => x.lanes));
  const lunch = day.hours.lunch;

  return (
    <div className="overflow-x-auto">
      <div
        className="grid gap-x-1.5 px-3 pb-3"
        style={{
          // 원장은 체어 여러 개를 오가며 동시에 진료한다 — 동시 진료 수만큼 열을 넓힌다
          gridTemplateColumns: `3.5rem ${day.staff.map((st) => `minmax(${Math.max(7, weight(st.id) * 6)}rem, ${weight(st.id)}fr)`).join(" ")}`,
          gridTemplateRows: `2.5rem repeat(${slots}, ${ROW})`,
        }}
      >
        {day.staff.map((st, i) => (
          <span
            key={st.id}
            className="self-center text-sm font-bold"
            style={{ gridColumn: i + 2, gridRow: 1 }}
          >
            {st.role} {st.name}
          </span>
        ))}
        {Array.from({ length: slots }, (_, i) => (
          <div
            key={i}
            aria-hidden="true"
            className={cn("border-t", i % 2 && "border-dashed")}
            style={{ gridColumn: "1 / -1", gridRow: i + 2 }}
          />
        ))}
        {Array.from({ length: slots }, (_, i) =>
          i % 2 ? null : (
            <span
              key={i}
              className="pt-0.5 text-xs text-muted-foreground"
              style={{ gridColumn: 1, gridRow: i + 2 }}
            >
              {toTime(open + i * SLOT_MIN)}
            </span>
          ),
        )}
        {lunch && (
          <span
            className="z-10 flex items-center justify-center rounded-md bg-muted text-xs text-muted-foreground"
            style={{
              gridColumn: "2 / -1",
              gridRow: `${row(lunch[0])} / span ${span(lunch[0], toMin(lunch[1]) - toMin(lunch[0]))}`,
            }}
          >
            {lunch[0]} – {lunch[1]} 점심시간
          </span>
        )}
        {day.staff.map((st, i) => (
          <div
            key={st.id}
            className="relative z-20"
            style={{ gridColumn: i + 2, gridRow: `2 / span ${slots}` }}
          >
            {(byStaff.get(st.id) ?? []).map(({ appt, lane, lanes: n }) => (
              <ApptCard
                key={appt.id}
                appt={appt}
                span={span(appt.time, appt.duration)}
                selected={appt.id === selected}
                onSelect={() => onSelect(appt.id)}
                style={{
                  position: "absolute",
                  top: `calc(${row(appt.time) - 2} * ${ROW} + 0.125rem)`,
                  left: `calc(${(lane / n) * 100}% + 1px)`,
                  width: `calc(${100 / n}% - 2px)`,
                }}
              />
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}

type Placed = {
  appt: Appt;
  start: number;
  end: number;
  lane: number;
  lanes: number;
};

/** 겹치는 예약을 나란히 — 시작 순으로 비어 있는 첫 줄에 넣고, 서로 겹치는 묶음의 줄 수로 너비를 나눈다 */
function lanes(appts: Appt[]): Placed[] {
  const items: Placed[] = appts
    .map((appt) => ({
      appt,
      start: toMin(appt.time),
      end: toMin(appt.time) + appt.duration,
      lane: 0,
      lanes: 1,
    }))
    .sort((a, b) => a.start - b.start || a.appt.id - b.appt.id);
  let group: Placed[] = [];
  let groupEnd = -1;
  const laneEnds: number[] = [];
  const close = () => {
    const n = Math.max(1, ...group.map((x) => x.lane + 1));
    for (const x of group) x.lanes = n;
    group = [];
    laneEnds.length = 0;
  };
  for (const it of items) {
    if (group.length && it.start >= groupEnd) close();
    let lane = laneEnds.findIndex((e) => e <= it.start);
    if (lane < 0) lane = laneEnds.push(it.end) - 1;
    else laneEnds[lane] = it.end;
    it.lane = lane;
    group.push(it);
    groupEnd = Math.max(groupEnd, it.end);
  }
  close();
  return items;
}

function ApptCard({
  appt,
  span,
  selected,
  onSelect,
  style,
}: {
  appt: Appt;
  span: number;
  selected: boolean;
  onSelect: () => void;
  style: React.CSSProperties;
}) {
  const off = appt.status === "cancelled" || appt.status === "noshow";
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={selected}
      className={cn(
        "flex min-w-0 flex-col gap-0.5 overflow-hidden rounded-md px-2 py-0.5 text-left text-xs",
        // 취소·노쇼는 흐리게 하지 않고(다크 모드에서 안 읽힘) 점선 테두리·취소선으로 구분한다
        off
          ? "border border-l-4 border-border border-l-status-danger-dot bg-hatch text-muted-foreground"
          : CARD[appt.type],
        selected && "ring-2 ring-primary",
      )}
      style={{ ...style, height: `calc(${span} * ${ROW} - 0.25rem)` }}
    >
      {/* 30분 한 칸에 두 줄이 들어가게 첫 줄은 줄바꿈하지 않고 이름을 줄이며, 배지는 낮게 */}
      <span className="flex min-w-0 items-center gap-1">
        <b className={cn("min-w-0 truncate", off && "line-through")}>
          {appt.time} {appt.name}
        </b>
        {off && (
          <Badge variant="danger" className="h-4 shrink-0 py-0">
            {STATUS_LABEL[appt.status]}
          </Badge>
        )}
        {appt.new_patient && (
          <Badge variant="info" className="h-4 shrink-0 py-0">
            신환
          </Badge>
        )}
        {appt.unconfirmed && (
          <Badge variant="warn" className="h-4 shrink-0 py-0">
            예약 미확인
          </Badge>
        )}
      </span>
      <span className="truncate">
        {appt.procedure} · {appt.duration}분
      </span>
      {appt.plan_step && span > 1 && (
        <span className="truncate">
          치료계획 {appt.plan_step.seq}/{appt.plan_step.total}
        </span>
      )}
    </button>
  );
}
