// 떠 있는 물어보기 버튼·창과 메뉴(기능 정의 §4 앱 구성, 앱 설계 §9). 챗과 저장된 질문 읽기는 과금·키 없이
// page.route 가짜 응답으로 준다
import { expect, type Page, test } from "@playwright/test";

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
const QUESTION = "틀니 수리 며칠 걸려?";
const ANSWER = "접수 후 기공소로 보내 사흘 걸린다";

const fab = (page: Page) => page.getByRole("button", { name: "물어보기" });
const panel = (page: Page) =>
  page.getByRole("complementary", { name: "물어보기" });

/** 챗 SSE 가짜 — 보낸 횟수를 센다 */
async function fakeChat(page: Page) {
  const posts = { n: 0 };
  await page.route("**/api/chat", (r) => {
    posts.n++;
    return r.fulfill({
      contentType: "text/event-stream",
      body:
        `event: step\ndata: ${JSON.stringify({ tool: "search_docs" })}\n\n` +
        `event: answer\ndata: ${JSON.stringify({ log_id: 42, status: "answered", text: ANSWER, sources: [CARD] })}\n\n`,
    });
  });
  return posts;
}

test("메뉴는 업무 8개 순서이고 물어보기가 없다", async ({ page }) => {
  await page.goto("/today");
  const menu = page.getByRole("navigation", { name: "메뉴" });
  await expect(menu.getByRole("link")).toHaveText([
    "오늘",
    "예약",
    "준비표",
    "통화",
    "미예약 환자",
    "청구 점검",
    "재무 대조",
    "경영 리포트",
  ]);
});

test("버튼은 업무 화면·AI 평가에 있고 물어보기·전환 전 EMR에는 없다", async ({
  page,
}) => {
  await page.goto("/today");
  await expect(fab(page)).toBeVisible();
  await page.goto("/eval");
  await expect(fab(page)).toBeVisible();
  await page.goto("/ask");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("물어보기");
  await expect(fab(page)).toHaveCount(0);
  await page.goto("/emr");
  await expect(fab(page)).toHaveCount(0);
});

test("누르면 오른쪽 창이 열리고 메뉴를 옮겨도 남고 Esc·X로 닫힌다", async ({
  page,
}) => {
  await page.goto("/today");
  await fab(page).click();
  await expect(panel(page).getByLabel("질문 입력")).toBeVisible();
  await page
    .getByRole("navigation", { name: "메뉴" })
    .getByRole("link", { name: "예약", exact: true })
    .click();
  await expect(page).toHaveURL(/\/schedule$/);
  await expect(panel(page)).toBeVisible();
  await panel(page).getByLabel("질문 입력").focus();
  await page.keyboard.press("Escape");
  await expect(panel(page)).toHaveCount(0);
  await fab(page).click();
  await panel(page).getByRole("button", { name: "닫기" }).click();
  await expect(panel(page)).toHaveCount(0);
  await expect(fab(page)).toBeVisible();
});

test("창에서 물은 답을 전체 화면으로 — 다시 묻지 않고 같은 질문·답", async ({
  page,
}) => {
  const posts = await fakeChat(page);
  let reads = 0;
  await page.route("**/api/chat/42", (r) => {
    reads++;
    return r.fulfill({
      json: {
        log_id: 42,
        question: QUESTION,
        steps: ["search_docs"],
        status: "answered",
        text: ANSWER,
        sources: [CARD],
      },
    });
  });
  await page.goto("/today");
  await fab(page).click();
  await panel(page).getByLabel("질문 입력").fill(QUESTION);
  await panel(page).getByRole("button", { name: "묻기" }).click();
  await expect(panel(page)).toContainText(ANSWER);
  await expect(
    panel(page).getByRole("link", { name: "문서함에서 보기" }),
  ).toHaveAttribute("href", "/ask?tab=docs&doc=3");
  await panel(page).getByRole("link", { name: "전체 화면으로" }).click();
  await expect(page).toHaveURL(/\/ask\?tab=chat&log=42$/);
  await expect(panel(page)).toHaveCount(0);
  const chat = page.getByRole("tabpanel", { name: "질문하기" });
  await expect(chat.getByText(QUESTION)).toBeVisible();
  await expect(chat.getByText(ANSWER)).toBeVisible();
  await expect(chat.getByRole("heading", { name: "틀니 수리" })).toBeVisible();
  expect(reads).toBeGreaterThan(0);
  expect(posts.n).toBe(1);
});

test("없는 저장 질문이면 알리고 새로 묻지 않는다", async ({ page }) => {
  const posts = await fakeChat(page);
  await page.route("**/api/chat/999", (r) =>
    r.fulfill({ status: 404, json: { detail: "저장된 질문이 없습니다" } }),
  );
  await page.goto("/ask?tab=chat&log=999");
  await expect(page.getByText("저장된 질문이 없습니다")).toBeVisible();
  expect(posts.n).toBe(0);
});

test("창이 열려 있어도 확인 창은 그 위에 뜨고, 닫으면 창이 남는다", async ({
  page,
}) => {
  // 좁은 폭 — 창이 화면을 다 덮으므로 확인 창이 위에 있어야만 버튼을 누를 수 있다
  await page.setViewportSize({ width: 500, height: 800 });
  await page.route("**/api/missed", (r) =>
    r.fulfill({
      json: {
        date: "2026-12-01",
        page: 10,
        chip_counts: { scaling_unused: 1 },
        rows: [
          {
            patient_id: 1,
            chart_no: "C-00001",
            name: "환자1",
            age: 40,
            phone: "010-0000-0000",
            last_visit: "2026-06-01",
            last_hygiene: null,
            balance: 0,
            chips: ["scaling_unused"],
            signals: { months_since_visit: 6 },
            score: 99,
            rank: 1,
            contact: null,
          },
        ],
        rules: [],
      },
    }),
  );
  await page.goto("/missed");
  const contact = page.getByRole("button", { name: "연락 남기기" });
  await expect(contact).toHaveCount(1);
  await fab(page).click();
  await expect(panel(page)).toBeVisible();
  await contact.focus();
  await page.keyboard.press("Enter");
  const dialog = page.getByRole("dialog");
  await dialog.getByRole("button", { name: "문자" }).click();
  await expect(dialog.getByRole("button", { name: "문자" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(panel(page)).toBeVisible();
});

test("닫았다 열어도, 전체 화면에 갔다 와도 질문·답·초안이 남는다", async ({
  page,
}) => {
  await fakeChat(page);
  await page.route("**/api/chat/42", (r) =>
    r.fulfill({
      json: {
        log_id: 42,
        question: QUESTION,
        steps: [],
        status: "answered",
        text: ANSWER,
        sources: [],
      },
    }),
  );
  await page.goto("/today");
  await fab(page).click();
  const input = panel(page).getByLabel("질문 입력");
  await expect(input).toBeFocused();
  await input.fill(QUESTION);
  await input.press("Enter");
  await expect(panel(page)).toContainText(ANSWER);
  await input.fill("쓰다 만 질문");
  await panel(page).getByRole("button", { name: "닫기" }).click();
  await expect(fab(page)).toBeFocused();
  await fab(page).click();
  await expect(panel(page)).toContainText(ANSWER);
  await expect(input).toHaveValue("쓰다 만 질문");
  await panel(page).getByRole("link", { name: "전체 화면으로" }).click();
  await expect(page).toHaveURL(/\/ask\?tab=chat&log=42$/);
  await page.goBack();
  await expect(page).toHaveURL(/\/today$/);
  await fab(page).click();
  await expect(panel(page)).toContainText(ANSWER);
  await expect(input).toHaveValue("쓰다 만 질문");
});

test("답을 기다리는 동안 전체 화면으로는 꺼지고, 실패하면 이전 답을 열지 않는다", async ({
  page,
}) => {
  await fakeChat(page);
  await page.goto("/today");
  await fab(page).click();
  const input = panel(page).getByLabel("질문 입력");
  await input.fill(QUESTION);
  await input.press("Enter");
  await expect(panel(page)).toContainText(ANSWER);
  const full = panel(page).getByRole("link", { name: "전체 화면으로" });
  await expect(full).toHaveAttribute("href", "/ask?tab=chat&log=42");
  // 두 번째 질문 — 답을 붙잡아 두었다가 실패로 끝낸다
  let release = () => {};
  const held = new Promise<void>((ok) => (release = ok));
  await page.route("**/api/chat", async (r) => {
    await held;
    await r.fulfill({
      contentType: "text/event-stream",
      body: `event: error\ndata: ${JSON.stringify({ message: "답을 만들지 못했습니다" })}\n\n`,
    });
  });
  await input.fill("두 번째 질문");
  await input.press("Enter");
  await expect(panel(page).getByText("답 쓰는 중…")).toBeVisible();
  await expect(full).toHaveCount(0);
  release();
  await expect(panel(page).getByText("답을 만들지 못했습니다")).toBeVisible();
  await expect(full).toHaveAttribute("href", "/ask?tab=chat");
});

test("AI 평가 화면에서 창에 친 숫자·Enter는 판정·투표 단축키로 가지 않는다", async ({
  page,
}) => {
  const posts = await fakeChat(page);
  await page.goto("/eval", { waitUntil: "networkidle" });
  await expect(page.getByText("0/9 항목")).toBeVisible();
  await fab(page).click();
  const input = panel(page).getByLabel("질문 입력");
  await input.pressSequentially("01121");
  await input.press("Enter");
  await expect(panel(page)).toContainText(ANSWER);
  expect(posts.n).toBe(1);
  await expect(page.getByText("0/9 항목")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "다음 건 (Enter)" }),
  ).toHaveCount(0);

  await page.goto("/eval?tab=blind", { waitUntil: "networkidle" });
  await expect(page.getByRole("button", { name: /1이 나음/ })).toBeEnabled();
  await fab(page).click();
  await panel(page).getByLabel("질문 입력").pressSequentially("3");
  await expect(page.getByRole("button", { name: "다음 (Enter)" })).toHaveCount(
    0,
  );
});
