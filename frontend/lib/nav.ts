import {
  BotMessageSquare,
  FolderSearch,
  LayoutGrid,
  Monitor,
  Scale,
  type LucideIcon,
} from "lucide-react";

// 공간 5개(아이콘 줄)와 업무 메뉴·AI 평가 탭(목록 칸) (AI 비서 설계 §3.1, 기능 정의 §4 앱 구성)
export type SpaceKey = "assistant" | "work" | "library" | "eval" | "emr";

export const SPACES: {
  key: SpaceKey;
  label: string;
  icon: LucideIcon;
  href: string;
}[] = [
  { key: "assistant", label: "AI 비서", icon: BotMessageSquare, href: "/" },
  { key: "work", label: "업무", icon: LayoutGrid, href: "/today" },
  { key: "library", label: "자료실", icon: FolderSearch, href: "/ask" },
  { key: "eval", label: "AI 평가", icon: Scale, href: "/eval" },
  { key: "emr", label: "EMR", icon: Monitor, href: "/emr" },
];

export const MENU = [
  { href: "/today", label: "오늘" },
  { href: "/schedule", label: "예약" },
  { href: "/prep", label: "준비표" },
  { href: "/calls", label: "통화" },
  { href: "/missed", label: "미예약 환자" },
  { href: "/claims", label: "청구 점검" },
  { href: "/finance", label: "재무 대조" },
  { href: "/report", label: "경영 리포트" },
  { href: "/chart-ocr", label: "종이 차트 이관" },
];

/** AI 평가 탭 — 목록 칸 링크(/eval?tab=…), 첫 탭은 주소에 ?tab이 없다 */
export const EVAL_TABS = [
  ["judge", "판정"],
  ["blind", "블라인드 비교"],
  ["acc", "정확도 표"],
  ["improve", "자동 개선"],
] as const;

/** 지금 경로가 속한 공간 — 업무 화면 전부는 work */
export function spaceOf(pathname: string): SpaceKey {
  const seg = pathname.split("/")[1] ?? "";
  if (seg === "") return "assistant";
  if (seg === "ask") return "library";
  if (seg === "eval") return "eval";
  if (seg === "emr") return "emr";
  return "work";
}
