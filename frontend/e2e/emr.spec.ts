// 전환 전 EMR 따라 보기 — README "진료 전 확인"의 화면 이동 5회를 지킨다(따라 보기 단계 frontend/components/emr/tour.ts).
// 하루 수는 5회 × 기준일 예약 환자(설명 카드 계산), 실데이터 복사본 기준
import { expect, test } from "@playwright/test";

test("따라 보기 — 환자 한 명 화면 이동 5회, 하루는 × 오늘 예약 환자", async ({
  page,
}) => {
  await page.goto("/emr");
  await page
    .getByRole("button", { name: /따라 보기/ })
    .first()
    .click();
  for (let i = 0; i < 4; i++) {
    const done = page.getByRole("button", { name: "마치기" });
    if (await done.isVisible()) {
      await done.click();
      break;
    }
    await page.getByRole("button", { name: "다음" }).click();
  }
  const result = page.locator("dl").filter({ hasText: "데스크의 하루" });
  await expect(result).toContainText("화면 이동 5회");
  const n = Number(
    (await result.getByText(/^\d+명$/).innerText()).replace("명", ""),
  );
  await expect(result).toContainText(`${5 * n}회`);
});
