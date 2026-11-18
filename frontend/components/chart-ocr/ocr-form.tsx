"use client";

import { useState } from "react";

import { ErrorBand } from "@/components/prep/prep-view";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { errorText } from "@/lib/api";
import {
  type ChartImport,
  fdi,
  type Fields,
  matchWords,
  unionBox,
  type Visit,
  visitBand,
} from "@/lib/chart-ocr-api";
import { type PatientHit, searchPatients } from "@/lib/emr-api";
import { cn } from "@/lib/utils";

type Box = [number, number, number, number];

const HEAD: { key: keyof Fields; label: string; long?: boolean }[] = [
  { key: "patient_name", label: "성명" },
  { key: "birth_date", label: "생년월일" },
  { key: "old_chart_no", label: "옛 차트번호" },
  { key: "first_visit", label: "초진일" },
  { key: "chief_complaint", label: "C.C (주소)", long: true },
  { key: "present_illness", label: "P.I (병력)", long: true },
  { key: "treatment_plan", label: "치료 계획", long: true },
  { key: "memo", label: "메모·경고", long: true },
];

// 십자 칸 배치 — 차트의 십자는 환자를 마주 본 모양이라 왼쪽 위가 위턱 오른쪽(1)
const CROSS: { q: number; label: string }[] = [
  { q: 1, label: "위턱 오른쪽" },
  { q: 2, label: "위턱 왼쪽" },
  { q: 4, label: "아래턱 오른쪽" },
  { q: 3, label: "아래턱 왼쪽" },
];

const norm = (w: string) => w.toUpperCase().replace(/[.)]+$/, "");

/** 글에 든 약어와 뜻 — 화면에서 바로 읽히게(툴팁은 영상에 안 보인다) */
function glossary(text: string, abbr: Record<string, string>) {
  const tokens = new Set((text.match(/[A-Za-z0-9&/]+[.)]?/g) ?? []).map(norm));
  const lower = text.toLowerCase();
  return Object.entries(abbr).filter(([k]) =>
    k.includes(" ") ? lower.includes(k.toLowerCase()) : tokens.has(norm(k)),
  );
}

/** 사분면 고르기 — 차트의 점선 십자와 같은 모양(왼쪽 위 = 1). 확대한 위치 칸 옆에 둔다 */
export function CrossPicker({
  value,
  onPick,
  disabled,
}: {
  value: number | null;
  onPick: (q: number) => void;
  disabled: boolean;
}) {
  return (
    <div className="grid w-44 shrink-0 grid-cols-2 overflow-hidden rounded-md border border-border">
      {CROSS.map(({ q, label }, i) => (
        <button
          key={q}
          type="button"
          aria-pressed={value === q}
          disabled={disabled}
          onClick={() => onPick(q)}
          className={cn(
            "flex h-12 flex-col items-center justify-center text-xs leading-tight",
            i % 2 === 0 && "border-r border-dashed border-border",
            i < 2 && "border-b border-dashed border-border",
            value === q
              ? "bg-primary font-bold text-primary-foreground"
              : "bg-card hover:bg-muted",
          )}
        >
          <b className="text-sm">{q}</b>
          {label}
        </button>
      ))}
    </div>
  );
}

/** 진료 한 건 고치기 — 치아·사분면이 바뀌면 FDI를 다시 정한다 */
export function withVisit(
  fields: Fields,
  i: number,
  patch: Partial<Visit>,
): Fields {
  const visits = fields.visits.map((v, j) => {
    if (j !== i) return v;
    const next = { ...v, ...patch };
    return { ...next, fdi: fdi(next.quadrant, next.teeth) };
  });
  return { ...fields, visits };
}

/** 환자 맞춤 — 이름·생년월일로 한 명이 맞으면 미리 채워지고, 아니면 검색해 고른다 */
function PatientPick({
  value,
  onPick,
  disabled,
}: {
  value: { chart_no: string; name: string } | null;
  onPick: (p: { chart_no: string; name: string }) => void;
  disabled: boolean;
}) {
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<PatientHit[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const search = () => {
    if (!q.trim()) return;
    setError(null);
    searchPatients(q.trim()).then(setHits, (e) => setError(errorText(e)));
  };
  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-bold">EMR 환자</span>
        {value ? (
          <Badge variant="ok">
            {value.name} · {value.chart_no}
          </Badge>
        ) : (
          <Badge variant="warn">고르지 않음</Badge>
        )}
      </div>
      {!disabled && (
        <div className="flex gap-2">
          <Input
            value={q}
            placeholder="이름이나 차트 번호로 찾기"
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && search()}
          />
          <Button variant="outline" onClick={search}>
            찾기
          </Button>
        </div>
      )}
      {error && <ErrorBand text={error} />}
      {hits && !disabled && (
        <div className="flex flex-wrap gap-1.5">
          {hits.length === 0 && (
            <span className="text-sm text-muted-foreground">
              맞는 환자 없음
            </span>
          )}
          {hits.map((h) => (
            <Button
              key={h.chart_no}
              size="xs"
              variant="chip"
              onClick={() => {
                onPick(h);
                setHits(null);
              }}
            >
              {h.name} · {h.chart_no} · {h.age}세 {h.sex}
            </Button>
          ))}
        </div>
      )}
    </div>
  );
}

/** 읽어 낸 항목 고치기 — 칸을 누르면 onFocus로 이미지 자리를 알리고, 승인은 부모가 보낸다 */
export function OcrForm({
  item,
  abbr,
  fields,
  setFields,
  patient,
  setPatient,
  onFocus,
  onInspect,
}: {
  item: ChartImport;
  abbr: Record<string, string>;
  fields: Fields;
  setFields: (f: Fields) => void;
  patient: { chart_no: string; name: string } | null;
  setPatient: (p: { chart_no: string; name: string }) => void;
  onFocus: (box: Box | null) => void;
  onInspect: (
    kind: "date" | "loc" | "word",
    index: number,
    word?: string,
  ) => void;
}) {
  const locked = item.status !== "draft";
  const setVisit = (i: number, patch: Partial<Visit>) =>
    setFields(withVisit(fields, i, patch));
  const headFocus = (text: string) =>
    onFocus(unionBox(matchWords(text, item.words)));

  return (
    <div className="flex flex-col gap-5">
      <PatientPick value={patient} onPick={setPatient} disabled={locked} />

      <section className="grid grid-cols-2 gap-3">
        {HEAD.map(({ key, label, long }) => {
          const value = String(fields[key] ?? "");
          return (
            <label
              key={key}
              className={cn("flex flex-col gap-1", long && "col-span-2")}
            >
              <span className="text-xs font-bold text-muted-foreground">
                {label}
              </span>
              {long ? (
                <Textarea
                  rows={2}
                  value={value}
                  disabled={locked}
                  onFocus={() => headFocus(value)}
                  onChange={(e) =>
                    setFields({ ...fields, [key]: e.target.value })
                  }
                />
              ) : (
                <Input
                  value={value}
                  disabled={locked}
                  onFocus={() => headFocus(value)}
                  onChange={(e) =>
                    setFields({ ...fields, [key]: e.target.value })
                  }
                />
              )}
            </label>
          );
        })}
      </section>

      <section className="flex flex-col gap-2">
        <h3 className="text-sm font-bold">
          진료 경과 {fields.visits.length}건
        </h3>
        {fields.visits.map((v, i) => {
          const words = glossary(v.treatment, abbr);
          const focus = () =>
            onFocus(visitBand(item.fields.visits, i, item.words));
          return (
            <div
              key={i}
              onFocus={focus}
              className={cn(
                "flex flex-col gap-2 rounded-md border bg-card p-2.5",
                v.date_check || (!v.quadrant && v.teeth && v.teeth !== "전악")
                  ? "border-status-warn-border"
                  : "border-border",
              )}
            >
              <div className="flex flex-wrap items-center gap-2">
                <Input
                  className="w-32"
                  value={v.date ?? ""}
                  placeholder="YYYY-MM-DD"
                  disabled={locked}
                  onChange={(e) =>
                    setVisit(i, { date: e.target.value, date_check: false })
                  }
                />
                {v.date_check ? (
                  <button
                    type="button"
                    className="cursor-pointer rounded-4xl hover:opacity-80"
                    onClick={() => onInspect("date", i)}
                  >
                    <Badge variant="warn">
                      날짜 확인 · 원문 {v.date_raw} ›
                    </Badge>
                  </button>
                ) : (
                  v.date_note && (
                    <button
                      type="button"
                      className="cursor-pointer rounded-4xl hover:opacity-80"
                      onClick={() => onInspect("date", i)}
                    >
                      <Badge
                        variant={v.date_note.includes("수납") ? "ok" : "info"}
                      >
                        {v.date_note}
                      </Badge>
                    </button>
                  )
                )}
                <Input
                  className="w-20"
                  value={v.teeth}
                  disabled={locked}
                  aria-label="치아"
                  onChange={(e) => setVisit(i, { teeth: e.target.value })}
                />
                {v.teeth !== "전악" && (
                  <button
                    type="button"
                    className="cursor-pointer rounded-4xl hover:opacity-80"
                    onClick={() => onInspect("loc", i)}
                  >
                    <Badge variant={v.fdi ? "info" : "warn"}>
                      {v.fdi ? `#${v.fdi}` : "위치 고르기"} ›
                    </Badge>
                  </button>
                )}
                <span className="ml-auto text-xs text-muted-foreground">
                  서명 {v.signature}
                </span>
              </div>
              <Input
                value={v.treatment}
                disabled={locked}
                onChange={(e) => setVisit(i, { treatment: e.target.value })}
              />
              {!locked &&
                v.suggestions.map((s) => (
                  <div
                    key={s.word}
                    className="flex flex-wrap items-center gap-2 rounded-md bg-status-info-bg px-2 py-1 text-xs text-status-info-fg"
                  >
                    <span>
                      잘못 읽은 것 같아요: <b>{s.word}</b> → <b>{s.abbr}</b>
                      {abbr[s.abbr] && `(${abbr[s.abbr]})`}로 고칠까요?
                    </span>
                    <Button
                      size="xs"
                      variant="outline"
                      className="ml-auto"
                      onClick={() => onInspect("word", i, s.word)}
                    >
                      손글씨 보고 고치기 ›
                    </Button>
                  </div>
                ))}
              {words.length > 0 && (
                <p className="text-xs text-muted-foreground">
                  {words.map(([k, m]) => (
                    <span key={k} className="mr-3">
                      <b className="text-foreground">{k}</b> {m}
                    </span>
                  ))}
                </p>
              )}
            </div>
          );
        })}
      </section>

      <section className="flex flex-col gap-2">
        <h3 className="text-sm font-bold">수납 {fields.payments.length}건</h3>
        <div className="grid grid-cols-[8rem_1fr_7rem_7rem] gap-1.5 text-xs font-bold text-muted-foreground">
          <span>날짜</span>
          <span>내용</span>
          <span>금액</span>
          <span>잔액</span>
        </div>
        {fields.payments.map((p, i) => {
          const set = (patch: Partial<typeof p>) =>
            setFields({
              ...fields,
              payments: fields.payments.map((x, j) =>
                j === i ? { ...x, ...patch } : x,
              ),
            });
          const num = (s: string) =>
            s.trim() === "" ? null : Number(s.replace(/[^0-9]/g, ""));
          return (
            <div
              key={i}
              className="grid grid-cols-[8rem_1fr_7rem_7rem] gap-1.5"
              onFocus={() => headFocus(`${p.date_raw} ${p.item}`)}
            >
              <Input
                value={p.date ?? ""}
                disabled={locked}
                className={cn(p.date_check && "border-status-warn-border")}
                onChange={(e) =>
                  set({ date: e.target.value, date_check: false })
                }
              />
              <Input
                value={p.item}
                disabled={locked}
                onChange={(e) => set({ item: e.target.value })}
              />
              <Input
                value={p.amount?.toLocaleString("ko-KR") ?? ""}
                disabled={locked}
                onChange={(e) => set({ amount: num(e.target.value) })}
              />
              <Input
                value={p.balance?.toLocaleString("ko-KR") ?? ""}
                disabled={locked}
                onChange={(e) => set({ balance: num(e.target.value) })}
              />
            </div>
          );
        })}
      </section>
    </div>
  );
}

/** 승인 전에 남은 확인 — 날짜 확인·위치 고르기·환자 */
export function remaining(
  fields: Fields,
  patient: { chart_no: string } | null,
): string[] {
  const out: string[] = [];
  if (!patient) out.push("EMR 환자 고르기");
  const dates =
    fields.visits.filter((v) => v.date_check || !v.date).length +
    fields.payments.filter((p) => p.date_check || !p.date).length;
  if (dates) out.push(`날짜 확인 ${dates}곳`);
  const loc = fields.visits.filter(
    (v) => v.teeth && v.teeth !== "전악" && !v.quadrant,
  ).length;
  if (loc) out.push(`위치 고르기 ${loc}곳`);
  return out;
}
