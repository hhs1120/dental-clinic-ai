// 종이 차트 이관 API 타입과 호출 — 응답 모양의 원본은 backend/api.py /chart-ocr/*·backend/chart_ocr.py fields
// (설계 docs/superpowers/specs/2026-11-18-chart-ocr-design.md §설계 2~4)
import { getJson, postJson } from "@/lib/api";

export interface OcrWord {
  text: string;
  confidence: number;
  box: [number, number, number, number]; // 이미지 비율 x0, y0, x1, y1
}

export interface Suggestion {
  word: string;
  abbr: string;
}

export interface Visit {
  date_raw: string;
  date: string | null;
  date_check: boolean;
  location_raw: string;
  teeth: string; // 숫자(공백 구분) 또는 "전악"
  quadrant: number | null; // 사람이 십자에서 고른 사분면
  fdi: string | null;
  treatment: string;
  signature: string;
  suggestions: Suggestion[];
}

export interface Payment {
  date_raw: string;
  date: string | null;
  date_check: boolean;
  item: string;
  amount: number | null;
  balance: number | null;
}

export interface Fields {
  patient_name: string;
  birth_date: string;
  old_chart_no: string;
  first_visit: string;
  chief_complaint: string;
  present_illness: string;
  treatment_plan: string;
  memo: string;
  visits: Visit[];
  payments: Payment[];
}

export type ImportStatus = "draft" | "approved" | "rejected";

export interface ChartImport {
  id: number;
  filename: string;
  status: ImportStatus;
  fields: Fields;
  final: Fields | null;
  patient: { id: number; chart_no: string; name: string } | null;
  words: OcrWord[];
  size: [number, number];
  low_confidence: number;
  decided_by: string | null;
  decided_at: string | null;
}

export interface ImportRow {
  id: number;
  filename: string;
  status: ImportStatus;
  patient_name: string;
  created_at: string;
}

export const STATUS_LABEL: Record<ImportStatus, string> = {
  draft: "확인 중",
  approved: "이관함",
  rejected: "반려",
};

export const fetchSamples = (signal?: AbortSignal) =>
  getJson<{ name: string; imported_id: number | null }[]>(
    "/chart-ocr/samples",
    undefined,
    signal,
  );
export const fetchImports = (signal?: AbortSignal) =>
  getJson<ImportRow[]>("/chart-ocr", undefined, signal);
export const fetchImport = (id: number, signal?: AbortSignal) =>
  getJson<ChartImport>(`/chart-ocr/${id}`, undefined, signal);
export const fetchAbbreviations = (signal?: AbortSignal) =>
  getJson<Record<string, string>>(
    "/chart-ocr/abbreviations",
    undefined,
    signal,
  );
export const readSample = (name: string) =>
  postJson<ChartImport>(`/chart-ocr/samples/${encodeURIComponent(name)}`);
export const uploadChart = (filename: string, image_base64: string) =>
  postJson<ChartImport>("/chart-ocr", { filename, image_base64 });
export const approveImport = (
  id: number,
  final: Fields,
  chart_no: string | null,
  part: string,
) =>
  postJson<ChartImport>(`/chart-ocr/${id}/approve`, { final, chart_no, part });
export const rejectImport = (id: number, part: string) =>
  postJson<ChartImport>(`/chart-ocr/${id}/reject`, { part });

export const imageUrl = (id: number) => `/api/chart-ocr/${id}/image`;
export const sampleUrl = (name: string) =>
  `/api/chart-ocr/samples/${encodeURIComponent(name)}`;

/** 사분면 + 치아 숫자 → FDI 번호(backend/chart_ocr.py fdi와 같다) */
export function fdi(quadrant: number | null, teeth: string): string | null {
  const nums = teeth.match(/[1-8]/g);
  if (!quadrant || !nums || teeth.includes("전악")) return null;
  return nums.map((n) => `${quadrant}${n}`).join(", ");
}

/** 칸 글과 같은 OCR 단어들 — 칸을 누르면 이미지에서 그 자리를 비춘다 */
export function matchWords(text: string, words: OcrWord[]): OcrWord[] {
  const tokens = new Set(
    text
      .split(/[\s,]+/)
      .map((t) => t.trim())
      .filter((t) => t.length >= 2),
  );
  return words.filter((w) => tokens.has(w.text.replace(/,$/, "")));
}

/** 진료 한 건의 손글씨 띠 — 이 진료 날짜 단어부터 다음 진료 날짜 단어 위까지(진료 경과는 오른쪽 쪽) */
export function visitBand(
  visits: Visit[],
  index: number,
  words: OcrWord[],
): [number, number, number, number] | null {
  const right = words.filter((w) => w.box[0] > 0.5);
  const anchors: (OcrWord | null)[] = [];
  let after = 0;
  for (const v of visits) {
    const hit = right.find((w) => w.text === v.date_raw && w.box[1] >= after);
    anchors.push(hit ?? null);
    if (hit) after = hit.box[1];
  }
  const start = anchors[index];
  if (!start) return null;
  const next = anchors.slice(index + 1).find((a) => a !== null);
  const top = start.box[1] - 0.012;
  const bottom = next ? next.box[1] + 0.004 : start.box[3] + 0.03;
  return [start.box[0] - 0.005, top, 0.97, bottom];
}

/** 여러 상자를 감싸는 상자 */
export function unionBox(
  words: OcrWord[],
): [number, number, number, number] | null {
  if (!words.length) return null;
  return [
    Math.min(...words.map((w) => w.box[0])) - 0.004,
    Math.min(...words.map((w) => w.box[1])) - 0.004,
    Math.max(...words.map((w) => w.box[2])) + 0.004,
    Math.max(...words.map((w) => w.box[3])) + 0.004,
  ];
}
