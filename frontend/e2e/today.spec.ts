// 오늘 화면 — 카드·사람이 볼 일·AI가 한 일(기능 정의 §4 오늘 화면). 실데이터는 자동 처리·사람 판정·거절 질문이 0건이라
// /api/today를 page.route 가짜 응답으로 준다
import { expect, type Page, test } from "@playwright/test";

const EMPTY = {
  date: "2026-12-01",
  cards: {
    appointments: { total: 0, treatment: 0, hygiene: 0, consult: 0 },
    new_patients: 0,
    alert_patients: 0,
    todo: { items: 0, calls: 0, urgent_calls: 0 },
  },
  people: {
    urgent_calls: [],
    review_calls: [],
    unconfirmed: 0,
    alert_patients: 0,
    todo_left: { items: 0, calls: 0 },
    judge_pending: 0,
    refused: [],
  },
  ai: {
    autoclose_on: false,
    auto_total: 0,
    auto_recent: [],
    prep: { rows: 0, suggest: 0, failed: 0 },
    classify_failed: 0,
    followup_failed: 0,
  },
};

const REVIEW = [
  {
    call_uid: "c-0450",
    name: "이영자",
    at: "2026-11-27 10:00:00",
    ai: "none",
    reopened: false,
    flags: ["emergency", "complaint"],
  },
  {
    call_uid: "c-0451",
    name: "김민지",
    at: "2026-11-28 11:00:00",
    ai: "closed",
    reopened: true,
    flags: [],
  },
  {
    call_uid: "c-0300",
    name: "박준호",
    at: "2026-11-10 09:00:00",
    ai: "old_todo",
    reopened: false,
    flags: ["urgent"],
  },
];

const AUTO = {
  id: 1,
  call_uid: "c-0460",
  name: "최수진",
  reason: "두 모델 모두 할 일 없음 · 응급·불만·급함·엇갈림 없음",
  at: "2026-12-01 03:10:00",
};

const FULL = {
  ...EMPTY,
  cards: {
    appointments: { total: 26, treatment: 17, hygiene: 7, consult: 2 },
    new_patients: 4,
    alert_patients: 6,
    todo: { items: 34, calls: 16, urgent_calls: 1 },
  },
  people: {
    urgent_calls: [
      {
        call_uid: "c-0470",
        name: "정다은",
        text: "통증 문의 회신",
        at: "2026-11-30 09:00:00",
        emergency: true,
      },
    ],
    review_calls: REVIEW,
    unconfirmed: 1,
    alert_patients: 6,
    todo_left: { items: 34, calls: 16 },
    judge_pending: 3,
    refused: [
      {
        id: 7,
        kind: "chat",
        question: "유치는 언제 빠져?",
        at: "2026-11-09 16:12:00",
      },
    ],
  },
  ai: {
    autoclose_on: true,
    auto_total: 1,
    auto_recent: [AUTO],
    prep: { rows: 26, suggest: 12, failed: 1 },
    classify_failed: 0,
    followup_failed: 0,
  },
};

// 개발 서버는 화면을 두 번 그려 조회가 두 번 갈 수 있다 — 응답은 요청 순번이 아니라 함수로 정한다
async function fakeToday(page: Page, body: unknown | (() => unknown)) {
  await page.route("**/api/today", (r) =>
    r.fulfill({ json: typeof body === "function" ? body() : body }),
  );
}

test("카드와 줄을 누르면 그 화면으로", async ({ page }) => {
  await fakeToday(page, FULL);
  await page.goto("/today");
  await page.getByRole("link", { name: /^6명\s*주의 환자/ }).click(); // 카드
  await expect(page).toHaveURL(/\/prep\?chip=alert$/);
  await page.goto("/today");
  await page.getByRole("link", { name: /이영자/ }).click();
  await expect(page).toHaveURL(/\/calls\?call=c-0450$/);
  await page.goto("/today");
  await page.getByRole("link", { name: /예약 미확인 1명/ }).click();
  await expect(page).toHaveURL(/\/prep\?chip=unconfirmed$/);
  await page.goto("/today");
  await page.getByRole("link", { name: /유치는 언제 빠져/ }).click();
  await expect(page).toHaveURL(/\/ask\?tab=chat&draft=/);
});

test("확인할 통화 문구", async ({ page }) => {
  await fakeToday(page, FULL);
  await page.goto("/today");
  const people = page.getByRole("region", { name: "사람이 볼 일" });
  await expect(people).toContainText("AI: 할 일 없음, 사람 판정: 응급·불만");
  await expect(people).toContainText("AI: 종결, 사람: 다시 열림");
  await expect(people).toContainText(
    "AI: 할 일 있음(지난 통화), 사람 판정: 급함",
  );
  await expect(people).toContainText("이영자 · AI: 할 일 없음");
  await expect(people).toContainText("11/27");
  for (const code of ["emergency", "has_todo", "old_todo", "closed", "refused"])
    await expect(page.getByText(code)).toHaveCount(0);
});

test("0건 줄은 숨김", async ({ page }) => {
  await fakeToday(page, {
    ...FULL,
    people: { ...FULL.people, refused: [], review_calls: [] },
  });
  await page.goto("/today");
  await expect(page.getByText("근거 없음")).toHaveCount(0);
  await expect(page.getByText("확인할 통화")).toHaveCount(0);
  await expect(page.getByText("급한 통화")).toBeVisible();
  await page.unroute("**/api/today");
  await fakeToday(page, EMPTY);
  await page.goto("/today");
  await expect(page.getByText("오늘 남은 일이 없습니다")).toBeVisible();
});

test("AI가 한 일은 접힌 채 요약, 펼치면 다시 열기", async ({ page }) => {
  await fakeToday(page, EMPTY);
  await page.goto("/today");
  await expect(page.getByText(/자동 종결 꺼짐/)).toBeVisible();
  await page.unroute("**/api/today");
  await fakeToday(page, FULL);
  await page.goto("/today");
  const ai = page.getByRole("region", { name: "AI가 한 일" });
  await expect(ai).toContainText("자동 처리 1건 · 오늘 준비표 26행 · 실패 1");
  await expect(ai.getByRole("button", { name: "다시 열기" })).toHaveCount(0);
  await ai.getByRole("button", { name: "펼치기" }).click();
  await expect(ai.getByRole("button", { name: "다시 열기" })).toBeVisible();
  await expect(ai).toContainText("최수진");
  await expect(ai).toContainText("사람에게 넘김"); // 준비표 후검증 실패 1
  await expect(ai).toContainText("끝남"); // 분류 실패 0
  await expect(ai.getByRole("link", { name: /전체 보기/ })).toHaveAttribute(
    "href",
    "/calls?tab=history",
  );
});

test("다시 열기 — AI가 한 일에서 빠지고 확인할 통화에 뜸", async ({ page }) => {
  const after = {
    ...FULL,
    people: {
      ...FULL.people,
      review_calls: [
        {
          call_uid: "c-0460",
          name: "최수진",
          at: "2026-11-29 10:00:00",
          ai: "closed",
          reopened: true,
          flags: [],
        },
      ],
    },
    ai: { ...FULL.ai, auto_total: 0, auto_recent: [] },
  };
  let release: () => void = () => {};
  const held = new Promise<void>((res) => (release = res));
  const posted: string[] = [];
  let reverted = false;
  await fakeToday(page, () => (reverted ? after : FULL));
  await page.route("**/api/auto-actions/1/revert", async (r) => {
    posted.push(r.request().postData() ?? "");
    await held;
    reverted = true;
    await r.fulfill({ json: { ok: true } });
  });
  await page.goto("/today");
  const ai = page.getByRole("region", { name: "AI가 한 일" });
  await ai.getByRole("button", { name: "펼치기" }).click();
  const btn = ai.getByRole("button", { name: "다시 열기" });
  await btn.click();
  await expect(btn).toBeDisabled(); // 응답 전에 꺼져 두 번 누를 수 없다
  release();
  await expect(ai).not.toContainText("최수진");
  await expect(
    page.getByRole("region", { name: "사람이 볼 일" }),
  ).toContainText("최수진 · AI: 종결, 사람: 다시 열림");
  expect(posted).toHaveLength(1);
});
