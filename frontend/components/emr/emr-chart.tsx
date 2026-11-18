"use client";

import { Th } from "@/components/emr/emr-desk";
import { Box, commas, PLAN_ST, Tabs } from "@/components/emr/emr-ui";
import {
  EMR_PANELS,
  type EmrNav,
  type Highlight,
} from "@/components/emr/emr-window";
import { Odontogram } from "@/components/emr/odontogram";
import type { PatientRecord, ScheduleDay } from "@/lib/emr-api";
import { cn } from "@/lib/utils";

const GROUPS = [
  "기본진료",
  "구강검사",
  "치석제거",
  "충치치료",
  "신경치료",
  "크라운",
  "임플란트",
  "발치",
  "치주치료",
  "소아치료",
  "X-ray",
  "약 처방",
];

/** 전자차트 — 진료 기록, 치식·처치 입력(읽기 전용), 진료 메모(최근 진료 내용), 옆 패널 탭 7개.
 * 옛 프로그램처럼 주의사항을 따로 띄우지 않는다 — 직원이 옆 패널 '메모' 탭에 적어 둔 것뿐이다 */
export function EmrChart({
  record,
  todaySchedule,
  today,
  nav,
  onNav,
  highlight,
  onPickPatient,
}: {
  record: PatientRecord;
  todaySchedule: ScheduleDay | null;
  nav: EmrNav;
  onNav: (next: EmrNav) => void;
  highlight: Highlight;
  today: string;
  onPickPatient: (chartNo: string) => void;
}) {
  const p = record.patient;
  // 메모 탭 — 기준일에 유효한 주의사항을 직원이 적어 둔 메모처럼(기록일·출처)
  const memos = record.alerts.filter(
    (a) => a.recorded <= today && (!a.ends || today < a.ends),
  );
  const treatments = [...record.treatments].reverse();
  // 승인된 종이 차트 이관 줄(차트 OCR 설계 §설계 3) — EMR 이전이라 진료 기록 맨 아래에, 최근 것 먼저
  const paper = [...record.paper_records].reverse();
  const live = (todaySchedule?.appointments ?? []).filter(
    (a) => a.status !== "cancelled" && a.status !== "noshow",
  );
  const pick = (chartNo: string, text: string) => (
    <button
      key={`${chartNo}-${text}`}
      type="button"
      onClick={() => onPickPatient(chartNo)}
      className={cn(
        "block w-full truncate text-left hover:bg-emr-surface-2",
        chartNo === p.chart_no && "bg-emr-row-on",
      )}
    >
      {text}
    </button>
  );
  const panels: Record<(typeof EMR_PANELS)[number], React.ReactNode> = {
    기본: (
      <dl className="grid grid-cols-[3rem_1fr] gap-y-0.5">
        {[
          ["나이", `${p.age}세 ${p.sex}`],
          ["보험", "건강보험"],
          ["실손", p.private_insurance ? "가입" : "미가입"],
          ["전화", p.phone],
        ].map(([k, v]) => (
          <div key={k} className="contents">
            <dt className="text-emr-fg-muted">{k}</dt>
            <dd>{v}</dd>
          </div>
        ))}
      </dl>
    ),
    메모:
      memos.length || record.paper_notes.length ? (
        <>
          {memos.map((a) => (
            <p key={a.code}>
              {a.recorded} {a.source === "intake" ? "문진표" : "진료 중"} —{" "}
              {a.label}
            </p>
          ))}
          {record.paper_notes.map((n) => (
            <div
              key={n.import_id}
              className="mt-1 border-t border-emr-line pt-1"
            >
              <p className="text-emr-fg-muted">
                <small className="border border-emr-line px-1">종이</small> 종이
                차트 이관 {n.decided_at.slice(0, 10)}
              </p>
              {n.memo && (
                <p className="font-bold text-emr-alert-fg">{n.memo}</p>
              )}
              {(
                [
                  ["병력", n.present_illness],
                  ["주소", n.chief_complaint],
                  ["계획", n.treatment_plan],
                ] as const
              ).map(
                ([k, v]) =>
                  v && (
                    <p key={k}>
                      <span className="text-emr-fg-muted">{k}</span> {v}
                    </p>
                  ),
              )}
            </div>
          ))}
        </>
      ) : (
        <p>개별 메모 없음</p>
      ),
    계획: record.plans.length ? (
      record.plans.map((x) => (
        <p key={x.id}>
          {x.created} {x.procedure} {x.tooth !== "00" ? `#${x.tooth}` : ""} ·{" "}
          {PLAN_ST[x.status] ?? x.status}
        </p>
      ))
    ) : (
      <p>치료계획 없음</p>
    ),
    접수: live.map((a) =>
      pick(a.chart_no, `${a.time} ${a.name} · ${a.procedure}`),
    ),
    예약: live.length ? (
      live.map((a) => pick(a.chart_no, `${a.time} ${a.name} · ${a.procedure}`))
    ) : (
      <p>오늘 예약 없음</p>
    ),
    수납:
      record.payments.length || record.paper_payments.length ? (
        <>
          {[...record.payments]
            .slice(-8)
            .reverse()
            .map((x, i) => (
              <p key={i}>
                {x.at.slice(0, 10)} · {commas(x.paid)} / {commas(x.due)}
              </p>
            ))}
          {record.paper_payments.length > 0 && (
            <p className="mt-1 border-t border-emr-line pt-1 text-emr-fg-muted">
              <small className="border border-emr-line px-1">종이</small> 종이
              차트 이관 수납 {record.paper_payments.length}건
            </p>
          )}
          {[...record.paper_payments].reverse().map((x, i) => (
            <p key={`paper-${i}`}>
              {x.date ?? "날짜 없음"} · {x.item} {commas(x.amount ?? 0)}
              {x.balance ? (
                <span className="text-emr-alert-fg">
                  {" "}
                  (잔액 {commas(x.balance)})
                </span>
              ) : null}
            </p>
          ))}
        </>
      ) : (
        <p>수납 내역 없음</p>
      ),
    가족: p.family.length ? (
      p.family.map((f) => <p key={f.chart_no}>{f.name}</p>)
    ) : (
      <p>등록된 가족 없음</p>
    ),
  };

  return (
    <div className="flex flex-col gap-1.5">
      <div className="grid grid-cols-[19rem_minmax(0,1fr)_15rem] gap-1.5">
        <Box title="진료 기록" className="max-h-[36rem] overflow-auto">
          {treatments.length || paper.length ? (
            treatments.map((t, i) => (
              <article
                key={i}
                className="grid grid-cols-[4.5rem_1fr] gap-1 border-b border-emr-line-soft py-1"
              >
                <time className="text-emr-fg-muted">{t.at}</time>
                <div>
                  <b>{t.procedure}</b>
                  {t.tooth !== "00" && (
                    <span className="tabular-nums"> #{t.tooth}</span>
                  )}
                  {t.dx && (
                    <small className="text-emr-fg-muted"> 상병 {t.dx}</small>
                  )}
                  <p>{t.note ?? "차트 서술 없음"}</p>
                </div>
              </article>
            ))
          ) : (
            <p className="text-emr-fg-muted">진료 기록 없음</p>
          )}
          {paper.length > 0 && (
            <p className="mt-1 border-b border-emr-line bg-emr-surface-2 px-1 font-bold">
              종이 차트 이관 {paper.length}건
            </p>
          )}
          {paper.map((t, i) => (
            <article
              key={`paper-${i}`}
              className="grid grid-cols-[4.5rem_1fr] gap-1 border-b border-emr-line-soft py-1"
            >
              <time className="text-emr-fg-muted">{t.date ?? "날짜 없음"}</time>
              <div>
                <small className="border border-emr-line px-1 text-emr-fg-muted">
                  종이
                </small>
                {t.fdi ? (
                  <span className="tabular-nums"> #{t.fdi}</span>
                ) : (
                  t.teeth && <span> {t.teeth}</span>
                )}
                <p>{t.treatment}</p>
              </div>
            </article>
          ))}
        </Box>
        <div className="flex min-w-0 flex-col gap-1.5">
          <div className="flex">
            {["PI 입력", "치료계획 입력", "진료 입력", "문서 발급"].map((t) => (
              <span
                key={t}
                className={cn(
                  "-mr-px border border-b-0 border-emr-line px-2.5 py-0.5",
                  t === "진료 입력"
                    ? "bg-emr-surface font-bold"
                    : "bg-emr-tab-off",
                )}
              >
                {t}
              </span>
            ))}
          </div>
          <Box>
            <Odontogram record={record} />
          </Box>
          <Box className="grid grid-cols-6 gap-1">
            {GROUPS.map((g) => (
              <button
                key={g}
                type="button"
                disabled
                className="border border-emr-line bg-emr-surface-2 py-1 opacity-70"
              >
                {g}
              </button>
            ))}
          </Box>
          <Box title="처치 입력 · 읽기 전용">
            <table className="w-full border-collapse">
              <tbody>
                <tr>
                  <Th>상병명</Th>
                  <td className="border border-emr-line-soft px-1.5">
                    입력 전
                  </td>
                  <Th>보험 구분</Th>
                  <td className="border border-emr-line-soft px-1.5">
                    건강보험
                  </td>
                </tr>
                <tr>
                  <Th>총진료비</Th>
                  <td className="border border-emr-line-soft px-1.5">0</td>
                  <Th>본인부담금</Th>
                  <td className="border border-emr-line-soft px-1.5">0</td>
                </tr>
              </tbody>
            </table>
          </Box>
          <Box title="진료 메모">
            <p>
              {treatments[0]?.note
                ? `최근 메모: ${treatments[0].note.slice(0, 90)}`
                : "메모 없음"}
            </p>
          </Box>
        </div>
        <Box highlight={highlight === "panel"}>
          <Tabs
            items={EMR_PANELS}
            value={nav.panel}
            onChange={(panel) => onNav({ ...nav, panel })}
          />
          <div className="max-h-[32rem] overflow-auto border border-emr-line p-1.5">
            {panels[nav.panel]}
          </div>
        </Box>
      </div>
    </div>
  );
}
