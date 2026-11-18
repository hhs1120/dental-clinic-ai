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
  source_call_pool: 0,
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
    page.getByRole("button", { name: /^(📞|🩺|🧾|💳|☎️|📋)/ }),
  ).toHaveCount(6);
  await expect(
    page.getByText("조회하고 예약 초안까지 — 확정은 사람이 합니다"),
  ).toBeVisible();
  // 시작 화면에는 입력칸 아래 예약 안내가 없다(맨 아래 문구와 겹친다)
  await expect(
    page.getByText("초안 카드가 없으면 예약되지 않은 것입니다", {
      exact: false,
    }),
  ).toHaveCount(0);
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
  const pending = page.getByText("승인 전 — 아직 일정에 없습니다");
  await expect(pending).toBeVisible();
  // 대화 중에도 입력칸 아래에 승인해야 예약된다는 안내가 남는다
  await expect(
    page.getByText("초안 카드가 없으면 예약되지 않은 것입니다", {
      exact: false,
    }),
  ).toBeVisible();
  await page.getByRole("button", { name: "승인" }).click();
  await expect(page.getByText("확정", { exact: true })).toBeVisible();
  await expect(pending).toHaveCount(0);
  await expect(
    page.getByRole("link", { name: "일정에서 보기" }),
  ).toHaveAttribute("href", "/schedule?date=2026-12-01");
  await expect(page.getByRole("button", { name: "승인" })).toHaveCount(0);
});

test("assistant: 근거 통화 링크 — 판정 풀 통화는 링크 없이", async ({
  page,
}) => {
  await fakeShell(page);
  const drafts = [
    DRAFT,
    { ...DRAFT, id: 8, source_call_uid: "c-9", source_call_pool: 1 },
  ];
  await page.route("**/api/assistant/turn", (r) =>
    r.fulfill({
      contentType: "text/event-stream",
      body: sse([
        [
          "reply",
          {
            type: "reply",
            ...REPLY({
              cards: drafts.map((d) => ({
                type: "draft",
                id: String(d.id),
                data: d,
              })),
            }),
          },
        ],
      ]),
    }),
  );
  await page.goto("/?draft=" + encodeURIComponent("예약 원한 사람 초안"));
  await expect(page.getByRole("link", { name: "c-1" })).toHaveAttribute(
    "href",
    "/calls?call=c-1",
  );
  await expect(page.getByText("c-9")).toBeVisible();
  await expect(page.getByRole("link", { name: "c-9" })).toHaveCount(0);
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

test("assistant: 대화 삭제는 확인 뒤 목록에서만 지운다", async ({ page }) => {
  let rows = [
    { id: 5, title: "미예약 요청", part: "데스크", created_at: "x" },
    { id: 4, title: "다른 대화", part: "데스크", created_at: "x" },
  ];
  await page.route("**/api/assistant/conversations", (r) =>
    r.fulfill({ json: rows }),
  );
  const deletes: object[] = [];
  await page.route("**/api/assistant/conversations/5/delete", (r) => {
    deletes.push(r.request().postDataJSON());
    rows = rows.filter((c) => c.id !== 5);
    return r.fulfill({ json: { ok: true } });
  });
  await page.route("**/api/assistant/conversations/5", (r) =>
    r.fulfill({
      json: { id: 5, title: "미예약 요청", deleted: false, messages: [] },
    }),
  );
  await page.goto("/?c=5");
  await page.getByRole("button", { name: "대화 삭제: 미예약 요청" }).click();
  await expect(
    page.getByText(
      "이 대화를 목록에서 지울까요? 예약 기록에서는 계속 볼 수 있습니다.",
    ),
  ).toBeVisible();
  await page.getByRole("button", { name: "취소" }).click();
  expect(deletes).toHaveLength(0);
  await page.getByRole("button", { name: "대화 삭제: 미예약 요청" }).click();
  await page.getByRole("button", { name: "삭제", exact: true }).click();
  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByRole("link", { name: "미예약 요청" })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "다른 대화" })).toBeVisible();
  expect(deletes).toEqual([{ part: "데스크" }]);
});

test("assistant: 지운 대화는 읽기 전용으로 열린다", async ({ page }) => {
  await page.route("**/api/assistant/conversations", (r) =>
    r.fulfill({ json: [] }),
  );
  await page.route("**/api/assistant/conversations/8", (r) =>
    r.fulfill({
      json: {
        id: 8,
        title: "지운 대화",
        deleted: true,
        messages: [
          {
            seq: 1,
            role: "user",
            text: "예전 질문",
            cards: [],
            tool_calls: [],
          },
          {
            seq: 2,
            role: "assistant",
            text: "초안을 만들었어요",
            cards: [{ type: "draft", id: "7", data: DRAFT }],
            tool_calls: [],
          },
        ],
      },
    }),
  );
  await page.goto("/?c=8");
  await expect(page.getByText("예전 질문")).toBeVisible();
  await expect(page.getByText("삭제된 대화 — 읽기 전용")).toBeVisible();
  await expect(page.getByLabel("말 입력")).toBeDisabled();
  // 초안도 승인·다른 시간·취소 없이 보기만
  await expect(page.getByText("김영철 (C0001)")).toBeVisible();
  for (const name of ["승인", "다른 시간", "취소"])
    await expect(page.getByRole("button", { name, exact: true })).toHaveCount(
      0,
    );
});

test("assistant: 시작 화면 오늘 요약 — 0건은 빼고 오늘 화면으로 잇는다", async ({
  page,
}) => {
  await fakeShell(page);
  await page.route("**/api/today", (r) =>
    r.fulfill({
      json: {
        cards: { todo: { items: 3 }, appointments: { total: 12 } },
        people: { urgent_calls: [] },
      },
    }),
  );
  await page.goto("/");
  const line = page.getByRole("link", { name: /오늘 화면 →/ });
  await expect(line).toHaveText(/할 일 3건\s*· 예약 12건/);
  await expect(line).not.toContainText("급한 통화");
  await expect(line).toHaveAttribute("href", "/today");
});

test("assistant: 환자·통화 카드는 사람별 바로 가기 칩 하나로 묶인다", async ({
  page,
}) => {
  await fakeShell(page);
  await page.route("**/api/assistant/turn", (r) =>
    r.fulfill({
      contentType: "text/event-stream",
      body: sse([
        [
          "reply",
          {
            type: "reply",
            ...REPLY({
              text: "2건입니다.",
              cards: [
                {
                  type: "call",
                  id: "c-1",
                  data: {
                    call_uid: "c-1",
                    at: "2026-11-27 15:15:00",
                    direction: "inbound",
                    name: "송미영",
                    chart_no: "C-1",
                    purpose: "book",
                    summary: "약 이름 확인 뒤 연락",
                  },
                },
                {
                  type: "patient",
                  id: "C-1",
                  data: { chart_no: "C-1", name: "송미영", birth_date: null },
                },
                {
                  type: "call",
                  id: "c-2",
                  data: {
                    call_uid: "c-2",
                    at: "2026-11-25 13:45:00",
                    direction: "inbound",
                    name: "김지훈",
                    chart_no: null,
                    purpose: "emergency",
                    summary: "잇몸 부기",
                  },
                },
              ],
            }),
          },
        ],
      ]),
    }),
  );
  await page.goto("/?draft=" + encodeURIComponent("안 잡힌 사람"));
  const bar = page.getByLabel("바로 가기");
  await expect(bar).toContainText("송미영");
  await expect(bar.getByRole("link", { name: "11/27 통화" })).toHaveAttribute(
    "href",
    "/calls?tab=history&q=C-1&call=c-1",
  );
  // 근거 통화가 있으면 통화 기록 링크는 따로 두지 않는다(날짜가 그 사람의 통화 기록으로 간다)
  await expect(bar.getByRole("link", { name: "통화 기록" })).toHaveCount(0);
  await expect(bar.getByRole("link", { name: "11/25 통화" })).toHaveAttribute(
    "href",
    `/calls?${new URLSearchParams({ tab: "history", q: "김지훈", call: "c-2" })}`,
  );
  await expect(page.getByText("약 이름 확인 뒤 연락")).toHaveCount(0);
});

test("assistant: 문서 출처는 카드, 사례 출처는 칩 — 누르면 발췌가 펼쳐진다", async ({
  page,
}) => {
  await fakeShell(page);
  const src = (id: string, extra: object) => ({
    type: "source",
    id,
    data: {
      id,
      title: null,
      body: "",
      event_date: null,
      chart_no: null,
      patient_name: null,
      caller_name: null,
      direction: null,
      doc_type: null,
      version: null,
      is_outdated: false,
      ...extra,
    },
  });
  await page.route("**/api/assistant/turn", (r) =>
    r.fulfill({
      contentType: "text/event-stream",
      body: sse([
        [
          "reply",
          {
            type: "reply",
            ...REPLY({
              text: "만 65세 이상입니다.",
              cards: [
                src("doc:21:1", {
                  source_type: "document",
                  doc_type: "insurance_guide",
                  title: "임플란트 건강보험 적용 > 적용 대상 나이",
                  body: "만 65세 이상",
                }),
                src("call:c-9:0", {
                  source_type: "call",
                  direction: "수신",
                  caller_name: "이진호",
                  event_date: "2026-11-25",
                  body: "임플란트 1차도 건강보험이 되는지",
                }),
              ],
            }),
          },
        ],
      ]),
    }),
  );
  await page.goto("/?draft=" + encodeURIComponent("임플란트 보험"));
  const docs = page.getByRole("region", { name: "문서 출처" });
  await expect(
    docs.getByRole("heading", {
      name: "임플란트 건강보험 적용 › 적용 대상 나이",
    }),
  ).toBeVisible();
  await expect(docs).not.toContainText("이진호");
  const chip = page
    .getByLabel("사례 출처")
    .getByRole("button", { name: /통화\s*이진호 · 11\/25/ });
  await expect(page.getByText("임플란트 1차도 건강보험이 되는지")).toHaveCount(
    0,
  );
  await chip.click();
  await expect(
    page.getByText("임플란트 1차도 건강보험이 되는지"),
  ).toBeVisible();
});

test("assistant: 예약 이야기가 없는 대화에는 입력칸 아래 예약 안내가 없다", async ({
  page,
}) => {
  await fakeShell(page);
  await page.route("**/api/assistant/turn", (r) =>
    r.fulfill({
      contentType: "text/event-stream",
      body: sse([
        [
          "reply",
          {
            type: "reply",
            ...REPLY({ text: "재심 기한 임박은 36건입니다.", cards: [] }),
          },
        ],
      ]),
    }),
  );
  await page.goto("/?draft=" + encodeURIComponent("재심 기한 임박 건"));
  await expect(page.getByText("재심 기한 임박은 36건입니다.")).toBeVisible();
  await expect(
    page.getByText("초안 카드가 없으면 예약되지 않은 것입니다", {
      exact: false,
    }),
  ).toHaveCount(0);
});

test("assistant: 목록 뒤에 붙은 끝맺음은 마지막 항목에 붙지 않고 선 아래 따로", async ({
  page,
}) => {
  await fakeShell(page);
  const text =
    "2명입니다.\n\n- 송미영 — 시간을 보류했습니다.\n- 김진호 — 날짜를 비워 두었습니다.\n김지훈 님은 차트가 없어 신환 등록을 먼저 해 주셔야 합니다.";
  await page.route("**/api/assistant/turn", (r) =>
    r.fulfill({
      contentType: "text/event-stream",
      body: sse([["reply", { type: "reply", ...REPLY({ text, cards: [] }) }]]),
    }),
  );
  await page.goto("/?draft=" + encodeURIComponent("지난주 안 잡힌 사람"));
  const items = page.getByRole("listitem");
  await expect(items).toHaveCount(2);
  await expect(items.nth(1)).toHaveText("김진호 — 날짜를 비워 두었습니다.");
  const tail = page.getByText("김지훈 님은 차트가 없어", { exact: false });
  await expect(tail).toBeVisible();
  await expect(tail.locator("xpath=ancestor::li")).toHaveCount(0);
});
