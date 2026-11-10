// ⑨ 미예약 환자 화면(설계 docs/superpowers/specs/2026-11-10-missed-patients-design.md §6) — 목록 모양은 쿼리로 정해지고
// 연락 기록은 앱 DB에 남으므로 /api/missed는 page.route 가짜 응답으로 준다
import { expect, type Page, test } from "@playwright/test";

type Row = {
  patient_id: number;
  chart_no: string;
  name: string;
  age: number;
  phone: string;
  last_visit: string;
  last_hygiene: string | null;
  balance: number;
  chips: string[];
  signals: Record<string, number | boolean>;
  score: number;
  rank: number;
  contact: {
    result: string;
    channel: string;
    by: string;
    at: string;
    active: boolean;
  } | null;
};

const CHIPS = [
  "recall",
  "scaling_unused",
  "noshow",
  "balance",
  "open_plan",
  "other_clinic",
];

function row(i: number): Row {
  return {
    patient_id: i,
    chart_no: `C-${String(i).padStart(5, "0")}`,
    name: `환자${i}`,
    age: 40,
    phone: "010-0000-0000",
    last_visit: "2026-06-01",
    last_hygiene: null,
    balance: i % 3 === 0 ? 30000 : 0,
    chips: i % 3 === 0 ? ["scaling_unused", "balance"] : ["scaling_unused"],
    signals: { months_since_visit: 6 },
    score: 100 - i,
    rank: i,
    // 2: 부재중(감점 기간 안 — 연락함 숨기기 대상), 5: 나중에 다시(기간 지남 — 숨기지 않음)
    contact:
      i === 2
        ? {
            result: "no_answer",
            channel: "call",
            by: "데스크",
            at: "2026-11-09 15:20:00",
            active: true,
          }
        : i === 5
          ? {
              result: "later",
              channel: "call",
              by: "데스크",
              at: "2026-10-01 10:00:00",
              active: false,
            }
          : null,
  };
}

function sheet(rows: Row[], page = 10) {
  return {
    date: "2026-12-01",
    page,
    chip_counts: Object.fromEntries(
      CHIPS.map((c) => [c, rows.filter((r) => r.chips.includes(c)).length]),
    ),
    rows: rows.map((r, i) => ({ ...r, rank: i + 1 })),
    rules: [
      {
        name: "진료계획 미예약",
        value: "+30점",
        reasons: ["진단과 계획까지 받은 치료라 예약으로 이어지기 쉽다"],
      },
    ],
  };
}

const ROWS = Array.from({ length: 25 }, (_, i) => row(i + 1));

// 개발 서버는 화면을 두 번 그려 조회가 두 번 갈 수 있다 — 응답은 요청 순번이 아니라 함수로 정한다
async function fakeMissed(page: Page, body: () => unknown) {
  await page.route("**/api/missed", (r) => r.fulfill({ json: body() }));
}

const table = (page: Page) => page.getByRole("table");
const names = (page: Page) =>
  table(page)
    .getByRole("link", { name: /^환자\d+$/ })
    .allTextContents();

test("처음 10명, 더보기로 10명씩", async ({ page }) => {
  await fakeMissed(page, () => sheet(ROWS));
  await page.goto("/missed");
  await expect(
    table(page).getByRole("link", { name: /^환자\d+$/ }),
  ).toHaveCount(10);
  await page.getByRole("button", { name: /더보기/ }).click();
  await expect(
    table(page).getByRole("link", { name: /^환자\d+$/ }),
  ).toHaveCount(20);
  await page.getByRole("button", { name: /더보기/ }).click();
  await expect(
    table(page).getByRole("link", { name: /^환자\d+$/ }),
  ).toHaveCount(25);
  await expect(page.getByRole("button", { name: /더보기/ })).toHaveCount(0);
  await expect(page.getByText("1순위", { exact: true })).toHaveCount(0);
  await expect(
    table(page).getByRole("columnheader", { name: "전체 순위" }),
  ).toBeVisible();
  await expect(
    table(page).getByRole("link", { name: "환자1", exact: true }),
  ).toHaveAttribute("href", "/emr?chart=C-00001");
});

test("처음·더보기 수는 응답의 page", async ({ page }) => {
  await fakeMissed(page, () => sheet(ROWS, 4));
  await page.goto("/missed");
  await expect(
    table(page).getByRole("link", { name: /^환자\d+$/ }),
  ).toHaveCount(4);
  await page.getByRole("button", { name: /더보기/ }).click();
  await expect(
    table(page).getByRole("link", { name: /^환자\d+$/ }),
  ).toHaveCount(8);
});

test("칩 필터와 인원수", async ({ page }) => {
  await fakeMissed(page, () => sheet(ROWS));
  await page.goto("/missed");
  const chip = page.getByRole("button", { name: /^미수 잔액 8$/ });
  await expect(page.getByRole("button", { name: /^전체 25$/ })).toBeVisible();
  await expect(
    page.getByRole("button", { name: /^올해 스케일링 급여 미사용 25$/ }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: /^다른 치과 추정 0$/ }),
  ).toBeVisible();
  await chip.click();
  await expect(page).toHaveURL(/\/missed\?chip=balance$/);
  await expect(
    table(page).getByRole("link", { name: /^환자\d+$/ }),
  ).toHaveCount(8);
  expect(await names(page)).toEqual([
    "환자3",
    "환자6",
    "환자9",
    "환자12",
    "환자15",
    "환자18",
    "환자21",
    "환자24",
  ]);
});

test("연락함 숨기기 — 연락 효과가 남은 환자만", async ({ page }) => {
  await fakeMissed(page, () => sheet(ROWS));
  await page.goto("/missed");
  await expect(table(page)).toContainText("부재중·안 받음");
  await page.getByRole("button", { name: /^연락함 숨기기 1$/ }).click();
  await expect(page).toHaveURL(/\/missed\?hide=1$/);
  await expect(table(page)).not.toContainText("부재중·안 받음");
  expect(await names(page)).not.toContain("환자2");
  expect(await names(page)).toContain("환자5"); // 나중에 다시 기간이 지남 — 다시 연락할 차례
  await expect(table(page)).toContainText("나중에 다시");
  await expect(
    table(page).getByRole("link", { name: /^환자\d+$/ }),
  ).toHaveCount(10);
});

test("연락 결과 남기면 다시 조회", async ({ page }) => {
  let rows = ROWS;
  const posted: { url: string; body: unknown }[] = [];
  await fakeMissed(page, () => sheet(rows));
  await page.route("**/api/missed/*/contacts", async (r) => {
    posted.push({ url: r.request().url(), body: r.request().postDataJSON() });
    rows = ROWS.map((x) =>
      x.patient_id === 1
        ? {
            ...x,
            contact: {
              result: "later",
              channel: "text",
              by: "데스크",
              at: "2026-11-10 10:05:00",
              active: true,
            },
          }
        : x,
    );
    await r.fulfill({ json: { ok: true } });
  });
  await page.goto("/missed");
  const first = table(page).getByRole("row").nth(1);
  await first.getByRole("button", { name: "연락 남기기" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByRole("button", { name: "문자" }).click();
  await dialog.getByRole("button", { name: "나중에 다시" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(first).toContainText("나중에 다시");
  await expect(first).toContainText("문자 · 11/10 10:05");
  expect(posted).toHaveLength(1);
  expect(posted[0].url).toMatch(/\/api\/missed\/1\/contacts$/);
  expect(posted[0].body).toEqual({
    result: "later",
    channel: "text",
    part: "데스크",
  });
});

test("더보기 뒤 연락함 — 중복 없이 다시 조회", async ({ page }) => {
  let rows = ROWS;
  await fakeMissed(page, () => sheet(rows));
  await page.route("**/api/missed/*/contacts", async (r) => {
    rows = ROWS.filter((x) => x.patient_id !== 3); // 예약함 — 목록에서 빠지고 순위가 당겨진다
    await r.fulfill({ json: { ok: true } });
  });
  await page.goto("/missed");
  await page.getByRole("button", { name: /더보기/ }).click();
  await expect(
    table(page).getByRole("link", { name: /^환자\d+$/ }),
  ).toHaveCount(20);
  const third = table(page).getByRole("row").nth(3);
  await third.getByRole("button", { name: "연락 남기기" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByRole("button", { name: "예약함" }).click();
  await dialog.getByRole("button", { name: "빼기" }).click();
  await expect(
    table(page).getByRole("link", { name: "환자3", exact: true }),
  ).toHaveCount(0);
  await expect(
    table(page).getByRole("link", { name: /^환자\d+$/ }),
  ).toHaveCount(20);
  const shown = await names(page);
  expect(new Set(shown).size).toBe(shown.length);
  expect(shown.at(-1)).toBe("환자21");
});

test("목록에서 빠지는 결과는 한 번 더 묻는다", async ({ page }) => {
  const posted: unknown[] = [];
  await fakeMissed(page, () => sheet(ROWS));
  await page.route("**/api/missed/*/contacts", async (r) => {
    posted.push(r.request().postDataJSON());
    await r.fulfill({ json: { ok: true } });
  });
  await page.goto("/missed");
  const first = table(page).getByRole("row").nth(1);
  const dialog = page.getByRole("dialog");
  for (const result of ["예약함", "안 오겠다고 함", "다른 치과 다님"]) {
    await first.getByRole("button", { name: "연락 남기기" }).click();
    await dialog.getByRole("button", { name: result }).click();
    await expect(dialog).toContainText("환자1님을 목록에서 뺄까요?");
    await dialog.getByRole("button", { name: "취소" }).click();
    await expect(dialog).not.toContainText("목록에서 뺄까요?");
    await expect(dialog.getByRole("button", { name: result })).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
  }
  expect(posted).toHaveLength(0);
  await first.getByRole("button", { name: "연락 남기기" }).click();
  await dialog.getByRole("button", { name: "안 오겠다고 함" }).click();
  await dialog.getByRole("button", { name: "빼기" }).click();
  await expect(dialog).toHaveCount(0);
  expect(posted).toEqual([
    { result: "declined", channel: "call", part: "데스크" },
  ]);
});

test("순위 설명 펼치기", async ({ page }) => {
  await fakeMissed(page, () => sheet(ROWS));
  await page.goto("/missed");
  const how = page.getByRole("button", { name: "순위는 어떻게 정해지나요?" });
  await expect(how).toHaveAttribute("aria-expanded", "false");
  await how.click();
  await expect(how).toHaveAttribute("aria-expanded", "true");
  await expect(page.getByText("진단과 계획까지 받은 치료라")).toBeVisible();
  await expect(page.getByText("+30점")).toBeVisible();
});

test("메뉴 이름 미예약 환자", async ({ page }) => {
  await fakeMissed(page, () => sheet(ROWS));
  await page.goto("/missed");
  await expect(
    page.getByRole("link", { name: "미예약 환자", exact: true }),
  ).toHaveAttribute("href", "/missed");
  await expect(page.getByRole("link", { name: "후속 관리" })).toHaveCount(0);
  await expect(page.getByRole("heading", { level: 1 })).toHaveText(
    "미예약 환자",
  );
});
