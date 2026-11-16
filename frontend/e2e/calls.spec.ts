// 통화 상세 — 처리안의 "과정 보기"(앱 설계 §9). 복사본 app.sqlite에 저장된 처리안 기록을 그대로 읽는다
import { expect, test } from "@playwright/test";

test("과정 보기 — 조회 조건은 영어 값이 아니라 이름으로", async ({ page }) => {
  // c-0450: 처리안이 예약 조회(2026-11-28, 위생)를 불렀다
  await page.goto("/calls?call=c-0450");
  await page.getByRole("button", { name: /과정 보기/ }).click();
  await expect(page.getByText("예약 조회 — 11-28 · 위생")).toBeVisible();
  await expect(page.getByText(/hygiene/)).toHaveCount(0);
});

// 상세 응답을 가로채 칸 하나를 바꾼다 — 실데이터는 자동 종결·사람 판정이 0건. 띠(review·autoclose)를 통째로 정해 주므로
// 같이 도는 eval.spec이 이 통화를 판정해도 결과가 달라지지 않는다
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

test("이미 확인된 통화에 확인함 — 오류 띠 없이 띠가 사라짐", async ({
  page,
}) => {
  // 다른 탭에서 먼저 확인함 — 서버는 404, 다시 불러온 상세에는 확인할 통화가 없다
  let posted = false;
  await patchDetail(page, "c-0450", (d) => ({
    ...d,
    review: posted
      ? null
      : { ai: "none", reopened: false, flags: ["emergency"] },
  }));
  await page.route("**/api/calls/c-0450/check", (r) => {
    posted = true;
    return r.fulfill({
      status: 404,
      json: { detail: "확인할 통화가 아닙니다" },
    });
  });
  await page.goto("/calls?call=c-0450");
  const detail = page.getByRole("complementary", { name: "통화 상세" });
  await detail.getByRole("button", { name: "확인함" }).click();
  await expect(detail).not.toContainText("사람 판정");
  await expect(page.getByText("확인할 통화가 아닙니다")).toHaveCount(0); // 오류 띠 없음
});

// 이 환자의 지난 통화(기능 정의 §4 ①) — C-01886은 판정 풀 밖 통화 5건(c-1066이 가장 최근)
test("지난 통화 — 최근 3건, 누르면 그 통화로, 더 있으면 전체 보기", async ({
  page,
}) => {
  await page.goto("/calls?tab=history&call=c-1066");
  const past = page.getByRole("list", { name: "이 환자의 지난 통화" });
  await expect(past.getByRole("listitem")).toHaveCount(3);
  await past.getByRole("button").first().click();
  await expect(page).toHaveURL(/call=c-1341/);
  // 건수는 지금 연 통화까지 — 열린 통화 기록의 건수와 같고, 보던 통화 패널은 열린 채
  await page
    .getByRole("link", { name: "통화 기록에서 전체 보기(5건)" })
    .click();
  await expect(page).toHaveURL(/q=C-01886/);
  await expect(page).toHaveURL(/call=c-1341/);
  const search = page.getByLabel("통화 검색");
  await expect(search).toHaveValue("C-01886");
  await expect(
    page.getByRole("complementary", { name: "통화 상세" }),
  ).toBeVisible();
  // 검색창을 고친 뒤 같은 링크를 다시 누르면 검색어가 다시 차트번호로
  await search.fill("김");
  await expect(page).not.toHaveURL(/q=/);
  await page
    .getByRole("link", { name: "통화 기록에서 전체 보기(5건)" })
    .click();
  await expect(search).toHaveValue("C-01886");
});

test("예약표 환자 카드 — 잡은 통화 링크·응급 여부·최근 통화", async ({
  page,
}) => {
  // 예약 11885(12/1, 신서준 C-02757)는 판정 풀 밖 통화 c-0319(응급 아님)로 잡혔다
  await page.goto("/schedule?date=2026-12-01&appt=11885");
  const card = page.getByLabel("환자 요약");
  await expect(card.getByRole("list", { name: "최근 통화" })).toBeVisible();
  await expect(card.getByText("응급 여부")).toContainText("AI");
  await expect(card.getByText("응급 여부")).toContainText("아님");
  await card.getByRole("link", { name: /통화 있음/ }).click();
  await expect(page).toHaveURL(/\/calls\?tab=history&call=c-0319/);
});
