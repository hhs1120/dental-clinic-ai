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
  date_suggest: string | null; // 앞뒤 진료 사이 짝 없는 수납 날짜(바꾸지 않고 제안만)
  uid?: string; // 화면에서 줄을 가리키는 안정 id — 나누기·삭제 뒤에도 커서·확인 패널이 같은 줄을 본다
  date_ocr?: string | null; // 정보 추출이 빠뜨려 OCR Date 칸 글자로 채운 원문 — 스캔에서 그 칸을 찾을 때
  src?: number; // 서버가 읽은 진료 순번 — 화면에서 줄을 나누거나 더해도 스캔 위치는 이걸로 찾는다
  date_note: string | null; // 교차검증 근거 — OCR 글자로 채움·수납 장부와 일치·수납 장부 연도로 고침·〃로 봄
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
  head_notes?: Record<string, string>; // 정보 추출이 비워 OCR 글자로 채운 머리 칸
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

type Box = [number, number, number, number];

/** YYYY-MM-DD이고 실제 날짜 */
export function isoDate(s: string | null): boolean {
  if (!s || !/^\d{4}-\d{2}-\d{2}$/.test(s)) return false;
  return new Date(`${s}T00:00:00Z`).toISOString().slice(0, 10) === s;
}

/** 진료 경과 Date 칸의 OCR 단어 — 머리 글자 Date·Location 가운데 사이 거리를 칸 폭으로(backend/chart_ocr.py _date_column) */
function dateColumn(words: OcrWord[]): OcrWord[] {
  const head = (t: string) =>
    words.find((w) => w.text === t && w.box[0] > 0.5 && w.box[1] < 0.1);
  const date = head("Date");
  const loc = head("Location");
  if (!date || !loc) return words.filter((w) => w.box[0] > 0.5);
  const cx = (w: OcrWord) => (w.box[0] + w.box[2]) / 2;
  const width = cx(loc) - cx(date);
  return words.filter(
    (w) => Math.abs(cx(w) - cx(date)) < width / 2 && w.box[1] > date.box[3],
  );
}

/** 진료마다 그 날짜 단어 — Date 칸 안에서 차례대로 찾아 앞 줄과 헷갈리지 않게. OCR로 채운 줄은 그 글자로 */
function anchors(visits: Visit[], words: OcrWord[]): (OcrWord | null)[] {
  const col = dateColumn(words);
  const out: (OcrWord | null)[] = [];
  let after = 0;
  for (const v of visits) {
    const text = v.date_ocr ?? v.date_raw;
    const hit = col.find((w) => w.text === text && w.box[1] >= after);
    out.push(hit ?? null);
    if (hit) after = hit.box[1];
  }
  return out;
}

/** 진료 한 건의 손글씨 띠 — 이 진료 날짜 단어부터 다음 진료 날짜 단어 위까지 */
export function visitBand(
  visits: Visit[],
  index: number,
  words: OcrWord[],
): Box | null {
  const all = anchors(visits, words);
  const start = all[index];
  if (!start) return null;
  const next = all.slice(index + 1).find((a) => a !== null);
  const top = start.box[1] - 0.012;
  const bottom = next ? next.box[1] + 0.004 : start.box[3] + 0.05; // 마지막 진료는 두세 줄까지
  return [start.box[0] - 0.005, top, 0.97, bottom];
}

/** 진료 첫 줄의 Date·Location 칸 — 머리 글자(Date·Location) 가운데를 칸 가운데로, 두 머리 사이 거리를 칸 폭으로.
 * 날짜는 줄 아래에 붙여 써서 날짜 단어 아래 끝을 줄 아래로 본다(위치 칸 위·아래 반쪽이 다 보이게 한 줄 높이) */
export function cellBox(
  visits: Visit[],
  index: number,
  words: OcrWord[],
  column: "Date" | "Location",
): Box | null {
  const start = anchors(visits, words)[index];
  const head = (t: string) =>
    words.find((w) => w.text === t && w.box[0] > 0.5 && w.box[1] < 0.1);
  const date = head("Date");
  const loc = head("Location");
  if (!start || !date || !loc) return null;
  const cx = (b: Box) => (b[0] + b[2]) / 2;
  const width = cx(loc.box) - cx(date.box);
  const mid = cx((column === "Date" ? date : loc).box);
  const bottom = start.box[3] + 0.008;
  return [mid - width * 0.6, bottom - 0.036, mid + width * 0.6, bottom];
}

/** 진료 줄 안에서 그 낱말의 상자(앞뒤 글자가 보이게 조금 넓혀) — 약어 제안을 고치기 전에 확대해 본다 */
export function wordBox(
  visits: Visit[],
  index: number,
  words: OcrWord[],
  word: string,
): Box | null {
  const band = visitBand(visits, index, words);
  if (!band) return null;
  const bare = (t: string) => t.replace(/[,.;]+$/, "");
  const hit = words.find((w) => {
    const cy = (w.box[1] + w.box[3]) / 2;
    return bare(w.text) === bare(word) && cy >= band[1] && cy <= band[3];
  });
  if (!hit) return null;
  return [
    hit.box[0] - 0.02,
    hit.box[1] - 0.008,
    hit.box[2] + 0.02,
    hit.box[3] + 0.008,
  ];
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
