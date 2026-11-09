// 문서 종류 이름·순서와 문서함 주소 (설계 2026-11-09 검색 요약 "화면"). 검색 카드·문서함 칩·요약 출처 칩이 같이 쓴다
import type { DocType } from "@/lib/api";

export const DOC_TYPE: Record<DocType, string> = {
  notice: "공지",
  manual: "매뉴얼",
  faq: "FAQ",
  insurance_guide: "보험 안내",
  claim_standard: "청구 기준",
  education: "교육 자료",
  external: "외부 자료",
};

/** 문서함 칩 순서 — 공지가 먼저(처음 열 때 고르는 종류) */
export const DOC_TYPE_ORDER: DocType[] = [
  "notice",
  "manual",
  "faq",
  "insurance_guide",
  "claim_standard",
  "education",
  "external",
];

export const docHref = (id: number) => `/ask?tab=docs&doc=${id}`;

/** 문서 청크 id(doc:<문서 id>:<n>) → 문서 id, 문서가 아니면 null */
export function docIdOf(chunkId: string): number | null {
  const m = /^doc:(\d+):/.exec(chunkId);
  return m ? Number(m[1]) : null;
}
