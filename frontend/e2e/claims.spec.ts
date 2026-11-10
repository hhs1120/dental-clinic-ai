// ⑤ 청구 점검 화면(설계 docs/superpowers/specs/2026-11-10-claim-review-design.md §1) — 실데이터엔 판단 기록·경계 사례가
// 없고 판단은 앱 DB에 남으므로 /api/claims·/api/claims/pre는 page.route 가짜 응답으로 준다
import { expect, type Page, test } from "@playwright/test";

type Judgment = { value: string; by: string; at: string } | null;

const base = {
  chart_no: "C-00001",
  service_date: "2026-08-01",
  tooth_no: "00",
  status: "adjusted",
  expired: false,
  judgment: null as Judgment,
};

const DOC = {
  ...base,
  claim_id: 11,
  chart_no: "C-00011",
  name: "김하나",
  tooth_no: "36",
  procedure_name: "신경치료",
  status: "denied",
  claim_amount: 12340,
  paid_amount: 0,
  unpaid: 12340,
  adjust_note: "내역설명 미기재로 인정 근거 없음",
  type: "documentation",
  reviewed_at: "2026-09-09",
  deadline: "2026-12-08",
  days_left: 7,
  chips: ["all", "soon", "denied"],
  suggestion: "resubmit",
  reason: "빠진 기재·증빙을 채워 다시 청구할 수 있습니다",
  fill_item: "내역설명 기재",
};
const DX = {
  ...base,
  claim_id: 13,
  name: "박세나",
  procedure_name: "스케일링",
  claim_amount: 60000,
  paid_amount: 30000,
  unpaid: 30000,
  adjust_note: "상병과 시행 행위 불일치",
  type: "diagnosis",
  reviewed_at: "2026-09-22",
  deadline: "2026-12-21",
  days_left: 20,
  chips: ["all", "diagnosis", "adjusted"],
  suggestion: "justified",
  reason: "상병 착오는 재청구할 수 없습니다 — 다음 청구 때 상병을 확인하세요",
  fill_item: null,
};
const REQ = {
  ...base,
  claim_id: 12,
  name: "이두리",
  procedure_name: "잇몸치료",
  claim_amount: 35000,
  paid_amount: 20000,
  unpaid: 15000,
  adjust_note: "치주치료 산정 기준 소견 미확인",
  type: "requirement",
  reviewed_at: "2026-10-10",
  deadline: "2027-01-08",
  days_left: 38,
  chips: ["all", "adjusted"],
  suggestion: null,
  reason: "차트에 요건 근거가 있으면 다툴 만함, 없으면 정당입니다",
  fill_item: null,
};
const OLD = {
  ...base,
  claim_id: 14,
  name: "최네오",
  tooth_no: "46",
  procedure_name: "아말감/GI",
  claim_amount: 25000,
  paid_amount: 15000,
  unpaid: 10000,
  adjust_note: "동일 부위 재시행 간격 기준 미충족",
  type: "requirement",
  reviewed_at: "2026-05-11",
  deadline: "2026-08-09",
  days_left: -114,
  expired: true,
  chips: ["expired"],
  suggestion: null,
  reason: "차트에 요건 근거가 있으면 다툴 만함, 없으면 정당입니다",
  fill_item: null,
  judgment: { value: "contestable", by: "원장", at: "2026-08-01 10:00:00" },
};

const CHIPS = ["all", "diagnosis", "soon", "denied", "adjusted", "expired"];

function sheet(rows: (typeof DOC | typeof DX | typeof REQ | typeof OLD)[]) {
  return {
    date: "2026-12-01",
    appeal_days: 90,
    chips: Object.fromEntries(
      CHIPS.map((c) => {
        const hit = rows.filter((r) => r.chips.includes(c));
        return [
          c,
          {
            count: hit.length,
            amount: hit.reduce((s, r) => s + r.unpaid, 0),
          },
        ];
      }),
    ),
    rows,
  };
}

const PRE = {
  counts: { lines: 2, warning: 1 },
  rows: [
    {
      claim_id: 21,
      service_date: "2026-11-12",
      chart_no: "C-00021",
      name: "정우리",
      procedure_name: "사랑니 발치",
      tooth_no: "38",
      claim_amount: 90000,
      dx_code: "D05",
      dx_name: "만성 치주염",
      warning: { allowed: ["매복치"], past_count: 2, past_amount: 22000 },
    },
    {
      claim_id: 22,
      service_date: "2026-11-10",
      chart_no: "C-00022",
      name: "한가람",
      procedure_name: "스케일링",
      tooth_no: "00",
      claim_amount: 60000,
      dx_code: "D06",
      dx_name: "치아 침착물(치석)",
      warning: null,
    },
  ],
};

// 개발 서버는 화면을 두 번 그려 조회가 두 번 갈 수 있다 — 응답은 요청 순번이 아니라 함수로 정한다
async function fakeClaims(page: Page, body: () => unknown) {
  await page.route("**/api/claims", (r) => r.fulfill({ json: body() }));
  await page.route("**/api/claims/pre", (r) => r.fulfill({ json: PRE }));
}

const ALL = [DOC, DX, REQ, OLD];
const table = (page: Page) => page.getByRole("table");
const bodyRows = (page: Page) => table(page).locator("tbody > tr[data-claim]");

test("메뉴에서 청구 점검이 열리고 기본은 삭감 대응", async ({ page }) => {
  await fakeClaims(page, () => sheet(ALL));
  await page.goto("/today");
  await page
    .getByRole("navigation", { name: "메뉴" })
    .getByRole("link", { name: "청구 점검" })
    .click();
  await expect(page).toHaveURL(/\/claims$/);
  await expect(page.getByRole("tab", { name: "삭감 대응" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await expect(page.getByText("준비 중")).toHaveCount(0);
  await expect(bodyRows(page)).toHaveCount(3);
});

test("5칸과 기한 표시", async ({ page }) => {
  await fakeClaims(page, () => sheet(ALL));
  await page.goto("/claims");
  await expect(table(page).getByRole("columnheader")).toHaveText([
    "재심 신청 기한",
    "환자 · 시술",
    "못 받은 돈",
    "왜 깎였나",
    "할 일",
  ]);
  const doc = bodyRows(page).filter({ hasText: "김하나" });
  await expect(doc).toContainText("12/08 · 7일 남음");
  await expect(doc).toContainText("30일 이내");
  await expect(doc).toContainText("₩12,340 (전액)");
  await expect(doc).toContainText("내역설명 미기재로 인정 근거 없음");
  await expect(doc).toContainText("보완 후 재청구(제안)");
  const req = bodyRows(page).filter({ hasText: "이두리" });
  await expect(req).toContainText("60일 이내");
  await expect(req).toContainText("차트 확인 필요");
  const dx = bodyRows(page).filter({ hasText: "박세나" });
  await expect(dx).toContainText("재청구 불가");
  await expect(dx).toContainText("정당(제안)");
  await expect(bodyRows(page).first()).toContainText("김하나"); // 서버 순서(기한 이른 순) 그대로
  for (const code of ["C", "justified", "U0001", "documentation", "denied"])
    await expect(page.getByText(code, { exact: true })).toHaveCount(0);
});

test("칩 건수와 합계", async ({ page }) => {
  await fakeClaims(page, () => sheet(ALL));
  await page.goto("/claims");
  await expect(page.getByText("못 받은 돈 합계 ₩57,340")).toBeVisible();
  await page.getByRole("button", { name: "신청 기한 30일 이내 1" }).click();
  await expect(page).toHaveURL(/\/claims\?chip=soon$/);
  await expect(bodyRows(page)).toHaveCount(1);
  await expect(page.getByText("못 받은 돈 합계 ₩12,340")).toBeVisible();
  await page.getByRole("button", { name: "기한 지남 1" }).click();
  await expect(page).toHaveURL(/chip=expired/);
  const old = bodyRows(page).filter({ hasText: "최네오" });
  await expect(old).toContainText("08/09 · 기한 지남");
  await expect(old).toContainText("다툴 만함 · 원장 08/01");
  await old.click();
  const detail = page.getByRole("region", { name: "최네오 청구 상세" });
  await expect(detail).toBeVisible();
  for (const name of ["정당", "다툴 만함", "보완 후 재청구"])
    await expect(detail.getByRole("button", { name, exact: true })).toHaveCount(
      0,
    );
});

test("상병 착오는 정당만", async ({ page }) => {
  await fakeClaims(page, () => sheet(ALL));
  await page.goto("/claims");
  await bodyRows(page).filter({ hasText: "박세나" }).click();
  const detail = page.getByRole("region", { name: "박세나 청구 상세" });
  await expect(
    detail.getByRole("button", { name: "정당", exact: true }),
  ).toHaveCount(1);
  await expect(
    detail.getByRole("button", { name: "다툴 만함", exact: true }),
  ).toHaveCount(0);
  await expect(
    detail.getByRole("button", { name: "보완 후 재청구", exact: true }),
  ).toHaveCount(0);
});

test("판단을 누르면 기록되고 할 일 칸이 바뀜", async ({ page }) => {
  let judged: Judgment = null;
  await fakeClaims(page, () =>
    sheet([DOC, DX, { ...REQ, judgment: judged }, OLD]),
  );
  let posted: unknown = null;
  await page.route("**/api/claims/12/judgment", async (r) => {
    posted = r.request().postDataJSON();
    judged = { value: "contestable", by: "데스크", at: "2026-11-09 10:00:00" };
    await r.fulfill({ json: { ok: true } });
  });
  await page.goto("/claims");
  const req = bodyRows(page).filter({ hasText: "이두리" });
  await req.click();
  const detail = page.getByRole("region", { name: "이두리 청구 상세" });
  await detail.getByRole("button", { name: "다툴 만함", exact: true }).click();
  await expect(req).toContainText("다툴 만함 · 데스크 11/09");
  expect(posted).toEqual({ judgment: "contestable", part: "데스크" });
});

test("보완 후 재청구 제안은 채울 항목", async ({ page }) => {
  await fakeClaims(page, () => sheet(ALL));
  await page.goto("/claims");
  await bodyRows(page).filter({ hasText: "김하나" }).click();
  const detail = page.getByRole("region", { name: "김하나 청구 상세" });
  await expect(detail).toContainText("채울 항목: 내역설명 기재");
  await expect(detail).toContainText("청구 ₩12,340 → 지급 ₩0");
  await expect(detail).toContainText("전액 불능");
});

test("청구 전 점검 — 경고 칩 기본, 과거 이력 문구", async ({ page }) => {
  await fakeClaims(page, () => sheet(ALL));
  await page.goto("/claims?tab=pre");
  await expect(page.getByRole("tab", { name: "청구 전 점검" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  const rows = table(page).locator("tbody > tr");
  await expect(rows).toHaveCount(1);
  await expect(rows.first()).toContainText("허용 상병 밖 — 이 시술은 매복치");
  await expect(rows.first()).toContainText(
    "지난 조정에서 이 조합 2건, ₩22,000 못 받음",
  );
  await page.getByRole("button", { name: "청구 전 2줄" }).click();
  await expect(rows).toHaveCount(2);
  await expect(page.getByRole("link", { name: "한가람" })).toHaveAttribute(
    "href",
    "/emr?chart=C-00022",
  );
  await expect(page.getByText("D05", { exact: true })).toHaveCount(0);
});
