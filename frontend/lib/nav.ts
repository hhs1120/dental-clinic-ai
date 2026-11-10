// 메뉴와 별도 공간 (기능 정의 §4 앱 구성, 앱 설계 §9)
export const MENU = [
  { href: "/today", label: "오늘" },
  { href: "/calls", label: "통화" },
  { href: "/schedule", label: "예약" },
  { href: "/prep", label: "준비표" },
  { href: "/ask", label: "물어보기" },
  { href: "/claims", label: "청구 점검" },
  { href: "/report", label: "경영 리포트" },
  { href: "/finance", label: "재무 대조" },
  { href: "/missed", label: "미예약 환자" },
];

/** 메뉴에 넣지 않는 공간 — 들어가면 메뉴 자리에 이름과 돌아가기 */
export const SPACES: Record<string, string> = {
  eval: "AI 평가",
  emr: "전환 전 EMR",
};

const BUILT = ["/today", "/ask", "/schedule", "/calls", "/prep", "/missed"];

/** 아직 화면이 없는 라우트 조각 → 화면 이름 (/emr은 화면이 있다) */
export const COMING_SOON: Record<string, string> = {
  ...Object.fromEntries(
    MENU.filter((m) => !BUILT.includes(m.href)).map((m) => [
      m.href.slice(1),
      m.label,
    ]),
  ),
};
