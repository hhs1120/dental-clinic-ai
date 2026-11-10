// 통화 상세 — 처리안의 "과정 보기"(앱 설계 §9). 복사본 app.sqlite에 저장된 처리안 기록을 그대로 읽는다
import { expect, test } from "@playwright/test";

test("과정 보기 — 조회 조건은 영어 값이 아니라 이름으로", async ({ page }) => {
  // c-0450: 처리안이 예약 조회(2026-11-28, 위생)를 불렀다
  await page.goto("/calls?call=c-0450");
  await page.getByRole("button", { name: /과정 보기/ }).click();
  await expect(page.getByText("예약 조회 — 11-28 · 위생")).toBeVisible();
  await expect(page.getByText(/hygiene/)).toHaveCount(0);
});
