"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import { OcrForm, remaining } from "@/components/chart-ocr/ocr-form";
import { OcrImage, OcrSnippet } from "@/components/chart-ocr/ocr-image";
import { ErrorBand } from "@/components/prep/prep-view";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { errorText, useFetch } from "@/lib/api";
import {
  approveImport,
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
} from "@/lib/chart-ocr-api";
import { readPart } from "@/lib/part";
import { cn } from "@/lib/utils";

type Box = [number, number, number, number];

const STATUS_VARIANT: Record<ImportStatus, "warn" | "ok" | "muted"> = {
  draft: "warn",
  approved: "ok",
  rejected: "muted",
};

// 한 번의 요청이라 진행을 알 수 없다 — 업스테이지 두 단계를 차례로 보여 준다
const STEPS = ["글자 인식 중 (OCR)", "항목 정리 중 (정보 추출)"];

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
  const [showBoxes, setShowBoxes] = useState(true);
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
    setFields(got.final ?? got.fields);
    setPatient(got.patient);
    setFocus(null);
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

  const left = item && fields ? remaining(fields, patient) : [];
  const src = item ? imageUrl(item.id) : "";

  return (
    <div className="flex flex-col gap-5">
      <section className="flex flex-col gap-2">
        <div className="flex items-center gap-2">
          <h2 className="text-sm font-bold">스캔 고르기</h2>
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
          <div className="flex flex-col gap-3 self-start lg:sticky lg:top-4">
            <div className="flex items-center gap-2">
              <h2 className="text-sm font-bold">{item.filename}</h2>
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
              words={item.words}
              lowConfidence={item.low_confidence}
              showBoxes={showBoxes}
              focus={focus}
            />
            {focus && (
              <div className="flex flex-col gap-1">
                <span className="text-xs font-bold text-muted-foreground">
                  지금 칸의 손글씨
                </span>
                <OcrSnippet src={src} size={item.size} box={focus} />
              </div>
            )}
          </div>

          <div className="flex flex-col gap-4">
            <OcrForm
              item={item}
              abbr={abbr.data ?? {}}
              fields={fields}
              setFields={setFields}
              patient={patient}
              setPatient={setPatient}
              onFocus={setFocus}
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
          <h2 className="text-sm font-bold">이관 기록</h2>
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
