// ⑥ 경영 리포트 — 기간 탭·숫자 카드·AI 해석(설계 2026-11-13 리포트 §6). 해석은 과금이라 /report를 page.route 가짜 응답으로 준다
import { expect, type Page, test } from "@playwright/test";

const MONTHS = ["2026-09", "2026-10", "2026-11"];
const WEEKS = ["2026-11-16", "2026-11-23"];

const base = {
  sales: 65_495_000,
  collected: 61_193_140,
  patient_paid: 46_133_900,
  insurer_paid: 15_059_240,
  visits: 421,
  noshow: 27,
  noshow_rate: 6,
  new_patients: 54,
  "prev.sales": 47_500_000,
  "diff.sales": 17_995_000,
  "pct.sales": 38,
  "diff.new_patients": 3,
};

const monthlyMetrics = {
  ...base,
  material: 4_853_200,
  lab: 6_320_000,
  material_unconfirmed: 3,
  account_none: 2,
  account_none_amount: 93_300,
  profit_ex_labor: 44_105_940,
  dx_errors: 5,
  dx_lost: 261_990,
};

const POINTS = [
  {
    text: "11월 매출은 6,550만 원으로 10월보다 1,800만 원(38%) 늘었습니다.",
    used: ["sales", "diff.sales", "pct.sales"],
  },
];

function monthly(period: string, interpretation: unknown) {
  return {
    scope: "monthly",
    periods: MONTHS,
    period,
    prev_period: "2026-10",
    metrics: monthlyMetrics,
    interpretation,
    trend: MONTHS.map((p) => ({
      period: p,
      sales: p === period ? monthlyMetrics.sales : 50_000_000,
      collected: 1,
      noshow_rate: 6,
      new_patients: 50,
      material: 1,
      profit_ex_labor: 1,
    })),
    dx_top: { name: "임플란트 1차 × 만성 치은염", n: 1, lost: 206_850 },
  };
}

const weekly = {
  scope: "weekly",
  periods: WEEKS,
  period: "2026-11-23",
  prev_period: "2026-11-16",
  metrics: base,
  interpretation: null,
  trend: WEEKS.map((p) => ({
    period: p,
    sales: 1,
    collected: 1,
    noshow_rate: 6,
    new_patients: 5,
  })),
  dx_top: null,
};

// 일간 — 지난주 같은 요일(11/23)과 비교, 근거에 음수 증감·%p
const daily = {
  ...weekly,
  scope: "daily",
  periods: ["2026-11-27", "2026-11-30"],
  period: "2026-11-30",
  prev_period: "2026-11-23",
  metrics: { ...base, "pct.sales": -66 },
  interpretation: {
    points: [
      {
        text: "11월 30일 매출은 지난주 같은 요일보다 393만 원 줄었습니다.",
        used: ["diff.sales", "diff.noshow_rate", "diff.sales.implant"],
      },
    ],
    values: {
      "diff.sales": -3_935_000,
      "diff.noshow_rate": -12,
      "diff.sales.implant": -1_300_000,
    },
    model: "m",
    created_at: "2026-12-01T05:00:00",
    stale: false,
  },
  trend: ["2026-11-27", "2026-11-30"].map((p) => ({
    period: p,
    sales: 1,
    collected: 1,
    noshow_rate: 6,
    new_patients: 5,
  })),
};

async function fakeReport(page: Page, interpretation: unknown) {
  await page.route("**/api/report**", (r) => {
    const q = new URL(r.request().url()).searchParams;
    if (q.get("scope") === "weekly") return r.fulfill({ json: weekly });
    if (q.get("scope") === "daily") return r.fulfill({ json: daily });
    return r.fulfill({
      json: monthly(q.get("period") ?? "2026-11", interpretation),
    });
  });
}

test("월간 — AI 주목할 점이 맨 위, 숫자 카드, 가정값 숫자 없음", async ({
  page,
}) => {
  await fakeReport(page, {
    points: POINTS,
    values: { sales: 65_495_000, "diff.sales": 17_995_000, "pct.sales": 38 },
    model: "m",
    created_at: "2026-12-01T05:00:00",
    stale: false,
  });
  await page.goto("/report");
  await expect(page.getByText("65,495,000원", { exact: true })).toBeVisible();
  await expect(page.getByText("과목 없는 출금 93,300원 제외")).toBeVisible();
  await expect(page.getByText("미확정 3건 포함")).toBeVisible();
  await expect(page.getByText("[추정]")).toHaveCount(0);
  const ai = await page
    .getByRole("heading", { name: "AI가 고른 주목할 점" })
    .boundingBox();
  const stat = await page.getByText("매출(진료 생산)").boundingBox();
  expect(ai!.y).toBeLessThan(stat!.y);
  await expect(page.getByText(POINTS[0].text)).toBeVisible();
  // 근거는 접혀 있다가 펼치면 한국어 이름·단위로(코드 키를 보이지 않는다)
  await expect(page.getByText("매출 증감 +17,995,000원")).toBeHidden();
  await page.getByText("근거 보기", { exact: true }).click();
  await expect(page.getByText("매출 증감 +17,995,000원")).toBeVisible();
  await expect(page.getByText("매출 증감률 +38%")).toBeVisible();
  await expect(page.getByText("10월 대비 +38%")).toBeVisible();
  await expect(page.getByText(/diff\.sales/)).toHaveCount(0);
  await expect(page.getByText("다시 만들기 필요")).toHaveCount(0);
  await expect(page.getByText("임플란트 1차 × 만성 치은염")).toBeVisible();
});

test("해석 없음·숫자가 바뀐 해석", async ({ page }) => {
  await fakeReport(page, null);
  await page.goto("/report");
  await expect(page.getByText(/해석 없음/)).toBeVisible();
  await page.unroute("**/api/report**");
  await fakeReport(page, {
    points: POINTS,
    values: { sales: 65_495_000, "diff.sales": 17_995_000, "pct.sales": 38 },
    model: "m",
    created_at: "2026-12-01T05:00:00",
    stale: true,
  });
  await page.goto("/report?period=2026-10");
  await expect(page.getByText(/다시 만들기 필요/)).toBeVisible();
});

test("주간 탭에는 월간 카드가 없고, 추이 표 행을 누르면 그 기간", async ({
  page,
}) => {
  await fakeReport(page, null);
  await page.goto("/report");
  await page.getByRole("tab", { name: "주간" }).click();
  await expect(page).toHaveURL(/scope=weekly/);
  await expect(page.getByText("인건비 제외 손익")).toHaveCount(0);
  await page.getByRole("tab", { name: "월간" }).click();
  await page.getByRole("row", { name: /9월/ }).click();
  await expect(page).toHaveURL(/period=2026-09/);
});

test("일간 — 지난주 같은 요일과 비교, 음수·%p 근거", async ({ page }) => {
  await fakeReport(page, null);
  await page.goto("/report?scope=daily");
  await expect(
    page.getByText("지난주 같은 요일(11/23) 대비 -66%"),
  ).toBeVisible();
  await page.getByText("근거 보기", { exact: true }).click();
  await expect(page.getByText("매출 증감 -3,935,000원")).toBeVisible();
  await expect(page.getByText("노쇼율 증감 -12%p")).toBeVisible();
  await expect(page.getByText("임플란트 매출 증감 -1,300,000원")).toBeVisible();
});
