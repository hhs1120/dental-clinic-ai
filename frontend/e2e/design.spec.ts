// 디자인 토큰·셸 배치(docs/superpowers/specs/2026-11-23-design-refresh-design.md)
import { expect as baseExpect, type Page, test } from "@playwright/test";

// 데이터를 불러온 뒤에 보는 단언이 많다 — 전체 e2e(작업자 4)에서는 API가 느려 기본 5초를 넘긴다
const expect = baseExpect.configure({ timeout: 15_000 });

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

test("design: 상태 딱지는 테두리 없는 알약, 굵기 500", async ({ page }) => {
  await page.goto("/claims");
  const sel = "[data-slot=badge][data-variant=danger]";
  await expect(page.locator(sel).first()).toBeVisible();
  expect(await css(page, sel, "border-top-color")).toBe("rgba(0, 0, 0, 0)");
  expect(await css(page, sel, "font-weight")).toBe("500");
});

test("design: 카드·표 감싸개 모서리 16px, 소제목 굵기 500", async ({
  page,
}) => {
  // 청구 점검 표 감싸개는 rounded-md였다 — 큰 덩어리는 16px, 바탕은 시안대로 본문 바탕 그대로
  await page.goto("/claims");
  const table = "main .scroll-shadow-x.border";
  await expect(page.locator(table).first()).toBeVisible();
  expect(await css(page, table, "border-top-left-radius")).toBe("16px");
  await page.goto("/today");
  await expect(page.locator("main h2").first()).toBeVisible();
  expect(await css(page, "main h2", "font-weight")).toBe("500");
});

test("design: AI 비서 질문 말풍선은 카드 면", async ({ page }) => {
  // 지난 대화는 다른 e2e가 지우기도 한다 — 번호를 박지 않고 남아 있는 첫 대화를 연다
  await page.goto("/");
  const past = page
    .getByRole("navigation", { name: "지난 대화" })
    .getByRole("link");
  await expect(past.first()).toBeVisible();
  await past.first().click();
  await setTheme(page, "light");
  const bubble = "main p.self-end";
  await expect(page.locator(bubble).first()).toBeVisible();
  expect(await css(page, bubble, "background-color")).toBe(
    "rgb(255, 255, 255)",
  );
});
