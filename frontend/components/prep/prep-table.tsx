"use client";

import Link from "next/link";
import { Fragment } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  type Chip,
  CHIP_LABEL,
  CHIPS,
  hasTodo,
  type PrepRow,
  type PrepSheet,
  type Suggestion,
  TYPE_LABEL,
} from "@/lib/prep-api";
import { won } from "@/lib/format";
import { cn } from "@/lib/utils";

type Go = (patch: Record<string, string | null>) => void;

const CHIP_VARIANT: Partial<Record<Chip, "danger" | "warn" | "info">> = {
  alert: "danger",
  balance: "warn",
  unconfirmed: "warn",
  insurance: "info",
  family_recall: "info",
  open_plan: "info",
};
const STATUS_LABEL = { scheduled: "예정", completed: "내원", noshow: "노쇼" };
const TYPES = ["treatment", "hygiene", "consult"] as const;

/** 준비표 목록 — 칩·종류·할 일 필터, 몰리는 시간대, 인쇄, 행 펼침(사실·제안·근거·후검증) */
export function PrepTable({
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
  const all = sheet.rows;
  const chip = (CHIPS as string[]).includes(params.get("chip") ?? "")
    ? (params.get("chip") as Chip)
    : "appointment";
  const type = params.get("type");
  const todoOnly = params.get("todo") === "1";
  const open = Number(params.get("open")) || null;
  const past = today !== null && sheet.date < today;
  const rows = all
    .filter((r) => r.chips.includes(chip))
    .filter((r) => !type || r.type === type)
    .filter((r) => !todoOnly || hasTodo(r));

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap gap-1.5 print:hidden" aria-label="칩">
        {CHIPS.map((c) => (
          <Button
            key={c}
            size="sm"
            variant={c === chip ? "secondary" : "outline"}
            aria-pressed={c === chip}
            onClick={() => go({ chip: c === "appointment" ? null : c })}
          >
            {CHIP_LABEL[c]} {all.filter((r) => r.chips.includes(c)).length}
          </Button>
        ))}
      </div>
      <div className="flex flex-wrap items-center gap-1.5 print:hidden">
        <Button
          size="sm"
          variant={!type ? "secondary" : "outline"}
          aria-pressed={!type}
          onClick={() => go({ type: null })}
        >
          전체 {all.length}
        </Button>
        {TYPES.map((t) => (
          <Button
            key={t}
            size="sm"
            variant={type === t ? "secondary" : "outline"}
            aria-pressed={type === t}
            onClick={() => go({ type: t })}
          >
            {TYPE_LABEL[t]} {all.filter((r) => r.type === t).length}
          </Button>
        ))}
        <Button
          size="sm"
          variant={todoOnly ? "secondary" : "outline"}
          aria-pressed={todoOnly}
          onClick={() => go({ todo: todoOnly ? null : "1" })}
        >
          할 일 있는 환자만 {all.filter(hasTodo).length}
        </Button>
        <span className="flex-1" />
        <Button size="sm" variant="outline" onClick={() => window.print()}>
          인쇄
        </Button>
      </div>
      <BusyHours rows={all} />
      <div className="overflow-x-auto rounded-md border">
        <table className="w-full text-sm">
          <thead className="bg-muted text-left text-muted-foreground">
            <tr>
              <th className="px-3 py-2">시간</th>
              <th className="px-3 py-2">환자</th>
              <th className="px-3 py-2">예정 시술</th>
              <th className="px-3 py-2">칩</th>
              <th className="px-3 py-2">제안 (AI)</th>
              {past && <th className="px-3 py-2">결과</th>}
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <Fragment key={r.appointment_id}>
                <tr
                  className={cn(
                    "cursor-pointer border-t hover:bg-muted/50",
                    open === r.appointment_id && "bg-muted/50",
                  )}
                  onClick={() =>
                    go({
                      open:
                        open === r.appointment_id
                          ? null
                          : String(r.appointment_id),
                    })
                  }
                >
                  <td className="px-3 py-2 align-middle whitespace-nowrap tabular-nums">
                    <b>{r.time}</b>
                    <div className="text-xs text-muted-foreground">
                      {r.chair_no ? `${r.chair_no}번 체어` : "상담실"}
                    </div>
                  </td>
                  <td className="px-3 py-2 align-middle whitespace-nowrap">
                    <Link
                      href={`/emr?chart=${encodeURIComponent(r.patient.chart_no)}`}
                      className="font-bold text-primary hover:underline"
                      onClick={(e) => e.stopPropagation()}
                    >
                      {r.patient.name}
                    </Link>{" "}
                    {r.patient.new_patient && (
                      <Badge variant="kind">신환</Badge>
                    )}
                    <div className="text-xs text-muted-foreground tabular-nums">
                      {r.patient.chart_no} · {r.patient.age}세{" "}
                      {r.patient.sex === "M" ? "남" : "여"}
                    </div>
                  </td>
                  <td className="px-3 py-2 align-middle whitespace-nowrap">
                    {r.procedure}
                    <div className="text-xs text-muted-foreground">
                      {TYPE_LABEL[r.type]} · {r.staff} · {r.duration_min}분
                    </div>
                  </td>
                  <td className="px-3 py-2 align-middle">
                    <div className="flex flex-wrap gap-1">
                      {r.chips
                        .filter((c) => c !== "appointment")
                        .map((c) => (
                          <Badge key={c} variant={CHIP_VARIANT[c] ?? "muted"}>
                            {CHIP_LABEL[c]}
                          </Badge>
                        ))}
                    </div>
                  </td>
                  <td className="min-w-60 px-3 py-2 align-middle">
                    <SuggestionLine row={r} />
                  </td>
                  {past && (
                    <td className="px-3 py-2 align-middle">
                      <Badge
                        variant={r.status === "noshow" ? "danger" : "muted"}
                      >
                        {STATUS_LABEL[r.status]}
                      </Badge>
                    </td>
                  )}
                </tr>
                {open === r.appointment_id && (
                  <tr className="border-t bg-muted/30">
                    <td />
                    <td colSpan={past ? 5 : 4} className="px-3 py-3">
                      <Expand row={r} />
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
            {rows.length === 0 && (
              <tr>
                <td
                  colSpan={past ? 6 : 5}
                  className="px-3 py-8 text-center text-muted-foreground"
                >
                  이 조건에 해당하는 환자가 없습니다
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export function SuggestionLine({ row }: { row: PrepRow }) {
  const prev = row.refreshed?.prev;
  return (
    <span>
      {row.refreshed && (
        <Badge variant="info" className="mr-1.5">
          방금 갱신
        </Badge>
      )}
      <SuggestionText s={row.suggestion} generated={row.generated} />
      {row.refreshed && "prev" in row.refreshed && (
        <span className="block text-xs text-muted-foreground">
          이전:{" "}
          {prev
            ? prev.action === "none"
              ? "그대로 진행"
              : prev.text
            : "제안 없음 — 후검증 실패"}
        </span>
      )}
      {row.stale && (
        <span className="block text-xs text-status-warn-fg">
          제안 만든 뒤 바뀜: {row.changed.map(changeText).join(", ") || "사실"}
        </span>
      )}
    </span>
  );
}

function SuggestionText({
  s,
  generated,
}: {
  s: Suggestion | null;
  generated: boolean;
}) {
  if (!generated)
    return <span className="text-muted-foreground">아직 만들지 않음</span>;
  if (!s)
    return (
      <span className="font-bold text-status-danger-fg">
        제안 없음 — 후검증 실패
      </span>
    );
  if (s.action === "none")
    return <span className="text-muted-foreground">그대로 진행</span>;
  return <>{s.text}</>;
}

// 내원 확인 값 — 누가 무엇을 확인했는지 드러나게 사실로 쓴다. 문자 없음은 기준일이면 발송 시각 뒤 잡힌 예약,
// 앞날이면 아직 보내기 전(backend/prep.py _confirm)
const CONFIRM_TEXT: Record<NonNullable<PrepRow["confirm"]>, string> = {
  replied: "환자가 온다고 답함",
  no_reply: "문자에 답 없음 — 확인 필요",
  no_message: "문자 발송 시각이 지난 뒤 잡힌 예약 — 확인 필요",
  not_sent: "확인 문자 보내기 전",
};

function Expand({ row }: { row: PrepRow }) {
  const s = row.suggestion;
  const facts: [string, string][] = [
    ["마지막 내원", row.last_visit ?? "첫 내원"],
    [
      "예정 시술",
      `${row.procedure} · ${TYPE_LABEL[row.type]} · ${row.staff} · ${row.duration_min}분 · ${row.chair_no ? `${row.chair_no}번 체어` : "상담실"}`,
    ],
    ["미수 잔액", row.balance > 0 ? won(row.balance) : "없음"],
    [
      "주의사항",
      row.alerts.length
        ? row.alerts
            .map(
              (a) =>
                `${a.label}${a.related ? ` → ${a.action}` : " (오늘 시술과 무관)"}`,
            )
            .join(", ")
        : "없음",
    ],
    ["올해 스케일링 급여", row.scaling_unused ? "미사용" : "해당 없음"],
    [
      "가족 리콜",
      row.family_recall.length
        ? row.family_recall
            .map((f) => `${f.name}(마지막 스케일링 ${f.last_hygiene})`)
            .join(", ")
        : "없음",
    ],
    [
      "남은 치료계획",
      row.open_plan.length
        ? row.open_plan
            .map(
              (p) =>
                `${p.procedure} ${p.tooth === "00" ? "" : `${p.tooth} `}· 예상 ${won(p.est_fee)}`,
            )
            .join(", ")
        : "없음",
    ],
    ["내원 확인", row.confirm ? CONFIRM_TEXT[row.confirm] : "—"],
  ];
  return (
    <div className="grid gap-6 md:grid-cols-2">
      <div>
        <h3 className="mb-2 text-xs font-bold text-muted-foreground">
          사실 (코드가 EMR에서 모음)
        </h3>
        <dl className="flex flex-col gap-1">
          {facts.map(([k, v]) => (
            <div key={k} className="flex gap-3">
              <dt className="w-28 shrink-0 text-muted-foreground">{k}</dt>
              <dd>{v}</dd>
            </div>
          ))}
        </dl>
      </div>
      <div className="flex flex-col gap-2">
        <h3 className="text-xs font-bold text-muted-foreground">
          오늘 할 일 제안 (AI)
        </h3>
        <SuggestionLine row={row} />
        {s && s.used_facts.length > 0 && (
          <div className="flex flex-wrap items-center gap-1 text-xs">
            근거
            {s.used_facts.map((u) => (
              <span key={u.key} className="rounded-sm bg-muted px-1">
                {factLabel(u.key, u.value)}
              </span>
            ))}
          </div>
        )}
        {row.generated && (
          <p className="text-xs">
            {row.suggestion_valid
              ? "후검증 통과 — 근거가 사실과 같고, 제안 속 숫자가 근거에 있음"
              : "후검증 실패 — 두 번 모두 사실에 없는 값이 있어 비워 둠"}
          </p>
        )}
      </div>
    </div>
  );
}

const FACT_LABEL: Record<string, string> = {
  procedure: "예정 시술",
  type: "예약 종류",
  last_visit: "마지막 내원",
  balance: "미수 잔액",
  scaling_unused: "올해 스케일링 급여",
};

const LIST_FACT: Record<string, string> = {
  alert: "주의",
  plan: "남은 치료계획",
  family: "가족 리콜",
};

/** 목록 사실(alert.·plan.·family.) 값(backend/prep.py llm_facts) → 항목 이름. 지금 행이 아니라 값에서 읽는다 —
 * 남은 치료계획 키는 순번이라 계획이 바뀌면 다른 항목을 가리킨다 */
function itemName(head: string, value: string): string {
  if (head === "alert") return value.split(" — ")[0];
  if (head === "family") return value.split(" ")[0];
  return value.replace(/(\d+)원/, (_, n) => won(Number(n)));
}

/** 근거 한 건 → 사실 목록과 같은 이름. 모르는 키는 그대로 */
function factLabel(key: string, value: string): string {
  const head = key.split(".")[0];
  if (head in LIST_FACT) return `${LIST_FACT[head]}: ${itemName(head, value)}`;
  return FACT_LABEL[key] ?? key;
}

/** 제안 만든 뒤 바뀐 제안 입력 한 건(backend/prep.py sheet changed) → 한 구절 */
function changeText(c: PrepRow["changed"][number]): string {
  const head = c.key.split(".")[0];
  if (head in LIST_FACT) {
    const what =
      c.before === null ? "생김" : c.after === null ? "빠짐" : "바뀜";
    return `${LIST_FACT[head]} ${what}: ${itemName(head, c.after ?? c.before ?? "")}`;
  }
  const fmt = (v: string | null) =>
    v === null
      ? c.key === "scaling_unused"
        ? "해당 없음"
        : "없음"
      : c.key === "balance"
        ? won(Number(v))
        : c.key === "type"
          ? (TYPE_LABEL[v as PrepRow["type"]] ?? v)
          : c.key === "scaling_unused"
            ? "미사용"
            : v;
  return `${FACT_LABEL[c.key] ?? c.key} ${fmt(c.before)} → ${fmt(c.after)}`;
}

/** 시작 시각 기준 시간대별 예약 수 */
function BusyHours({ rows }: { rows: PrepRow[] }) {
  const counts = new Map<number, number>();
  for (const r of rows) {
    const h = Number(r.time.slice(0, 2));
    counts.set(h, (counts.get(h) ?? 0) + 1);
  }
  const hours = [...counts.keys()];
  const lo = Math.min(...hours);
  const hi = Math.max(...hours);
  const max = Math.max(...counts.values());
  const span = Array.from({ length: hi - lo + 1 }, (_, i) => lo + i);
  return (
    <section aria-label="몰리는 시간대" className="flex flex-col gap-1">
      <h2 className="text-xs font-bold text-muted-foreground">
        몰리는 시간대 (시작 시각 기준 예약 수)
      </h2>
      <div className="flex items-end gap-1.5">
        {span.map((h) => {
          const n = counts.get(h) ?? 0;
          return (
            <div key={h} className="flex w-8 flex-col items-center gap-0.5">
              <span className="text-xs tabular-nums">{n}</span>
              <div
                className={cn(
                  "w-full rounded-sm",
                  n === max ? "bg-primary" : "bg-muted-foreground/40",
                )}
                style={{ height: `${(n / max) * 40}px` }}
              />
              <span className="text-xs text-muted-foreground">{h}시</span>
            </div>
          );
        })}
      </div>
    </section>
  );
}
