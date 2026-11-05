// 판정 키 시퀀스 10회 → ax_judgments +10, 블라인드 5회 → ax_blind_votes +5 (기능 정의 §4 ② 완료 기준)
import { expect, type Page, test } from "@playwright/test";

const api = "http://localhost:8100";

async function judged(page: Page): Promise<number> {
  const n = await (await page.request.get(`${api}/judge/next`)).json();
  return n.done_total;
}

async function voted(page: Page): Promise<number> {
  const r = await (await page.request.get(`${api}/metrics/accuracy`)).json();
  return r.blind.human.n;
}

test("판정 — 키보드만으로 10건", async ({ page }) => {
  const before = await judged(page);
  await page.goto("/eval", { waitUntil: "networkidle" });
  const id = page.getByText(/#[cp]-\d+/);
  let prev = "";
  for (let i = 0; i < 10; i++) {
    // 다음 건을 불러오는 동안 누른 키는 무시되므로 통화가 바뀐 뒤에 친다
    await expect(id).not.toHaveText(prev);
    prev = (await id.textContent()) ?? "";
    await expect(page.getByText("0/9 항목")).toBeVisible();
    // 목적 01 · 상대 1 · 급함 2 · 플래그 6개 1(아니오)
    for (const k of ["0", "1", "1", "2", "1", "1", "1", "1", "1", "1"])
      await page.keyboard.press(k);
    await expect(page.getByText("9/9 항목")).toBeVisible();
    await page.keyboard.press("Enter");
    await expect(
      page.getByRole("button", { name: "다음 건 (Enter)" }),
    ).toBeVisible();
    await page.keyboard.press("Enter");
  }
  await expect.poll(() => judged(page)).toBe(before + 10);
});

test("블라인드 — 키보드만으로 5건", async ({ page }) => {
  const before = await voted(page);
  await page.goto("/eval?tab=blind", { waitUntil: "networkidle" });
  const id = page.getByText(/#c-\d+/);
  let prev = "";
  for (let i = 0; i < 5; i++) {
    await expect(id).not.toHaveText(prev);
    prev = (await id.textContent()) ?? "";
    await expect(page.getByRole("button", { name: /1이 나음/ })).toBeEnabled();
    await page.keyboard.press("3");
    await expect(
      page.getByRole("button", { name: "다음 (Enter)" }),
    ).toBeVisible();
    await page.keyboard.press("Enter");
  }
  await expect.poll(() => voted(page)).toBe(before + 5);
});
