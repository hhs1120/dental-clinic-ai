"use client";

import Link from "next/link";
import { useState } from "react";

import { Transcript } from "@/components/calls/transcript";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { useFetch } from "@/lib/api";
import {
  type CallDetail as Detail,
  DIRECTION_LABEL,
  type LabelNames,
  type TodoItem,
} from "@/lib/calls-api";
import { fetchPatient, won } from "@/lib/emr-api";
import { cn } from "@/lib/utils";

const FLAGS = [
  "emergency",
  "complaint",
  "price_inquiry",
  "wants_appt",
  "appt_confirmed",
  "has_todo",
] as const;
const ITEMS = ["primary_purpose", "counterpart", "urgency", ...FLAGS] as const;
const ITEM_NAME: Record<string, string> = {
  primary_purpose: "목적",
  counterpart: "상대",
  urgency: "급함",
};

// 직원이 먼저 봐야 할 항목 — 이름을 굵게, 해당하면(급함·예) 표시로 강조한다
const KEY_ITEMS: string[] = ["urgency", "emergency", "has_todo"];
const notable = (item: string, v: unknown) =>
  (item === "urgency" && v === "urgent") ||
  ((item === "emergency" || item === "has_todo") && (v === true || v === 1));

/** 라벨 값 → 우리말 */
function show(item: string, v: unknown, names: LabelNames | null): string {
  if (v === undefined || v === null) return "—";
  if (item === "primary_purpose") return names?.purpose[String(v)] ?? String(v);
  if (item === "counterpart") return names?.counterpart[String(v)] ?? String(v);
  if (item === "urgency") return names?.urgency[String(v)] ?? String(v);
  return v === true || v === 1 ? "예" : "아니오";
}

/** 통화 상세 — 패널(기본)과 넓게 보기(통화·② 판정 화면 설계 "통화 상세"). 섹션은 펼친 채 시작, 제목으로 접기 */
export function CallDetail({
  detail,
  names,
  wide,
  onWide,
  onClose,
  onCheck,
  onReclassify,
  busy,
}: {
  detail: Detail;
  names: LabelNames | null;
  wide: boolean;
  onWide: (wide: boolean) => void;
  onClose: () => void;
  onCheck: (item: TodoItem, done: boolean) => void;
  onReclassify: () => void;
  busy: boolean;
}) {
  const { call, classification: k, judge, disagree } = detail;
  const head = (
    <div className="flex flex-wrap items-start gap-2">
      <div className="flex min-w-0 flex-1 flex-col">
        <span className="text-lg font-bold">
          {call.name ?? "이름 없음"}
          <span className="ml-2 text-sm font-normal text-muted-foreground">
            {call.chart_no ?? "차트 없음"}
            {call.phone ? ` · ${call.phone}` : ""}
          </span>
        </span>
        <span className="text-sm text-muted-foreground">
          {call.at.slice(0, 16)} · {DIRECTION_LABEL[call.direction]}
          {call.duration_sec
            ? ` · ${Math.floor(call.duration_sec / 60)}분 ${call.duration_sec % 60}초`
            : ""}
        </span>
      </div>
      <Button variant="outline" size="sm" onClick={() => onWide(!wide)}>
        {wide ? "패널로 돌아가기" : "넓게 보기"}
      </Button>
      {!wide && (
        <Button variant="ghost" size="sm" onClick={onClose}>
          닫기
        </Button>
      )}
    </div>
  );
  const failed = k.status !== "ok" && (
    <div className="flex items-center gap-2 rounded-lg border border-status-danger-border bg-status-danger-bg px-3 py-2 text-sm text-status-danger-fg">
      AI 분류가 아직 없거나 실패했습니다.
      <Button size="sm" onClick={onReclassify} disabled={busy}>
        {busy ? "분류하는 중…" : "다시 분류"}
      </Button>
    </div>
  );
  const todo = (
    <Section title="할 일">
      {k.todo.length === 0 ? (
        <p className="text-sm text-muted-foreground">없음</p>
      ) : (
        <ul className="flex flex-col gap-1.5">
          {k.todo.map((t) => (
            <li key={t.item} className="flex items-start gap-2 text-sm">
              <Checkbox
                className="mt-0.5"
                checked={t.done}
                onCheckedChange={(v) => onCheck(t, v === true)}
                aria-label={`${t.text} 끝냄`}
              />
              <Badge variant={t.kind === "contact" ? "kind" : "muted"}>
                {t.kind === "contact" ? "연락" : "안에서"}
              </Badge>
              <span
                className={cn(t.done && "text-muted-foreground line-through")}
              >
                {t.kind === "contact" && t.who ? `${t.who} · ` : ""}
                {t.text}
              </span>
            </li>
          ))}
        </ul>
      )}
    </Section>
  );
  const labels = k.labels;
  const summary = (
    <Section title="요약">
      <p className="text-sm">{k.summary ?? "—"}</p>
      {labels && (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {labels.emergency ? <Badge variant="danger">응급</Badge> : null}
          {labels.urgency === "urgent" ? (
            <Badge variant="danger">급함</Badge>
          ) : null}
          {labels.complaint ? <Badge variant="warn">불만</Badge> : null}
          {labels.appt_confirmed ? (
            <Badge variant="muted">예약 잡힘</Badge>
          ) : null}
        </div>
      )}
    </Section>
  );
  const diff = disagree && (
    <Section title="AI 두 개가 다르게 본 곳">
      <p className="rounded-lg border border-status-warn-border bg-status-warn-bg px-3 py-2 text-sm text-status-warn-fg">
        {disagree.items.length
          ? `${disagree.items.map((i) => ITEM_NAME[i] ?? names?.flag[i] ?? i).join("·")}에서 분류기와 판정기가 달랐습니다. 아래 분류 표에 표시했습니다.`
          : "할 일 목록이 있는데 분류가 '할 일 없음'이라 확인이 필요합니다."}
      </p>
    </Section>
  );
  const table = labels && (
    <Section title="분류 9항목">
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
        {ITEMS.map((i) => {
          const differs =
            judge !== null &&
            disagree?.items.includes(i) &&
            judge[i] !== labels[i];
          return (
            <div key={i} className="contents">
              <dt
                className={cn(
                  KEY_ITEMS.includes(i)
                    ? "font-bold text-foreground"
                    : "text-muted-foreground",
                )}
              >
                {ITEM_NAME[i] ?? names?.flag[i] ?? i}
              </dt>
              <dd>
                {notable(i, labels[i]) ? (
                  <Badge variant={i === "has_todo" ? "warn" : "danger"}>
                    {show(i, labels[i], names)}
                  </Badge>
                ) : (
                  show(i, labels[i], names)
                )}
                {differs && (
                  <span className="ml-2 text-xs font-bold text-status-danger-fg">
                    판정기: {show(i, judge[i], names)}
                  </span>
                )}
              </dd>
            </div>
          );
        })}
      </dl>
    </Section>
  );
  const transcript = (
    <Section title="녹취록">
      <Transcript text={call.transcript} bubbles={wide} />
    </Section>
  );
  const patient = (
    <Section title="환자 정보">
      <Patient chartNo={call.chart_no} />
    </Section>
  );

  if (wide)
    return (
      <div className="flex flex-col gap-3">
        {head}
        {failed}
        <div className="grid gap-4 lg:grid-cols-2">
          <div className="flex max-h-[calc(100vh-14rem)] flex-col gap-1 overflow-y-auto rounded-lg border bg-card p-4">
            {todo}
            {summary}
            {diff}
            {table}
            {patient}
          </div>
          <div className="max-h-[calc(100vh-14rem)] overflow-y-auto rounded-lg border bg-card p-4">
            <Transcript text={call.transcript} bubbles />
          </div>
        </div>
      </div>
    );
  return (
    <aside
      className="flex max-h-[calc(100vh-10rem)] flex-col gap-1 overflow-y-auto rounded-lg border bg-card p-4 shadow-card"
      aria-label="통화 상세"
    >
      {head}
      {failed}
      {todo}
      {summary}
      {diff}
      {table}
      {transcript}
      {patient}
    </aside>
  );
}

function Section({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(true);
  return (
    <section className="border-t pt-2.5 pb-1.5 first:border-t-0">
      <button
        type="button"
        className="mb-1.5 flex w-full items-center gap-1 text-left font-bold"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
      >
        <span className="text-muted-foreground">{open ? "▾" : "▸"}</span>
        {title}
      </button>
      {open && children}
    </section>
  );
}

function Patient({ chartNo }: { chartNo: string | null }) {
  const { data, error } = useFetch(chartNo, (signal) =>
    fetchPatient(chartNo as string, signal),
  );
  if (!chartNo)
    return <p className="text-sm text-muted-foreground">차트 없음</p>;
  if (error) return <p className="text-sm text-muted-foreground">{error}</p>;
  if (!data)
    return <p className="text-sm text-muted-foreground">불러오는 중…</p>;
  const s = data.summary;
  const next = s.upcoming[0];
  return (
    <div className="flex flex-col gap-1.5 text-sm">
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1">
        <dt className="text-muted-foreground">나이</dt>
        <dd>
          {data.patient.age}세 {data.patient.sex === "M" ? "남" : "여"}
        </dd>
        <dt className="text-muted-foreground">다음 예약</dt>
        <dd>{next ? `${next.date} ${next.time} ${next.procedure}` : "없음"}</dd>
        <dt className="text-muted-foreground">마지막 내원</dt>
        <dd>{s.last_visit ?? "없음"}</dd>
        <dt className="text-muted-foreground">미수</dt>
        <dd>{s.balance > 0 ? won(s.balance) : "없음"}</dd>
        <dt className="text-muted-foreground">주의사항</dt>
        <dd>
          {s.alerts.length ? s.alerts.map((a) => a.label).join(", ") : "없음"}
        </dd>
      </dl>
      <Link
        className="text-link hover:underline"
        href={`/emr?chart=${encodeURIComponent(chartNo)}`}
      >
        EMR에서 {chartNo} 보기 →
      </Link>
    </div>
  );
}
