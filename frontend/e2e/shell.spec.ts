// 앱 셸 — 아이콘 줄 5칸·목록 칸·좁은 화면 서랍(AI 비서 설계 §3.1)
import { expect, test } from "@playwright/test";

test("shell: 아이콘 줄 5칸과 이름", async ({ page }) => {
  await page.goto("/");
  const rail = page.getByRole("navigation", { name: "공간" });
  await expect(rail.getByRole("link")).toHaveText([
    "AI 비서",
    "업무",
    "자료실",
    "AI 평가",
    "EMR",
  ]);
  await expect(rail.getByRole("link", { name: "AI 비서" })).toHaveAttribute(
    "aria-current",
    "page",
  );
  await expect(
    page.getByRole("heading", { name: "무엇을 도와드릴까요?" }),
  ).toBeVisible();
});

test("shell: 업무를 누르면 목록 칸이 메뉴 8개", async ({ page }) => {
  await page.goto("/");
  await page
    .getByRole("navigation", { name: "공간" })
    .getByRole("link", { name: "업무" })
    .click();
  await expect(page).toHaveURL(/\/today$/);
  const menu = page.getByRole("navigation", { name: "메뉴" });
  await expect(menu.getByRole("link")).toHaveCount(8);
  await expect(menu.getByRole("link", { name: /^오늘/ })).toHaveAttribute(
    "aria-current",
    "page",
  );
  await menu.getByRole("link", { name: "경영 리포트" }).click();
  await expect(page).toHaveURL(/\/report$/);
});

test("shell: AI 평가 탭은 목록 칸 링크", async ({ page }) => {
  await page.goto("/eval");
  const menu = page.getByRole("navigation", { name: "메뉴" });
  await expect(menu.getByRole("link")).toHaveText([
    "판정",
    "블라인드 비교",
    "정확도 표",
    "자동 개선",
  ]);
  await menu.getByRole("link", { name: "정확도 표" }).click();
  await expect(page).toHaveURL(/\/eval\?tab=acc$/);
  await expect(menu.getByRole("link", { name: "정확도 표" })).toHaveAttribute(
    "aria-current",
    "page",
  );
});

test("shell: 좁은 화면에서 서랍", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 800 });
  await page.goto("/today");
  const rail = page.getByRole("navigation", { name: "공간" });
  await expect(rail).toBeHidden();
  await page.getByRole("button", { name: "메뉴 열기" }).click();
  await expect(rail.getByRole("link", { name: "자료실" })).toBeVisible();
  await expect(
    page.getByRole("navigation", { name: "메뉴" }).getByRole("link"),
  ).toHaveCount(8);
  await page
    .getByRole("navigation", { name: "메뉴" })
    .getByRole("link", { name: "예약", exact: true })
    .click();
  await expect(page).toHaveURL(/\/schedule$/);
  await expect(rail).toBeHidden();
});

test("shell: 떠 있는 물어보기 버튼 없음", async ({ page }) => {
  await page.goto("/today");
  await expect(page.getByRole("button", { name: "물어보기" })).toHaveCount(0);
  await expect(
    page.getByRole("complementary", { name: "물어보기" }),
  ).toHaveCount(0);
});

test("shell: 화면을 옮기는 동안 목록 칸 숫자가 남아 있다", async ({ page }) => {
  const sheet = {
    cards: {
      appointments: { total: 0 },
      todo: { items: 7 },
    },
    people: { urgent_calls: [], appeal_soon: 0, recon_open: 0 },
  };
  let slow = false;
  await page.route("**/api/today", async (r) => {
    // 숫자가 한 번 뜬 뒤의 요청은 늦게 답한다 — 그 사이에도 숫자가 보여야 한다
    if (slow) await new Promise((ok) => setTimeout(ok, 1500));
    await r.fulfill({ json: sheet });
  });
  await page.goto("/report");
  const menu = page.getByRole("navigation", { name: "메뉴" });
  const badge = menu.getByRole("link", { name: /^오늘/ }).getByText("7");
  await expect(badge).toBeVisible();
  slow = true;
  await menu.getByRole("link", { name: "준비표" }).click();
  await expect(page).toHaveURL(/\/prep$/);
  await expect(badge).toBeVisible();
});

test("shell: 접은 목록 칸은 새로 고쳐도 접힌 채로 그린다", async ({ page }) => {
  await page.goto("/today");
  const menu = page.getByRole("navigation", { name: "메뉴" });
  await expect(menu).toBeVisible();
  await page.getByRole("button", { name: "목록 접기" }).click();
  await expect(menu).toBeHidden();
  // 하이드레이션 전 서버 HTML부터 접혀 있어야 한다 — 스크립트를 끈 채 그린 첫 화면을 본다
  await page.route("**/_next/static/**/*.js", (r) => r.abort());
  await page.reload();
  await expect(page.getByRole("button", { name: "목록 펴기" })).toBeVisible();
  await expect(menu).toBeHidden();
  await page.unrouteAll();
  await page.reload();
  await page.getByRole("button", { name: "목록 펴기" }).click();
  await expect(menu).toBeVisible();
});

test("shell: 접은 목록 칸은 같은 공간 안에선 그대로, 아이콘으로 다른 공간에 가면 펴진다", async ({
  page,
}) => {
  await page.goto("/today");
  const menu = page.getByRole("navigation", { name: "메뉴" });
  await page.getByRole("button", { name: "목록 접기" }).click();
  await expect(menu).toBeHidden();
  await page.goto("/schedule");
  await expect(page.getByRole("button", { name: "목록 펴기" })).toBeVisible();
  await page
    .getByRole("navigation", { name: "공간" })
    .getByRole("link", { name: "자료실" })
    .click();
  await expect(page).toHaveURL(/\/ask/);
  await expect(menu).toBeVisible();
});

test("shell: 지금 공간 아이콘을 다시 누르면 옮기지 않고 목록 칸을 접고 편다", async ({
  page,
}) => {
  await page.goto("/schedule");
  const menu = page.getByRole("navigation", { name: "메뉴" });
  const work = page
    .getByRole("navigation", { name: "공간" })
    .getByRole("link", { name: "업무" });
  await work.click();
  await expect(menu).toBeHidden();
  await expect(page).toHaveURL(/\/schedule$/);
  await work.click();
  await expect(menu).toBeVisible();
  await expect(page).toHaveURL(/\/schedule$/);
});

test("shell: 저장소가 막혀도 테마 단추가 바뀐다", async ({ page }) => {
  await page.addInitScript(() => {
    Storage.prototype.setItem = () => {
      throw new Error("막힘");
    };
  });
  await page.emulateMedia({ colorScheme: "light" });
  await page.goto("/today");
  await page.getByRole("button", { name: "다크 테마로 바꾸기" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await expect(
    page.getByRole("button", { name: "라이트 테마로 바꾸기" }),
  ).toBeVisible();
});
