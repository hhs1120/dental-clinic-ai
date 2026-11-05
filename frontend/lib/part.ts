// 담당 파트 — 머리 띠에서 고르고 판정·체크·새 통화의 "누가"로 쓴다(기능 정의 §4 앱 구성)
export const PARTS = ["원장", "데스크", "상담실장"];
export const PART_KEY = "ax.part";

/** 저장한 파트, 없거나 저장소가 막혔으면 데스크 */
export function readPart(): string {
  try {
    const saved = localStorage.getItem(PART_KEY);
    if (saved && PARTS.includes(saved)) return saved;
  } catch {}
  return "데스크";
}
