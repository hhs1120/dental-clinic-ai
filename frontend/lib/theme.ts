// 화면 테마 — 라이트·다크 단추로 바꾼다. 고른 적이 없으면(옛 "system" 값 포함) OS 설정을 따른다.
// 고른 값은 브라우저에 두고 <html data-theme>로 적용한다
export type Theme = "light" | "dark";
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

/** 지금 적용된 테마 — <html data-theme>(그리기 전 스크립트가 붙이고, OS를 따를 때는 바뀌면 다시 붙인다) */
export function currentTheme(): Theme {
  return document.documentElement.dataset.theme === "dark" ? "dark" : "light";
}

/** data-theme이 바뀔 때 알린다 — useSyncExternalStore 구독용 */
export function watchTheme(onChange: () => void): () => void {
  const mo = new MutationObserver(onChange);
  mo.observe(document.documentElement, {
    attributes: true,
    attributeFilter: ["data-theme"],
  });
  return () => mo.disconnect();
}

export function saveTheme(t: Theme) {
  try {
    localStorage.setItem(THEME_KEY, t);
  } catch {}
  (window as { __axApplyTheme?: () => void }).__axApplyTheme?.();
}
