// ⑧ 재무 대조 일계표·지출 증빙(설계 docs/superpowers/specs/2026-11-11-finance-recon-design.md §1·§2) — 확정은 앱 DB에 남고
// 실데이터엔 경계 사례(정산 대기·확정된 건)가 고정돼 있지 않으므로 /api/finance/recon·expenses는 page.route 가짜 응답으로 준다
import { expect, type Page, test } from "@playwright/test";

type Confirmation = {
  cause: string;
  proposed_cause: string;
  by: string;
  at: string;
} | null;

const zero = { ledger: 0, fin: 0, diff: 0 };
const noCash = {
  ledger: 0,
  fin: null,
  diff: null,
  deposit: null,
  pending: false,
};

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

const MONTHS = ["2025-12", "2026-10", "2026-11"];

const feeItem = {
  key: "cccccccccccc",
  date: "2026-11-05",
  dates: ["2026-11-05"],
  channel: "settlement",
  payment_ids: [],
  fin: [{ table: "fin_card_deposits", id: 4 }],
  ledger_total: 0,
  fin_total: 98500,
  rule_cause: "fee_mismatch",
  ledger_rows: [],
  fin_rows: [
    {
      table: "fin_card_deposits",
      at: "2026-11-09",
      amount: 98500,
      label: "가온카드 정산",
      detail: {
        sales_date: "2026-11-05",
        sales_total: 100000,
        fee: 1500,
        expected_fee: 1200,
        expected_on: "2026-11-09",
      },
    },
  ],
  confirmation: null,
};

const NOV = (confirmation: Confirmation = null) => ({
  months: MONTHS,
  month: "2026-11",
  causes: CAUSES,
  chips: {
    days: 6,
    mismatch_days: 3,
    open_items: confirmation ? 1 : 2,
    open_all: confirmation ? 4 : 5,
  },
  days: [
    {
      date: "2026-11-02",
      status: "ok",
      card: { ledger: 50000, fin: 50000, diff: 0 },
      cash: { ...noCash, ledger: 30000 },
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
        pending: false,
      },
      transfer: zero,
      items: [typoItem],
    },
    {
      date: "2026-11-05",
      status: "ok",
      card: { ledger: 100000, fin: 100000, diff: 0 },
      cash: noCash,
      transfer: zero,
      items: [feeItem],
    },
    {
      date: "2026-11-25",
      status: "ok",
      card: zero,
      cash: { ...noCash, ledger: 10000, pending: true },
      transfer: zero,
      items: [],
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
  months: MONTHS,
  month: "2026-10",
  causes: CAUSES,
  chips: { days: 1, mismatch_days: 0, open_items: 0, open_all: 5 },
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
  await expect(page.getByRole("tab", { name: "일계표" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await expect(page.getByText("불일치 있는 날 3")).toBeVisible();
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
  await page.getByRole("button", { name: "2026년 10월" }).click();
  await expect(page).toHaveURL(/month=2026-10/);
  await expect(row(page, "2026-10-05")).toBeVisible();
  await expect(row(page, "2026-11-03")).toHaveCount(0);
});

test("달 버튼은 연도가 바뀌는 곳에 연도", async ({ page }) => {
  await fakeRecon(page, () => NOV());
  await page.goto("/finance");
  const months = page.getByRole("group", { name: "달" }).getByRole("button");
  await expect(months).toHaveText(["2025년 12월", "2026년 10월", "11월"]);
});

test("전체 기간 확정 안 된 건 수", async ({ page }) => {
  await fakeRecon(page, () => NOV());
  await page.goto("/finance");
  await expect(page.getByText("전체 확정 안 된 건 5")).toBeVisible();
  await expect(page.getByText("이 달 확정 안 된 건 2")).toBeVisible();
});

test("정산 건은 기대 수수료·예상 입금일을 함께", async ({ page }) => {
  await fakeRecon(page, () => NOV());
  await page.goto("/finance?day=2026-11-05");
  const item = page.getByRole("region", { name: /불일치 건/ });
  await expect(item).toContainText("수수료 1,500원");
  await expect(item).toContainText("기대 1,200원");
  await expect(item).toContainText("예상 입금 11/09");
  await expect(item).toContainText("매출 100,000원");
});

test("현금이 대조 전인 날은 현금 입금 칸에 대조 전", async ({ page }) => {
  await fakeRecon(page, () => NOV());
  await page.goto("/finance");
  await expect(row(page, "2026-11-25")).toContainText("대조 전");
  await expect(row(page, "2026-11-25")).toContainText("차이 없음");
});

// ── ⑧-2 지출 증빙 탭 (설계 §2) ─────────────────────────────

const receipt = {
  id: 11,
  kind: "receipt",
  issuer: "솔밭식당",
  issued_on: "2026-11-03",
  amount: 18000,
  body: "솔밭식당 영수증 점심 식대 18,000원",
};
const statement = (id: number, on: string, amount: number) => ({
  id,
  kind: "statement",
  issuer: "바른덴탈상사",
  issued_on: on,
  amount,
  body: `거래명세서 ${amount}원`,
});

type ExpConfirmed = { by: string; at: string } | null;

const EXP = (kcp: ExpConfirmed = null) => {
  const rows = [
    {
      table: "fin_card_expenses",
      id: 1,
      at: "2026-11-03 12:00:00",
      label: "솔밭식당",
      amount: 18000,
      vendor: "솔밭식당",
      docs: [receipt],
      diff: 0,
      account: "복리후생비",
      account_from: "vendor",
      check: false,
      confirmed: null,
    },
    {
      table: "fin_card_expenses",
      id: 2,
      at: "2026-11-10 12:00:00",
      label: "KCP*낯선가게",
      amount: 9000,
      vendor: null,
      docs: [],
      diff: null,
      account: kcp ? "소모품비" : null,
      account_from: kcp ? "confirmed" : null,
      check: !kcp,
      confirmed: kcp,
    },
    {
      table: "fin_bank_transactions",
      id: 3,
      at: "2026-11-30 10:00:00",
      label: "바른덴탈상사",
      amount: 90000,
      vendor: "바른덴탈상사",
      docs: [
        statement(21, "2026-11-10", 40000),
        statement(22, "2026-11-20", 50000),
      ],
      diff: 0,
      account: "재료비",
      account_from: "vendor",
      check: false,
      confirmed: null,
    },
  ];
  return {
    months: ["2026-10", "2026-11"],
    month: "2026-11",
    accounts: ["재료비", "소모품비", "복리후생비"],
    chips: {
      outflows: 3,
      amount: 117000,
      found: 2,
      none: 1,
      check: kcp ? 0 : 1,
      open: kcp ? 2 : 3,
    },
    rows,
  };
};

async function fakeExpenses(page: Page, sheet: () => unknown) {
  await page.route("**/api/finance/expenses?*", (r) =>
    r.fulfill({ json: sheet() }),
  );
  await page.route("**/api/finance/expenses", (r) =>
    r.fulfill({ json: sheet() }),
  );
}

const expRow = (page: Page, label: string) =>
  page.locator("tr[data-outflow]", { hasText: label });

test("지출 증빙 탭으로 가면 칩과 줄", async ({ page }) => {
  await fakeRecon(page, () => NOV());
  await fakeExpenses(page, () => EXP());
  await page.goto("/finance");
  await page.getByRole("tab", { name: "지출 증빙" }).click();
  await expect(page).toHaveURL(/tab=expense/);
  await expect(page.getByText("출금 3 · 117,000원")).toBeVisible();
  await expect(page.getByRole("button", { name: "미확정 3" })).toBeVisible();
  await expect(expRow(page, "솔밭식당")).toContainText("영수증 · 솔밭식당");
  await expect(expRow(page, "솔밭식당")).toContainText("거래처 기본");
  await expect(expRow(page, "바른덴탈상사")).toContainText(
    "거래명세서 2장 합계",
  );
  await expect(expRow(page, "KCP*낯선가게")).toContainText("증빙 못 찾음");
  await expect(expRow(page, "KCP*낯선가게")).toContainText("과목 확인 필요");
  await expect(page.getByText("fin_card_expenses")).toHaveCount(0);
});

test("증빙 못 찾음 칩으로 거른다", async ({ page }) => {
  await fakeExpenses(page, () => EXP());
  await page.goto("/finance?tab=expense");
  await page.getByRole("button", { name: "증빙 못 찾음 1" }).click();
  await expect(page).toHaveURL(/chip=none/);
  await expect(page.locator("tr[data-outflow]")).toHaveCount(1);
  await expect(expRow(page, "KCP*낯선가게")).toBeVisible();
});

test("과목을 골라 확정하면 확정으로 바뀌고 다시 불러도 남는다", async ({
  page,
}) => {
  let confirmed: ExpConfirmed = null;
  await fakeExpenses(page, () => EXP(confirmed));
  let body: unknown = null;
  await page.route(
    "**/api/finance/expenses/fin_card_expenses/2/confirm",
    (r) => {
      body = r.request().postDataJSON();
      confirmed = { by: "데스크", at: "2026-12-01 09:10:00" };
      return r.fulfill({ json: { ok: true } });
    },
  );
  await page.goto("/finance?tab=expense");
  const kcp = expRow(page, "KCP*낯선가게");
  await expect(kcp.getByRole("button", { name: "확정" })).toBeDisabled();
  await kcp.getByRole("combobox", { name: "계정 과목" }).click();
  await page.getByRole("option", { name: "소모품비" }).click();
  await kcp.getByRole("button", { name: "확정" }).click();
  await expect
    .poll(() => body)
    .toEqual({ document_ids: [], account: "소모품비", part: "데스크" });
  await expect(kcp).toContainText("확정함 · 데스크");
  await page.reload();
  await expect(expRow(page, "KCP*낯선가게")).toContainText("확정함 · 데스크");
});

test("줄을 펼치면 증빙 본문과 후보 — 체크해서 증빙을 바꿔 확정", async ({
  page,
}) => {
  await fakeExpenses(page, () => EXP());
  await page.route(
    "**/api/finance/expenses/fin_card_expenses/1/candidates",
    (r) =>
      r.fulfill({
        json: {
          docs: [
            receipt,
            { ...receipt, id: 12, issued_on: "2026-11-05", amount: 17000 },
          ],
        },
      }),
  );
  let body: { document_ids?: number[] } | null = null;
  await page.route(
    "**/api/finance/expenses/fin_card_expenses/1/confirm",
    (r) => {
      body = r.request().postDataJSON();
      return r.fulfill({ json: { ok: true } });
    },
  );
  await page.goto("/finance?tab=expense");
  await expRow(page, "솔밭식당")
    .getByRole("button", { name: "펼치기" })
    .click();
  const detail = page.getByRole("region", { name: /출금 상세/ });
  await expect(detail).toContainText("점심 식대 18,000원");
  await detail.getByRole("checkbox", { name: /11\/05 · 17,000원/ }).click();
  await detail.getByRole("checkbox", { name: /11\/03 · 18,000원/ }).click();
  await expRow(page, "솔밭식당").getByRole("button", { name: "확정" }).click();
  await expect.poll(() => body?.document_ids).toEqual([12]);
});

test("증빙 없이 확정한 줄은 중립 표시 증빙 없음", async ({ page }) => {
  await fakeExpenses(page, () =>
    EXP({ by: "데스크", at: "2026-12-01 09:10:00" }),
  );
  await page.goto("/finance?tab=expense");
  const kcp = expRow(page, "KCP*낯선가게");
  await expect(kcp).toContainText("증빙 없음");
  await expect(kcp).not.toContainText("증빙 못 찾음");
  await expect(kcp).not.toContainText("과목 확인 필요");
});

test("같은 출금처의 다른 줄을 확정해도 펼친 줄은 그대로", async ({ page }) => {
  let remembered = false;
  const twin = (id: number, day: string) => ({
    table: "fin_card_expenses",
    id,
    at: `2026-11-${day} 12:00:00`,
    label: "KCP*낯선가게",
    amount: 9000,
    vendor: null,
    by_item: false,
    docs: [],
    diff: null,
    account: remembered ? "소모품비" : null,
    account_from: remembered ? "memory" : null,
    check: !remembered,
    confirmed: null,
  });
  await fakeExpenses(page, () => ({
    ...EXP(),
    rows: [twin(2, "10"), twin(4, "20")],
  }));
  await page.route(
    "**/api/finance/expenses/fin_card_expenses/*/candidates",
    (r) => r.fulfill({ json: { docs: [receipt] } }),
  );
  await page.route(
    "**/api/finance/expenses/fin_card_expenses/4/confirm",
    (r) => {
      remembered = true;
      return r.fulfill({ json: { ok: true } });
    },
  );
  await page.goto("/finance?tab=expense");
  const rows = page.locator("tr[data-outflow]");
  await rows.nth(0).getByRole("button", { name: "펼치기" }).click();
  await expect(page.getByRole("region", { name: /출금 상세/ })).toBeVisible();
  await rows.nth(1).getByRole("combobox", { name: "계정 과목" }).click();
  await page.getByRole("option", { name: "소모품비" }).click();
  await rows.nth(1).getByRole("button", { name: "확정" }).click();
  await expect(rows.nth(0)).toContainText("지난 확정");
  await expect(page.getByRole("region", { name: /출금 상세/ })).toBeVisible();
});

// ── 4단계 지출 증빙 AI(설계 §4) ─────────────────────────────

const unknownShop = {
  id: 31,
  kind: "receipt",
  issuer: "낯선가게",
  issued_on: "2026-11-10",
  amount: 9000,
  body: "낯선가게 영수증 종이컵 9,000원",
};

const AI = (
  ids: number[],
  docs: unknown[],
  account: string,
  see: string | null,
) => ({
  document_ids: ids,
  docs,
  account,
  confidence: "mid",
  reasons: [{ text: "같은 날 같은 금액 영수증", lookups: [1] }],
  see,
  status: "done",
  lines: [
    { kind: "hypothesis", text: "같은 날 같은 금액 증빙" },
    {
      kind: "lookup",
      tool: "search_docs",
      lookup_no: 1,
      text: "증빙 검색 (금액 9,000~9,000) 1건",
    },
  ],
});

/** 1번 줄 = 규칙·AI 같음, 2번 줄 = 규칙과 다름(규칙은 증빙 못 찾음). emptyAi면 2번 줄 AI가 증빙 없음 */
const EXP_AI = (opts: { emptyAi?: boolean; auto?: boolean } = {}) => {
  const base = EXP() as {
    chips: Record<string, number>;
    rows: Record<string, unknown>[];
  };
  const rows: Record<string, unknown>[] = base.rows.map((r) => ({
    ...r,
    key: `k${r.id}`,
    agent: null,
    agree: null,
    auto: null,
  }));
  rows[0] = {
    ...rows[0],
    agent: AI([11], [receipt], "복리후생비", null),
    agree: true,
    ...(opts.auto
      ? { auto: { id: 5, at: "2026-12-01 06:00:00" }, account_from: "auto" }
      : {}),
  };
  rows[1] = {
    ...rows[1],
    agent: opts.emptyAi
      ? AI([], [], "복리후생비", "영수증을 받아 두었는지 확인")
      : AI([31], [unknownShop], "소모품비", "영수증 날짜 확인"),
    agree: false,
  };
  return {
    ...base,
    chips: { ...base.chips, differ: 1, open: opts.auto ? 2 : 3 },
    rows,
  };
};

test("규칙과 다른 줄은 AI 칸이 펼쳐지고 이 제안으로 확정", async ({ page }) => {
  await fakeExpenses(page, () => EXP_AI());
  let body = "";
  await page.route(
    "**/api/finance/expenses/fin_card_expenses/2/confirm",
    (r) => {
      body = r.request().postData() ?? "";
      return r.fulfill({ json: { ok: true } });
    },
  );
  await page.goto("/finance?tab=expense");
  const ai = page.getByRole("region", { name: "AI 제안 KCP*낯선가게" });
  await expect(ai).toContainText("규칙과 다름");
  await expect(ai).toContainText("영수증 · 낯선가게");
  await expect(ai).toContainText("소모품비");
  await expect(ai).toContainText("확신 중간");
  await expect(ai).toContainText("같은 날 같은 금액 영수증");
  await expect(ai).toContainText("조회 1");
  await expect(ai).toContainText("직원이 볼 것: 영수증 날짜 확인");
  await ai.getByText("조사 과정 보기").click();
  await expect(ai).toContainText("같은 날 같은 금액 증빙");
  await ai.getByRole("button", { name: "이 제안으로 확정" }).click();
  await expect.poll(() => body).toContain('"document_ids":[31]');
  expect(body).toContain("소모품비");
});

test("규칙과 같은 줄은 짧게", async ({ page }) => {
  await fakeExpenses(page, () => EXP_AI());
  await page.goto("/finance?tab=expense");
  const ai = page.getByRole("region", { name: "AI 제안 솔밭식당" });
  await expect(ai).toContainText("규칙·AI 같음");
  await expect(
    ai.getByRole("button", { name: "이 제안으로 확정" }),
  ).toHaveCount(0);
  await expect(ai).not.toContainText("직원이 볼 것");
});

test("증빙 없음 제안은 증빙 없이 확정", async ({ page }) => {
  await fakeExpenses(page, () => EXP_AI({ emptyAi: true }));
  let body = "";
  await page.route(
    "**/api/finance/expenses/fin_card_expenses/2/confirm",
    (r) => {
      body = r.request().postData() ?? "";
      return r.fulfill({ json: { ok: true } });
    },
  );
  await page.goto("/finance?tab=expense");
  const ai = page.getByRole("region", { name: "AI 제안 KCP*낯선가게" });
  await expect(ai).toContainText("증빙 없음 → 복리후생비");
  await ai.getByRole("button", { name: "증빙 없이 확정" }).click();
  await expect.poll(() => body).toContain('"document_ids":[]');
});

test("규칙과 다름 칩으로 거른다", async ({ page }) => {
  await fakeExpenses(page, () => EXP_AI());
  await page.goto("/finance?tab=expense");
  await page.getByRole("button", { name: "규칙과 다름 1" }).click();
  await expect(page.locator("tr[data-outflow]")).toHaveCount(1);
  await expect(expRow(page, "KCP*낯선가게")).toBeVisible();
});

test("자동 확정 줄은 배지와 되돌리기", async ({ page }) => {
  await fakeExpenses(page, () => EXP_AI({ auto: true }));
  let reverted = false;
  await page.route("**/api/auto-actions/5/revert", (r) => {
    reverted = true;
    return r.fulfill({ json: { ok: true } });
  });
  await page.goto("/finance?tab=expense");
  const r = expRow(page, "솔밭식당");
  await expect(r).toContainText("자동 확정");
  await r.getByRole("button", { name: "되돌리기" }).click();
  await expect.poll(() => reverted).toBe(true);
});

// ── 3단계 조사 에이전트(설계 §3) ─────────────────────────────

const AGENT = {
  run_id: 1,
  status: "done",
  cause: "cancel_reapprove",
  runner_up: "split_payment",
  evidence: [1],
  reason: "같은 금액 승인이 취소되고 다시 승인됨",
  lines: [
    { kind: "hypothesis", text: "취소 후 재승인 같다" },
    {
      kind: "lookup",
      tool: "card_sales",
      lookup_no: 1,
      text: "2026-11-03~2026-11-03 카드 승인 3건",
    },
    {
      kind: "conclusion",
      cause: "cancel_reapprove",
      runner_up: "split_payment",
      evidence: [1],
      text: "같은 금액 승인이 취소되고 다시 승인됨",
    },
  ],
};

const withAgent =
  (agent: unknown, auto: unknown = null) =>
  () => {
    const nov = NOV() as { days: { items: Record<string, unknown>[] }[] };
    nov.days[1].items = [{ ...cancelItem(null), agent, auto }];
    return nov;
  };

test("저장된 조사 과정과 에이전트 원인 — 맞음은 에이전트 1순위를 확정", async ({
  page,
}) => {
  await fakeRecon(page, withAgent(AGENT));
  let body = "";
  await page.route("**/api/finance/recon/aaaaaaaaaaaa/confirm", (r) => {
    body = r.request().postData() ?? "";
    return r.fulfill({ json: { ok: true } });
  });
  await page.goto("/finance?month=2026-11");
  await row(page, "2026-11-03").click();
  const item = page.getByRole("region", { name: /불일치 건/ });
  await expect(item).toContainText("조회 1");
  await expect(item).toContainText(
    "카드 승인 — 2026-11-03~2026-11-03 카드 승인 3건",
  );
  await expect(item).toContainText("에이전트: 취소 후 재승인");
  await expect(item.getByRole("button", { name: "조사 시작" })).toHaveCount(0);
  await item.getByRole("button", { name: "맞음" }).click();
  await expect.poll(() => body).toContain("cancel_reapprove");
});

test("원인 미정이면 맞음 없이 고르기만", async ({ page }) => {
  await fakeRecon(
    page,
    withAgent({
      ...AGENT,
      status: "handed_off",
      cause: "unknown",
      runner_up: null,
      evidence: [],
    }),
  );
  await page.goto("/finance?month=2026-11");
  await row(page, "2026-11-03").click();
  const item = page.getByRole("region", { name: /불일치 건/ });
  await expect(item).toContainText("에이전트: 원인 미정");
  await expect(item.getByRole("button", { name: "맞음" })).toBeDisabled();
});

test("조사 시작 — 줄이 생기고 끝나면 다시 조회", async ({ page }) => {
  let done = false;
  await fakeRecon(page, () => (done ? withAgent(AGENT)() : NOV()));
  const sse = [
    { event: "line", data: { line: AGENT.lines[0] } },
    { event: "line", data: { line: AGENT.lines[1] } },
    { event: "line", data: { line: AGENT.lines[2] } },
    { event: "done", data: { agent: { ...AGENT, lines: undefined } } },
  ]
    .map((e) => `event: ${e.event}\ndata: ${JSON.stringify(e.data)}\n\n`)
    .join("");
  await page.route("**/api/finance/recon/aaaaaaaaaaaa/investigate", (r) => {
    done = true;
    return r.fulfill({ contentType: "text/event-stream", body: sse });
  });
  await page.goto("/finance?month=2026-11");
  await row(page, "2026-11-03").click();
  const item = page.getByRole("region", { name: /불일치 건/ });
  await item.getByRole("button", { name: "조사 시작" }).click();
  await expect(item).toContainText("취소 후 재승인 같다");
  await expect(item).toContainText("에이전트: 취소 후 재승인");
  await expect(item.getByRole("button", { name: "조사 시작" })).toHaveCount(0);
});

test("조사가 실패하면 오류와 함께 다시 조사 시작", async ({ page }) => {
  await fakeRecon(page, NOV);
  const sse = [
    { event: "line", data: { line: AGENT.lines[0] } },
    {
      event: "error",
      data: { message: "조사 실패 — 다시 시작할 수 있습니다" },
    },
  ]
    .map((e) => `event: ${e.event}\ndata: ${JSON.stringify(e.data)}\n\n`)
    .join("");
  await page.route("**/api/finance/recon/aaaaaaaaaaaa/investigate", (r) =>
    r.fulfill({ contentType: "text/event-stream", body: sse }),
  );
  await page.goto("/finance?month=2026-11");
  await row(page, "2026-11-03").click();
  const item = page.getByRole("region", { name: /불일치 건/ });
  await item.getByRole("button", { name: "조사 시작" }).click();
  await expect(item).toContainText("조사 실패 — 다시 시작할 수 있습니다");
  await expect(item).not.toContainText("조사 중…");
  await expect(item.getByRole("button", { name: "조사 시작" })).toBeVisible();
});

test("다른 곳에서 조사 중인 건은 조사 중 표시 뒤 끝나면 결과", async ({
  page,
}) => {
  let first = 0; // 첫 요청 뒤 2초가 지나면 끝난 결과
  const running = {
    ...AGENT,
    status: "running",
    cause: null,
    runner_up: null,
    evidence: [],
    reason: null,
    lines: [AGENT.lines[0]],
  };
  await fakeRecon(page, () => {
    first ||= Date.now();
    return withAgent(Date.now() - first > 2_000 ? AGENT : running)();
  });
  await page.goto("/finance?month=2026-11");
  await row(page, "2026-11-03").click();
  const item = page.getByRole("region", { name: /불일치 건/ });
  await expect(item).toContainText("조사 중…");
  await expect(item.getByRole("button", { name: "조사 시작" })).toHaveCount(0);
  await expect(item).toContainText("에이전트: 취소 후 재승인", {
    timeout: 10_000,
  });
});

test("자동 마감 건은 배지와 되돌리기", async ({ page }) => {
  await fakeRecon(
    page,
    withAgent(AGENT, {
      id: 5,
      cause: "cancel_reapprove",
      reason: "규칙과 에이전트 원인이 같음 · 장부 수정 불필요",
      at: "2026-12-01 06:00:00",
    }),
  );
  let reverted = false;
  await page.route("**/api/auto-actions/5/revert", (r) => {
    reverted = true;
    return r.fulfill({ json: { ok: true } });
  });
  await page.goto("/finance?month=2026-11");
  await row(page, "2026-11-03").click();
  const item = page.getByRole("region", { name: /불일치 건/ });
  await expect(item).toContainText("자동 마감");
  await item.getByRole("button", { name: "되돌리기" }).click();
  await expect.poll(() => reverted).toBe(true);
});
