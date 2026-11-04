"use client";

import {
  Box,
  CLAIM_ST,
  commas,
  Grid,
  MSG_TRIG,
  PAY_KIND,
  PAY_METHOD,
  PLAN_ST,
  Tabs,
} from "@/components/emr/emr-ui";
import {
  EMR_TABS,
  type EmrNav,
  type EmrTab,
  type Highlight,
} from "@/components/emr/emr-window";
import {
  type PatientRecord,
  type ScheduleDay,
  STATUS_LABEL,
  TYPE_LABEL,
} from "@/lib/emr-api";
import { cn } from "@/lib/utils";

/** 데스크 — 환자 정보·당일 수납·히스토리 탭 7개, 오른쪽에 접수 목록과 오늘 예약 */
export function EmrDesk({
  record,
  today,
  todaySchedule,
  nav,
  onNav,
  highlight,
  onPickPatient,
}: {
  record: PatientRecord;
  today: string;
  todaySchedule: ScheduleDay | null;
  nav: EmrNav;
  onNav: (next: EmrNav) => void;
  highlight: Highlight;
  onPickPatient: (chartNo: string) => void;
}) {
  const p = record.patient;
  const bal = record.summary.balance;
  const live = (todaySchedule?.appointments ?? []).filter(
    (a) => a.status !== "cancelled" && a.status !== "noshow",
  );
  const form: [string, string][] = [
    ["이름", p.name],
    ["차트번호", p.chart_no],
    ["생년월일", `${p.birth} (${p.age}세 ${p.sex})`],
    ["전화", p.phone],
    ["등록일", p.registered],
    ["실손보험", p.private_insurance ? "가입" : "미가입"],
    ["가족", p.family.map((f) => f.name).join(", ") || "-"],
  ];

  return (
    <div className="grid grid-cols-[27rem_minmax(0,1fr)] gap-1.5">
      <div className="flex min-w-0 flex-col gap-1.5">
        <Box title="환자 정보">
          <div className="grid grid-cols-[4rem_1fr] gap-x-1.5 gap-y-0.5">
            {form.map(([k, v]) => (
              <Pair key={k} k={k} v={v} />
            ))}
          </div>
        </Box>
        <Box title="당일 수납 정보" highlight={highlight === "balance"}>
          <table className="w-full border-collapse">
            <tbody>
              <tr>
                <Th>총진료비</Th>
                <Td>0</Td>
                <Th>본인부담금</Th>
                <Td>0</Td>
              </tr>
              <tr>
                <Th>미수금</Th>
                <Td red={bal > 0}>{commas(bal)}</Td>
                <Th>수납할 금액</Th>
                <Td red={bal > 0}>{commas(bal)}</Td>
              </tr>
            </tbody>
          </table>
        </Box>
        <Box
          title={`${p.name}님 히스토리`}
          className="flex-1"
          highlight={highlight === "messages"}
        >
          <Tabs
            items={EMR_TABS}
            value={nav.tab}
            onChange={(tab) => onNav({ ...nav, tab })}
          />
          <div className="max-h-72 overflow-auto border border-emr-line p-1">
            <HistoryPane record={record} tab={nav.tab} />
          </div>
        </Box>
      </div>
      <div className="flex min-w-0 flex-col gap-1.5">
        <Box title={`접수 목록 · ${today.slice(5)}`}>
          <div className="max-h-52 overflow-auto">
            <table className="w-full border-collapse">
              <thead>
                <tr>
                  {[
                    "순번",
                    "상태",
                    "차트번호",
                    "이름",
                    "예약",
                    "담당",
                    "진료 내용",
                    "확인",
                  ].map((h) => (
                    <Th key={h}>{h}</Th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {live.map((a, i) => (
                  <tr
                    key={a.id}
                    onClick={() => onPickPatient(a.chart_no)}
                    className={cn(
                      "cursor-pointer",
                      a.chart_no === p.chart_no && "bg-emr-row-on",
                    )}
                  >
                    <Td>{i + 1}</Td>
                    <Td>예약</Td>
                    <Td>{a.chart_no}</Td>
                    <Td>{a.name}</Td>
                    <Td>{a.time}</Td>
                    <Td>{a.staff_name}</Td>
                    <Td>{a.procedure}</Td>
                    <Td>{a.unconfirmed ? "미확인" : "확인"}</Td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Box>
        <Box title="예약 목록 · 오늘" className="flex-1">
          <DayTable day={todaySchedule} onPickPatient={onPickPatient} />
        </Box>
      </div>
    </div>
  );
}

/** 히스토리 탭 내용 — 최근 것이 위 */
export function HistoryPane({
  record,
  tab,
}: {
  record: PatientRecord;
  tab: EmrTab;
}) {
  const r = record;
  if (tab === "수납")
    return (
      <Grid
        heads={["일시", "구분", "청구액", "수납액", "수단"]}
        rows={[...r.payments]
          .reverse()
          .map((x) => [
            x.at,
            PAY_KIND[x.kind] ?? x.kind,
            x.due,
            x.paid,
            PAY_METHOD[x.method] ?? x.method,
          ])}
      />
    );
  if (tab === "청구")
    return (
      <Grid
        heads={[
          "청구월",
          "진료일",
          "시술",
          "청구액",
          "상태",
          "지급액",
          "조정 사유",
        ]}
        rows={[...r.claims]
          .reverse()
          .map((x) => [
            x.month,
            x.service,
            x.procedure,
            x.amount,
            CLAIM_ST[x.status] ?? x.status,
            x.paid ?? "-",
            x.adjust ?? "",
          ])}
        wrapLast
      />
    );
  if (tab === "치료계획")
    return (
      <Grid
        heads={["작성일", "시술", "치아", "예상 금액", "상태"]}
        rows={r.plans.map((x) => [
          x.created,
          x.procedure,
          x.tooth,
          x.est_fee,
          PLAN_ST[x.status] ?? x.status,
        ])}
      />
    );
  if (tab === "주의사항")
    return (
      <Grid
        heads={["항목", "기록일", "종료일", "출처"]}
        rows={r.alerts.map((a) => [
          a.label,
          a.recorded,
          a.ends ?? "-",
          a.source === "intake" ? "문진표" : "진료 중",
        ])}
      />
    );
  if (tab === "예약")
    return (
      <Grid
        heads={["일시", "구분", "예정 시술", "상태", "담당", "체어"]}
        rows={[...r.appointments]
          .reverse()
          .map((a) => [
            a.at,
            TYPE_LABEL[a.type],
            a.procedure,
            STATUS_LABEL[a.status],
            a.staff,
            a.chair ?? "-",
          ])}
      />
    );
  if (tab === "문자")
    return (
      <Grid
        heads={["일시", "방향", "종류", "내용"]}
        rows={[...r.messages]
          .reverse()
          .map((m) => [
            m.at,
            m.direction === "out" ? "발신" : "수신",
            MSG_TRIG[m.trigger] ?? m.trigger,
            m.body,
          ])}
        wrapLast
      />
    );
  return (
    <Grid
      heads={[
        "일자",
        "시술",
        "치아",
        "진료비",
        "공단",
        "본인부담",
        "상병",
        "차트 내용",
      ]}
      rows={[...r.treatments]
        .reverse()
        .map((x) => [
          x.at,
          x.procedure,
          x.tooth,
          x.fee,
          x.insured,
          x.patient_amount,
          x.dx ?? "",
          x.note ?? "",
        ])}
      wrapLast
    />
  );
}

/** 오늘 예약을 시 × 담당자 표로 — 옛 프로그램식(카드 크기는 소요 시간과 무관) */
export function DayTable({
  day,
  onPickPatient,
}: {
  day: ScheduleDay | null;
  onPickPatient: (chartNo: string) => void;
}) {
  if (!day) return null;
  const rows = day.appointments.filter((a) => a.status !== "cancelled");
  const hours = [...new Set(rows.map((a) => a.time.slice(0, 2)))].sort();
  if (!hours.length)
    return (
      <p className="text-emr-fg-muted">
        {day.hours.closed ? "휴진" : "예약 없음"}
      </p>
    );
  return (
    <div className="overflow-auto">
      <table className="w-full table-fixed border-collapse">
        <thead>
          <tr>
            <Th className="w-9" />
            {day.staff.map((s) => (
              <Th key={s.id}>{s.name}</Th>
            ))}
          </tr>
        </thead>
        <tbody>
          {hours.map((h) => (
            <tr key={h}>
              <Th>{Number(h)}시</Th>
              {day.staff.map((s) => (
                <td
                  key={s.id}
                  className="border border-emr-line-soft p-0.5 align-top"
                >
                  {rows
                    .filter((a) => a.staff_id === s.id && a.time.startsWith(h))
                    .map((a) => (
                      <button
                        key={a.id}
                        type="button"
                        onClick={() => onPickPatient(a.chart_no)}
                        title={a.new_patient ? "신환" : undefined}
                        className={cn(
                          "mb-0.5 block w-full truncate border-l-4 border-emr-line bg-emr-surface-2 px-1 text-left",
                          a.new_patient && "border-l-status-info-dot",
                        )}
                      >
                        <b>
                          {a.time} {a.name}
                        </b>{" "}
                        {a.procedure}
                      </button>
                    ))}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Pair({ k, v }: { k: string; v: string }) {
  return (
    <>
      <span className="text-emr-fg-muted">{k}</span>
      <span>{v}</span>
    </>
  );
}

export function Th({
  children,
  className,
}: {
  children?: React.ReactNode;
  className?: string;
}) {
  return (
    <th
      className={cn(
        "border border-emr-line-soft bg-emr-chrome px-1.5 py-0.5 text-left font-semibold whitespace-nowrap",
        className,
      )}
    >
      {children}
    </th>
  );
}

export function Td({
  children,
  red,
}: {
  children: React.ReactNode;
  red?: boolean;
}) {
  return (
    <td
      className={cn(
        "border border-emr-line-soft px-1.5 py-0.5 whitespace-nowrap",
        red && "font-bold text-emr-alert-fg",
      )}
    >
      {children}
    </td>
  );
}
