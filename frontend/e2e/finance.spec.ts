// ⑧ 재무 대조 일계표(설계 docs/superpowers/specs/2026-11-11-finance-recon-design.md §1) — 확정은 앱 DB에 남고
// 실데이터엔 경계 사례(정산 대기·확정된 건)가 고정돼 있지 않으므로 /api/finance/recon은 page.route 가짜 응답으로 준다
import { expect, type Page, test } from "@playwright/test";

type Confirmation = {
  cause: string;
  proposed_cause: string;
  by: string;
  at: string;
} | null;

const zero = { ledger: 0, fin: 0, diff: 0 };
const noCash = { ledger: 0, fin: null, diff: null, deposit: null };

const cancelItem = (confirmation: Confirmation) => ({
  key: "aaaaaaaaaaaa",
  date: "2026-11-03",
  dates: ["2026-11-03"],
  channel: "card",
  payment_ids: [7],
  fin: [
    { table: "fin_card_sales", id: 1 },
    { table: "fin_card_sales", id: 2 },
    { table: "fin_card_sales", id: 3 },
  ],
  ledger_total: 30000,
  fin_total: 30000,
  rule_cause: "cancel_reapprove",
  ledger_rows: [
    {
      id: 7,
      paid_at: "2026-11-03 10:00:00",
      name: "김하나",
      amount_due: 30000,
      amount_paid: 30000,
      method: "card",
      approval_no: "11112222",
    },
  ],
  fin_rows: [
    {
      table: "fin_card_sales",
      at: "2026-11-03 10:00:00",
      amount: 30000,
      label: "가온카드",
    },
    {
      table: "fin_card_sales",
      at: "2026-11-03 10:01:00",
      amount: 30000,
      label: "가온카드 취소",
    },
    {
      table: "fin_card_sales",
      at: "2026-11-03 10:02:00",
      amount: 30000,
      label: "가온카드",
    },
  ],
  confirmation,
});

const typoItem = {
  key: "bbbbbbbbbbbb",
  date: "2026-11-04",
  dates: ["2026-11-04"],
  channel: "cash",
  payment_ids: [8],
  fin: [{ table: "fin_bank_transactions", id: 9 }],
  ledger_total: 1800,
  fin_total: 18000,
  rule_cause: "amount_typo",
  ledger_rows: [
    {
      id: 8,
      paid_at: "2026-11-04 14:30:00",
      name: "이두리",
      amount_due: 18000,
      amount_paid: 1800,
      method: "cash",
      approval_no: null,
    },
  ],
  fin_rows: [
    {
      table: "fin_bank_transactions",
      at: "2026-11-09 13:10:00",
      amount: 18000,
      label: "현금 입금",
    },
  ],
  confirmation: {
    cause: "amount_typo",
    proposed_cause: "amount_typo",
    by: "데스크",
    at: "2026-12-01 09:00",
  },
};

const CAUSES = {
  split_payment: "rule",
  cancel_reapprove: "rule",
  fee_mismatch: "rule",
  deposit_delay: "rule",
  non_clinical_sale: "ambiguous",
  entry_missing: "ambiguous",
  amount_typo: "ambiguous",
  date_shift: "ambiguous",
  method_wrong: "ambiguous",
};

const NOV = (confirmation: Confirmation = null) => ({
  months: ["2026-10", "2026-11"],
  month: "2026-11",
  causes: CAUSES,
  chips: { days: 4, mismatch_days: 2, open_items: confirmation ? 0 : 1 },
  days: [
    {
      date: "2026-11-02",
      status: "ok",
      card: { ledger: 50000, fin: 50000, diff: 0 },
      cash: { ledger: 30000, fin: null, diff: null, deposit: null },
      transfer: zero,
      items: [],
    },
    {
      date: "2026-11-03",
      status: "ok",
      card: { ledger: 30000, fin: 30000, diff: 0 },
      cash: noCash,
      transfer: zero,
      items: [cancelItem(confirmation)],
    },
    {
      date: "2026-11-04",
      status: "ok",
      card: zero,
      cash: {
        ledger: 1800,
        fin: 48000,
        diff: -16200,
        deposit: { on: "2026-11-09", days: 5 },
      },
      transfer: zero,
      items: [typoItem],
    },
    {
      date: "2026-11-28",
      status: "pending",
      card: { ledger: 10000, fin: 99000, diff: -89000 },
      cash: noCash,
      transfer: zero,
      items: [],
    },
  ],
});

const OCT = {
  months: ["2026-10", "2026-11"],
  month: "2026-10",
  causes: CAUSES,
  chips: { days: 1, mismatch_days: 0, open_items: 0 },
  days: [
    {
      date: "2026-10-05",
      status: "ok",
      card: { ledger: 77000, fin: 77000, diff: 0 },
      cash: noCash,
      transfer: zero,
      items: [],
    },
  ],
};

async function fakeRecon(page: Page, nov: () => unknown) {
  await page.route("**/api/finance/recon*", (r) =>
    r.fulfill({
      json:
        new URL(r.request().url()).searchParams.get("month") === "2026-10"
          ? OCT
          : nov(),
    }),
  );
}

const row = (page: Page, date: string) =>
  page.locator(`tr[data-day="${date}"]`);

test("메뉴에서 재무 대조가 열림", async ({ page }) => {
  await fakeRecon(page, () => NOV());
  await page.goto("/today");
  await page.getByRole("link", { name: "재무 대조" }).first().click();
  await expect(page).toHaveURL(/\/finance$/);
  await expect(page.getByRole("heading", { name: "재무 대조" })).toBeVisible();
  await expect(page.getByText("준비 중")).toHaveCount(0);
  await expect(page.getByRole("tablist")).toHaveCount(0);
  await expect(page.getByText("불일치 있는 날 2")).toBeVisible();
});

test("차이 0인 날도 불일치 배지", async ({ page }) => {
  await fakeRecon(page, () => NOV());
  await page.goto("/finance");
  await expect(row(page, "2026-11-03")).toContainText("불일치 1건");
  await expect(row(page, "2026-11-02")).toContainText("차이 없음");
  await expect(row(page, "2026-11-04")).toContainText("확인함");
});

test("현금 입금은 기간 끝 날에만", async ({ page }) => {
  await fakeRecon(page, () => NOV());
  await page.goto("/finance");
  await expect(page.getByText(/5일 치 합산/)).toHaveCount(1);
  await expect(row(page, "2026-11-04")).toContainText("5일 치 합산");
});

test("정산 대기 날은 펼치지 않음", async ({ page }) => {
  await fakeRecon(page, () => NOV());
  await page.goto("/finance");
  await expect(row(page, "2026-11-28")).toContainText("정산 대기");
  await row(page, "2026-11-28").click();
  await expect(page).not.toHaveURL(/day=/);
});

test("펼치면 관련 행과 규칙 추정", async ({ page }) => {
  await fakeRecon(page, () => NOV());
  await page.goto("/finance");
  await row(page, "2026-11-03").click();
  await expect(page).toHaveURL(/day=2026-11-03/);
  const item = page.getByRole("region", { name: /불일치 건/ });
  await expect(item).toContainText("규칙 추정: 취소 후 재승인");
  await expect(item).toContainText("가온카드 취소");
  await expect(item).toContainText("김하나");
  await expect(page.getByText("cancel_reapprove")).toHaveCount(0);
  await expect(page.getByText("fin_card_sales")).toHaveCount(0);
});

test("맞음을 누르면 확정", async ({ page }) => {
  let confirmed = false;
  await fakeRecon(page, () =>
    NOV(
      confirmed
        ? {
            cause: "cancel_reapprove",
            proposed_cause: "cancel_reapprove",
            by: "데스크",
            at: "2026-12-01 09:10",
          }
        : null,
    ),
  );
  let body: unknown = null;
  await page.route("**/api/finance/recon/aaaaaaaaaaaa/confirm", (r) => {
    body = r.request().postDataJSON();
    confirmed = true;
    return r.fulfill({ json: { ok: true } });
  });
  await page.goto("/finance?day=2026-11-03");
  await page.getByRole("button", { name: "맞음" }).click();
  await expect.poll(() => body).toMatchObject({ cause: "cancel_reapprove" });
  await expect(page.getByRole("region", { name: /불일치 건/ })).toContainText(
    "취소 후 재승인 · 데스크",
  );
});

test("아님에서 원인을 고르면 그 원인으로 확정", async ({ page }) => {
  let body: { cause?: string } | null = null;
  await fakeRecon(page, () => NOV());
  await page.route("**/api/finance/recon/aaaaaaaaaaaa/confirm", (r) => {
    body = r.request().postDataJSON();
    return r.fulfill({ json: { ok: true } });
  });
  await page.goto("/finance?day=2026-11-03");
  await page.getByRole("combobox", { name: "원인 고르기" }).click();
  await page.getByRole("option", { name: "입력 누락" }).click();
  await expect.poll(() => body?.cause).toBe("entry_missing");
});

test("장부 쪽 원인으로 확정된 건은 전자차트에서 고친다는 안내", async ({
  page,
}) => {
  await fakeRecon(page, () => NOV());
  await page.goto("/finance?day=2026-11-04");
  await expect(page.getByRole("region", { name: /불일치 건/ })).toContainText(
    "장부 수정은 전자차트에서",
  );
});

test("달을 바꾸면 URL과 표가 바뀜", async ({ page }) => {
  await fakeRecon(page, () => NOV());
  await page.goto("/finance");
  await page.getByRole("button", { name: "10월" }).click();
  await expect(page).toHaveURL(/month=2026-10/);
  await expect(row(page, "2026-10-05")).toBeVisible();
  await expect(row(page, "2026-11-03")).toHaveCount(0);
});
