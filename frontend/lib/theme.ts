// 화면 테마 — 라이트·다크 단추로 바꾼다. 고른 적이 없으면(옛 "system" 값 포함) OS 설정을 따른다.
// 고른 값은 브라우저에 두고 <html data-theme>로 적용한다. 목록 칸 접힘도 같은 방식(<html data-list-collapsed>)
export type Theme = "light" | "dark";
export const THEME_KEY = "ax.theme";
export const LIST_COLLAPSED_KEY = "ax.listCollapsed"; // 목록 칸 접힘(넓은 화면, app-shell) — "1"이면 접힘

/** 처음 그리기 전에 도는 스크립트(app/layout.tsx) — 깜빡임 없이 테마·목록 칸 접힘을 정하고, 테마가 시스템이면
 * OS 변경을 따른다 */
export const THEME_SCRIPT = `(() => {
  try {
    if (localStorage.getItem(${JSON.stringify(LIST_COLLAPSED_KEY)}) === "1")
      document.documentElement.dataset.listCollapsed = "";
  } catch {}
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

/** 고른 테마를 저장하고 적용한다 — 저장소가 막혔으면 이 화면에만 바로 붙인다 */
export function saveTheme(t: Theme) {
  try {
    localStorage.setItem(THEME_KEY, t);
  } catch {
    document.documentElement.dataset.theme = t;
    return;
  }
  (window as { __axApplyTheme?: () => void }).__axApplyTheme?.();
}

/** 목록 칸을 접거나 편다 — <html data-list-collapsed>로 적용하고(globals.css list-collapsed 변형) 브라우저에 둔다 */
export function saveListCollapsed(collapsed: boolean) {
  if (collapsed) document.documentElement.dataset.listCollapsed = "";
  else delete document.documentElement.dataset.listCollapsed;
  try {
    localStorage.setItem(LIST_COLLAPSED_KEY, collapsed ? "1" : "0");
  } catch {}
}
