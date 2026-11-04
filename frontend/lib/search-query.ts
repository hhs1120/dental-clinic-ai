// 기록 검색 상태 ↔ URL 쿼리 (앱 뼈대 설계안 '화면'). 새로고침·공유·뒤로 가기가 같은 경로를 타게 상태는 URL에 둔다
import type { SourceType } from "@/lib/api";

export const ALL_TYPES: SourceType[] = [
  "document",
  "call",
  "message_thread",
  "chart_note",
];

export const SOURCE_LABEL: Record<SourceType, string> = {
  document: "문서",
  call: "통화",
  message_thread: "문자",
  chart_note: "차트 기록",
};

export interface SearchState {
  q: string;
  types: SourceType[];
  from: string;
  to: string;
  chart: string;
}

const isAll = (types: SourceType[]) =>
  ALL_TYPES.every((t) => types.includes(t));

export function readSearch(p: URLSearchParams): SearchState {
  const types = p.get("types");
  return {
    q: p.get("q") ?? "",
    types:
      types === null
        ? ALL_TYPES
        : ALL_TYPES.filter((t) => types.split(",").includes(t)),
    from: p.get("from") ?? "",
    to: p.get("to") ?? "",
    chart: p.get("chart") ?? "",
  };
}

/** 화면 URL — 빈 값은 빼고, 출처가 전부면 생략한다 */
export function writeSearch(s: SearchState): URLSearchParams {
  const p = new URLSearchParams({ tab: "search" });
  if (s.q.trim()) p.set("q", s.q.trim());
  if (!isAll(s.types)) p.set("types", s.types.join(","));
  if (s.from) p.set("from", s.from);
  if (s.to) p.set("to", s.to);
  if (s.chart.trim()) p.set("chart", s.chart.trim());
  return p;
}

/** GET /search 인자 — 빈 값은 보내지 않는다 */
export function toApiParams(s: SearchState): URLSearchParams {
  const p = new URLSearchParams({ q: s.q.trim() });
  if (!isAll(s.types)) p.set("source_types", s.types.join(","));
  if (s.from) p.set("date_from", s.from);
  if (s.to) p.set("date_to", s.to);
  if (s.chart.trim()) p.set("chart_no", s.chart.trim());
  return p;
}

export function sourceSummary(s: SearchState): string {
  return isAll(s.types)
    ? "전체"
    : s.types.map((t) => SOURCE_LABEL[t]).join("·");
}

/** 매뉴얼 칸이 필터 때문에 비었을 때의 이유 — 앞의 것이 우선 */
export function manualEmptyReason(s: SearchState): string | null {
  if (!s.types.includes("document")) return "문서가 출처에서 빠졌습니다";
  if (s.chart.trim()) return "환자번호 필터에서는 문서를 찾지 않습니다";
  if (s.from || s.to) return "기간 필터에서는 시행일 있는 공지만 찾습니다";
  return null;
}
