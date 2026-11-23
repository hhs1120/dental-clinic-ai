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

test("design: 아이콘 줄과 목록 칸은 같은 면, 선택 칸은 흰 바탕", async ({
  page,
}) => {
  await page.goto("/claims");
  await setTheme(page, "light");
  const rail = await css(page, "aside > div:first-child", "background-color");
  const panel = await css(page, "aside > :nth-child(2)", "background-color");
  expect(await css(page, "aside", "background-color")).toBe(
    "rgb(239, 233, 225)",
  );
  expect(rail).toBe("rgba(0, 0, 0, 0)");
  expect(panel).toBe("rgba(0, 0, 0, 0)");
  expect(
    await css(
      page,
      'nav[aria-label="메뉴"] a[aria-current=page]',
      "background-color",
    ),
  ).toBe("rgb(255, 255, 255)");
  expect(
    await css(
      page,
      'nav[aria-label="공간"] a[aria-current=page]',
      "background-color",
    ),
  ).toBe("rgb(255, 255, 255)");
});

test("design: 테마를 바꾸면 사이드바도", async ({ page }) => {
  await page.goto("/claims");
  await setTheme(page, "dark");
  expect(await css(page, "aside", "background-color")).toBe("rgb(28, 25, 22)");
});

test("design: 업무 화면 제목은 위쪽 띠, AI 비서·EMR은 띠 없음", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/claims");
  const band = page.locator('[data-slot="page-header"]');
  await expect(
    band.getByRole("heading", { level: 1, name: "청구 점검" }),
  ).toBeVisible();
  await expect(band.getByText("업무", { exact: true })).toBeVisible();
  expect(await css(page, '[data-slot="page-header"]', "position")).toBe(
    "sticky",
  );
  await page.mouse.move(900, 600);
  await page.mouse.wheel(0, 2000);
  await expect.poll(async () => (await band.boundingBox())!.y).toBe(0);
  for (const path of ["/", "/emr"]) {
    await page.goto(path);
    await expect(page.locator('[data-slot="page-header"]')).toHaveCount(0);
  }
});

test("design: 띠가 높아져도 sticky 칸이 띠 아래", async ({ page }) => {
  await page.setViewportSize({ width: 1024, height: 800 });
  await page.goto("/calls?call=c-0450"); // 상세 칸이 열린 채로(e2e/calls.spec.ts와 같은 통화)
  const panel = page.locator(".lg\\:sticky").first();
  await expect(panel).toBeVisible();
  // 통화 목록이 다 불러와져 페이지가 길어진 뒤에 내린다
  await expect
    .poll(() => page.evaluate(() => document.documentElement.scrollHeight), {
      timeout: 30_000,
    })
    .toBeGreaterThan(3000);
  await page.evaluate(() => window.scrollTo(0, 2000));
  await expect
    .poll(() => page.evaluate(() => window.scrollY))
    .toBeGreaterThan(200);
  const band = (await page.locator('[data-slot="page-header"]').boundingBox())!;
  const box = (await panel.boundingBox())!;
  expect(box.y).toBeGreaterThanOrEqual(band.y + band.height);
});

test("design: 목록 펴기는 띠 안에", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/claims");
  await page.getByRole("button", { name: "목록 접기" }).click();
  const expandBtn = page
    .locator('[data-slot="page-header"]')
    .getByRole("button", { name: "목록 펴기" });
  await expect(expandBtn).toBeVisible();
  await expandBtn.click();
  await expect(page.getByRole("navigation", { name: "메뉴" })).toBeVisible();
});

test("design: 인쇄하면 띠는 제목만", async ({ page }) => {
  await page.goto("/prep");
  await page.emulateMedia({ media: "print" });
  const band = page.locator('[data-slot="page-header"]');
  await expect(
    band.getByRole("heading", { level: 1, name: "준비표" }),
  ).toBeVisible();
  await expect(band.getByText("업무", { exact: true })).toBeHidden();
  expect(await css(page, '[data-slot="page-header"]', "position")).toBe(
    "static",
  );
});

test("design: 좁은 화면은 띠가 붙지 않고 가로 넘침 없음", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  for (const path of ["/today", "/claims", "/ask"]) {
    await page.goto(path);
    expect(await css(page, '[data-slot="page-header"]', "position")).toBe(
      "static",
    );
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
  }
});
