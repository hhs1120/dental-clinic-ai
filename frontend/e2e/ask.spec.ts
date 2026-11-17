// 물어보기 — 문서함·기록 검색 요약·탭 연결(설계 2026-11-09 검색 요약 "화면"). 문서함은 복사본 source.sqlite를 그대로 읽고,
// 검색·요약·챗은 과금·키 없이 돌게 page.route로 가짜 응답을 준다
import { expect, type Page, test } from "@playwright/test";

const api = "http://localhost:8100";

test("문서함은 공지 칩으로 열리고 칩을 바꾸면 그 종류만 보인다", async ({
  page,
}) => {
  await page.goto("/ask?tab=docs");
  await expect(
    page.getByRole("button", { name: /^공지 \d+$/ }),
  ).toHaveAttribute("aria-pressed", "true");
  await page.getByRole("button", { name: /^매뉴얼 \d+$/ }).click();
  await expect(page).toHaveURL(/tab=docs&doc=\d+/);
  await expect(
    page.getByRole("article").getByText("매뉴얼", { exact: true }),
  ).toBeVisible();
});

test("옛 버전 공지를 열면 최신 보기 링크가 있다", async ({ page }) => {
  const { documents } = await (
    await page.request.get(`${api}/documents`)
  ).json();
  let old: number | null = null;
  let latest: number | null = null;
  for (const d of documents.filter(
    (x: { type: string }) => x.type === "notice",
  )) {
    const detail = await (
      await page.request.get(`${api}/documents/${d.id}`)
    ).json();
    if (detail.versions.length > 1) {
      old = detail.versions[1].id;
      latest = detail.latest_id;
      break;
    }
  }
  expect(old).not.toBeNull();
  await page.goto(`/ask?tab=docs&doc=${old}`);
  await page.getByRole("link", { name: /^옛 버전 — 최신 v.+ 보기$/ }).click();
  await expect(page).toHaveURL(new RegExp(`doc=${latest}$`));
});

test("문서를 바꿔 여는 동안 목록은 그 종류에 머문다", async ({ page }) => {
  const { documents } = await (
    await page.request.get(`${api}/documents`)
  ).json();
  const manuals = documents.filter(
    (x: { type: string }) => x.type === "manual",
  );
  let release = () => {};
  const held = new Promise<void>((r) => (release = r));
  await page.route(`**/api/documents/${manuals[1].id}`, async (r) => {
    await held;
    await r.continue();
  });
  await page.goto(`/ask?tab=docs&doc=${manuals[0].id}`);
  const nav = page.getByRole("navigation", { name: "문서 목록" });
  await expect(nav).toContainText(`매뉴얼 ${manuals.length}`);
  await nav.getByRole("link", { name: manuals[1].title }).click();
  await expect(page.getByText("불러오는 중…")).toBeVisible();
  await expect(nav).toContainText(`매뉴얼 ${manuals.length}`);
  release();
});

const CARD = {
  id: "doc:3:0",
  source_type: "document",
  title: "틀니 수리",
  body: "접수 후 기공소로 보낸다",
  event_date: null,
  chart_no: null,
  patient_name: null,
  caller_name: null,
  direction: null,
  doc_type: "manual",
  version: "1",
  is_outdated: false,
};

async function fakeSearch(page: Page, empty = false) {
  await page.route("**/api/search?*", (r) =>
    r.fulfill({
      json: { manual_results: empty ? [] : [CARD], case_results: [] },
    }),
  );
}

test("요약 본문 번호와 출처 칩 번호가 맞는다", async ({ page }) => {
  await fakeSearch(page);
  await page.route("**/api/search/summary", (r) =>
    r.fulfill({
      json: {
        status: "answered",
        text: "접수 후 기공소로 보낸다[1].",
        sources: [CARD],
      },
    }),
  );
  await page.goto("/ask?tab=search&q=틀니");
  const summary = page.getByRole("region", { name: "AI 요약" });
  await expect(summary).toContainText("접수 후 기공소로 보낸다1.");
  await expect(summary.getByRole("link", { name: /^1 · / })).toHaveAttribute(
    "href",
    /tab=docs&doc=3/,
  );
});

test("빈 결과면 요약을 부르지 않는다", async ({ page }) => {
  await fakeSearch(page, true);
  let calls = 0;
  await page.route("**/api/search/summary", (r) => {
    calls++;
    return r.fulfill({ json: { status: "refused", text: "", sources: [] } });
  });
  await page.goto("/ask?tab=search&q=틀니");
  await expect(page.getByText("결과가 없습니다.")).toBeVisible();
  await expect(page.getByRole("region", { name: "AI 요약" })).toHaveCount(0);
  expect(calls).toBe(0);
});

test("자료실 제목, 질문하기 탭 없음, AI 비서에게 묻기", async ({ page }) => {
  await fakeSearch(page);
  await page.route("**/api/search/summary", (r) =>
    r.fulfill({
      json: { status: "failed", text: "요약을 만들지 못했습니다", sources: [] },
    }),
  );
  await page.goto("/ask?tab=search&q=틀니");
  await expect(
    page.getByRole("heading", { name: "자료실", level: 1 }),
  ).toBeVisible();
  await expect(page.getByRole("tab", { name: "질문하기" })).toHaveCount(0);
  await expect(page.getByRole("tab")).toHaveCount(0);
  const menu = page.getByRole("navigation", { name: "메뉴" });
  await expect(menu.getByRole("link", { name: "기록 검색" })).toHaveAttribute(
    "aria-current",
    "page",
  );
  await expect(menu.getByRole("link", { name: "문서함" })).toBeVisible();
  await expect(page.getByRole("region", { name: "AI 요약" })).toContainText(
    "요약을 만들지 못했습니다",
  );
  const ask = page.getByRole("link", { name: "AI 비서에게 묻기" });
  await expect(ask).toHaveAttribute(
    "href",
    `/?${new URLSearchParams({ prefill: "틀니" })}`,
  );
});

test("검색 한 번에 요약은 한 번만 부르고 탭을 오가도 다시 부르지 않는다", async ({
  page,
}) => {
  await fakeSearch(page);
  let calls = 0;
  await page.route("**/api/search/summary", (r) => {
    calls++;
    return r.fulfill({
      json: { status: "answered", text: "접수한다[1].", sources: [CARD] },
    });
  });
  await page.goto("/ask?tab=search&q=틀니");
  const summary = page.getByRole("region", { name: "AI 요약" });
  await expect(summary).toContainText("접수한다");
  // 탭은 URL로 바뀐다 — 앞 전환이 끝나기 전에 누르면 그 클릭이 묻힌다
  const menu = page.getByRole("navigation", { name: "메뉴" });
  const docsTab = menu.getByRole("link", { name: "문서함" });
  await docsTab.click();
  await expect(docsTab).toHaveAttribute("aria-current", "page");
  await menu.getByRole("link", { name: "기록 검색" }).click();
  await expect(summary).toContainText("접수한다");
  expect(calls).toBe(1);
});

test("목록 칸의 기록 검색·문서함 링크로 전환, 상단 탭 없음", async ({
  page,
}) => {
  await page.goto("/ask");
  const menu = page.getByRole("navigation", { name: "메뉴" });
  const search = menu.getByRole("link", { name: "기록 검색" });
  const docs = menu.getByRole("link", { name: "문서함" });
  await expect(search).toHaveAttribute("aria-current", "page");
  await expect(page.getByRole("tab")).toHaveCount(0);
  await docs.click();
  await expect(page).toHaveURL(/tab=docs/);
  await expect(docs).toHaveAttribute("aria-current", "page");
  await expect(search).not.toHaveAttribute("aria-current", "page");
});
