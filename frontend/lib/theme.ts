// 화면 테마 — 시스템(OS 설정)·라이트·다크. 고른 값은 브라우저에 두고 <html data-theme>로 적용한다
export const THEMES = [
  ["system", "시스템"],
  ["light", "라이트"],
  ["dark", "다크"],
] as const;
export type Theme = (typeof THEMES)[number][0];
export const THEME_KEY = "ax.theme";

/** 처음 그리기 전에 도는 스크립트(app/layout.tsx) — 깜빡임 없이 테마를 정하고 시스템이면 OS 변경을 따른다 */
export const THEME_SCRIPT = `(() => {
  const key = ${JSON.stringify(THEME_KEY)};
  const mq = matchMedia("(prefers-color-scheme: dark)");
  const apply = () => {
    let t = "system";
    try { t = localStorage.getItem(key) || "system"; } catch {}
    const dark = t === "dark" || (t !== "light" && mq.matches);
    document.documentElement.dataset.theme = dark ? "dark" : "light";
  };
  apply();
  mq.addEventListener("change", apply);
  window.__axApplyTheme = apply;
})();`;

export function readTheme(): Theme {
  try {
    const t = localStorage.getItem(THEME_KEY);
    if (t === "light" || t === "dark") return t;
  } catch {}
  return "system";
}

export function saveTheme(t: Theme) {
  try {
    localStorage.setItem(THEME_KEY, t);
  } catch {}
  (window as { __axApplyTheme?: () => void }).__axApplyTheme?.();
}
