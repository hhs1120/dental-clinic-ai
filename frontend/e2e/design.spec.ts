// 디자인 토큰·셸 배치(docs/superpowers/specs/2026-11-23-design-refresh-design.md)
import { expect, type Page, test } from "@playwright/test";

const css = (page: Page, sel: string, prop: string) =>
  page
    .locator(sel)
    .first()
    .evaluate((e, p) => getComputedStyle(e).getPropertyValue(p).trim(), prop);
const setTheme = (page: Page, t: "light" | "dark") =>
  page.evaluate(
    (v) => document.documentElement.setAttribute("data-theme", v),
    t,
  );

test("design: 라이트·다크 바탕과 링크 색", async ({ page }) => {
  await page.goto("/today");
  await setTheme(page, "light");
  expect(await css(page, "body", "background-color")).toBe(
    "rgb(246, 244, 240)",
  );
  expect(await css(page, ":root", "--link")).toBe("#1f5f7a");
  await setTheme(page, "dark");
  expect(await css(page, "body", "background-color")).toBe("rgb(37, 33, 29)");
  expect(await css(page, ":root", "--link")).toBe("#8cc0b8");
});

test("design: 글꼴은 Inter 다음 Pretendard, 숫자 폭 고정", async ({ page }) => {
  await page.goto("/today");
  // next/font/local은 글꼴 이름을 변수 이름(소문자 pretendard)으로 붙인다
  const family = (await css(page, "body", "font-family")).toLowerCase();
  expect(family.indexOf("inter")).toBeGreaterThanOrEqual(0);
  expect(family.indexOf("inter")).toBeLessThan(family.indexOf("pretendard"));
  expect(family).not.toContain("noto");
  expect(await css(page, "body", "font-variant-numeric")).toBe("tabular-nums");
});
