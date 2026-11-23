"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import {
  CrossPicker,
  OcrForm,
  remaining,
  withVisit,
} from "@/components/chart-ocr/ocr-form";
import {
  centerOn,
  FULL_VIEW,
  OcrImage,
  OcrSnippet,
  type View,
} from "@/components/chart-ocr/ocr-image";
import { ErrorBand } from "@/components/prep/prep-view";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { errorText, useFetch } from "@/lib/api";
import {
  approveImport,
  cellBox,
  type ChartImport,
  fetchAbbreviations,
  fetchImport,
  fetchImports,
  fetchSamples,
  type Fields,
  imageUrl,
  type ImportStatus,
  readSample,
  rejectImport,
  sampleUrl,
  STATUS_LABEL,
  uploadChart,
  visitBand,
  wordBox,
} from "@/lib/chart-ocr-api";
import { readPart } from "@/lib/part";
import { cn } from "@/lib/utils";

type Box = [number, number, number, number];
// 확인 패널이 가리키는 줄은 uid로 — 줄을 나누거나 지워도 같은 줄을 보고, 줄이 없어지면 닫힌다
type Inspect = { kind: "date" | "loc" | "word"; uid: string; word?: string };

const STATUS_VARIANT: Record<ImportStatus, "warn" | "ok" | "muted"> = {
  draft: "warn",
  approved: "ok",
  rejected: "muted",
};

// 한 번의 요청이라 진행을 알 수 없다 — 업스테이지 두 단계를 차례로 보여 준다
const STEPS = ["글자 인식 중 (OCR)", "항목 정리 중 (AI)"];

function toBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result).split(",", 2)[1] ?? "");
    r.onerror = () => reject(r.error);
    r.readAsDataURL(file);
  });
}

/** 종이 차트 이관 — 샘플·파일을 업스테이지로 읽고, 사람이 고쳐 승인하면 EMR 기록에 붙는다 */
export function ChartOcrView() {
  const samples = useFetch("samples", fetchSamples);
  const imports = useFetch("imports", fetchImports);
  const abbr = useFetch("abbr", fetchAbbreviations);
  const [item, setItem] = useState<ChartImport | null>(null);
  const [fields, setFields] = useState<Fields | null>(null);
  const [patient, setPatient] = useState<{
    chart_no: string;
    name: string;
  } | null>(null);
  const [focus, setFocus] = useState<Box | null>(null);
  // 노란 딱지로 연 칸 — 그 칸을 확대해 보이고, 위치는 확대 옆에서 고른다(값은 고를 때만 바뀐다)
  const [inspect, setInspect] = useState<Inspect | null>(null);
  const [showBoxes, setShowBoxes] = useState(true);
  const [view, setView] = useState<View>(FULL_VIEW);
  const [step, setStep] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (step === null) return;
    const t = setTimeout(() => setStep((s) => (s === 0 ? 1 : s)), 2500);
    return () => clearTimeout(t);
  }, [step]);

  const open = (got: ChartImport) => {
    setItem(got);
    const f = got.final ?? got.fields;
    setFields({
      ...f,
      visits: f.visits.map((v, i) => ({
        ...v,
        src: v.src ?? i,
        uid: v.uid ?? crypto.randomUUID(),
      })),
    });
    setPatient(got.patient);
    setFocus(null);
    setInspect(null);
    setView(FULL_VIEW);
    samples.reload();
    imports.reload();
  };
  const read = (run: () => Promise<ChartImport>) => {
    setError(null);
    setStep(0);
    run()
      .then(open, (e) => setError(errorText(e)))
      .finally(() => setStep(null));
  };
  const decide = (run: () => Promise<ChartImport>) => {
    setError(null);
    setBusy(true);
    run()
      .then(open, (e) => setError(errorText(e)))
      .finally(() => setBusy(false));
  };

  // 확대한 채로 다른 칸을 누르면 배율은 두고 그 칸으로 옮겨 간다(1배면 테두리만)
  const focusOn = (box: Box | null) => {
    setFocus(box);
    if (box) setView((v) => (v.s > 1 ? centerOn(v, box) : v));
  };

  const left = item && fields ? remaining(fields, patient) : [];
  const src = item ? imageUrl(item.id) : "";

  return (
    <div className="flex flex-col gap-5">
      <section className="flex flex-col gap-2">
        <div className="flex items-center gap-2">
          <h2 className="text-sm font-medium tracking-tight">스캔 고르기</h2>
          <input
            ref={fileRef}
            type="file"
            accept="image/png,image/jpeg"
            className="hidden"
            onChange={(e) => {
              const f = e.target.files?.[0];
              e.target.value = "";
              if (f) read(async () => uploadChart(f.name, await toBase64(f)));
            }}
          />
          <Button
            size="sm"
            variant="outline"
            className="ml-auto"
            disabled={step !== null}
            onClick={() => fileRef.current?.click()}
          >
            파일 올리기
          </Button>
        </div>
        {samples.error && <ErrorBand text={samples.error} />}
        <div className="grid grid-cols-4 gap-3">
          {(samples.data ?? []).map((s) => (
            <button
              key={s.name}
              type="button"
              disabled={step !== null}
              onClick={() => read(() => readSample(s.name))}
              className={cn(
                "flex flex-col gap-1.5 rounded-xl border bg-card p-2 text-left shadow-card hover:border-primary",
                item?.filename === s.name ? "border-primary" : "border-border",
              )}
            >
              {/* eslint-disable-next-line @next/next/no-img-element -- API가 주는 샘플 */}
              <img
                src={sampleUrl(s.name)}
                alt={s.name}
                className="aspect-[24/17] w-full rounded-md object-cover"
              />
              <span className="flex items-center gap-1.5 text-xs">
                {s.name.replace(/^chart-|\.png$/g, "")}
                {s.imported_id !== null && (
                  <Badge variant="muted" className="ml-auto">
                    읽음
                  </Badge>
                )}
              </span>
            </button>
          ))}
        </div>
      </section>

      {step !== null && (
        <div
          role="status"
          className="flex items-center gap-4 rounded-md border border-status-info-border bg-status-info-bg px-3 py-2.5 text-sm text-status-info-fg"
        >
          {STEPS.map((s, i) => (
            <span
              key={s}
              className={cn(i <= step ? "font-bold" : "opacity-50")}
            >
              {i < step ? "✓ " : i === step ? "… " : ""}
              {s}
            </span>
          ))}
        </div>
      )}
      {error && <ErrorBand text={error} />}

      {item && fields && (
        <section className="grid grid-cols-[minmax(0,1.35fr)_minmax(0,1fr)] gap-5">
          <div className="flex flex-col gap-3 self-start lg:sticky lg:top-[calc(var(--page-band-h,0px)+1rem)]">
            <div className="flex items-center gap-2">
              <h2 className="text-sm font-medium tracking-tight">
                {item.filename}
              </h2>
              <Badge variant={STATUS_VARIANT[item.status]}>
                {STATUS_LABEL[item.status]}
              </Badge>
              <span className="text-xs text-muted-foreground">
                글자 {item.words.length}개 · 자신 없는 글자{" "}
                {
                  item.words.filter((w) => w.confidence < item.low_confidence)
                    .length
                }
                개
              </span>
              <Button
                size="xs"
                variant="ghost"
                className="ml-auto"
                onClick={() => setShowBoxes((v) => !v)}
              >
                {showBoxes ? "글자 상자 끄기" : "글자 상자 켜기"}
              </Button>
            </div>
            <OcrImage
              src={src}
              size={item.size}
              words={item.words}
              lowConfidence={item.low_confidence}
              showBoxes={showBoxes}
              focus={focus}
              view={view}
              onView={setView}
            />
            {inspect ? (
              <InspectPanel
                item={item}
                fields={fields}
                src={src}
                abbr={abbr.data ?? {}}
                inspect={inspect}
                onChange={setFields}
                onClose={() => setInspect(null)}
              />
            ) : (
              focus && (
                <div className="flex flex-col gap-1">
                  <span className="text-xs font-bold text-muted-foreground">
                    지금 칸의 손글씨
                  </span>
                  <OcrSnippet src={src} size={item.size} box={focus} />
                </div>
              )
            )}
          </div>

          <div className="flex flex-col gap-4">
            {item.status === "draft" && (
              // 표시 세 종류(기능 정의 §4 앱 구성) — 초안은 OCR·AI가 만든 것, 승인한 뒤는 사람이 정한 것
              <div className="flex items-center gap-2 text-sm">
                <Badge variant="kind">AI</Badge>
                <span className="text-muted-foreground">
                  OCR·AI가 읽은 초안입니다. 노란 칸을 원본과 비교해 고친 뒤
                  승인하세요.
                </span>
              </div>
            )}
            <OcrForm
              item={item}
              abbr={abbr.data ?? {}}
              fields={fields}
              setFields={setFields}
              patient={patient}
              setPatient={setPatient}
              onFocus={(box) => {
                focusOn(box);
                setInspect(null);
              }}
              onInspect={(kind, index, word) => {
                const v = fields.visits[index];
                setInspect({ kind, uid: v.uid ?? "", word });
                focusOn(inspectBox(item, kind, v.src ?? index, word));
              }}
            />
            <div className="sticky bottom-0 flex flex-wrap items-center gap-2 border-t border-border bg-background py-3">
              {item.status === "draft" ? (
                <>
                  {left.length ? (
                    left.map((t) => (
                      <Badge key={t} variant="warn">
                        {t}
                      </Badge>
                    ))
                  ) : (
                    <Badge variant="ok">확인 끝</Badge>
                  )}
                  <Button
                    variant="outline"
                    className="ml-auto"
                    disabled={busy}
                    onClick={() =>
                      decide(() => rejectImport(item.id, readPart()))
                    }
                  >
                    반려
                  </Button>
                  <Button
                    disabled={busy || left.length > 0}
                    onClick={() =>
                      decide(() =>
                        approveImport(
                          item.id,
                          fields,
                          patient?.chart_no ?? null,
                          readPart(),
                        ),
                      )
                    }
                  >
                    EMR로 이관 승인
                  </Button>
                </>
              ) : (
                <>
                  <span className="text-sm text-muted-foreground">
                    {item.decided_by} · {item.decided_at?.slice(0, 16)}{" "}
                    {STATUS_LABEL[item.status]}
                  </span>
                  {item.status === "approved" && item.patient && (
                    <Button asChild className="ml-auto">
                      <Link
                        href={`/emr?chart=${item.patient.chart_no}&view=chart`}
                      >
                        EMR에서 보기
                      </Link>
                    </Button>
                  )}
                </>
              )}
            </div>
          </div>
        </section>
      )}

      {(imports.data ?? []).length > 0 && (
        <section className="flex flex-col gap-2">
          <h2 className="text-sm font-medium tracking-tight">이관 기록</h2>
          <ul className="flex flex-col divide-y divide-border rounded-xl border border-border bg-card">
            {imports.data!.map((r) => (
              <li key={r.id}>
                <button
                  type="button"
                  className="flex w-full items-center gap-3 px-3 py-2 text-left text-sm hover:bg-muted"
                  onClick={() =>
                    fetchImport(r.id).then(open, (e) => setError(errorText(e)))
                  }
                >
                  <Badge variant={STATUS_VARIANT[r.status]}>
                    {STATUS_LABEL[r.status]}
                  </Badge>
                  <span className="font-bold">{r.patient_name}</span>
                  <span className="text-muted-foreground">{r.filename}</span>
                  <span className="ml-auto text-xs text-muted-foreground">
                    {r.created_at.slice(0, 16)}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

/** 확인할 칸의 스캔 자리 — 날짜·위치는 그 칸, 약어 제안은 그 낱말 */
function inspectBox(
  item: ChartImport,
  kind: Inspect["kind"],
  index: number, // 서버가 읽은 진료 순번(src)
  word?: string,
): Box | null {
  const visits = item.fields.visits;
  if (kind === "word") return wordBox(visits, index, item.words, word ?? "");
  return cellBox(
    visits,
    index,
    item.words,
    kind === "date" ? "Date" : "Location",
  );
}

const INSPECT_LABEL = {
  date: "날짜 확인",
  loc: "위치 고르기",
  word: "약어 확인",
};
const ZOOM_LABEL = {
  date: "Date 칸 확대",
  loc: "Location 칸 확대",
  word: "손글씨 확대",
};

/** 딱지·제안으로 연 칸 — 칸 확대 + 그 진료 줄. 날짜는 맞으면 확인, 위치는 십자에서 고르고, 약어는 보고 고친다 */
function InspectPanel({
  item,
  fields,
  src,
  abbr,
  inspect,
  onChange,
  onClose,
}: {
  item: ChartImport;
  fields: Fields;
  src: string;
  abbr: Record<string, string>;
  inspect: Inspect;
  onChange: (f: Fields) => void;
  onClose: () => void;
}) {
  const { kind, uid, word } = inspect;
  const index = fields.visits.findIndex((x) => x.uid === uid);
  if (index < 0) return null; // 그 줄을 지웠다
  const v = fields.visits[index];
  const locked = item.status !== "draft";
  const at = v.src ?? index; // 스캔 위치는 서버가 읽은 순번으로
  const cell = inspectBox(item, kind, at, word);
  const sug = v.suggestions.find((s) => s.word === word);
  // 바로 아래로 이어지는 같은 치아 줄 중 위치를 아직 안 고른 것 — 연달아 한 치료(신경 치료 등)
  const below: number[] = [];
  for (let j = index + 1; j < fields.visits.length; j++) {
    const w = fields.visits[j];
    if (w.teeth !== v.teeth) break;
    if (!w.quadrant) below.push(j);
  }
  const band = visitBand(item.fields.visits, at, item.words);
  return (
    <div className="flex flex-col gap-3 rounded-xl border border-status-warn-border bg-card p-3 shadow-card">
      <div className="flex items-center gap-2">
        <Badge variant="warn">{INSPECT_LABEL[kind]}</Badge>
        <span className="text-sm font-bold">
          진료 {index + 1} · {v.date ?? v.date_raw}
        </span>
        <Button size="xs" variant="ghost" className="ml-auto" onClick={onClose}>
          닫기
        </Button>
      </div>
      <div className="flex items-start gap-4">
        <div className="flex w-56 shrink-0 flex-col gap-1">
          <span className="text-xs text-muted-foreground">
            {ZOOM_LABEL[kind]}
          </span>
          {cell ? (
            <OcrSnippet src={src} size={item.size} box={cell} />
          ) : (
            <span className="text-sm text-muted-foreground">
              스캔에서 이 칸을 찾지 못했습니다
            </span>
          )}
        </div>
        {kind === "word" ? (
          <div className="flex flex-col gap-2">
            {sug ? (
              <>
                <p className="text-sm">
                  OCR이 <b>{sug.word}</b>로 읽었습니다. 확대한 손글씨가{" "}
                  <b>{sug.abbr}</b>
                  {abbr[sug.abbr] && `(${abbr[sug.abbr]})`}이면 고치세요.
                </p>
                <div className="flex gap-2">
                  <Button
                    size="sm"
                    disabled={locked}
                    onClick={() => {
                      onChange(
                        withVisit(fields, index, {
                          treatment: v.treatment.replace(sug.word, sug.abbr),
                          suggestions: v.suggestions.filter((x) => x !== sug),
                        }),
                      );
                      onClose();
                    }}
                  >
                    {sug.abbr}로 고치기
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={locked}
                    onClick={() => {
                      onChange(
                        withVisit(fields, index, {
                          suggestions: v.suggestions.filter((x) => x !== sug),
                        }),
                      );
                      onClose();
                    }}
                  >
                    그대로 두기
                  </Button>
                </div>
              </>
            ) : (
              <p className="text-sm text-muted-foreground">
                처리한 제안입니다.
              </p>
            )}
          </div>
        ) : kind === "loc" ? (
          <div className="flex flex-col gap-2">
            <p className="text-sm">
              확대한 십자에서 숫자 <b>{v.teeth}</b>가 있는 칸과 같은 칸을
              누르세요.
            </p>
            <CrossPicker
              value={v.quadrant}
              disabled={locked}
              onPick={(q) =>
                onChange(withVisit(fields, index, { quadrant: q }))
              }
            />
            <p className="text-xs text-muted-foreground">
              {v.fdi ? `#${v.fdi}로 정했습니다. ` : ""}승인 전까지 다시 바꿀 수
              있습니다.
            </p>
            {v.quadrant && below.length > 0 && !locked && (
              <div className="flex flex-col gap-1.5 rounded-md bg-status-info-bg p-2 text-xs text-status-info-fg">
                <span>
                  바로 아래 같은 치아({v.teeth}) {below.length}줄도 아직 위치를
                  안 골랐습니다. 같은 치아를 이어서 치료한 기록이면 한 번에
                  정하세요.
                </span>
                <Button
                  size="xs"
                  variant="outline"
                  className="self-start"
                  onClick={() =>
                    onChange(
                      below.reduce(
                        (acc, j) => withVisit(acc, j, { quadrant: v.quadrant }),
                        fields,
                      ),
                    )
                  }
                >
                  {below.length}줄에도 {v.quadrant}번 칸 적용
                </Button>
              </div>
            )}
          </div>
        ) : (
          <div className="flex flex-col gap-2">
            <p className="text-sm">
              정리된 원문 <b>{v.date_raw || "(없음)"}</b> → 날짜{" "}
              <b>{v.date ?? "없음"}</b>.{" "}
              {v.date_note
                ? `${v.date_note}.`
                : v.date_check
                  ? "앞 진료보다 날짜가 거꾸로 가거나 읽지 못해 확인합니다."
                  : ""}
            </p>
            <p className="text-xs text-muted-foreground">
              다르면 오른쪽 날짜 칸을 고치세요.
            </p>
            {v.date_suggest && v.date_check && !locked && (
              <div className="flex flex-col gap-1.5 rounded-md bg-status-info-bg p-2 text-xs text-status-info-fg">
                <span>
                  앞뒤 진료 사이 수납 장부에 <b>{v.date_suggest}</b>이 있고,
                  아직 어느 진료와도 짝이 없습니다. 이 날짜일까요?
                </span>
                <Button
                  size="xs"
                  variant="outline"
                  className="self-start"
                  onClick={() =>
                    onChange(
                      withVisit(fields, index, {
                        date: v.date_suggest,
                        date_check: false,
                        date_note: "수납 장부 날짜로 고침",
                        date_suggest: null,
                      }),
                    )
                  }
                >
                  {v.date_suggest}로 고치기
                </Button>
              </div>
            )}
            <Button
              size="sm"
              disabled={locked || !v.date || !v.date_check}
              onClick={() =>
                onChange(withVisit(fields, index, { date_check: false }))
              }
            >
              {v.date_check ? "이 날짜가 맞아요" : "확인함"}
            </Button>
          </div>
        )}
      </div>
      {band && (
        <div className="flex flex-col gap-1">
          <span className="text-xs text-muted-foreground">이 진료 줄</span>
          <OcrSnippet src={src} size={item.size} box={band} />
        </div>
      )}
    </div>
  );
}
