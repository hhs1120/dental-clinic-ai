// AI 비서 화면(AI 비서 설계 §3.2) — 모델·DB를 쓰지 않게 page.route로 가짜 응답을 준다
import { expect, type Page, test } from "@playwright/test";

const sse = (events: [string, object][]) =>
  events
    .map(([k, d]) => `event: ${k}\ndata: ${JSON.stringify(d)}\n\n`)
    .join("");

const DRAFT = {
  id: 7,
  scheduled_at: "2026-12-01 10:00:00",
  duration_min: 30,
  type: "treatment",
  chair_no: 2,
  status: "draft",
  reason: null,
  source_call_uid: "c-1",
  decided_by: null,
  decided_at: null,
  chart_no: "C0001",
  name: "김영철",
  staff: "이원장",
  staff_role: "원장",
  procedure: "스케일링",
};

const REPLY = (extra: object = {}) => ({
  conversation_id: 5,
  seq: 2,
  status: "ok",
  text: "예약을 원했는데 안 잡힌 분은 1명입니다.",
  cards: [{ type: "draft", id: "7", data: DRAFT }],
  tool_calls: [],
  run_id: 1,
  ...extra,
});

async function fakeShell(page: Page) {
  await page.route("**/api/assistant/conversations", (r) =>
    r.fulfill({ json: [] }),
  );
}

async function fakeTurn(page: Page, bodies: object[] = []) {
  await page.route("**/api/assistant/turn", (r) => {
    bodies.push(r.request().postDataJSON());
    return r.fulfill({
      contentType: "text/event-stream",
      body: sse([
        ["step", { type: "step", tool: "unbooked_requests", args: {} }],
        ["reply", { type: "reply", ...REPLY() }],
      ]),
    });
  });
}

test("assistant: 칩을 누르면 질문이 보내지고 단계 줄 뒤 답", async ({
  page,
}) => {
  await fakeShell(page);
  const bodies: Record<string, unknown>[] = [];
  await fakeTurn(page, bodies);
  await page.goto("/");
  await expect(
    page.getByRole("button", { name: /^(📞|🩺|🧾|💳|☎️)/ }),
  ).toHaveCount(5);
  await expect(
    page.getByText("조회하고 예약 초안까지 — 확정은 사람이 합니다"),
  ).toBeVisible();
  await page
    .getByRole("button", { name: /지난주 전화로 예약 원했는데/ })
    .click();
  await expect(
    page.getByText("📞 지난주 전화로 예약 원했는데 안 잡힌 사람"),
  ).toBeVisible();
  await expect(page.getByText("조회 1번")).toBeVisible();
  await expect(
    page.getByText("예약을 원했는데 안 잡힌 분은 1명입니다."),
  ).toBeVisible();
  expect(bodies[0]).toMatchObject({ conversation_id: null, part: "데스크" });
  await expect(page).toHaveURL(/\/\?c=5$/);
});

test("assistant: 초안 승인 → 확정 표시", async ({ page }) => {
  await fakeShell(page);
  await fakeTurn(page);
  await page.route("**/api/assistant/drafts/7/approve", (r) =>
    r.fulfill({
      json: {
        ok: true,
        card: {
          type: "draft",
          id: "7",
          data: { ...DRAFT, status: "approved", decided_by: "데스크" },
        },
      },
    }),
  );
  await page.goto("/?draft=" + encodeURIComponent("예약 원한 사람 초안"));
  await expect(page.getByText("AI", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "승인" }).click();
  await expect(page.getByText("확정", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("link", { name: "일정에서 보기" }),
  ).toHaveAttribute("href", "/schedule?date=2026-12-01");
  await expect(page.getByRole("button", { name: "승인" })).toHaveCount(0);
});

test("assistant: 승인 실패 이유 표시", async ({ page }) => {
  await fakeShell(page);
  await fakeTurn(page);
  await page.route("**/api/assistant/drafts/7/approve", (r) =>
    r.fulfill({
      json: {
        ok: false,
        reasons: ["staff_busy"],
        card: {
          type: "draft",
          id: "7",
          data: { ...DRAFT, status: "rejected", reason: "staff_busy" },
        },
      },
    }),
  );
  await page.goto("/?draft=" + encodeURIComponent("예약 원한 사람 초안"));
  await page.getByRole("button", { name: "승인" }).click();
  await expect(page.getByText(/승인하지 못했어요 — .*담당/)).toBeVisible();
  await page.getByRole("button", { name: "다른 시간으로 다시 만들기" }).click();
  await expect(page.getByLabel("말 입력")).toHaveValue("다른 시간으로");
});

test("assistant: 지난 대화 열기는 저장된 그대로", async ({ page }) => {
  await page.route("**/api/assistant/conversations", (r) =>
    r.fulfill({
      json: [{ id: 5, title: "미예약 요청", part: "데스크", created_at: "x" }],
    }),
  );
  let turns = 0;
  await page.route("**/api/assistant/turn", (r) => {
    turns++;
    return r.abort();
  });
  await page.route("**/api/assistant/conversations/5", (r) =>
    r.fulfill({
      json: {
        id: 5,
        title: "미예약 요청",
        messages: [
          {
            seq: 1,
            role: "user",
            text: "지난주 예약 원한 사람",
            cards: [],
            tool_calls: [],
          },
          {
            seq: 2,
            role: "assistant",
            text: "저장된 답입니다.",
            tool_calls: [{ name: "find_calls", args: {}, ok: true }],
            cards: [{ type: "draft", id: "7", data: DRAFT }],
          },
        ],
      },
    }),
  );
  await page.goto("/");
  await page.getByRole("link", { name: "미예약 요청" }).click();
  await expect(page).toHaveURL(/\/\?c=5$/);
  await expect(page.getByText("지난주 예약 원한 사람")).toBeVisible();
  await expect(page.getByText("저장된 답입니다.")).toBeVisible();
  await expect(page.getByText("조회 1번")).toBeVisible();
  await expect(page.getByRole("button", { name: "승인" })).toBeVisible();
  expect(turns).toBe(0);
});

test("assistant: ?draft= 로 바로 보냄, ?prefill= 은 채우기만", async ({
  page,
}) => {
  await fakeShell(page);
  const bodies: Record<string, unknown>[] = [];
  await fakeTurn(page, bodies);
  await page.goto("/?draft=" + encodeURIComponent("재심 낼 만한 건"));
  await expect(page.getByText("재심 낼 만한 건")).toBeVisible();
  await expect(
    page.getByText("예약을 원했는데 안 잡힌 분은 1명입니다."),
  ).toBeVisible();
  expect(bodies).toHaveLength(1);
  expect(bodies[0]).toMatchObject({ text: "재심 낼 만한 건" });

  const before = bodies.length;
  await page.goto("/?prefill=" + encodeURIComponent("틀니 수리 며칠"));
  await expect(page.getByLabel("말 입력")).toHaveValue("틀니 수리 며칠");
  expect(bodies.length).toBe(before);
});

test("assistant: 답을 기다리는 중 다른 대화를 열면 옛 답이 섞이지 않는다", async ({
  page,
}) => {
  await page.route("**/api/assistant/conversations", (r) =>
    r.fulfill({
      json: [{ id: 9, title: "다른 대화", part: "데스크", created_at: "x" }],
    }),
  );
  await page.route("**/api/assistant/conversations/9", (r) =>
    r.fulfill({
      json: {
        id: 9,
        title: "다른 대화",
        messages: [
          {
            seq: 1,
            role: "user",
            text: "열어 본 대화",
            cards: [],
            tool_calls: [],
          },
        ],
      },
    }),
  );
  let release = () => {};
  const held = new Promise<void>((r) => (release = r));
  const bodies: Record<string, unknown>[] = [];
  await page.route("**/api/assistant/turn", async (r) => {
    bodies.push(r.request().postDataJSON());
    await held;
    return r.fulfill({
      contentType: "text/event-stream",
      body: sse([["reply", { type: "reply", ...REPLY({ cards: [] }) }]]),
    });
  });
  await page.goto("/");
  await page.getByLabel("말 입력").fill("느린 질문");
  await page.getByLabel("말 입력").press("Enter");
  await page.getByRole("link", { name: "다른 대화" }).click();
  await expect(page.getByText("열어 본 대화")).toBeVisible();
  release();
  await page.waitForTimeout(300);
  await expect(
    page.getByText("예약을 원했는데 안 잡힌 분은 1명입니다."),
  ).toHaveCount(0);
  await expect(page).toHaveURL(/\/\?c=9$/);
  await page.getByLabel("말 입력").fill("이어서");
  await page.getByLabel("말 입력").press("Enter");
  await expect.poll(() => bodies.length).toBe(2);
  expect(bodies[1]).toMatchObject({ conversation_id: 9, text: "이어서" });
});
