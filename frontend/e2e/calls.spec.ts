// 통화 상세 — 처리안의 "과정 보기"(앱 설계 §9). 복사본 app.sqlite에 저장된 처리안 기록을 그대로 읽는다
import { expect, test } from "@playwright/test";

test("과정 보기 — 조회 조건은 영어 값이 아니라 이름으로", async ({ page }) => {
  // c-0450: 처리안이 예약 조회(2026-11-28, 위생)를 불렀다
  await page.goto("/calls?call=c-0450");
  await page.getByRole("button", { name: /과정 보기/ }).click();
  await expect(page.getByText("예약 조회 — 11-28 · 위생")).toBeVisible();
  await expect(page.getByText(/hygiene/)).toHaveCount(0);
});

// 상세 응답을 가로채 칸 하나를 바꾼다 — 실데이터는 자동 종결·사람 판정이 0건
async function patchDetail(
  page: import("@playwright/test").Page,
  uid: string,
  patch: (d: Record<string, unknown>) => Record<string, unknown>,
) {
  await page.route(`**/api/calls/${uid}`, async (r) => {
    if (r.request().method() !== "GET") return r.fallback();
    const res = await r.fetch();
    return r.fulfill({ json: patch(await res.json()) });
  });
}

test("자동 종결 띠 버튼은 다시 열기", async ({ page }) => {
  await patchDetail(page, "c-0450", (d) => ({
    ...d,
    autoclose: {
      id: 1,
      reason: "두 모델 모두 할 일 없음 · 응급·불만·급함·엇갈림 없음",
      at: "2026-12-01 03:10:00",
    },
  }));
  await page.goto("/calls?call=c-0450");
  const detail = page.getByRole("complementary", { name: "통화 상세" });
  await expect(detail.getByRole("button", { name: "다시 열기" })).toBeVisible();
  await expect(detail.getByRole("button", { name: "되돌리기" })).toHaveCount(0);
});

test("확인할 통화 띠의 확인함", async ({ page }) => {
  let checked = false;
  await patchDetail(page, "c-0450", (d) => ({
    ...d,
    review: checked
      ? null
      : { ai: "none", reopened: false, flags: ["emergency", "complaint"] },
  }));
  await page.route("**/api/calls/c-0450/check", (r) => {
    checked = true;
    return r.fulfill({ json: { ok: true } });
  });
  await page.goto("/calls?call=c-0450");
  const detail = page.getByRole("complementary", { name: "통화 상세" });
  await expect(detail).toContainText("AI: 할 일 없음, 사람 판정: 응급·불만");
  await detail.getByRole("button", { name: "확인함" }).click();
  await expect(detail).not.toContainText("사람 판정");
  expect(checked).toBe(true);
});
